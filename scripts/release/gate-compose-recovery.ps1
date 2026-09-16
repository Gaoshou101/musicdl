param([string]$RedisUrl,[switch]$ConfirmIsolated,[string]$Namespace)
& (Join-Path $PSScriptRoot 'verify_release_gates.ps1') -Gate compose-recovery -RedisUrl $RedisUrl -ConfirmIsolated:$ConfirmIsolated -Namespace $Namespace; exit $LASTEXITCODE
