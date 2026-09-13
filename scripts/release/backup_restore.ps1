param([ValidateSet('dry-run','backup','restore')][string]$Action='dry-run',[string]$Target,[switch]$ConfirmIsolated)
$ErrorActionPreference='Stop'
if ($Action -ne 'dry-run' -and -not $ConfirmIsolated) { Write-Error 'backup/restore requires -ConfirmIsolated and a disposable target'; exit 2 }
if ($Action -ne 'dry-run' -and [string]::IsNullOrWhiteSpace($Target)) { Write-Error 'explicit backup target is required'; exit 2 }
Write-Output "[$Action] NOT_RUN - volume backup/restore requires an isolated operator-selected target"
exit $(if ($Action -eq 'dry-run') { 0 } else { 3 })
