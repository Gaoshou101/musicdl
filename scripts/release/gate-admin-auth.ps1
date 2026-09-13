& (Join-Path $PSScriptRoot 'verify_release_gates.ps1') -Gate admin-auth; exit $LASTEXITCODE
