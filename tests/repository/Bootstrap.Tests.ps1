$testBase = Split-Path $PSScriptRoot -Parent
# Root bootstrap (install.ps1 and install.sh): it only obtains uv, then runs `legion install` through uv.
. (Join-Path $testBase 'lib/installer.ps1')

$bootstrap = Join-Path $repo 'install.ps1'
$fixture = Join-Path $testBase 'fixtures/install/Test-Bootstrap.ps1'

function Get-BootstrapRuntimes {
    Get-InstallTestPwsh
    if ($IsWindows) {
        $legacy = Get-Command powershell.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        Assert-True ($null -ne $legacy) 'Windows PowerShell 5.1 (powershell.exe) is required for bootstrap tests'
        $legacy.Source
    }
}

function New-BootstrapScratch {
    $scratch = New-InstallTestFolder 'bootstrap'
    $fakeSource = Join-Path $scratch 'fake-uv-source'
    New-Item -ItemType Directory -Path $fakeSource, (Join-Path $scratch 'bin') -Force | Out-Null
    # A fake uv logs one argument per line, then the project environment, and exits with FAKE_UV_EXIT.
    if ($env:OS -eq 'Windows_NT') {
        $lines = @('@echo off', 'del "%FAKE_UV_LOG%" 2>nul', ':loop', 'if "%~1"=="" goto done', '>> "%FAKE_UV_LOG%" echo %~1', 'shift', 'goto loop', ':done',
            '>> "%FAKE_UV_LOG%" echo env=%UV_PROJECT_ENVIRONMENT%', 'if not defined FAKE_UV_SAY goto finish',
            'echo fake uv diagnostic 1>&2', 'set /p "answer=fake uv prompt [y/N] "',
            'echo answer=%answer%', 'echo bootstrap=%LEGION_BOOTSTRAP_POWERSHELL%', 'echo cwd=%CD%',
            'type "%FAKE_UV_UTF8%"', ':finish', 'exit /b %FAKE_UV_EXIT%')
        $name = 'uv.cmd'
    } else {
        $lines = @('#!/bin/sh', ': > "$FAKE_UV_LOG"', 'for word in "$@"; do echo "$word" >> "$FAKE_UV_LOG"; done',
            'echo "env=$UV_PROJECT_ENVIRONMENT" >> "$FAKE_UV_LOG"', 'if [ -n "$FAKE_UV_SAY" ]; then',
            '  echo "fake uv diagnostic" >&2', '  printf "%s" "fake uv prompt [y/N] "', '  read -r answer',
            '  echo "answer=$answer"', '  echo "bootstrap=$LEGION_BOOTSTRAP_POWERSHELL"', '  echo "cwd=$(pwd)"',
            '  cat "$FAKE_UV_UTF8"', 'fi', 'exit "$FAKE_UV_EXIT"')
        $name = 'uv'
    }
    Set-Content -LiteralPath (Join-Path $fakeSource $name) -Value $lines
    Set-TestExecutable (Join-Path $fakeSource $name)
    # Present on PATH for the runs that start with uv.
    Copy-Item -LiteralPath (Join-Path $fakeSource $name) -Destination (Join-Path $scratch "bin/$name")
    Set-TestExecutable (Join-Path $scratch "bin/$name")
    foreach ($project in @('checkout/plugins/legion', 'archive/ps-legion-marketplace-stable/plugins/legion')) {
        New-Item -ItemType Directory -Path (Join-Path $scratch $project) -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $scratch "$project/pyproject.toml") -Value '[project]'
    }
    Compress-Archive -Path (Join-Path $scratch 'archive/ps-legion-marketplace-stable') -DestinationPath (Join-Path $scratch 'plugin.zip')
    return $scratch
}

function Invoke-BootstrapFixture {
    param([string]$Runtime, [string]$Mode, [string]$Scratch, [bool]$UvOnPath = $true)
    $savedPath = $env:PATH
    if ($UvOnPath) { $env:PATH = (Join-Path $Scratch 'bin') + [IO.Path]::PathSeparator + $env:PATH }
    try {
        # Closed standard input: the bootstrap must never wait on it.
        $output = @('' | & $Runtime -NoProfile -File $fixture -Installer $bootstrap -Mode $Mode -Scratch $Scratch -Archive (Join-Path $Scratch 'plugin.zip') 2>&1)
        return @{ ExitCode = $LASTEXITCODE; Lines = @($output | ForEach-Object { "$_" }) }
    } finally { $env:PATH = $savedPath }
}

Invoke-InstallTestCase 'BootstrapParses' {
    $tokens = $null
    $errors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile($bootstrap, [ref]$tokens, [ref]$errors)
    Assert-Equal 0 $errors.Count 'install.ps1 parses'
    $shell = Get-Content -LiteralPath (Join-Path $repo 'install.sh') -Raw
    Assert-True (-not ($shell -match 'pwsh|PowerShell')) 'install.sh never mentions PowerShell'
    $text = Get-Content -LiteralPath $bootstrap -Raw
    Assert-True (-not ($text -match 'pwsh')) 'install.ps1 never asks for pwsh'
    Assert-True (-not ($text -match '\?\?|\?\.|\s\?\s')) 'install.ps1 uses no PowerShell 7 syntax'
}

