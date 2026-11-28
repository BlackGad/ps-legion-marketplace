param(
    [string]$Source = 'BlackGad/ps-legion-marketplace',
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$NoRun,
    [ValidateSet('release','main')][string]$Ref = 'release',
    [switch]$Update,
    [switch]$Uninstall,
    [switch]$Purge,
    [string]$HomeRoot
)

# The Windows bootstrap runs in the built-in Windows PowerShell 5.1 (or PowerShell 7).
# It only obtains uv, then calls the selected Python legion command.
# Hand-built, reason: this runs before uv and Python exist, so it cannot use any Python library; it stays small on purpose.

function Get-InstallHome {
    if ($script:installHomeRoot) { return $script:installHomeRoot }
    return $HOME
}

# Persistent changes need consent: -Yes, or a yes at the prompt.
function Confirm-InstallChange {
    param([string]$Question, [switch]$Yes)
    return $Yes -or (Read-Host $Question) -match '^(?i:y|yes)$'
}

function Get-InstallLegionPath {
    param([string]$Path)
    return Join-Path (Join-Path (Get-InstallHome) '.legion') $Path
}

# uv's own default folder, where its installer puts it and `uv tool` puts commands.
function Get-InstallUvFolder {
    foreach ($folder in @($env:UV_INSTALL_DIR, $env:XDG_BIN_HOME)) {
        if ($folder) { return $folder }
    }
    return (Join-Path (Join-Path (Get-InstallHome) '.local') 'bin')
}

# uv is found on PATH, or in the folder its own installer uses.
function Get-InstallUv {
    $found = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    foreach ($folder in @($env:UV_INSTALL_DIR, $env:XDG_BIN_HOME, (Join-Path (Join-Path (Get-InstallHome) '.local') 'bin'))) {
        if (-not $folder) { continue }
        foreach ($name in @('uv.exe', 'uv')) {
            $path = Join-Path $folder $name
            if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
        }
    }
}

# Hand-built, reason: Invoke-WebRequest follows redirects without a check, so every hop is requested here and must be HTTPS.
function Invoke-InstallDownload {
    param([string]$Url, [string]$OutFile)
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    for ($hop = 0; $hop -lt 10; $hop++) {
        if ($Url -notmatch '^https://') { throw "Refusing to download over anything but HTTPS: $Url" }
        $request = [Net.HttpWebRequest]::Create($Url)
        $request.AllowAutoRedirect = $false
        $request.UserAgent = 'legion-bootstrap'
        $response = $request.GetResponse()
        try {
            $status = [int]$response.StatusCode
            if ($status -ge 300 -and $status -lt 400) {
                $Url = (New-Object Uri ([Uri]$Url), $response.Headers['Location']).AbsoluteUri
                continue
            }
            $file = [IO.File]::Create($OutFile)
            try { $response.GetResponseStream().CopyTo($file) } finally { $file.Dispose() }
            return
        } finally { $response.Dispose() }
    }
    throw 'Too many redirects.'
}

function Test-InstallUv {
    param([string]$Uv)
    try { & $Uv --version *> $null } catch { return $false }
    return $LASTEXITCODE -eq 0
}

function Test-InstallPython {
    param([string]$Uv)
    & $Uv python find '>=3.12' *> $null
    return $LASTEXITCODE -eq 0
}

# Temporary folders live in the system temp folder, outside .legion, so a purge can delete .legion while they are in use.
function New-InstallTemporary {
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('legion-install-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary -Force -ErrorAction Stop | Out-Null
    return $temporary
}

function Remove-InstallTemporary {
    param([string]$Path)
    if (-not ([IO.Path]::GetFileName($Path)).StartsWith('legion-install-')) { throw 'Unsafe installer temporary path' }
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop }
}

# Downloads and verifies uv, then puts it in the home for install or a temporary folder for cleanup.
function Install-InstallUvRelease {
    param([string]$TemporaryUv)
    $architecture = 'x86_64'
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $architecture = 'aarch64' }
    $temporary = New-InstallTemporary
    try {
        $archive = Join-Path $temporary 'uv.zip'
        $asset = "uv-$architecture-pc-windows-msvc.zip"
        $url = "https://github.com/astral-sh/uv/releases/latest/download/$asset"
        $checksum = Join-Path $temporary 'uv.zip.sha256'
        Invoke-InstallDownload -Url "$url.sha256" -OutFile $checksum
        $digest = (Get-Content -LiteralPath $checksum -Raw -ErrorAction Stop).Trim()
        if ($digest -notmatch "^([a-fA-F0-9]{64})\s+\*?$([regex]::Escape($asset))$") {
            throw 'The uv asset or its SHA-256 digest is missing from the official release.'
        }
        $expectedHash = $Matches[1]
        Invoke-InstallDownload -Url $url -OutFile $archive
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256 -ErrorAction Stop).Hash -ine $expectedHash) {
            Remove-Item -LiteralPath $archive -Force
            throw 'uv SHA-256 digest mismatch; download deleted.'
        }
        $staging = Join-Path $temporary 'uv'
        Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force -ErrorAction Stop
        $staged = Join-Path $staging 'uv.exe'
        if (-not (Test-InstallUv -Uv $staged)) { throw 'The downloaded uv does not run; nothing was installed.' }
        if ($TemporaryUv) {
            Copy-Item -LiteralPath $staged -Destination $TemporaryUv -ErrorAction Stop
        } else {
            $folder = Get-InstallUvFolder
            New-Item -ItemType Directory -Path $folder -Force -ErrorAction Stop | Out-Null
            # Copy beside the target, then rename: an interrupted run never leaves a partial uv.exe.
            Copy-Item -LiteralPath $staged -Destination (Join-Path $folder 'uv.exe.new') -Force -ErrorAction Stop
            Move-Item -LiteralPath (Join-Path $folder 'uv.exe.new') -Destination (Join-Path $folder 'uv.exe') -Force -ErrorAction Stop
        }
    } finally {
        Remove-InstallTemporary -Path $temporary
    }
}

