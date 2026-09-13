& (Join-Path $PSScriptRoot 'verify_release_gates.ps1') -Gate media-integrity; exit $LASTEXITCODE