Invoke-InstallTestCase 'BootstrapArguments' {
    . $bootstrap -NoRun
    $all = @{ Source = 'owner/repo'; Ref = 'main'; HomeRoot = 'C:\h'; DryRun = $true; Yes = $true }
    Assert-Equal '--home|C:\h|install|--source|owner/repo|--ref|main|--dry-run|--yes' (@(Get-InstallArguments -Parameters $all) -join '|') 'Install accepts source, ref and approval options'
    $all.Update = $true
    Assert-Equal '--home|C:\h|update|--source|owner/repo|--ref|main|--dry-run|--yes' (@(Get-InstallArguments -Parameters $all) -join '|') 'Update selects the update verb'
    $all.Update = $false
    $all.Uninstall = $true
    Assert-Equal '--home|C:\h|uninstall|--dry-run|--yes' (@(Get-InstallArguments -Parameters $all) -join '|') 'Uninstall selects the uninstall verb'
    $all.Purge = $true
    Assert-Equal '--home|C:\h|uninstall|--purge|--dry-run|--yes' (@(Get-InstallArguments -Parameters $all) -join '|') 'Purge uses the uninstall verb'
    Assert-Equal "--home|$HOME|install" (@(Get-InstallArguments -Parameters @{}) -join '|') 'Without -HomeRoot the effective home is still handed over'
}

