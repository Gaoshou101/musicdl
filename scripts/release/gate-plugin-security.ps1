& (Join-Path $PSScriptRoot 'verify_release_gates.ps1') -Gate plugin-security; exit $LASTEXITCODE
