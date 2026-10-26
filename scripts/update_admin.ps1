param(
    [string]$ProjectDirectory,
    [ValidateSet('stop', 'start')][string]$Action
)
$ErrorActionPreference = 'Stop'
. (Join-Path $ProjectDirectory 'scripts\admin.ps1')
if ($Action -eq 'stop') {
    Stop-Admin -ProjectDirectory $ProjectDirectory
} else {
    Start-Admin -ProjectDirectory $ProjectDirectory -OpenBrowser $false
}
