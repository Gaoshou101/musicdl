<#
.SYNOPSIS
    Volume backup/restore drill for the three-container Compose deployment.

.DESCRIPTION
    The roadmap requires a Compose restart/backup-restore drill before 1.0. This
    script performs that drill against an operator-selected, disposable target.
    It never stops or starts a running deployment, and it never reads, writes,
    or logs credentials. Before backup or restore it checks, read-only, that no
    running container uses any target volume. Quiesce the app, Redis, and every
    other container using those volumes first.

    Actions:
      dry-run  Resolve and print the exact plan for every volume. Always exits 0.
      backup   Archive every declared volume into the target directory.
      restore  Extract the archives from the target directory into the volumes.

    backup and restore both require -ConfirmIsolated and an explicit -Target,
    because the operator must assert that the target is disposable.

    Archives are named after the logical volume and replace any previous file of
    the same name in that target. Keep the target dedicated to this drill.

    The helper image only needs a POSIX tar. The default project name is `musicdl`,
    matching compose.yaml. Pass -ProjectName to match an override used at deploy
    time (`docker compose -p`). The drill includes the bundled Redis data volume.

.NOTES
    Exit codes: 0 completed, 1 failed, 2 usage error or unsafe target,
    3 prerequisite unavailable (docker CLI, or a missing restore archive).
#>
param(
    [ValidateSet('dry-run','backup','restore')][string]$Action = 'dry-run',
    [string]$Target,
    [string]$ProjectName,
    [string]$HelperImage = 'alpine:3.20',
    [switch]$ConfirmIsolated
)

$ErrorActionPreference = 'Stop'

$volumes = @(
    [pscustomobject]@{ Logical = 'musicdl-media';    Mount = '/data/music' }
    [pscustomobject]@{ Logical = 'musicdl-app-data'; Mount = '/data/app' }
    [pscustomobject]@{ Logical = 'musicdl-telegram'; Mount = '/data/telegram-sessions' }
    [pscustomobject]@{ Logical = 'musicdl-redis-data'; Mount = '/data' }
)

function Write-Line([string]$Message) { Write-Output "[$Action] $Message" }
function Stop-With([int]$Code, [string]$Message) { Write-Line $Message; exit $Code }
function Resolve-Volume([string]$Logical) {
    $resolvedProjectName = if ([string]::IsNullOrWhiteSpace($ProjectName)) { 'musicdl' } else { $ProjectName }
    return "${resolvedProjectName}_$Logical"
}

if ($Action -ne 'dry-run' -and -not $ConfirmIsolated) {
    Stop-With 2 'FAIL - backup/restore requires -ConfirmIsolated and a disposable target'
}
if ($Action -ne 'dry-run' -and [string]::IsNullOrWhiteSpace($Target)) {
    Stop-With 2 'FAIL - explicit backup target is required'
}