Invoke-InstallTestCase 'BootstrapHandsOverToUv' {
    $scratch = New-BootstrapScratch
    try {
        foreach ($runtime in @(Get-BootstrapRuntimes)) {
            $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'checkout' -Scratch $scratch
            $text = $result.Lines -join "`n"
            Assert-Equal 0 $result.ExitCode "Checkout run in ${runtime}: $text"
            Assert-True ($result.Lines -contains 'skip: uv (already done)') 'Present uv is reused'
            Assert-True ($result.Lines -contains 'downloads=') "A checkout downloads nothing: $text"
            $project = Join-Path $scratch 'checkout/plugins/legion'
            Assert-True ($result.Lines -contains "uv:$project") "uv runs the checkout project: $text"
            foreach ($word in @('run', '--project', '--frozen', '--quiet', 'legion', 'install', '--yes', '--home', $scratch)) {
                Assert-True ($result.Lines -contains "uv:$word") "uv gets $word"
            }
            Assert-True (@($result.Lines | Where-Object { $_ -like 'uv:env=*temp*env' }).Count -eq 1) "uv gets its own project environment: $text"
            Assert-True ($result.Lines -contains 'tempLeft=0') 'The temporary folder is gone afterwards'
            Assert-True ($result.Lines -contains 'exit=0') "Exit code is zero: $text"

            $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'exit-code' -Scratch $scratch
            Assert-True ($result.Lines -contains 'exit=3') "uv's exit code is the bootstrap's: $($result.Lines -join ' ')"
        }
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'BootstrapDownloadsTheRightBranch' {
    $scratch = New-BootstrapScratch
    try {
        foreach ($case in @(@('remote', 'stable'), @('remote-main', 'main'))) {
            $result = Invoke-BootstrapFixture -Runtime (Get-InstallTestPwsh) -Mode $case[0] -Scratch $scratch
            $text = $result.Lines -join "`n"
            Assert-Equal 0 $result.ExitCode "$($case[0]): $text"
            Assert-True ($result.Lines -contains "downloads=https://github.com/BlackGad/ps-legion-marketplace/archive/refs/heads/$($case[1]).zip") "$($case[0]) downloads the $($case[1]) code: $text"
            Assert-True ($result.Lines -contains 'tempLeft=0') "$($case[0]) removes its download"
            Assert-True (@($result.Lines | Where-Object { $_ -like 'uv:*expanded*legion' }).Count -eq 1) "$($case[0]) runs the downloaded project: $text"
        }
        $result = Invoke-BootstrapFixture -Runtime (Get-InstallTestPwsh) -Mode 'remote-main' -Scratch $scratch
        Assert-True ($result.Lines -contains 'uv:--ref') 'main reaches legion install as --ref main'
        $result = Invoke-BootstrapFixture -Runtime (Get-InstallTestPwsh) -Mode 'no-release' -Scratch $scratch
        Assert-True (($result.Lines -join ' ').Contains('could not find the latest release; try again or run with main')) 'The failure says what to do'
        Assert-True (-not ($result.Lines -contains 'uv:run')) 'uv never runs after a failed download'
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'RemoteBootstrapDryRun' {
    $scratch = New-BootstrapScratch
    try {
        foreach ($runtime in @(Get-BootstrapRuntimes)) {
            $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'remote-dry-run' -Scratch $scratch -UvOnPath $false
            $text = $result.Lines -join "`n"
            Assert-Equal 0 $result.ExitCode "Remote dry run in ${runtime}: $text"
            Assert-True ($result.Lines -contains 'exit=0') "The handoff succeeds: $text"
            Assert-True ($result.Lines -contains 'downloads=https://github.com/BlackGad/ps-legion-marketplace/archive/refs/heads/main.zip') "Only the source archive is downloaded: $text"
            Assert-True ($result.Lines -contains 'prompts=0') 'A dry run never asks'
            $words = @($result.Lines | Where-Object { $_ -like 'uv:*' } | ForEach-Object { $_.Substring(3) })
            Assert-True ($words[2] -like (Join-Path $scratch 'tmp/*/expanded/*/plugins/legion')) "uv runs the downloaded project: $text"
            Assert-Equal "run|--project|$($words[2])|--frozen|--quiet|legion|--home|$scratch|update|--ref|main|--dry-run|--yes" ($words[0..($words.Count - 2)] -join '|') "uv gets the complete dry-run command: $text"
            $project = [IO.DirectoryInfo]$words[2]
            $temporary = $project.Parent.Parent.Parent.Parent
            Assert-Equal "env=$(Join-Path $temporary.FullName 'env')" $words[-1] 'uv uses a disposable project environment'
            Assert-True ($result.Lines -contains 'tempLeft=0') 'Temporary files are removed'
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $scratch '.legion'))) 'The dry run leaves the Legion home absent'
        }
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'BootstrapUvOutputAndExitCode' {
    $scratch = New-BootstrapScratch
    try {
        $unicode = 'Updating… ✔ →'
        $utf8 = [Text.UTF8Encoding]::new($false)
        $unicodeFile = Join-Path $scratch 'utf8.txt'
        [IO.File]::WriteAllText($unicodeFile, "$unicode`n", $utf8)
        foreach ($runtime in @(Get-BootstrapRuntimes)) {
            $start = [Diagnostics.ProcessStartInfo]::new($runtime)
            foreach ($argument in @('-NoProfile', '-File', $fixture, '-Installer', $bootstrap, '-Mode', 'uv-call', '-Scratch', $scratch, '-Archive', (Join-Path $scratch 'plugin.zip'))) {
                $start.ArgumentList.Add($argument)
            }
            $start.Environment['PATH'] = (Join-Path $scratch 'bin') + [IO.Path]::PathSeparator + $env:PATH
            $start.Environment['FAKE_UV_UTF8'] = $unicodeFile
            $start.UseShellExecute = $false
            $start.RedirectStandardInput = $true
            $start.RedirectStandardOutput = $true
            $start.StandardOutputEncoding = $utf8
            $start.RedirectStandardError = $true
            $process = [Diagnostics.Process]::Start($start)
            try {
                foreach ($expected in @('skip: uv (already done)', 'Preparing legion (first run can take a minute)...')) {
                    $line = $process.StandardOutput.ReadLineAsync()
                    Assert-True ($line.Wait(10000)) "Expected '$expected' before the prompt in $runtime"
                    Assert-Equal $expected $line.Result 'Bootstrap progress'
                }
                $codePage = $process.StandardOutput.ReadLineAsync()
                Assert-True ($codePage.Wait(10000)) "Expected the code page during hand-over in $runtime"
                if ($IsWindows) { Assert-Equal 'uvCodePage=65001' $codePage.Result 'uv writes to a UTF-8 console' }
                $prompt = 'fake uv prompt [y/N] '
                $buffer = [char[]]::new($prompt.Length)
                $read = $process.StandardOutput.ReadBlockAsync($buffer, 0, $buffer.Length)
                Assert-True ($read.Wait(10000)) "The prompt is visible before an answer in $runtime"
                Assert-Equal $prompt (-join $buffer) 'uv passes through a prompt without a newline'
                $process.StandardInput.WriteLine('y')
                $process.StandardInput.Close()
                Assert-True ($process.WaitForExit(10000)) "The answer reaches uv in $runtime"
                $lines = $process.StandardOutput.ReadToEnd() -split '\r?\n'
                Assert-True ($lines -contains 'answer=y') 'uv gets the answer'
                Assert-True ($lines -contains $unicode) 'UTF-8 progress reaches the caller intact'
                Assert-True ($lines -contains 'bootstrap=1') 'The installer gets the bootstrap context'
                Assert-True ($lines -contains 'bootstrapAfter=original') 'The flag context is scoped to uv'
                if ($IsWindows) { Assert-True ($lines -contains 'codePageAfter=437') 'The bootstrap restores the console code page' }
                $cwd = $lines | Where-Object { $_ -like 'cwd=*' } | Select-Object -First 1
                Assert-True ($null -ne $cwd) 'uv reports its working directory'
                $expectedCwd = Resolve-LegionDirectoryPath (Join-Path $scratch 'checkout')
                Assert-Equal $expectedCwd (Resolve-LegionDirectoryPath $cwd.Substring(4)) 'Relative arguments use the current PowerShell location'
                Assert-True ($lines -contains 'exit=3') 'The hand-over preserves the exit code'
                Assert-True ($lines -contains 'lastExit=3') 'The exit code reaches the PowerShell caller'
                Assert-True ($lines -contains 'resultCount=0') 'uv output bypasses the output pipeline'
                Assert-Equal 'fake uv diagnostic' $process.StandardError.ReadToEnd().Trim() 'uv stderr reaches the owner'
            } finally {
                $process.StandardInput.Close()
                if (-not $process.WaitForExit(1000)) { $process.Kill($true) }
                $process.Dispose()
            }
        }
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'BootstrapToleratesUnavailableConsoleEncoding' {
    if (-not $IsWindows) { return }
    $scratch = New-BootstrapScratch
    try {
        foreach ($runtime in @(Get-BootstrapRuntimes)) {
            foreach ($mode in @('encoding-set-error', 'encoding-restore-error')) {
                $result = Invoke-BootstrapFixture -Runtime $runtime -Mode $mode -Scratch $scratch
                $expected = if ($mode -eq 'encoding-set-error') { 1 } else { 2 }
                Assert-True ($result.Lines -contains "encodingWrites=$expected") "Expected encoding failure in $mode"
                Assert-True ($result.Lines -contains 'lastExit=3') 'uv still runs and passes its status to the caller'
                Assert-True ($result.Lines -contains 'tempLeft=0') 'Cleanup still runs'
            }
        }
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'BootstrapDownloadsOnlyOverHttps' {
    $scratch = New-BootstrapScratch
    try {
        $result = Invoke-BootstrapFixture -Runtime (Get-InstallTestPwsh) -Mode 'http-refused' -Scratch $scratch
        Assert-True ($result.Lines -contains 'refused=True') "A plain HTTP address is refused: $($result.Lines -join ' ')"
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'BootstrapHomeAndPurge' {
    $scratch = New-BootstrapScratch
    try {
        $runtime = Get-InstallTestPwsh
        $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'default-home' -Scratch $scratch
        Assert-True (($result.Lines -join "`n").Contains("uv:--home`nuv:$scratch`nuv:install")) "The redirected `$HOME is handed over as --home: $($result.Lines -join ' ')"

        # Purge deletes .legion, so uv and the project environment must be outside it.
        $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'purge' -Scratch $scratch
        $text = $result.Lines -join "`n"
        $legion = Join-Path $scratch '.legion'
        Assert-True ($result.Lines -contains 'uv:--purge') "Purge reaches legion install: $text"
        Assert-True ($result.Lines -contains 'downloads=') "The existing uv is reused: $text"
        Assert-True (@($result.Lines | Where-Object { $_ -like 'uv:env=*' -and -not $_.Contains($legion) }).Count -eq 1) "The environment is outside .legion: $text"
        Assert-True ($result.Lines -contains 'tempLeft=0') "The project environment is removed afterwards: $text"
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'BootstrapPythonPrerequisite' {
    $scratch = New-BootstrapScratch
    try {
        $runtime = Get-InstallTestPwsh
        $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'no-python-dry' -Scratch $scratch
        $text = $result.Lines -join "`n"
        Assert-True ($text.Contains('Python 3.12 is missing')) "A preview reports the prerequisite: $text"
        Assert-True ($result.Lines -contains 'prompts=0') 'A preview never asks'
        Assert-True (-not ($result.Lines -contains 'uv:run')) "A preview never lets uv fetch Python: $text"

        $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'no-python-declined' -Scratch $scratch
        Assert-True ($result.Lines -contains 'prompts=1') 'An ordinary run asks before fetching Python'
        Assert-True (-not ($result.Lines -contains 'uv:run')) 'A no never lets uv fetch Python'

        $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'no-python-yes' -Scratch $scratch
        Assert-True ($result.Lines -contains 'uv:run') "A yes lets uv fetch Python and run: $($result.Lines -join ' ')"
        Assert-True ($result.Lines -contains 'prompts=0') '-Yes never asks'
        Assert-True ($result.Lines -contains 'lastExit=0') 'Successful hand-over clears a preceding failure code'
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'BootstrapObtainsUvFirst' {
    $scratch = New-BootstrapScratch
    try {
        $runtime = Get-InstallTestPwsh
        $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'uv-missing-dry' -Scratch $scratch -UvOnPath $false
        $text = $result.Lines -join "`n"
        Assert-Equal 0 $result.ExitCode "Remote dry run without uv: $text"
        Assert-True ($result.Lines -contains 'uv is missing.') "Missing uv is reported: $text"
        Assert-True ($result.Lines -contains 'downloads=') 'A dry run downloads nothing'
        Assert-True ($result.Lines -contains 'uvInstalled=False') 'A dry run installs no uv'
        Assert-True ($result.Lines -contains 'prompts=0') 'A dry run never asks'
        Assert-True (-not ($result.Lines -contains 'uv:run')) 'A dry run never reaches legion'
        Assert-True ($result.Lines -contains 'tempLeft=0') 'The dry run leaves no temporary files'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $scratch '.legion'))) 'The dry run leaves the Legion home absent'

        $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'uv-missing-declined' -Scratch $scratch -UvOnPath $false
        Assert-True ($result.Lines -contains 'prompts=1') 'Without -Yes it asks once'
        Assert-True ($result.Lines -contains 'downloads=') 'A no downloads nothing'
        Assert-True (($result.Lines -join ' ').Contains('Nothing changed. Run again with -Yes to apply.')) 'A no says how to apply'

        $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'uv-missing-yes' -Scratch $scratch -UvOnPath $false
        $text = $result.Lines -join "`n"
        Assert-Equal 0 $result.ExitCode "uv-missing-yes: $text"
        Assert-True ($result.Lines -contains 'prompts=0') '-Yes never asks'
        Assert-True ($result.Lines -contains 'uvInstalled=True') 'The verified uv release is published to the default uv folder'
        $asset = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'uv-aarch64-pc-windows-msvc.zip' } else { 'uv-x86_64-pc-windows-msvc.zip' }
        $url = "https://github.com/astral-sh/uv/releases/latest/download/$asset"
        Assert-True ($result.Lines -contains "downloads=$url.sha256,$url") "Only the uv release and its checksum are downloaded for a checkout: $text"
        Assert-True ($result.Lines -contains 'uv:install') 'Then legion install runs through the new uv'
        Assert-True ($result.Lines -contains 'tempLeft=0') 'The temporary folder is gone afterwards'

        Remove-Item -LiteralPath (Join-Path $scratch '.local/bin') -Recurse -Force
        foreach ($mode in @('uv-bad-digest', 'uv-no-digest', 'uv-broken')) {
            $result = Invoke-BootstrapFixture -Runtime $runtime -Mode $mode -Scratch $scratch -UvOnPath $false
            $text = $result.Lines -join "`n"
            Assert-True ($result.ExitCode -ne 0) "$mode fails: $text"
            Assert-True ($text.Contains('digest') -or $text.Contains('does not run')) "$mode says why: $text"
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $scratch '.local/bin/uv.exe'))) "$mode publishes nothing"
            Assert-Equal 0 @(Get-ChildItem -LiteralPath (Join-Path $scratch 'tmp') -Force).Count "$mode deletes the download"
            Assert-True (-not ($result.Lines -contains 'uv:run')) "$mode never reaches legion"
        }

        $result = Invoke-BootstrapFixture -Runtime $runtime -Mode 'find-folder' -Scratch $scratch -UvOnPath $false
        Assert-True (@($result.Lines | Where-Object { $_ -like 'found=*uv*' }).Count -eq 1) "uv is found in its own folder: $($result.Lines -join ' ')"
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'BootstrapCleanupWithMissingUvKeepsHomeClean' {
    $scratch = New-BootstrapScratch
    try {
        $retained = 0
        foreach ($mode in @('uv-missing-uninstall', 'uv-missing-purge')) {
            foreach ($invocation in 1..2) {
                $result = Invoke-BootstrapFixture -Runtime (Get-InstallTestPwsh) -Mode $mode -Scratch $scratch -UvOnPath $false
                $text = $result.Lines -join "`n"
                Assert-Equal 0 $result.ExitCode "$mode invocation ${invocation}: $text"
                Assert-True ($text.Contains('temporary')) "Cleanup explains temporary uv on $mode invocation ${invocation}: $text"
                Assert-True ($result.Lines -contains 'uv:uninstall') "Cleanup reaches Python on $mode invocation ${invocation}: $text"
                Assert-True ($result.Lines -contains 'uvInstalled=False') "Cleanup leaves no uv in the home on $mode invocation ${invocation}: $text"
                Assert-True (-not (Test-Path -LiteralPath (Join-Path $scratch '.legion'))) "Cleanup leaves a clean home on $mode invocation ${invocation}: $text"
                Assert-True (@($result.Lines | Where-Object { $_ -like 'uvUsed=*' -and $_.Contains((Join-Path $scratch 'tmp')) }).Count -eq 1) "Cleanup runs temporary uv on $mode invocation ${invocation}: $text"
                $uv = ($result.Lines | Where-Object { $_ -like 'uvUsed=*' }).Substring(7)
                Assert-True (Test-Path -LiteralPath $uv -PathType Leaf) "Temporary uv is available to the helper after bootstrap exit: $text"
                $retained += 1
                Assert-True ($result.Lines -contains "tempLeft=$retained") "Only the temporary uv folders remain: $text"
            }
        }
    } finally { Remove-InstallTestFolder $scratch }
}

Invoke-InstallTestCase 'ShellBootstrap' {
    if ($IsWindows) { return }
    $scratch = New-InstallTestFolder 'shell-bootstrap'
    try {
        $fakeHome = Join-Path $scratch 'home'
        $fakeBin = Join-Path $scratch 'bin'
        New-Item -ItemType Directory -Path (Join-Path $fakeHome 'checkout/plugins/legion'), $fakeBin -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $fakeHome 'checkout/plugins/legion/pyproject.toml') -Value '[project]'
        $log = Join-Path $scratch 'uv-log.txt'
        $uvLines = @('#!/bin/sh', '[ "$1" != python ] || exit "${FAKE_UV_PYTHON:-0}"', ': > "$FAKE_UV_LOG"', 'for word in "$@"; do echo "$word" >> "$FAKE_UV_LOG"; done', 'echo "env=$UV_PROJECT_ENVIRONMENT" >> "$FAKE_UV_LOG"', 'echo "uv=$(command -v uv)" >> "$FAKE_UV_LOG"', 'exit "${FAKE_UV_EXIT:-0}"')
        Set-Content -LiteralPath (Join-Path $fakeBin 'uv') -Value $uvLines
        # A fake curl serves the source archive, uv release and checksum, and logs the URLs.
        $archiveSource = Join-Path $scratch 'archive/legion-stable/plugins/legion'
        New-Item -ItemType Directory -Path $archiveSource -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $archiveSource 'pyproject.toml') -Value '[project]'
        & tar -czf (Join-Path $scratch 'plugin.tar.gz') -C (Join-Path $scratch 'archive') legion-stable
        $curlLines = @(
            '#!/bin/sh'
            'printf ''%s\n'' "$*" >> "$FAKE_CURL_ARGS"'
            'out=; url='
            'while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift ;; -*) ;; *) url=$1 ;; esac; shift; done'
            'echo "$url" >> "$FAKE_CURL_LOG"'
            'case "$url" in'
            ('  */archive/*) [ -z "$FAKE_CURL_FAIL" ] || exit 22; cp "' + $scratch + '/plugin.tar.gz" "$out" ;;')
            '  */releases/latest/download/*.sha256) printf ''%s  %s\n'' "$FAKE_UV_DIGEST" "$FAKE_UV_ASSET" > "$out" ;;'
            ('  */releases/latest/download/*) cp "' + $scratch + '/uv-release.tar.gz" "$out" ;;')
            '  *) exit 22 ;;'
            'esac'
        )
        Set-Content -LiteralPath (Join-Path $fakeBin 'curl') -Value $curlLines
        # The fake uv release: a tar.gz with one top folder holding the fake uv, as the real ones do.
        $releaseFolder = Join-Path $scratch 'uv-release/uv-triple'
        New-Item -ItemType Directory -Path $releaseFolder -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $fakeBin 'uv') -Destination (Join-Path $releaseFolder 'uv')
        Set-TestExecutable (Join-Path $releaseFolder 'uv')
        & tar -czf (Join-Path $scratch 'uv-release.tar.gz') -C (Join-Path $scratch 'uv-release') uv-triple
        $platform = (& uname -s) + '-' + (& uname -m)
        $triples = @{ 'Linux-x86_64' = 'x86_64-unknown-linux-gnu'; 'Linux-aarch64' = 'aarch64-unknown-linux-gnu'; 'Darwin-x86_64' = 'x86_64-apple-darwin'; 'Darwin-arm64' = 'aarch64-apple-darwin' }
        $env:FAKE_UV_ASSET = 'uv-' + $triples[$platform] + '.tar.gz'
        $env:FAKE_UV_DIGEST = (Get-FileHash -LiteralPath (Join-Path $scratch 'uv-release.tar.gz') -Algorithm SHA256).Hash
        foreach ($tool in @('uv', 'curl')) { Set-TestExecutable (Join-Path $fakeBin $tool) }
        $shell = (Get-Command sh -CommandType Application | Select-Object -First 1).Source
        $setsid = Get-Command setsid -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        # Only the fake tools and required system utilities go on the bootstrap's PATH.
        $systemBin = Join-Path $scratch 'systembin'
        New-Item -ItemType Directory -Path $systemBin -Force | Out-Null
        foreach ($utility in @('uname', 'mktemp', 'tar', 'gzip', 'rm', 'mkdir', 'cp', 'mv', 'dirname', 'tr', 'head', 'sed', 'sha256sum', 'shasum')) {
            $found = Get-Command $utility -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($found) { New-Item -ItemType SymbolicLink -Path (Join-Path $systemBin $utility) -Target $found.Source | Out-Null }
        }
        $env:FAKE_UV_LOG = $log
        $env:FAKE_CURL_LOG = Join-Path $scratch 'curl-log.txt'
        $env:FAKE_CURL_ARGS = Join-Path $scratch 'curl-args.txt'
        # Temporary folders go to the system temp folder; keep that inside the scratch folder.
        $systemTemp = Join-Path $scratch 'tmp'
        New-Item -ItemType Directory -Path $systemTemp -Force | Out-Null
        $savedTempDir = $env:TMPDIR
        $env:TMPDIR = $systemTemp
        $savedUvFolders = @($env:UV_INSTALL_DIR, $env:XDG_BIN_HOME)
        Remove-Item Env:UV_INSTALL_DIR, Env:XDG_BIN_HOME -ErrorAction SilentlyContinue
        $env:FAKE_UV_SOURCE = Join-Path $fakeBin 'uv'
        $savedPath = $env:PATH
        $savedHome = $env:HOME
        $env:HOME = $fakeHome
        try {
            # A checkout run with uv on PATH downloads nothing and passes the options through.
            Copy-Item -LiteralPath (Join-Path $repo 'install.sh') -Destination (Join-Path $fakeHome 'checkout/install.sh')
            $env:PATH = $fakeBin + [IO.Path]::PathSeparator + $systemBin
            $text = @('' | & $shell (Join-Path $fakeHome 'checkout/install.sh') --yes --main --dry-run 2>&1) -join "`n"
            Assert-Equal 0 $LASTEXITCODE "Shell checkout run: $text"
            $logged = (Get-Content -LiteralPath $log) -join '|'
            $words = $logged -split '\|'
            $project = Resolve-LegionDirectoryPath (Join-Path $fakeHome 'checkout/plugins/legion')
            $actualProject = Resolve-LegionDirectoryPath $words[2]
            Assert-True ((Get-LegionPathComparer).Equals($actualProject, $project)) "Shell checkout project: $logged"
            Assert-True ($logged.StartsWith('run|--project|' + $words[2] + '|--frozen|--quiet|legion|--home|' + $fakeHome + '|install|--ref|main|--dry-run|--yes|env=')) "Shell hand-over: $logged"
            Assert-True (-not (Test-Path $env:FAKE_CURL_LOG)) 'A checkout run downloads nothing'
            Assert-True (@(Get-ChildItem -LiteralPath $systemTemp -Force).Count -eq 0) 'The shell run removes its temporary folder'
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $fakeHome '.legion/temp'))) 'Nothing temporary is made inside .legion'
            Assert-True (@(Get-Content -LiteralPath $env:FAKE_CURL_ARGS -ErrorAction SilentlyContinue).Count -eq 0) 'A checkout run runs no curl'

            # Piped from curl (no file on disk): the stable archive is downloaded and used.
            $script = Get-Content -LiteralPath (Join-Path $repo 'install.sh') -Raw
            $text = @($script | & $shell -s -- --yes 2>&1) -join "`n"
            Assert-Equal 0 $LASTEXITCODE "Shell remote run: $text"
            Assert-True ($text.Contains('Preparing legion (first run can take a minute)...')) 'The shell announces preparation'
            Assert-True ((Get-Content -LiteralPath $env:FAKE_CURL_LOG) -contains 'https://github.com/BlackGad/ps-legion-marketplace/archive/refs/heads/stable.tar.gz') 'The stable archive is downloaded'
            Assert-True (((Get-Content -LiteralPath $log) -join '|') -like 'run|--project|*/source/plugins/legion|*') 'uv runs the downloaded project'
            Remove-Item $env:FAKE_CURL_LOG
            Remove-Item -LiteralPath $log
            $text = @($script | & $shell -s -- --main --dry-run --yes --update 2>&1) -join "`n"
            Assert-Equal 0 $LASTEXITCODE "Shell remote dry run: $text"
            Assert-Equal 'https://github.com/BlackGad/ps-legion-marketplace/archive/refs/heads/main.tar.gz' ((Get-Content -LiteralPath $env:FAKE_CURL_LOG) -join '|') 'Only the source archive is downloaded'
            $words = @(Get-Content -LiteralPath $log)
            Assert-True (($words[2] -like '*/source/plugins/legion') -and (Test-LegionPathUnder -Path $words[2] -Root $systemTemp)) "uv runs the downloaded project: $($words -join '|')"
            Assert-Equal "run|--project|$($words[2])|--frozen|--quiet|legion|--home|$fakeHome|update|--ref|main|--dry-run|--yes" ($words[0..($words.Count - 3)] -join '|') 'uv gets the complete dry-run command'
            $project = [IO.DirectoryInfo]$words[2]
            $temporary = $project.Parent.Parent.Parent
            Assert-Equal "env=$($temporary.FullName)/env" $words[-2] 'uv uses a disposable project environment'
            Assert-Equal 0 @(Get-ChildItem -LiteralPath $systemTemp -Force).Count 'Temporary files are removed'
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $fakeHome '.legion'))) 'The dry run leaves the Legion home absent'

            # A failed release download says what to do and never runs uv.
            Remove-Item $log
            $env:FAKE_CURL_FAIL = '1'
            $text = @($script | & $shell -s -- --yes 2>&1) -join "`n"
            Assert-True ($LASTEXITCODE -ne 0) 'A failed download stops'
            Assert-True ($text.Contains('could not find the latest release; try again or run with --main')) 'The failure says what to do'
            Assert-True (-not (Test-Path $log)) 'uv never runs after a failed download'
            Remove-Item Env:FAKE_CURL_FAIL

            # Every download is HTTPS-only, redirects included.
            $null = @($script | & $shell -s -- --yes 2>&1)
            foreach ($line in (Get-Content -LiteralPath $env:FAKE_CURL_ARGS)) { Assert-True ($line.Contains("--proto =https") -and $line.Contains("--proto-redir =https")) "curl is HTTPS-only: $line" }

            # uv exit code is the bootstrap's.
            $env:FAKE_UV_EXIT = '3'
            $null = @($script | & $shell -s -- --yes 2>&1)
            Assert-Equal 3 $LASTEXITCODE "uv's exit code is the bootstrap's"
            Remove-Item Env:FAKE_UV_EXIT

            # With uv present but no Python 3.12: a preview reports it and never lets uv fetch Python; a run asks first.
            Copy-Item -LiteralPath (Join-Path $fakeHome 'checkout/install.sh') -Destination (Join-Path $scratch 'copy-install.sh')
            $env:FAKE_UV_PYTHON = '1'
            Remove-Item $log -ErrorAction SilentlyContinue
            $text = @('' | & $shell (Join-Path $fakeHome 'checkout/install.sh') --dry-run 2>&1) -join "`n"
            Assert-True ($text.Contains('Python 3.12 is missing') -and -not (Test-Path $log)) "A preview reports Python and never runs uv: $text"
            if ($setsid) {
                # No controlling terminal, so the question cannot be answered.
                $text = @('' | & $setsid.Source -w $shell (Join-Path $fakeHome 'checkout/install.sh') 2>&1) -join "`n"
                Assert-True ($text.Contains('Nothing changed. Run again with --yes to apply.') -and -not (Test-Path $log)) "Without a yes Python is not fetched: $text"
            }
            $null = @('' | & $shell (Join-Path $fakeHome 'checkout/install.sh') --yes 2>&1)
            Assert-True (Test-Path $log) 'With a yes uv runs and may fetch Python'
            Remove-Item Env:FAKE_UV_PYTHON

            # A purge runs from an environment outside .legion.
            $null = @('' | & $shell (Join-Path $fakeHome 'checkout/install.sh') --yes --uninstall --purge 2>&1)
            $logged = Get-Content -LiteralPath $log
            Assert-True (($logged -join '|') -like '*|uninstall|--purge|--yes|env=*') 'Purge uses the uninstall verb'
            $projectEnv = (@(Get-Content -LiteralPath $log | Where-Object { $_ -like 'env=*' })[0]).Substring(4)
            Assert-True ((Test-LegionPathUnder -Path $projectEnv -Root $systemTemp) -and -not (Test-LegionPathUnder -Path $projectEnv -Root (Join-Path $fakeHome '.legion'))) "Purge runs outside .legion: $projectEnv"

            # Without uv: a remote dry run changes nothing; --yes obtains uv, then hands over.
            $noUv = Join-Path $scratch 'nouv'
            New-Item -ItemType Directory -Path $noUv -Force | Out-Null
            Copy-Item -LiteralPath (Join-Path $fakeBin 'curl') -Destination (Join-Path $noUv 'curl')
            Set-TestExecutable (Join-Path $noUv 'curl')
            $env:PATH = $noUv + [IO.Path]::PathSeparator + $systemBin
            Remove-Item $env:FAKE_CURL_LOG -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $log
            $text = @($script | & $shell -s -- --dry-run --yes 2>&1) -join "`n"
            Assert-Equal 0 $LASTEXITCODE "Shell remote dry run without uv: $text"
            Assert-True ($text.Contains('uv is missing.')) 'A missing uv is reported'
            Assert-True (-not (Test-Path $env:FAKE_CURL_LOG)) 'A dry run downloads nothing'
            Assert-True (-not (Test-Path -LiteralPath $log)) 'A dry run without uv does not reach legion install'
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $fakeHome '.legion'))) 'The dry run leaves the Legion home absent'
            if ($setsid) {
                # Launched without a controlling terminal, so the question cannot reach a person.
                $text = @('' | & $setsid.Source -w $shell (Join-Path $fakeHome 'checkout/install.sh') 2>&1) -join "`n"
                Assert-True ($text.Contains('Nothing changed. Run again with --yes to apply.')) 'Without a terminal and without --yes nothing changes'
            }
            $text = @('' | & $shell (Join-Path $fakeHome 'checkout/install.sh') --yes 2>&1) -join "`n"
            Assert-Equal 0 $LASTEXITCODE "Shell uv install: $text"
            $uvUrl = "https://github.com/astral-sh/uv/releases/latest/download/$env:FAKE_UV_ASSET"
            Assert-True ((Get-Content -LiteralPath $env:FAKE_CURL_LOG) -contains $uvUrl) 'The uv release is downloaded'
            Assert-True ((Get-Content -LiteralPath $env:FAKE_CURL_LOG) -contains "$uvUrl.sha256") 'The uv checksum is downloaded'
            Assert-True (-not ((Get-Content -LiteralPath $env:FAKE_CURL_LOG) -match 'api.github.com')) 'The GitHub API is not needed'
            $installedUv = Join-Path $fakeHome '.local/bin/uv'
            Assert-True (Test-Path -LiteralPath $installedUv) 'The verified uv is published to the default uv folder'
            Assert-True ((Get-Content -LiteralPath $log) -contains 'install') 'Then legion install runs'

            $text = @('' | & $shell (Join-Path $fakeHome 'checkout/install.sh') --uninstall --purge --yes 2>&1) -join "`n"
            Assert-Equal 0 $LASTEXITCODE "Shell cleanup with installed uv: $text"
            $resolvedUv = @(Get-Content -LiteralPath $log | Where-Object { $_ -like 'uv=*' })
            Assert-Equal 1 $resolvedUv.Count 'Cleanup records one uv lookup'
            Assert-True ((Get-LegionPathComparer).Equals((Resolve-LegionDirectoryPath $resolvedUv[0].Substring(3)), (Resolve-LegionDirectoryPath $installedUv))) 'Cleanup children resolve the installed uv'

            # Cleanup from a clean home may download uv twice, but each run must leave the home clean.
            Remove-Item -LiteralPath $installedUv
            foreach ($invocation in 1..2) {
                $text = @('' | & $shell (Join-Path $fakeHome 'checkout/install.sh') --uninstall --purge --yes 2>&1) -join "`n"
                Assert-Equal 0 $LASTEXITCODE "Shell cleanup invocation ${invocation}: $text"
                Assert-True ($text.Contains('temporary')) "Cleanup explains temporary uv on invocation ${invocation}: $text"
                Assert-True ((Get-Content -LiteralPath $log) -contains '--purge') "Cleanup reaches Python on invocation ${invocation}"
                $resolvedUv = @(Get-Content -LiteralPath $log | Where-Object { $_ -like 'uv=*' })
                Assert-Equal 1 $resolvedUv.Count "Cleanup records one uv lookup on invocation ${invocation}"
                Assert-True (Test-LegionPathUnder -Path $resolvedUv[0].Substring(3) -Root $systemTemp) "Cleanup children resolve temporary uv on invocation ${invocation}"
                Assert-True (-not (Test-Path -LiteralPath $installedUv)) "Cleanup leaves no installed uv on invocation ${invocation}"
                Assert-True (-not (Test-Path -LiteralPath (Join-Path $fakeHome '.legion'))) "Cleanup leaves a clean home on invocation ${invocation}"
                $uv = $resolvedUv[0].Substring(3)
                Assert-True (Test-Path -LiteralPath $uv -PathType Leaf) "Temporary uv is available to the helper after bootstrap exit"
                & $uv --version
                Assert-Equal 0 $LASTEXITCODE 'Temporary uv is still executable'
                $left = @(Get-ChildItem -LiteralPath $systemTemp -Force)
                Assert-Equal $invocation $left.Count 'Only temporary uv folders remain'
            }

            # A digest mismatch deletes the download, extracts nothing and never runs uv.
            Remove-Item $log
            $env:FAKE_UV_DIGEST = '0' * 64
            $text = @('' | & $shell (Join-Path $fakeHome 'checkout/install.sh') --yes 2>&1) -join "`n"
            Assert-True ($LASTEXITCODE -ne 0 -and $text.Contains('uv SHA-256 digest mismatch; download deleted.')) "A digest mismatch fails: $text"
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $fakeHome '.local/bin/uv'))) 'A digest mismatch publishes nothing'
            Assert-True (-not (Test-Path $log)) 'uv never runs after a digest mismatch'
            Assert-Equal 2 @(Get-ChildItem -LiteralPath $systemTemp -Force).Count 'A digest mismatch adds no download'

            $text = @('' | & $shell (Join-Path $fakeHome 'checkout/install.sh') --bogus 2>&1) -join "`n"
            Assert-True ($LASTEXITCODE -ne 0 -and $text.Contains('Unknown option: --bogus')) 'An unknown option is refused'
        } finally {
            $env:PATH = $savedPath
            $env:HOME = $savedHome
            if ($null -eq $savedTempDir) { Remove-Item Env:TMPDIR -ErrorAction SilentlyContinue }
            else { $env:TMPDIR = $savedTempDir }
            if ($savedUvFolders[0]) { $env:UV_INSTALL_DIR = $savedUvFolders[0] }
            if ($savedUvFolders[1]) { $env:XDG_BIN_HOME = $savedUvFolders[1] }
            foreach ($name in @('FAKE_UV_LOG', 'FAKE_CURL_LOG', 'FAKE_UV_SOURCE', 'FAKE_UV_ASSET', 'FAKE_UV_DIGEST', 'FAKE_CURL_FAIL', 'FAKE_UV_EXIT', 'FAKE_UV_PYTHON', 'FAKE_CURL_ARGS')) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        }
    } finally { Remove-InstallTestFolder $scratch }
}
