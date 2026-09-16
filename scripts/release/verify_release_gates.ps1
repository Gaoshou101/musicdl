param([ValidateSet('all','compose','proxy','wecom-callback','telegram-session-isolation','media-integrity','plugin-security','admin-auth','compose-recovery')][string]$Gate='all',[switch]$DryRun,[switch]$SelfTest,[string]$RedisUrl,[switch]$ConfirmIsolated,[string]$Namespace)
$ErrorActionPreference='Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path (Split-Path -Parent (Split-Path -Parent $repo)) '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { Write-Error 'Python interpreter not found; expected repository .venv\Scripts\python.exe'; exit 2 }
$args = @((Join-Path $PSScriptRoot 'run_gates.py'),'--gate',$Gate)
if ($DryRun) { $args += '--dry-run' }; if ($SelfTest) { $args += '--self-test' }
if ($RedisUrl) { $args += @('--redis-url',$RedisUrl) }
if ($ConfirmIsolated) { $args += '--confirm-isolated' }
if ($Namespace) { $args += @('--namespace',$Namespace) }
& $python @args
exit $LASTEXITCODE
