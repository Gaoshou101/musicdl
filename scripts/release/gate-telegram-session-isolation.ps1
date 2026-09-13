& (Join-Path $PSScriptRoot 'verify_release_gates.ps1') -Gate telegram-session-isolation; exit $LASTEXITCODE
