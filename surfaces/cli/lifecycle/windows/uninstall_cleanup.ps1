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

if ($PSVersionTable.PSEdition -cne 'Desktop' -or
    [int]$PSVersionTable.PSVersion.Major -ne 5 -or
    [int]$PSVersionTable.PSVersion.Minor -lt 1) {
    Exit-OpenSreCleanup -ExitCode 1
}

$payloadJson = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($CleanupPayload)
)
$payload = ConvertFrom-Json -InputObject $payloadJson
if (-not $payload.operation_id -or
    $null -eq $payload.parent -or
    [int]$payload.parent.pid -ne $ParentProcessId -or
    -not [string]$payload.parent.path -or
    [int64]$payload.parent.started_filetime_utc -le 0) {
    Exit-OpenSreCleanup -ExitCode 1
}

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

function Initialize-OpenSreCleanupNativePathApi {
    if (([System.Management.Automation.PSTypeName]'OpenSre.CleanupNativePathApi').Type) {
        return
    }

    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace OpenSre
{
    public sealed class CleanupPathIdentity
    {
        public string FinalPath { get; private set; }
        public uint VolumeSerialNumber { get; private set; }
        public ulong FileIndex { get; private set; }
        public long CreationFileTimeUtc { get; private set; }

        public CleanupPathIdentity(
            string finalPath,
            uint volumeSerialNumber,
            ulong fileIndex,
            long creationFileTimeUtc
        )
        {
            FinalPath = finalPath;
            VolumeSerialNumber = volumeSerialNumber;
            FileIndex = fileIndex;
            CreationFileTimeUtc = creationFileTimeUtc;
        }
    }

    public static class CleanupNativePathApi
    {
        private const uint FileShareRead = 0x00000001;
        private const uint FileShareWrite = 0x00000002;
        private const uint FileShareDelete = 0x00000004;
        private const uint OpenExisting = 3;
        private const uint FileFlagBackupSemantics = 0x02000000;
        private const uint InvalidFileAttributes = 0xFFFFFFFF;
        private const int ErrorFileNotFound = 2;
        private const int ErrorPathNotFound = 3;

        [StructLayout(LayoutKind.Sequential)]
        private struct NativeFileTime
        {
            public uint LowDateTime;
            public uint HighDateTime;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ByHandleFileInformation
        {
            public uint FileAttributes;
            public NativeFileTime CreationTime;
            public NativeFileTime LastAccessTime;
            public NativeFileTime LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(
            string fileName,
            uint desiredAccess,
            uint shareMode,
            IntPtr securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile
        );

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(
            SafeFileHandle file,
            StringBuilder path,
            uint pathLength,
            uint flags
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle file,
            out ByHandleFileInformation information
        );

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFileAttributes(string fileName);

        public static bool ExistsNoFollow(string path)
        {
            uint attributes = GetFileAttributes(path);
            if (attributes != InvalidFileAttributes)
            {
                return true;
            }
            int error = Marshal.GetLastWin32Error();
            if (error == ErrorFileNotFound || error == ErrorPathNotFound)
            {
                return false;
            }
            throw new Win32Exception(error);
        }

        public static CleanupPathIdentity GetIdentity(string path)
        {
            using (SafeFileHandle handle = CreateFile(
                path,
                0,
                FileShareRead | FileShareWrite | FileShareDelete,
                IntPtr.Zero,
                OpenExisting,
                FileFlagBackupSemantics,
                IntPtr.Zero
            ))
            {
                if (handle.IsInvalid)
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }

                uint capacity = 512;
                string finalPath;
                while (true)
                {
                    StringBuilder value = new StringBuilder((int)capacity);
                    uint length = GetFinalPathNameByHandle(handle, value, capacity, 0);
                    if (length == 0)
                    {
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    if (length < capacity)
                    {
                        finalPath = value.ToString();
                        break;
                    }
                    capacity = length + 1;
                }

                ByHandleFileInformation information;
                if (!GetFileInformationByHandle(handle, out information))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                ulong fileIndex = ((ulong)information.FileIndexHigh << 32)
                    | information.FileIndexLow;
                ulong creationFileTime = ((ulong)information.CreationTime.HighDateTime << 32)
                    | information.CreationTime.LowDateTime;
                return new CleanupPathIdentity(
                    finalPath,
                    information.VolumeSerialNumber,
                    fileIndex,
                    checked((long)creationFileTime)
                );
            }
        }
    }
}
'@
}

function Get-OpenSreExistingPathIdentity {
    param([string]$Path)

    Initialize-OpenSreCleanupNativePathApi
    $identity = [OpenSre.CleanupNativePathApi]::GetIdentity(
        (ConvertTo-OpenSreExtendedPath -Path $Path)
    )
    $canonicalPath = ([string]$identity.FinalPath).TrimEnd('\', '/')
    if ($canonicalPath.StartsWith('\\?\UNC\')) {
        $canonicalPath = '\\' + $canonicalPath.Substring(8)
    }
    elseif ($canonicalPath.StartsWith('\\?\')) {
        $canonicalPath = $canonicalPath.Substring(4)
    }
    return [pscustomobject]@{
        Path = $canonicalPath
        VolumeSerialNumber = [uint32]$identity.VolumeSerialNumber
        FileIndex = [uint64]$identity.FileIndex
        CreationFileTimeUtc = [int64]$identity.CreationFileTimeUtc
    }
}

function Get-OpenSreCanonicalPath {
    param([string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if (-not $fullPath) {
        throw 'OpenSRE cleanup path is empty.'
    }
    $existingPath = $fullPath
    $missingSegments = New-Object 'System.Collections.Generic.List[string]'
    while (-not (Test-OpenSreCleanupTarget -Path $existingPath)) {
        $leaf = [System.IO.Path]::GetFileName($existingPath)
        $parent = [System.IO.Path]::GetDirectoryName($existingPath)
        if (-not $leaf -or -not $parent -or $parent -eq $existingPath) {
            throw "Could not resolve an existing cleanup-path ancestor: $Path"
        }
        $missingSegments.Insert(0, $leaf)
        $existingPath = $parent
    }
    $canonicalPath = [string](
        Get-OpenSreExistingPathIdentity -Path $existingPath
    ).Path
    foreach ($segment in $missingSegments) {
        $canonicalPath = [System.IO.Path]::Combine($canonicalPath, $segment)
    }
    return $canonicalPath.TrimEnd('\', '/')
}

function Test-OpenSreSameExistingFile {
    param(
        [string]$Left,
        [string]$Right
    )

    $leftIdentity = Get-OpenSreExistingPathIdentity -Path $Left
    $rightIdentity = Get-OpenSreExistingPathIdentity -Path $Right
    return (
        ([string]$leftIdentity.Path).Equals(
            [string]$rightIdentity.Path,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        [uint32]$leftIdentity.VolumeSerialNumber -eq [uint32]$rightIdentity.VolumeSerialNumber -and
        [uint64]$leftIdentity.FileIndex -eq [uint64]$rightIdentity.FileIndex
    )
}

function Test-OpenSreCleanupTarget {
    param([string]$Path)

    Initialize-OpenSreCleanupNativePathApi
    return [OpenSre.CleanupNativePathApi]::ExistsNoFollow(
        (ConvertTo-OpenSreExtendedPath -Path $Path)
    )
}

function Test-OpenSreReparsePoint {
    param([string]$Path)

    $attributes = [System.IO.File]::GetAttributes(
        (ConvertTo-OpenSreExtendedPath -Path $Path)
    )
    return [bool](
        $attributes -band [System.IO.FileAttributes]::ReparsePoint
    )
}

function Assert-OpenSreSafeAncestorChain {
    param([string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not [System.IO.Path]::IsPathRooted($fullPath)) {
        throw "OpenSRE cleanup path is not absolute: $Path"
    }
    $ancestors = New-Object 'System.Collections.Generic.Stack[string]'
    $current = $fullPath
    while ($current) {
        $ancestors.Push($current)
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent -or
            $parent.FullName.Equals($current, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $current = $parent.FullName
    }
    while ($ancestors.Count -gt 0) {
        $ancestor = $ancestors.Pop()
        if ((Test-OpenSreCleanupTarget -Path $ancestor) -and
            (Test-OpenSreReparsePoint -Path $ancestor)) {
            throw "OpenSRE cleanup refuses a reparse-point ancestor: $ancestor"
        }
    }
}

function Assert-OpenSreSafeTree {
    param([string]$Path)

    if (-not (Test-OpenSreCleanupTarget -Path $Path)) {
        return
    }
    Assert-OpenSreSafeAncestorChain -Path $Path
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push((ConvertTo-OpenSreExtendedPath -Path $Path))
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        $attributes = [System.IO.File]::GetAttributes($current)
        if ($attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "OpenSRE cleanup refuses a reparse point: $current"
        }
        if ($attributes -band [System.IO.FileAttributes]::Directory) {
            foreach ($entry in [System.IO.Directory]::EnumerateFileSystemEntries($current)) {
                $pending.Push($entry)
            }
        }
    }
}

function Test-OpenSreExpectedTarget {
    param([psobject]$Target)

    $path = [string]$Target.path
    $kind = [string]$Target.kind
    if (-not $path -or $kind -notin @('missing', 'file', 'directory')) {
        throw 'OpenSRE cleanup target metadata is invalid.'
    }
    $exists = Test-OpenSreCleanupTarget -Path $path
    if ($kind -ceq 'missing') {
        if ($exists) {
            throw "OpenSRE cleanup target was replaced after scheduling: $path"
        }
        return $false
    }
    if (-not $exists) {
        return $false
    }
    Assert-OpenSreSafeTree -Path $path
    $extendedPath = ConvertTo-OpenSreExtendedPath -Path $path
    $attributes = [System.IO.File]::GetAttributes($extendedPath)
    $isDirectory = [bool](
        $attributes -band [System.IO.FileAttributes]::Directory
    )
    if (($kind -ceq 'directory') -ne $isDirectory) {
        throw "OpenSRE cleanup target type changed after scheduling: $path"
    }
    if ($kind -ceq 'file') {
        $expectedHash = [string]$Target.sha256
        if ($expectedHash -notmatch '\A[0-9a-f]{64}\z' -or
            $null -eq $Target.PSObject.Properties['volume_serial_number'] -or
            $null -eq $Target.PSObject.Properties['file_index'] -or
            $null -eq $Target.PSObject.Properties['creation_filetime_utc']) {
            throw 'OpenSRE cleanup target identity is invalid.'
        }
        $expectedVolumeSerial = [uint32]$Target.volume_serial_number
        $expectedFileIndex = [uint64]$Target.file_index
        $expectedCreationFileTime = [int64]$Target.creation_filetime_utc
        if ($expectedFileIndex -eq 0 -or $expectedCreationFileTime -le 0) {
            throw 'OpenSRE cleanup target identity is invalid.'
        }
        $actualIdentity = Get-OpenSreExistingPathIdentity -Path $path
        if ([uint32]$actualIdentity.VolumeSerialNumber -ne $expectedVolumeSerial -or
            [uint64]$actualIdentity.FileIndex -ne $expectedFileIndex -or
            [int64]$actualIdentity.CreationFileTimeUtc -ne $expectedCreationFileTime) {
            throw "OpenSRE cleanup target was replaced after scheduling: $path"
        }
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -cne $expectedHash) {
            throw "OpenSRE cleanup target changed after scheduling: $path"
        }
    }
    return $true
}

function Close-OpenSreOwnedProcess {
    param([object]$Process)

    try {
        if ($Process -is [System.IDisposable]) {
            $Process.Dispose()
        }
    }
    catch {
        # Releasing a local handle must not override the safety classification.
    }
}

function Get-OpenSreParentIdentityState {
    $parent = $null
    try {
        $parent = Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue
        if ($null -eq $parent) {
            return 'exited'
        }
        try {
            $parentPath = [string]$parent.Path
            $parentStarted = [int64]$parent.StartTime.ToUniversalTime().ToFileTimeUtc()
            $sameExecutable = Test-OpenSreSameExistingFile `
                -Left $parentPath `
                -Right ([string]$payload.parent.path)
        }
        catch {
            # The parent can exit after enumeration but before its metadata is read.
            try {
                $hasExited = $parent.HasExited
                if ($hasExited -is [bool] -and $hasExited -eq $true) {
                    return 'exited'
                }
            }
            catch {
                # An inspection error alone is not evidence that the parent exited.
            }
            return 'unknown'
        }
        if (-not $parentPath) {
            return 'unknown'
        }
        if (-not $sameExecutable -or
            $parentStarted -ne [int64]$payload.parent.started_filetime_utc) {
            # The scheduled parent exited and Windows reused its PID.
            return 'exited'
        }
        return 'running'
    }
    finally {
        Close-OpenSreOwnedProcess -Process $parent
    }
}

function Remove-OpenSreCleanupTarget {
    param([string]$Path)

    Assert-OpenSreSafeAncestorChain -Path $Path
    Assert-OpenSreSafeTree -Path $Path
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

    $rootPath = Get-OpenSreCanonicalPath -Path $Root
    $candidatePath = [string](
        Get-OpenSreExistingPathIdentity -Path $Candidate
    ).Path
    if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidatePath.StartsWith(
        $rootPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Get-OpenSreTargetUseState {
    param(
        [string]$Path,
        [switch]$TreatAsDirectory
    )

    $targetIsDirectory = $TreatAsDirectory -or [System.IO.Directory]::Exists(
        (ConvertTo-OpenSreExtendedPath -Path $Path)
    )
    $processes = New-Object 'System.Collections.Generic.List[object]'
    try {
        try {
            Get-Process -ErrorAction Stop | ForEach-Object { $processes.Add($_) }
        }
        catch {
            return 'unknown'
        }
        foreach ($process in $processes) {
            try {
                if ($process.ProcessName -ine 'opensre') {
                    continue
                }
                $processPath = [string]$process.Path
            }
            catch {
                return 'unknown'
            }
            if (-not $processPath) {
                return 'unknown'
            }
            if ($targetIsDirectory) {
                if (Test-OpenSrePathContains -Root $Path -Candidate $processPath) {
                    return 'busy'
                }
            }
            else {
                try {
                    if (Test-OpenSreCleanupTarget -Path $Path) {
                        $sameFile = Test-OpenSreSameExistingFile `
                            -Left $Path `
                            -Right $processPath
                    }
                    else {
                        $targetPath = Get-OpenSreCanonicalPath -Path $Path
                        $runningPath = [string](
                            Get-OpenSreExistingPathIdentity -Path $processPath
                        ).Path
                        $sameFile = $runningPath.Equals(
                            $targetPath,
                            [System.StringComparison]::OrdinalIgnoreCase
                        )
                    }
                }
                catch {
                    return 'unknown'
                }
                if ($sameFile) {
                    return 'busy'
                }
            }
        }
        return 'safe'
    }
    finally {
        foreach ($process in $processes) {
            Close-OpenSreOwnedProcess -Process $process
        }
    }
}

function Test-OpenSreTargetInUse {
    param(
        [string]$Path,
        [switch]$TreatAsDirectory
    )

    # Incomplete enumeration is neither proof of use nor permission to delete.
    # Rescans share the lock deadline; successive targets cannot reset the budget.
    do {
        try {
            $state = Get-OpenSreTargetUseState -Path $Path -TreatAsDirectory:$TreatAsDirectory
        }
        catch {
            $state = 'unknown'
        }
        if ($state -ceq 'safe') { return $false }
        if ($state -ceq 'busy') { return $true }
        if ([System.DateTime]::UtcNow -ge $lockDeadline) { return $true }
        Start-Sleep -Milliseconds 250
    } while ([System.DateTime]::UtcNow -lt $lockDeadline)
    return $true
}

function Move-OpenSreTargetIfUnused {
    param([string]$Path)

    if (-not (Test-OpenSreCleanupTarget -Path $Path)) {
        return ''
    }
    Assert-OpenSreSafeAncestorChain -Path $Path
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
        Assert-OpenSreSafeAncestorChain -Path $Path
        Assert-OpenSreSafeAncestorChain -Path $retiredPath
        Move-Item -LiteralPath $Path -Destination $retiredPath -ErrorAction Stop
        if ((Test-OpenSreTargetInUse -Path $Path -TreatAsDirectory:$targetWasDirectory) -or
            (Test-OpenSreTargetInUse -Path $retiredPath -TreatAsDirectory:$targetWasDirectory)) {
            Assert-OpenSreSafeAncestorChain -Path $retiredPath
            Assert-OpenSreSafeAncestorChain -Path $Path
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
    $parentState = Get-OpenSreParentIdentityState
    if ($parentState -ceq 'exited') {
        break
    }
    if ($parentState -cne 'running') {
        Exit-OpenSreCleanup -ExitCode 1
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
$lockDeadline = [System.DateTime]::UtcNow.AddSeconds(30)
if ($null -ne $managed) {
    if ([string]$managed.layout_marker_text -cne 'OpenSRE Windows bundle layout v1') {
        Exit-OpenSreCleanup -ExitCode 1
    }
    $cleanupLockPath = [string]$managed.lock_path
}
elseif ($payload.lock_path) {
    $cleanupLockPath = [string]$payload.lock_path
}
if ($cleanupLockPath) {
    try {
        if ((Test-OpenSreCleanupTarget -Path $cleanupLockPath) -and
            (Test-OpenSreReparsePoint -Path $cleanupLockPath)) {
            Exit-OpenSreCleanup -ExitCode 1
        }
    }
    catch {
        Exit-OpenSreCleanup -ExitCode 1
    }
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
    try {
        Assert-OpenSreSafeAncestorChain -Path $cleanupLockPath
        if (Test-OpenSreReparsePoint -Path $cleanupLockPath) {
            $lockHandle.Dispose()
            $lockHandle = $null
            Exit-OpenSreCleanup -ExitCode 1
        }
    }
    catch {
        if ($null -ne $lockHandle) {
            $lockHandle.Dispose()
            $lockHandle = $null
        }
        Exit-OpenSreCleanup -ExitCode 1
    }
}

if ($null -ne $managed) {
    $movedLauncher = ''
    $versionGuards = @()
    try {
        $appRoot = [string]$managed.app_root
        $expectedInstallId = [string]$managed.expected_install_id
        $activeVersion = [string]$managed.active_version
        $activeExecutable = Join-Path $activeVersion 'opensre.exe'
        $versionsRoot = Join-Path $appRoot 'versions'
        $expectedActiveVersion = Join-Path $versionsRoot $expectedInstallId
        $expectedLockPath = Join-Path (Split-Path -Parent $appRoot) '.opensre-app.install.lock'
        if (-not $appRoot -or
            [System.IO.Path]::GetFileName($appRoot) -ine '.opensre-app' -or
            $expectedInstallId -notmatch '\A[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\z' -or
            -not ([System.IO.Path]::GetFullPath($activeVersion)).Equals(
                [System.IO.Path]::GetFullPath($expectedActiveVersion),
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            -not ([System.IO.Path]::GetFullPath([string]$managed.lock_path)).Equals(
                [System.IO.Path]::GetFullPath($expectedLockPath),
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            Exit-OpenSreCleanup -ExitCode 1
        }
        if (-not (Test-OpenSreCleanupTarget -Path $appRoot)) {
            Exit-OpenSreCleanup -ExitCode 1
        }
        Assert-OpenSreSafeAncestorChain -Path $appRoot
        Assert-OpenSreSafeTree -Path $appRoot
        $appCreated = [int64](
            Get-Item -LiteralPath $appRoot -Force -ErrorAction Stop
        ).CreationTimeUtc.ToFileTimeUtc()
        if ($appCreated -ne [int64]$managed.app_created_filetime_utc) {
            Exit-OpenSreCleanup -ExitCode 1
        }
        if (-not (Test-Path -LiteralPath $activeExecutable -PathType Leaf)) {
            Exit-OpenSreCleanup -ExitCode 1
        }
        $activeHash = (
            Get-FileHash -LiteralPath $activeExecutable -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($activeHash -cne [string]$managed.active_executable_sha256) {
            Exit-OpenSreCleanup -ExitCode 1
        }
        $pointerPath = Join-Path $appRoot 'current.txt'
        $currentInstallId = ''
        if (Test-Path -LiteralPath $pointerPath -PathType Leaf) {
            if (Test-OpenSreReparsePoint -Path $pointerPath) {
                Exit-OpenSreCleanup -ExitCode 1
            }
            $pointerText = [string](Get-Content -LiteralPath $pointerPath -Raw)
            $pointerMatch = [System.Text.RegularExpressions.Regex]::Match(
                $pointerText,
                '\A(?<id>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?:\r?\n)?\z',
                [System.Text.RegularExpressions.RegexOptions]::CultureInvariant
            )
            if (-not $pointerMatch.Success) {
                Exit-OpenSreCleanup -ExitCode 1
            }
            $currentInstallId = $pointerMatch.Groups['id'].Value
        }
        if (-not $currentInstallId) {
            Exit-OpenSreCleanup -ExitCode 1
        }

        if ($currentInstallId -ne $expectedInstallId) {
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
                if (Test-OpenSreReparsePoint -Path $markerPath) {
                    Exit-OpenSreCleanup -ExitCode 1
                }
                $markerText = ([string](Get-Content -LiteralPath $markerPath -Raw)).Trim()
            }
            if ($markerText -cne [string]$managed.layout_marker_text) {
                Exit-OpenSreCleanup -ExitCode 1
            }

            $launcher = [string]$managed.launcher
            if ($launcher -and (Test-Path -LiteralPath $launcher -PathType Leaf)) {
                if (-not (Test-OpenSreManagedLauncher -Path $launcher)) {
                    Exit-OpenSreCleanup -ExitCode 1
                }
                $movedLauncher = "$launcher.uninstall-$([System.Guid]::NewGuid().ToString('N'))"
                Assert-OpenSreSafeAncestorChain -Path $launcher
                Assert-OpenSreSafeAncestorChain -Path $movedLauncher
                Move-Item -LiteralPath $launcher -Destination $movedLauncher -ErrorAction Stop
            }

            if (Test-OpenSreTargetInUse -Path $appRoot) {
                throw 'OpenSRE bundle is still in use.'
            }
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
            Assert-OpenSreSafeAncestorChain -Path $appRoot
            Assert-OpenSreSafeAncestorChain -Path $movedAppRoot
            Move-Item -LiteralPath $appRoot -Destination $movedAppRoot -ErrorAction Stop
            if ((Test-OpenSreTargetInUse -Path $appRoot -TreatAsDirectory) -or
                (Test-OpenSreTargetInUse -Path $movedAppRoot -TreatAsDirectory)) {
                try {
                    Assert-OpenSreSafeAncestorChain -Path $movedAppRoot
                    Assert-OpenSreSafeAncestorChain -Path $appRoot
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
            if (-not (Test-OpenSreExpectedTarget -Target $targetValue)) {
                continue
            }
            $retiredTarget = Move-OpenSreTargetIfUnused -Path ([string]$targetValue.path)
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
            Assert-OpenSreSafeAncestorChain -Path $movedLauncher
            Assert-OpenSreSafeAncestorChain -Path ([string]$managed.launcher)
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
        if (-not (Test-OpenSreExpectedTarget -Target $targetValue)) {
            continue
        }
        $retiredTarget = Move-OpenSreTargetIfUnused -Path ([string]$targetValue.path)
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
            Assert-OpenSreSafeTree -Path $dataTarget
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
    Assert-OpenSreSafeAncestorChain -Path $deleteLockPath
    Remove-Item -LiteralPath $deleteLockPath -Force -ErrorAction SilentlyContinue
}
if ($dataFailed) {
    Exit-OpenSreCleanup -ExitCode 1
}
Exit-OpenSreCleanup -ExitCode 0
