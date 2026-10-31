# Run with pwsh or Windows PowerShell; no VM or Windows configuration is modified.
$ErrorActionPreference = 'Stop'
$file = Join-Path (Split-Path -Parent $PSScriptRoot) 'mods/shepherd/windows-bootstrap.ps1'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$installer = Join-Path (Split-Path -Parent $file) 'windows-install-core.ps1'
[Management.Automation.Language.Parser]::ParseFile($installer, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count) { throw ($errors | Out-String) }

# Load only the pure configuration function, never the bootstrap entry point.
$helper = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Update-SshConfig'
}, $true)
if (-not $helper) { throw 'Missing SSH configuration helper.' }
. ([scriptblock]::Create($helper.Extent.Text))

function Assert([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

$key = 'C:\ProgramData\shepherd\authorized_keys\shepherd'
foreach ($newline in @("`n", "`r`n")) {
    $original = @('Port 22', '# Preserve this setting', 'PubkeyAuthentication yes',
        'Match Group administrators', '    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys') -join $newline
    $updated = Update-SshConfig $original 'shepherd' $key
    Assert ($updated.IndexOf('Port 22') -lt $updated.IndexOf('Match User shepherd')) 'Global directives must remain global.'
    Assert ($updated.IndexOf('Match User shepherd') -lt $updated.IndexOf('Match Group administrators')) 'User-specific keys must override the administrators rule.'
    Assert ($updated.Contains('AuthorizedKeysFile "C:/ProgramData/shepherd/authorized_keys/shepherd"')) 'The key path must be absolute and quoted.'
    Assert ($updated.Contains('PasswordAuthentication no')) 'The generated account must use key-only SSH.'
    Assert ($updated.Contains('AuthenticationMethods publickey')) 'Other authentication methods must not bypass the key.'
    Assert ($updated.EndsWith(($original.Substring($original.IndexOf('Match Group'))))) 'Existing Match rules must be preserved.'
    Assert ((Update-SshConfig $updated 'shepherd' $key) -ceq $updated) 'Retry must not duplicate or reorder SSH configuration.'

    $other = Update-SshConfig $updated 'other' 'C:\ProgramData\shepherd\authorized_keys\other'
    Assert ($other.Contains('Match User shepherd') -and $other.Contains('Match User other')) 'Keep other managed accounts intact.'
    $changedKey = Update-SshConfig $updated 'shepherd' 'D:\Keys With Spaces\shepherd'
    Assert ($changedKey.Contains('AuthorizedKeysFile "D:/Keys With Spaces/shepherd"')) 'Update an existing managed key path.'
    Assert (-not $changedKey.Contains('C:/ProgramData/shepherd/authorized_keys/shepherd')) 'Remove the obsolete managed key path.'
}

$withoutMatch = Update-SshConfig 'Port 22' 'shepherd' $key
Assert ($withoutMatch.StartsWith("Port 22`r`n# BEGIN")) 'Append a Match block after all global settings.'
Write-Output 'PowerShell checks passed: syntax, Match precedence, key-only authentication, path quoting, preservation, and retry idempotence.'
