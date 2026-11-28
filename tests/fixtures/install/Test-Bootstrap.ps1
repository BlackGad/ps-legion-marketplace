# Runs the root bootstrap with fakes in the PowerShell that runs this file (5.1 or 7): no network, no real uv.
param([string]$Installer, [string]$Mode, [string]$Scratch, [string]$Archive)
$ErrorActionPreference = 'Stop'
. $Installer -NoRun
$script:installHomeRoot = $Scratch
$script:downloads = [Collections.Generic.List[string]]::new()
$uvLog = Join-Path $Scratch 'uv-log.txt'
Remove-Item -LiteralPath $uvLog -ErrorAction SilentlyContinue
$env:FAKE_UV_LOG = $uvLog
$env:FAKE_UV_EXIT = '0'
# The bootstrap's temporary folders go to the system temp folder; keep that inside the scratch folder.
$systemTemp = Join-Path $Scratch 'tmp'
New-Item -ItemType Directory -Path $systemTemp -Force | Out-Null
foreach ($name in @('TMP', 'TEMP', 'TMPDIR')) { Set-Item -Path "Env:$name" -Value $systemTemp }
foreach ($name in @('UV_INSTALL_DIR', 'XDG_BIN_HOME')) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
$onWindows = $env:OS -eq 'Windows_NT'
$uvName = 'uv'
if ($onWindows) { $uvName = 'uv.cmd' }
# A PATH with no uv on it, but with the system tools the bootstrap needs.
function Set-NoUvPath {
    if ($onWindows) { $env:PATH = Join-Path $env:SystemRoot 'System32' } else { $env:PATH = Join-Path $Scratch 'empty' }
}

# A fake uv release: a ZIP and its checksum sidecar.
$uvAsset = 'uv-x86_64-pc-windows-msvc.zip'
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $uvAsset = 'uv-aarch64-pc-windows-msvc.zip' }
$uvZip = Join-Path $Scratch 'uv-release.zip'
function New-UvRelease {
    $folder = Join-Path $Scratch 'uv-release'
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $folder 'uv.exe') -Value 'uv placeholder'
    Compress-Archive -Path (Join-Path $folder 'uv.exe') -DestinationPath $uvZip -Force
}

$realDownload = ${function:Invoke-InstallDownload}
function Invoke-InstallDownload {
    param([string]$Url, [string]$OutFile)
    $script:downloads.Add($Url)
    if ($Url -like '*/archive/refs/heads/*') {
        if ($script:Mode -eq 'no-release') { throw 'HTTP 404' }
        Copy-Item -LiteralPath $script:Archive -Destination $OutFile
    } elseif ($Url -like 'https://github.com/astral-sh/uv/releases/latest/download/*.sha256') {
        New-UvRelease
        $hash = (Get-FileHash -LiteralPath $uvZip -Algorithm SHA256).Hash
        if ($script:Mode -eq 'uv-bad-digest') { $hash = '0' * 64 }
        if ($script:Mode -eq 'uv-no-digest') { $hash = '' }
        Set-Content -LiteralPath $OutFile -Value "$hash  $script:uvAsset"
    } elseif ($Url -like 'https://github.com/astral-sh/uv/releases/latest/download/*.zip') {
        Copy-Item -LiteralPath $uvZip -Destination $OutFile
    } else { throw "unexpected download $Url" }
}
# The downloaded and cleanup executables are placeholders. Send those paths to the executable fake.
$realInvokeUv = ${function:Invoke-InstallUv}
function Invoke-InstallUv {
    param([string]$Uv, [string[]]$Arguments)
    if ($script:Mode -eq 'uv-call') { Write-Host "uvCodePage=$([Console]::OutputEncoding.CodePage)" }
    if ($Uv -eq $script:fakeUvPath) {
        if (-not (Test-Path -LiteralPath $Uv -PathType Leaf)) { throw "Missing fixture uv: $Uv" }
        $script:uvUsed = $Uv
        $script:pathHead = ($env:PATH -split [regex]::Escape([string][IO.Path]::PathSeparator))[0]
        $Uv = Join-Path $Scratch "fake-uv-source/$uvName"
    }
    & $realInvokeUv -Uv $Uv -Arguments $Arguments
}

# A cleanup mode reserves the first temporary folder so it can identify the copied uv before bootstrapping.
$realNewTemporary = ${function:New-InstallTemporary}
function New-InstallTemporary {
    if ($script:reservedTemporary) {
        $reserved = $script:reservedTemporary
        $script:reservedTemporary = $null
        return $reserved
    }
    & $realNewTemporary
}

function Set-CleanupFakeUv {
    $script:reservedTemporary = & $realNewTemporary
    $script:fakeUvPath = Join-Path $script:reservedTemporary 'uv.exe'
}

# The extracted uv is a placeholder, so "runs" is decided by the mode.
function Test-InstallUv { param([string]$Uv) return $script:Mode -ne 'uv-broken' }
function Test-InstallPython { param([string]$Uv) return -not $script:Mode.StartsWith('no-python') }
function Read-Host { param($Prompt) $script:prompts++; return $script:answer }
$script:prompts = 0
$script:answer = 'n'

