# The overseer lease: one lock file per repository that names the session allowed to orchestrate.

import os
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Optional

from filelock import FileLock, Timeout

from legion.core._lease_record import (
    Lease,
    lease_path,
    round_trip,
    same_host,
)
from legion.core._process import owner_process, process_started_at, same_start_time

State = Literal["free", "held", "stale", "foreign"]
# A lock that is not a lease yet was most likely just created by a session that is still writing it.
WRITE_GRACE_SECONDS = 10
# On Windows another process may have the lock open for a moment; wait this long between the few tries.
RETRY_PAUSE_SECONDS = 0.025


class LeaseError(Exception):
    pass


@dataclass(frozen=True)
class LeaseState:
    state: State
    lease: Optional[Lease]


def state(repo: Path) -> LeaseState:
    path = lease_path(repo)
    try:
        lease = Lease.from_json(_retry_sharing(lambda: path.read_text(encoding="utf-8-sig")))
    except FileNotFoundError:
        return LeaseState("free", None)
    except OSError:
        return LeaseState("held", None)  # Unreadable is not stale: the owner may be alive behind a sharing or permission error.
    except (ValueError, TypeError):  # Not a lease, including bytes that are not UTF-8.
        if _age_seconds(path) < WRITE_GRACE_SECONDS:
            return LeaseState("held", None)
        return LeaseState("stale", None)
    if not lease.host or not lease.pid or lease.process_started_at is None:
        return LeaseState("stale", lease)
    if not same_host(lease.host):
        return LeaseState("foreign", lease)
    started = process_started_at(lease.pid)
    if started is not None and same_start_time(started, lease.process_started_at):
        return LeaseState("held", lease)
    return LeaseState("stale", lease)


# Take the owner's lock; replace only a stale lease. The guard is held from the check until the new lease is written.
def take(repo: Path, tool: str, account: str, process_id: Optional[int] = None) -> str:
    pid, started = _owner(process_id)
    if started is None:
        raise LeaseError("lease owner process is not running")
    path = lease_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    thread = os.environ.get("CODEX_THREAD_ID")
    lease = Lease(
        tool=tool,
        account=account,
        pid=pid,
        process_started_at=started,
        host=socket.gethostname(),
        started_at=round_trip(datetime.now(UTC)) + "+00:00",
        session=thread if tool == "codex" and thread else None,
    )
    with _guard(path):
        current = state(repo)
        found = current.lease
        if current.state == "held" and found is not None and same_host(found.host) and found.pid == pid:
            return "skip: lease take (already done)"
        if current.state == "foreign" and found is not None:
            raise LeaseError(f"lease from host {found.host}; ask the owner")
        if current.state == "held" and found is None:
            raise LeaseError("lease is being written or cannot be read; retry, or check its permissions")
        if current.state == "held" and found is not None:
            raise LeaseError(f"lease held by {found.tool} {found.account} since {found.started_at}")
        if current.state == "stale" and not _remove(path):
            raise LeaseError("lease takeover in progress in another session; retry")
        try:
            with open(path, "x", encoding="utf-8", newline="") as file:
                file.write(lease.to_json())
        except (FileExistsError, PermissionError):
            raise LeaseError("lease unavailable; retry takeover") from None
        return "lease taken"


# Release only this owner's lock, checked by pid, host and start time.
def release(repo: Path, process_id: Optional[int] = None) -> str:
    path = lease_path(repo)
    if not path.exists():
        return "skip: lease release (already done)"
    pid, started = _owner(process_id)
    with _guard(path):
        current = state(repo)
        if current.state == "free":
            return "skip: lease release (already done)"
        found = current.lease
        if found is not None and started is not None and found.process_started_at is not None and same_host(found.host) and found.pid == pid and same_start_time(found.process_started_at, started):
            if not _remove(path):
                raise LeaseError("lease change in progress in another session; retry")
            return "lease released"
        raise LeaseError("lease belongs to another session; cannot release it")


def _owner(process_id: Optional[int]) -> tuple[int, Optional[datetime]]:
    if process_id is not None:
        return process_id, process_started_at(process_id)
    try:
        owner = owner_process()
    except LookupError as error:
        raise LeaseError(str(error)) from error
    return owner.pid, owner.started_at


# A paused holder keeps the OS lock until it exits.
@contextmanager
def _guard(path: Path) -> Iterator[None]:
    lock = FileLock(path.with_name(path.name + ".takeover"), timeout=0)
    try:
        lock.acquire()
    except (Timeout, OSError):
        raise LeaseError("lease change in progress in another session; retry") from None
    try:
        yield
    finally:
        lock.release()


# Windows may deny access while another session has the lease open.
def _retry_sharing[T](operation: Callable[[], T]) -> T:
    for _ in range(4):
        try:
            return operation()
        except PermissionError:
            time.sleep(RETRY_PAUSE_SECONDS)
    return operation()


def _remove(path: Path) -> bool:
    try:
        _retry_sharing(lambda: path.unlink(missing_ok=True))
    except PermissionError:
        return False
    return True


def _age_seconds(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return float("inf")


# The live session that holds the lease, unless it is the caller; None when the lease is free, stale or the caller's own.
def other_holder(repo: Path) -> Optional[str]:
    current = state(repo)
    if current.state != "held":
        return None
    found = current.lease
    if found is None:
        return "an unreadable lease"
    try:
        pid, started = _owner(None)
    except LeaseError:
        pid, started = None, None
    if started is not None and found.process_started_at is not None and found.pid == pid and same_start_time(found.process_started_at, started):
        return None
    return f"{found.tool} {found.account} since {found.started_at}"
