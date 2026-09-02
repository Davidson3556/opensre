param(
    [int]$ParentProcessId,
    [string]$CleanupPayload,
    [string]$CleanupScriptPath
)

$ErrorActionPreference = 'Stop'

function Exit-OpenSreCleanup {
    param([int]$ExitCode)

    Remove-Item -LiteralPath $CleanupScriptPath -Force -ErrorAction SilentlyContinue
    exit $ExitCode
}

trap {
    Remove-Item -LiteralPath $CleanupScriptPath -Force -ErrorAction SilentlyContinue
    exit 1
}

$payloadJson = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($CleanupPayload)
)
$payload = ConvertFrom-Json -InputObject $payloadJson

function ConvertTo-OpenSreExtendedPath {
    param([string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath.StartsWith('\\?\')) {
        return $fullPath
    }
    if ($fullPath.StartsWith('\\')) {
        return '\\?\UNC\' + $fullPath.Substring(2)
    }
    return '\\?\' + $fullPath
}

function Test-OpenSreCleanupTarget {
    param([string]$Path)

    try {
        if (Test-Path -LiteralPath $Path) {
            return $true
        }
    }
    catch {
        # Fall through to extended-length path checks.
    }
    $extendedPath = ConvertTo-OpenSreExtendedPath -Path $Path
    return (
        [System.IO.Directory]::Exists($extendedPath) -or
        [System.IO.File]::Exists($extendedPath)
    )
}

function Remove-OpenSreCleanupTarget {
    param([string]$Path)

    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        return
    }
    catch {
        $extendedPath = ConvertTo-OpenSreExtendedPath -Path $Path
        if ([System.IO.Directory]::Exists($extendedPath)) {
            [System.IO.Directory]::Delete($extendedPath, $true)
            return
        }
        if ([System.IO.File]::Exists($extendedPath)) {
            [System.IO.File]::Delete($extendedPath)
        }
    }
}

function Test-OpenSrePathContains {
    param(
        [string]$Root,
        [string]$Candidate
    )

    try {
        $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
        $candidatePath = [System.IO.Path]::GetFullPath($Candidate)
    }
    catch {
        return $false
    }
    if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidatePath.StartsWith(
        $rootPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Test-OpenSreTargetInUse {
    param(
        [string]$Path,
        [switch]$TreatAsDirectory
    )

    $targetIsDirectory = $TreatAsDirectory -or [System.IO.Directory]::Exists(
        (ConvertTo-OpenSreExtendedPath -Path $Path)
    )
    try {
        $processes = @(
            Get-Process -ErrorAction Stop |
                Where-Object { $_.ProcessName -ieq 'opensre' }
        )
    }
    catch {
        return $true
    }
    foreach ($process in $processes) {
        try {
            $processPath = [string]$process.Path
        }
        catch {
            return $true
        }
        if (-not $processPath) {
            return $true
        }
        if ($targetIsDirectory) {
            if (Test-OpenSrePathContains -Root $Path -Candidate $processPath) {
                return $true
            }
        }
        else {
            try {
                $targetPath = [System.IO.Path]::GetFullPath($Path)
                $runningPath = [System.IO.Path]::GetFullPath($processPath)
            }
            catch {
                return $true
            }
            if ($runningPath.Equals($targetPath, [System.StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }
    return $false
}

function Move-OpenSreTargetIfUnused {
    param([string]$Path)

    if (-not (Test-OpenSreCleanupTarget -Path $Path)) {
        return ''
    }
    if (Test-OpenSreTargetInUse -Path $Path) {
        throw "OpenSRE cleanup target is still in use: $Path"
    }

    $guard = $null
    $targetWasDirectory = [System.IO.Directory]::Exists(
        (ConvertTo-OpenSreExtendedPath -Path $Path)
    )
    try {
        $guardPath = if ($targetWasDirectory) {
            Join-Path $Path 'opensre.exe'
        }
        else {
            $Path
        }
        if ([System.IO.File]::Exists((ConvertTo-OpenSreExtendedPath -Path $guardPath))) {
            $guard = [System.IO.File]::Open(
                $guardPath,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Delete
            )
        }
        if (Test-OpenSreTargetInUse -Path $Path) {
            throw "OpenSRE cleanup target became busy: $Path"
        }
        if ($null -ne $guard) {
            $guard.Dispose()
            $guard = $null
        }
        $retiredPath = "$Path.uninstall-$([System.Guid]::NewGuid().ToString('N'))"
        Move-Item -LiteralPath $Path -Destination $retiredPath -ErrorAction Stop
        if ((Test-OpenSreTargetInUse -Path $Path -TreatAsDirectory:$targetWasDirectory) -or
            (Test-OpenSreTargetInUse -Path $retiredPath -TreatAsDirectory:$targetWasDirectory)) {
            Move-Item -LiteralPath $retiredPath -Destination $Path -ErrorAction Stop
            throw "OpenSRE cleanup target became busy during retirement: $Path"
        }
        return $retiredPath
    }
    catch {
        throw
    }
    finally {
        if ($null -ne $guard) {
            $guard.Dispose()
        }
    }
}

function Test-OpenSreManagedLauncher {
    param([string]$Path)

    try {
        $lines = @(Get-Content -LiteralPath $Path)
        return (
            $lines.Count -ge 2 -and
            $lines[0].Trim() -ieq '@echo off' -and
            $lines[1].Trim() -ceq ':: OpenSRE Windows launcher v1'
        )
    }
    catch {
        return $false
    }
}

for ($waitAttempt = 0; $waitAttempt -lt 2400; $waitAttempt++) {
    $parent = Get-Process -Id $parentProcessId -ErrorAction SilentlyContinue
    if ($null -eq $parent) {
        break
    }
    if ($waitAttempt -eq 2399) {
        Exit-OpenSreCleanup -ExitCode 1
    }
    Start-Sleep -Milliseconds 250
}

$managed = $payload.managed
$retiredTargets = @()
$deleteLockPath = ''
$deleteData = $null -eq $managed
$lockHandle = $null
$cleanupLockPath = ''
if ($null -ne $managed) {
    $cleanupLockPath = [string]$managed.lock_path
}
elseif ($payload.lock_path) {
    $cleanupLockPath = [string]$payload.lock_path
}
if ($cleanupLockPath) {
    $lockDeadline = [System.DateTime]::UtcNow.AddSeconds(30)
    while ($null -eq $lockHandle -and [System.DateTime]::UtcNow -lt $lockDeadline) {
        try {
            $lockHandle = [System.IO.File]::Open(
                $cleanupLockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if ($null -eq $lockHandle) {
        Exit-OpenSreCleanup -ExitCode 1
    }
}

if ($null -ne $managed) {
    $movedLauncher = ''
    $versionGuards = @()
    try {
        $appRoot = [string]$managed.app_root
        $pointerPath = Join-Path $appRoot 'current.txt'
        $currentInstallId = ''
        if (Test-Path -LiteralPath $pointerPath -PathType Leaf) {
            $currentInstallId = ([string](Get-Content -LiteralPath $pointerPath -Raw)).Trim()
        }

        if ($currentInstallId -ne [string]$managed.expected_install_id) {
            $currentVersionPath = Join-Path (Join-Path $appRoot 'versions') $currentInstallId
            $currentExecutable = Join-Path $currentVersionPath 'opensre.exe'
            if (-not $currentInstallId -or
                -not (Test-Path -LiteralPath $currentExecutable -PathType Leaf)) {
                Exit-OpenSreCleanup -ExitCode 1
            }
            $retiredVersion = Move-OpenSreTargetIfUnused -Path ([string]$managed.active_version)
            if ($retiredVersion) {
                $retiredTargets += $retiredVersion
            }
        }
        else {
            $markerPath = Join-Path $appRoot 'layout-v1.marker'
            $markerText = ''
            if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
                $markerText = ([string](Get-Content -LiteralPath $markerPath -Raw)).Trim()
            }
            if ($markerText -cne 'OpenSRE Windows bundle layout v1') {
                Exit-OpenSreCleanup -ExitCode 1
            }

            $launcher = [string]$managed.launcher
            if ($launcher -and (Test-Path -LiteralPath $launcher -PathType Leaf)) {
                if (-not (Test-OpenSreManagedLauncher -Path $launcher)) {
                    Exit-OpenSreCleanup -ExitCode 1
                }
                $movedLauncher = "$launcher.uninstall-$([System.Guid]::NewGuid().ToString('N'))"
                Move-Item -LiteralPath $launcher -Destination $movedLauncher -ErrorAction Stop
            }

            if (Test-OpenSreTargetInUse -Path $appRoot) {
                throw 'OpenSRE bundle is still in use.'
            }
            $versionsRoot = Join-Path $appRoot 'versions'
            if (Test-Path -LiteralPath $versionsRoot -PathType Container) {
                foreach ($versionDirectory in @(Get-ChildItem -LiteralPath $versionsRoot -Directory -Force)) {
                    $versionExecutable = Join-Path $versionDirectory.FullName 'opensre.exe'
                    if (Test-Path -LiteralPath $versionExecutable -PathType Leaf) {
                        $versionGuards += [System.IO.File]::Open(
                            $versionExecutable,
                            [System.IO.FileMode]::Open,
                            [System.IO.FileAccess]::Read,
                            [System.IO.FileShare]::Delete
                        )
                    }
                }
            }
            if (Test-OpenSreTargetInUse -Path $appRoot) {
                throw 'OpenSRE bundle became busy during uninstall.'
            }
            foreach ($guard in $versionGuards) {
                $guard.Dispose()
            }
            $versionGuards = @()

            $movedAppRoot = "$appRoot.uninstall-$([System.Guid]::NewGuid().ToString('N'))"
            Move-Item -LiteralPath $appRoot -Destination $movedAppRoot -ErrorAction Stop
            if ((Test-OpenSreTargetInUse -Path $appRoot -TreatAsDirectory) -or
                (Test-OpenSreTargetInUse -Path $movedAppRoot -TreatAsDirectory)) {
                try {
                    Move-Item -LiteralPath $movedAppRoot -Destination $appRoot -ErrorAction Stop
                }
                catch {
                    $movedLauncher = ''
                    throw 'OpenSRE bundle retirement could not be rolled back safely.'
                }
                throw 'OpenSRE bundle became busy during retirement.'
            }
            $retiredTargets += $movedAppRoot
            $deleteData = $true
            if ($movedLauncher) {
                $retiredTargets += $movedLauncher
                $movedLauncher = ''
            }
            $deleteLockPath = [string]$managed.lock_path
        }

        foreach ($targetValue in @($payload.targets)) {
            $retiredTarget = Move-OpenSreTargetIfUnused -Path ([string]$targetValue)
            if ($retiredTarget) {
                $retiredTargets += $retiredTarget
            }
        }
    }
    catch {
        if ($movedLauncher -and
            (Test-Path -LiteralPath ([string]$managed.app_root) -PathType Container) -and
            (Test-Path -LiteralPath $movedLauncher -PathType Leaf) -and
            -not (Test-Path -LiteralPath ([string]$managed.launcher))) {
            Move-Item `
                -LiteralPath $movedLauncher `
                -Destination ([string]$managed.launcher) `
                -ErrorAction Stop
        }
        Exit-OpenSreCleanup -ExitCode 1
    }
    finally {
        foreach ($guard in $versionGuards) {
            $guard.Dispose()
        }
    }
}
else {
    foreach ($targetValue in @($payload.targets)) {
        $retiredTarget = Move-OpenSreTargetIfUnused -Path ([string]$targetValue)
        if ($retiredTarget) {
            $retiredTargets += $retiredTarget
        }
    }
}

$failed = $false
foreach ($target in $retiredTargets) {
    $removed = $false
    for ($removeAttempt = 0; $removeAttempt -lt 150; $removeAttempt++) {
        if (-not (Test-OpenSreCleanupTarget -Path $target)) {
            $removed = $true
            break
        }
        try {
            Remove-OpenSreCleanupTarget -Path $target
            if (-not (Test-OpenSreCleanupTarget -Path $target)) {
                $removed = $true
                break
            }
        }
        catch {
            # Retried only after the live path has been atomically retired.
        }
        Start-Sleep -Milliseconds 200
    }
    if (-not $removed) {
        $failed = $true
    }
}

if ($failed) {
    if ($null -ne $lockHandle) {
        $lockHandle.Dispose()
        $lockHandle = $null
    }
    Exit-OpenSreCleanup -ExitCode 1
}

# User data is the final phase of the transaction.  A refusal or failure while
# retiring/removing the executable installation must leave it untouched so an
# otherwise working installation never loses its configuration.
if ($deleteData) {
    foreach ($guardPathValue in @($payload.data_guard_paths)) {
        if (Test-OpenSreCleanupTarget -Path ([string]$guardPathValue)) {
            $deleteData = $false
            break
        }
    }
}
$dataFailed = $false
if ($deleteData) {
    foreach ($dataTargetValue in @($payload.data_targets)) {
        $dataTarget = [string]$dataTargetValue
        if (-not (Test-OpenSreCleanupTarget -Path $dataTarget)) {
            continue
        }
        try {
            Remove-OpenSreCleanupTarget -Path $dataTarget
            if (Test-OpenSreCleanupTarget -Path $dataTarget) {
                $dataFailed = $true
            }
        }
        catch {
            $dataFailed = $true
        }
    }
    if (-not $deleteLockPath -and $cleanupLockPath) {
        $deleteLockPath = $cleanupLockPath
    }
}
if ($null -ne $lockHandle) {
    $lockHandle.Dispose()
    $lockHandle = $null
}
if ($deleteLockPath -and
    ($null -eq $managed -or -not (Test-Path -LiteralPath ([string]$managed.app_root)))) {
    Remove-Item -LiteralPath $deleteLockPath -Force -ErrorAction SilentlyContinue
}
if ($dataFailed) {
    Exit-OpenSreCleanup -ExitCode 1
}
Exit-OpenSreCleanup -ExitCode 0