# Returns the path of uv, or $null when the owner has to act first (dry run, or no consent).
function Ensure-InstallUv {
    param([switch]$DryRun, [switch]$Yes, [switch]$Cleanup)
    $uv = Get-InstallUv
    if ($uv) {
        Write-Host 'skip: uv (already done)'
        return $uv
    }
    Write-Host 'uv is missing.'
    if ($env:OS -ne 'Windows_NT') {
        Write-Host 'On macOS and Linux run install.sh instead: https://github.com/BlackGad/ps-legion-marketplace'
        return $null
    }
    if ($Cleanup) {
        Write-Host 'Use temporary uv: download the latest official uv release, verified with its SHA-256 digest, outside the home for cleanup.'
    } else {
        Write-Host 'Install uv: download the latest official uv release, verified with its SHA-256 digest, to the default uv folder (~/.local/bin); legion install then puts it on your PATH with uv.'
    }
    if ($DryRun) { return $null }
    if (-not (Confirm-InstallChange -Yes:$Yes -Question 'Install uv now? [y/N]')) {
        Write-Host 'Nothing changed. Run again with -Yes to apply.'
        return $null
    }
    if ($Cleanup) {
        # The detached uninstall may start after the bootstrap exits. Leave this copy to the system temp cleaner.
        $folder = New-InstallTemporary
        $temporaryUv = Join-Path $folder 'uv.exe'
        try { Install-InstallUvRelease -TemporaryUv $temporaryUv }
        catch {
            Remove-InstallTemporary -Path $folder
            throw
        }
        return $temporaryUv
    }
    Install-InstallUvRelease
    $uv = Get-InstallUv
    if (-not $uv) { throw 'uv was installed but could not be found; open a new terminal and run the bootstrap again.' }
    return $uv
}

# Previews never let uv download Python; an ordinary run asks first. Returns whether to go on.
function Confirm-InstallPython {
    param([string]$Uv, [switch]$DryRun, [switch]$Yes)
    if (Test-InstallPython -Uv $Uv) { return $true }
    Write-Host 'Python 3.12 is missing. uv can download it for legion.'
    if ($DryRun) { return $false }
    if (Confirm-InstallChange -Yes:$Yes -Question 'Download Python with uv now? [y/N]') { return $true }
    Write-Host 'Nothing changed. Run again with -Yes to apply.'
    return $false
}

