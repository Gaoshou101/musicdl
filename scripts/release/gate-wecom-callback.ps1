& (Join-Path $PSScriptRoot 'verify_release_gates.ps1') -Gate wecom-callback; exit $LASTEXITCODE