$targetFull = $null
if (-not [string]::IsNullOrWhiteSpace($Target)) {
    $targetFull = [System.IO.Path]::GetFullPath($Target)
    $targetRoot = [System.IO.Path]::GetPathRoot($targetFull)
    if ($targetFull.TrimEnd('\', '/') -eq $targetRoot.TrimEnd('\', '/')) {
        Stop-With 2 "FAIL - refusing to use a filesystem root as the target: $targetFull"
    }
}

if ($Action -eq 'backup') {
    if ((Test-Path -LiteralPath $targetFull) -and -not (Test-Path -LiteralPath $targetFull -PathType Container)) {
        Stop-With 2 "FAIL - backup target is not a directory: $targetFull"
    }
}
if ($Action -eq 'restore' -and -not (Test-Path -LiteralPath $targetFull -PathType Container)) {
    Stop-With 2 "FAIL - restore target directory does not exist: $targetFull"
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
$dockerAvailable = $null -ne $docker
$targetDisplay = if ($targetFull) { $targetFull } else { '<unset>' }

Write-Line "plan - helper image '$HelperImage', target '$targetDisplay'"
foreach ($volume in $volumes) {
    Write-Line "plan - volume $(Resolve-Volume $volume.Logical) holds $($volume.Mount); archive $($volume.Logical).tar.gz"
}

if ($Action -eq 'dry-run') {
    foreach ($volume in $volumes) {
        $name = Resolve-Volume $volume.Logical
        Write-Line "plan - backup:  docker run --rm --mount type=volume,source=$name,target=/source,readonly --mount type=bind,source=<target>,target=/backup $HelperImage tar czf /backup/$($volume.Logical).tar.gz -C /source ."
        Write-Line "plan - restore: docker run --rm --mount type=volume,source=$name,target=/target --mount type=bind,source=<target>,target=/backup,readonly $HelperImage tar xzf /backup/$($volume.Logical).tar.gz -C /target"
    }
    Write-Line "PASS - plan resolved; docker $(if ($dockerAvailable) { 'is available' } else { 'was not found on PATH' })"
    exit 0
}

if (-not $targetFull) { Stop-With 2 'FAIL - explicit backup target is required' }
if ($Action -eq 'restore') {
    foreach ($volume in $volumes) {
        $name = Resolve-Volume $volume.Logical
        $archivePath = Join-Path $targetFull "$($volume.Logical).tar.gz"
        if (-not (Test-Path -LiteralPath $archivePath)) { Stop-With 3 "FAIL - restore archive is missing for $name`: $archivePath" }
    }
}
if (-not $dockerAvailable) { Stop-With 3 'FAIL - docker CLI not found on PATH; the drill cannot run here' }

if ($Action -in @('backup','restore')) {
    $volumesInUse = @()
    foreach ($volume in $volumes) {
        $name = Resolve-Volume $volume.Logical
        $runningContainers = & docker ps --filter "volume=$name" --format '{{.ID}}'
        if ($LASTEXITCODE -ne 0) {
            Stop-With 3 "FAIL - could not verify whether a running container uses $name; stop musicdl, Redis, and all containers using the target volumes, then retry"
        }
        if ($runningContainers) {
            $volumesInUse += $name
        }
    }
    if ($volumesInUse.Count -gt 0) {
        Stop-With 2 "FAIL - running container(s) use target volume(s): $($volumesInUse -join ', '); stop musicdl, Redis, and all containers using the target volumes before backup or restore"
    }
}

if ($Action -eq 'backup') {
    if (-not (Test-Path -LiteralPath $targetFull)) { New-Item -ItemType Directory -Path $targetFull | Out-Null }
    foreach ($volume in $volumes) {
        $name = Resolve-Volume $volume.Logical
        $archivePath = Join-Path $targetFull "$($volume.Logical).tar.gz"
        $arguments = @('run','--rm',
            '--mount',"type=volume,source=$name,target=/source,readonly",
            '--mount',"type=bind,source=$targetFull,target=/backup",
            $HelperImage,'tar','czf',"/backup/$($volume.Logical).tar.gz",'-C','/source','.')
        & docker @arguments
        if ($LASTEXITCODE -ne 0) { Stop-With 1 "FAIL - archiving $name failed (docker exit $LASTEXITCODE)" }
        if (-not (Test-Path -LiteralPath $archivePath)) { Stop-With 1 "FAIL - archive for $name was not created: $archivePath" }
        $bytes = (Get-Item -LiteralPath $archivePath).Length
        if ($bytes -eq 0) { Stop-With 1 "FAIL - archive for $name is empty: $archivePath" }
        Write-Line "OK - $name -> $archivePath ($bytes bytes)"
    }
    Write-Line "PASS - $($volumes.Count) volumes archived into $targetFull"
    exit 0
}

foreach ($volume in $volumes) {
    $name = Resolve-Volume $volume.Logical
    $archivePath = Join-Path $targetFull "$($volume.Logical).tar.gz"
    $arguments = @('run','--rm',
        '--mount',"type=volume,source=$name,target=/target",
        '--mount',"type=bind,source=$targetFull,target=/backup,readonly",
        $HelperImage,'tar','xzf',"/backup/$($volume.Logical).tar.gz",'-C','/target')
    & docker @arguments
    if ($LASTEXITCODE -ne 0) { Stop-With 1 "FAIL - restoring $name failed (docker exit $LASTEXITCODE)" }
    Write-Line "OK - $name restored from $archivePath"
}
Write-Line "PASS - $($volumes.Count) volumes restored from $targetFull"
exit 0
