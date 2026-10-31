# Parameters are supplied as a decoded JSON object by the local worker.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Join-Path $env:ProgramData 'shepherd'
$lock = $null

function Assert-RegularPath([string]$Path) {
    $item = $Path
    while ($item) {
        if (Test-Path -LiteralPath $item) {
            if ((Get-Item -Force -LiteralPath $item).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing a reparse point in bootstrap path: $item"
            }
        }
        $item = Split-Path -Parent $item
    }
}

function Set-PrivateAcl([string]$Path, [string]$UserSid = '') {
    Assert-RegularPath $Path
    $directory = (Get-Item -Force -LiteralPath $Path).PSIsContainer
    if ($directory) {
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = New-Object Security.AccessControl.FileSecurity
        $inherit = [Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetAccessRuleProtection($true, $false)
    $system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
    $acl.SetOwner($system)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', $inherit, 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    if ($UserSid) {
        $identity = New-Object Security.Principal.SecurityIdentifier($UserSid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'ReadAndExecute', $inherit, 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Write-Phase([string]$Message) {
    $line = '{0} {1}' -f [DateTime]::UtcNow.ToString('o'), $Message
    [IO.File]::AppendAllText((Join-Path $root 'bootstrap.log'), $line + "`r`n", (New-Object Text.UTF8Encoding($false)))
    Write-Output $line
}

function Run-Native([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

function Update-SshConfig([string]$Text, [string]$User, [string]$KeyPath) {
    $begin = '# BEGIN shepherd user ' + $User
    $end = '# END shepherd user ' + $User
    $pattern = '(?ms)^' + [regex]::Escape($begin) + '\r?\n.*?^' + [regex]::Escape($end) + '\r?\n?'
    $Text = [regex]::Replace($Text, $pattern, '')
    $block = $begin + "`r`nMatch User " + $User + "`r`n" +
        '    AuthorizedKeysFile "' + $KeyPath.Replace('\', '/') + "`"`r`n" +
        "    PubkeyAuthentication yes`r`n    PasswordAuthentication no`r`n    AuthenticationMethods publickey`r`n" + $end + "`r`n"
    # The first matching block wins. Keep global directives before all Match blocks.
    $firstMatch = [regex]::Match($Text, '(?im)^[ \t]*Match[ \t]')
    $at = if ($firstMatch.Success) { $firstMatch.Index } else { $Text.Length }
    return $Text.Substring(0, $at).TrimEnd() + "`r`n" + $block + $Text.Substring($at)
}

try {
    Assert-RegularPath $root
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    foreach ($file in @('bootstrap.lock', 'bootstrap.log', 'ssh-identity')) {
        Assert-RegularPath (Join-Path $root $file)
    }
    try {
        $lock = [IO.File]::Open((Join-Path $root 'bootstrap.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch {
        throw 'Another Windows bootstrap is still running. Wait for it to finish before retrying.'
    }
    Set-PrivateAcl $root
    Write-Phase "Starting Windows bootstrap for job $($config.job)"

    Write-Phase 'Installing OpenSSH Server if missing'
    if (-not (Get-Service -Name sshd -ErrorAction SilentlyContinue)) {
        $result = Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
        if ($result.RestartNeeded) { throw 'OpenSSH installation requires a Windows restart. Restart the VM and retry.' }
    }
    $service = Get-CimInstance Win32_Service -Filter "Name='sshd'"
    if (-not $service) { throw 'OpenSSH Server service was not installed.' }
    $match = [regex]::Match($service.PathName, '^(?:"([^"]+)"|(\S+))')
    $sshd = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
    if (-not (Test-Path -LiteralPath $sshd)) { throw 'Cannot locate sshd.exe.' }
    $sshDir = Split-Path -Parent $sshd
    $sshKeygen = Join-Path $sshDir 'ssh-keygen.exe'
    $sshData = Join-Path $env:ProgramData 'ssh'
    Assert-RegularPath $sshData
    New-Item -ItemType Directory -Force -Path $sshData | Out-Null

    Write-Phase 'Preparing the local SSH account'
    $user = Get-LocalUser -Name $config.user -ErrorAction SilentlyContinue
    if (-not $user) {
        # Windows requires a password for a usable account. SSH remains key-only.
        # Generate it inside the guest; never print, return, or persist it.
        $bytes = New-Object byte[] 48
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $password = ConvertTo-SecureString ('Aa1!' + [Convert]::ToBase64String($bytes)) -AsPlainText -Force
        try {
            $user = New-LocalUser -Name $config.user -Password $password -PasswordNeverExpires -AccountNeverExpires -Description 'Herdr SSH account'
        } finally {
            $password.Dispose()
            [Array]::Clear($bytes, 0, $bytes.Length)
        }
        Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $user
    }
    if (-not $user.Enabled) { throw "The local account '$($config.user)' is disabled." }
    $userSid = $user.SID.Value

    Write-Phase 'Authorizing the SSH public key'
    # A per-user absolute path also works before the first Windows profile login.
    # A dedicated Match block avoids the shared administrators_authorized_keys file.
    $keysDir = Join-Path $root 'authorized_keys'
    Assert-RegularPath $keysDir
    New-Item -ItemType Directory -Force -Path $keysDir | Out-Null
    Set-PrivateAcl $root $userSid
    Set-PrivateAcl $keysDir $userSid
    $keyPath = Join-Path $keysDir $config.user
    Assert-RegularPath $keyPath
    [IO.File]::WriteAllText($keyPath, $config.publicKey + "`n", (New-Object Text.UTF8Encoding($false)))
    Set-PrivateAcl $keyPath $userSid

    Write-Phase 'Configuring key-only SSH access'
    $sshdConfig = Join-Path $sshData 'sshd_config'
    Assert-RegularPath $sshdConfig
    if (-not (Test-Path -LiteralPath $sshdConfig)) {
        Copy-Item -LiteralPath (Join-Path $sshDir 'sshd_config_default') -Destination $sshdConfig
    }
    $text = [IO.File]::ReadAllText($sshdConfig)
    $backup = $sshdConfig + '.before-shepherd'
    Assert-RegularPath $backup
    if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $sshdConfig -Destination $backup }
    $updated = Update-SshConfig $text $config.user $keyPath
    $stagedConfig = $sshdConfig + '.shepherd-new'
    Assert-RegularPath $stagedConfig
    [IO.File]::WriteAllText($stagedConfig, $updated, (New-Object Text.UTF8Encoding($false)))
    Set-PrivateAcl $stagedConfig

    Write-Phase 'Preparing unique SSH host keys'
    $marker = Join-Path $root 'ssh-identity'
    $identity = if (Test-Path -LiteralPath $marker) { [IO.File]::ReadAllText($marker).Trim() } else { '' }
    if ($identity -ne $config.job) {
        Stop-Service -Name sshd -ErrorAction Stop
        foreach ($kind in @('rsa', 'ecdsa', 'ed25519', 'dsa')) {
            foreach ($suffix in @('', '.pub')) {
                $file = Join-Path $sshData ('ssh_host_' + $kind + '_key' + $suffix)
                Assert-RegularPath $file
                if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
            }
        }
        Run-Native $sshKeygen @('-A')
        [IO.File]::WriteAllText($marker, $config.job)
        Set-PrivateAcl $marker
    }
    # Validate before replacing the active configuration or restarting SSH.
    Run-Native $sshd @('-t', '-f', $stagedConfig)
    Move-Item -LiteralPath $stagedConfig -Destination $sshdConfig -Force
    Set-PrivateAcl $sshdConfig

    Write-Phase 'Enabling the SSH service and firewall rule'
    $ruleName = 'shepherd-OpenSSH-Server-In-TCP'
    if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
        Set-NetFirewallRule -Name $ruleName -Enabled True -Direction Inbound -Action Allow -Profile Any | Out-Null
    } else {
        New-NetFirewallRule -Name $ruleName -DisplayName 'shepherd SSH' -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow -Profile Any | Out-Null
    }
    Set-Service -Name sshd -StartupType Automatic
    Restart-Service -Name sshd
    if ((Get-Service -Name sshd).Status -ne 'Running') { throw 'OpenSSH Server did not start.' }
    Write-Phase 'Windows SSH bootstrap completed'
} catch {
    $message = $_.Exception.Message
    if ($lock) { Write-Phase ('ERROR: ' + $message) }
    [Console]::Error.WriteLine($message)
    exit 1
} finally {
    if ($lock) { $lock.Dispose() }
}
