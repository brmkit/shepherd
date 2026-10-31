# Run over SSH as the guest user, not as the QEMU Guest Agent service account.
# The local worker supplies $version to match the local herdr-core release.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable for the SSH user.' }
    $dir = Join-Path $env:LOCALAPPDATA 'Programs\herdr-core'
    $exe = Join-Path $dir 'herdr-core.exe'
    $installed = ''
    if (Test-Path -LiteralPath $exe) {
        $installed = (& $exe --version | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { $installed = '' }
    }
    if ($installed -ne "hc $version" -or -not (Test-Path -LiteralPath (Join-Path $dir 'conpty\conpty.dll'))) {
        Write-Output "Installing herdr-core $version for the SSH user"
        $env:HC_VERSION = $version
        $env:HC_INSTALL_DIR = $dir
        $env:HERDR_RELEASES_URL = 'https://releases.herdr.dev'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        # The official installer verifies the release archive against its manifest.
        $installer = Invoke-RestMethod -Uri 'https://releases.herdr.dev/core/install.ps1' -UseBasicParsing
        & ([scriptblock]::Create($installer))
    }
    if (-not (Test-Path -LiteralPath $exe)) { throw 'The Windows hc installer did not produce herdr-core.exe.' }
    if (-not (Test-Path -LiteralPath (Join-Path $dir 'conpty\conpty.dll'))) { throw 'The Windows hc installation is missing ConPTY.' }
    $actual = (& $exe --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $actual -ne "hc $version") { throw "Windows hc version mismatch: expected hc $version, got $actual" }
    # Repair discovery after an interrupted install, even when the binary exists.
    if (-not (Test-Path -LiteralPath (Join-Path $dir 'hc.exe'))) {
        New-Item -ItemType HardLink -Path (Join-Path $dir 'hc.exe') -Target $exe | Out-Null
    }
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $listed = $raw -split ';' | Where-Object { $_.Trim().TrimEnd('\') -ieq $dir.TrimEnd('\') }
        if (-not $listed) {
            $kind = if ($key.GetValueNames() -contains 'Path') { $key.GetValueKind('Path') } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }
            $value = if ($raw) { $raw.TrimEnd(';') + ';' + $dir } else { $dir }
            $key.SetValue('Path', $value, $kind)
        }
    } finally { $key.Close() }
    Write-Output "Windows herdr-core verified: $actual"
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