function Show-Result {
    Write-Output ("downloads=" + ($script:downloads -join ','))
    Write-Output ("uvInstalled=" + (Test-Path -LiteralPath (Join-Path $Scratch '.local/bin/uv.exe')))
    Write-Output "prompts=$($script:prompts)"
    Write-Output "exit=$($script:bootstrapExitCode)"
    Write-Output "lastExit=$LASTEXITCODE"
    Write-Output "uvUsed=$($script:uvUsed)"
    Write-Output "pathHead=$($script:pathHead)"
    if (Test-Path -LiteralPath $uvLog) { Get-Content -LiteralPath $uvLog | ForEach-Object { Write-Output "uv:$_" } }
    $left = @(Get-ChildItem -LiteralPath $systemTemp -Force -ErrorAction SilentlyContinue)
    Write-Output "tempLeft=$($left.Count)"
}

$parameters = @{ Yes = [switch]$true; HomeRoot = $Scratch }
switch ($Mode) {
    'checkout' { Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout') }
    'remote' { Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot '' }
    'remote-main' { $parameters.Ref = 'main'; Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot '' }
    'remote-dry-run' {
        Set-NoUvPath
        $env:PATH = (Join-Path $Scratch 'bin') + [IO.Path]::PathSeparator + $env:PATH
        $parameters.Ref = 'main'
        $parameters.DryRun = [switch]$true
        $parameters.Update = [switch]$true
        Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot ''
    }
    'no-release' { Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot '' }
    'exit-code' { $env:FAKE_UV_EXIT = '3'; Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout') }
    { $_ -in 'encoding-set-error', 'encoding-restore-error' } {
        $script:encodingWrites = 0
        function Set-InstallConsoleEncoding {
            param([Text.Encoding]$Encoding)
            $script:encodingWrites++
            if ($script:Mode -eq 'encoding-set-error' -or $script:encodingWrites -eq 2) {
                throw [IO.IOException]::new('The handle is invalid.')
            }
        }
        $env:FAKE_UV_EXIT = '3'
        Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout')
        Write-Output "encodingWrites=$script:encodingWrites"
    }
    { $_ -in 'uv-missing-yes', 'uv-missing-uninstall', 'uv-missing-purge', 'uv-bad-digest', 'uv-no-digest', 'uv-broken' } {
        # No uv on PATH: -Yes downloads and verifies the uv release, then the hand-over runs.
        $env:OS = 'Windows_NT'
        Set-NoUvPath
        if ($Mode -in @('uv-missing-uninstall', 'uv-missing-purge')) {
            $parameters.Uninstall = [switch]$true
            Set-CleanupFakeUv
        } elseif ($Mode -eq 'uv-missing-yes') {
            $script:fakeUvPath = Join-Path $Scratch '.local/bin/uv.exe'
        }
        if ($Mode -eq 'uv-missing-purge') { $parameters.Purge = [switch]$true }
        Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout')
    }
    'uv-missing-dry' {
        $env:OS = 'Windows_NT'
        Set-NoUvPath
        $parameters.DryRun = [switch]$true
        Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot ''
    }
    'uv-missing-declined' {
        $env:OS = 'Windows_NT'
        Set-NoUvPath
        $parameters.Remove('Yes')
        Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout')
    }
    'purge' {
        $parameters.Purge = [switch]$true
        Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout')
    }
    'no-python-dry' { $parameters.DryRun = [switch]$true; Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout') }
    'no-python-declined' { $parameters.Remove('Yes'); Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout') }
    'no-python-yes' {
        $global:LASTEXITCODE = 7
        Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout')
    }
    'uv-call' {
        # Exercise the real hand-over with the executable fake on PATH.
        $env:FAKE_UV_SAY = '1'
        $env:FAKE_UV_EXIT = '3'
        $env:LEGION_BOOTSTRAP_POWERSHELL = 'original'
        if ($onWindows) { [Console]::OutputEncoding = [Text.Encoding]::GetEncoding(437) }
        $parameters.Remove('Yes')
        $parameters.Update = [switch]$true
        Push-Location -LiteralPath (Join-Path $Scratch 'checkout')
        try { $result = @(Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Get-Location).Path) }
        finally { Pop-Location }
        Write-Output "resultCount=$($result.Count)"
        Write-Output "bootstrapAfter=$env:LEGION_BOOTSTRAP_POWERSHELL"
        Write-Output "codePageAfter=$([Console]::OutputEncoding.CodePage)"
    }
    'http-refused' {
        # The real download refuses a plain HTTP address before it connects.
        try { & $realDownload -Url 'http://127.0.0.1:9/uv.zip' -OutFile (Join-Path $Scratch 'x') ; Write-Output 'refused=False' }
        catch { Write-Output "refused=$($_.Exception.Message.Contains('HTTPS'))" }
        return
    }
    'default-home' {
        # Without -HomeRoot the effective home is $HOME, and it is handed over.
        Set-Variable -Name HOME -Value $Scratch -Force
        $parameters.Remove('HomeRoot')
        Invoke-InstallBootstrap -Parameters $parameters -CheckoutRoot (Join-Path $Scratch 'checkout')
    }
    'find-folder' {
        Set-NoUvPath
        $folder = Join-Path $Scratch '.local/bin'
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
        $name = 'uv'
        if ($onWindows) { $name = 'uv.exe' }
        Set-Content -LiteralPath (Join-Path $folder $name) -Value ''
        Write-Output ('found=' + (Get-InstallUv))
        return
    }
    default { throw "unknown mode $Mode" }
}
Show-Result
