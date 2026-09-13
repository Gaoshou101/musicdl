& (Join-Path $PSScriptRoot 'verify_release_gates.ps1') -Gate compose-recovery; exit $LASTEXITCODE