# The plugin folder that holds the Python project: this checkout, or the archive downloaded from the fixed legion repository.
function Get-InstallProject {
    param([string]$CheckoutRoot, [string]$Branch, [string]$Temporary)
    if ($CheckoutRoot) {
        $project = Join-Path (Join-Path $CheckoutRoot 'plugins') 'legion'
        if (Test-Path -LiteralPath (Join-Path $project 'pyproject.toml') -PathType Leaf) { return $project }
    }
    $archive = Join-Path $Temporary 'legion.zip'
    $expanded = Join-Path $Temporary 'expanded'
    try { Invoke-InstallDownload -Url "https://github.com/BlackGad/ps-legion-marketplace/archive/refs/heads/$Branch.zip" -OutFile $archive }
    catch {
        if ($Branch -eq 'stable') { throw 'could not find the latest release; try again or run with main' }
        throw
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded -ErrorAction Stop
    $top = @(Get-ChildItem -LiteralPath $expanded -Force)
    if ($top.Count -ne 1 -or -not $top[0].PSIsContainer) { throw 'Plugin archive must contain a single top folder.' }
    $project = Join-Path (Join-Path $top[0].FullName 'plugins') 'legion'
    if (-not (Test-Path -LiteralPath (Join-Path $project 'pyproject.toml') -PathType Leaf)) { throw 'Plugin archive is missing the legion project.' }
    return $project
}

# Select the Python verb and pass the options it accepts.
function Get-InstallArguments {
    param([Collections.IDictionary]$Parameters)
    # Always the home this script used, so a redirected $HOME reaches Python too.
    $homeRoot = $Parameters.HomeRoot
    if (-not $homeRoot) { $homeRoot = $HOME }
    $cleanup = $Parameters.Uninstall -or $Parameters.Purge
    if ($Parameters.Update -and $cleanup) { throw 'Choose either -Update or -Uninstall/-Purge.' }
    $verb = if ($cleanup) { 'uninstall' } elseif ($Parameters.Update) { 'update' } else { 'install' }
    $arguments = @('--home', $homeRoot, $verb)
    if ($cleanup) {
        if ($Parameters.Purge) { $arguments += '--purge' }
    } else {
        if ($Parameters.Source) { $arguments += @('--source', $Parameters.Source) }
        if ($Parameters.Ref) { $arguments += @('--ref', $Parameters.Ref) }
    }
    if ($Parameters.DryRun) { $arguments += '--dry-run' }
    if ($Parameters.Yes) { $arguments += '--yes' }
    return $arguments
}

function Invoke-InstallUv {
    param([string]$Uv, [string[]]$Arguments)
    # Inherit all three standard streams, including prompts written without a newline.
    $start = [Diagnostics.ProcessStartInfo]::new($Uv)
    $start.UseShellExecute = $false
    $start.WorkingDirectory = (Get-Location -PSProvider FileSystem).ProviderPath
    $start.EnvironmentVariables['LEGION_BOOTSTRAP_POWERSHELL'] = '1'
    # Windows PowerShell 5.1 has no ArgumentList property. Escape arguments for the native parser.
    $quoted = foreach ($argument in $Arguments) {
        $escaped = $argument -replace '(\\*)"', '$1$1\"'
        '"' + ($escaped -replace '(\\+)$', '$1$1') + '"'
    }
    $start.Arguments = $quoted -join ' '
    $process = [Diagnostics.Process]::Start($start)
    try {
        # Wait only for uv. A detached update or uninstall helper can outlive the installer.
        $process.WaitForExit()
        return $process.ExitCode
    } finally { $process.Dispose() }
}

function Set-InstallConsoleEncoding {
    param([Text.Encoding]$Encoding)
    [Console]::OutputEncoding = $Encoding
}

function Invoke-InstallBootstrap {
    param([Collections.IDictionary]$Parameters, [string]$CheckoutRoot = $PSScriptRoot)
    $script:bootstrapExitCode = 0
    $script:installHomeRoot = $Parameters.HomeRoot
    $dryRun = [bool]$Parameters.DryRun
    $temporary = $null
    $savedEnvironment = $env:UV_PROJECT_ENVIRONMENT
    $savedPath = $env:PATH
    $cleanup = [bool]$Parameters.Uninstall -or [bool]$Parameters.Purge
    try {
        $uv = Ensure-InstallUv -DryRun:$dryRun -Yes:([bool]$Parameters.Yes) -Cleanup:$cleanup
        if (-not $uv) { return }
        $folder = Split-Path $uv -Parent
        if (($env:PATH -split [regex]::Escape([string][IO.Path]::PathSeparator)) -notcontains $folder) {
            $env:PATH = $folder + [IO.Path]::PathSeparator + $env:PATH
        }
        if (-not (Confirm-InstallPython -Uv $uv -DryRun:$dryRun -Yes:([bool]$Parameters.Yes))) { return }
        if (-not $dryRun) { Write-Host 'Preparing legion (first run can take a minute)...' }
        $temporary = New-InstallTemporary
        $branch = 'stable'
        if ($Parameters.Ref -eq 'main') { $branch = 'main' }
        $project = Get-InstallProject -CheckoutRoot $CheckoutRoot -Branch $branch -Temporary $temporary
        # The project environment lives beside the downloaded copy and goes with it.
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $temporary 'env'
        $arguments = @('run', '--project', $project, '--frozen', '--quiet', 'legion') + @(Get-InstallArguments -Parameters $Parameters)
        $previousEncoding = $null
        if ($env:OS -eq 'Windows_NT') {
            try {
                # Legion relays UTF-8. Some PowerShell hosts have no console to configure.
                $originalEncoding = [Console]::OutputEncoding
                Set-InstallConsoleEncoding ([Text.UTF8Encoding]::new($false))
                $previousEncoding = $originalEncoding
            } catch {}
        }
        try {
            $script:bootstrapExitCode = Invoke-InstallUv -Uv $uv -Arguments $arguments
        } finally {
            if ($previousEncoding) {
                try { Set-InstallConsoleEncoding $previousEncoding } catch {}
            }
        }
        $global:LASTEXITCODE = $script:bootstrapExitCode
    } finally {
        $env:UV_PROJECT_ENVIRONMENT = $savedEnvironment
        if ($cleanup) { $env:PATH = $savedPath }
        if ($temporary) { Remove-InstallTemporary -Path $temporary }
    }
}

if (-not $NoRun) {
    Invoke-InstallBootstrap -Parameters $PSBoundParameters
    if ($script:bootstrapExitCode) { exit $script:bootstrapExitCode }
}
