param(
    [ValidateSet("release", "main")]
    [string]$Channel = $(if ($env:OPENSRE_INSTALL_CHANNEL) { $env:OPENSRE_INSTALL_CHANNEL } else { "main" }),
    [switch]$SkipMain
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:OpenSreProgressStep = 0
$script:OpenSreChannelExplicit = $PSBoundParameters.ContainsKey("Channel") -or [bool]$env:OPENSRE_INSTALL_CHANNEL
$script:OpenSreLauncherMarker = ":: OpenSRE Windows launcher v1"
$script:OpenSreLayoutMarkerName = "layout-v1.marker"
$script:OpenSreLayoutMarkerText = "OpenSRE Windows bundle layout v1"
$script:OpenSreLayoutRootName = ".opensre-app"
$script:OpenSreCurrentPointerName = "current.txt"
$script:OpenSreInstallLockName = ".opensre-app.install.lock"
$script:OpenSreReplaceExistingBinaryEnv = "OPENSRE_INSTALL_REPLACE_EXISTING_BINARY"
$script:OpenSreUpdateParentStartedEnv = "OPENSRE_UPDATE_PARENT_STARTED"
$script:OpenSreMaxCommandPathLength = 259

function Test-OpenSreVerboseInstall {
    $value = [string]$env:OPENSRE_INSTALL_VERBOSE
    return ($value -eq "1" -or $value -eq "true" -or $value -eq "TRUE" -or $value -eq "yes" -or $value -eq "YES")
}

function Test-OpenSreInteractiveHost {
    try {
        if ([System.Console]::IsOutputRedirected) {
            return $false
        }
    }
    catch {
        if ($null -eq $Host -or $null -eq $Host.UI) {
            return $false
        }
    }

    try {
        if ($null -eq $Host -or $null -eq $Host.UI -or $null -eq $Host.UI.RawUI) {
            return $false
        }

        $null = $Host.UI.RawUI.WindowSize
    }
    catch {
        return $false
    }

    return $true
}

function Get-OpenSreConsoleWidth {
    [int]$width = 0

    try {
        if ($null -ne $Host -and $null -ne $Host.UI -and $null -ne $Host.UI.RawUI) {
            $hostWidth = [int]$Host.UI.RawUI.WindowSize.Width
            if ($hostWidth -gt 0) {
                $width = $hostWidth
            }
        }
    }
    catch {
        $width = 0
    }

    if ($width -le 0) {
        try {
            $consoleWidth = [int][System.Console]::WindowWidth
            if ($consoleWidth -gt 0) {
                $width = $consoleWidth
            }
        }
        catch {
            $width = 0
        }
    }

    if ($width -lt 20) {
        $width = 80
    }

    return $width
}

function Limit-OpenSreText {
    param(
        [AllowEmptyString()]
        [string]$Text,
        [int]$MaxWidth
    )

    $value = [string]$Text
    $value = $value.Replace("`r", " ").Replace("`n", " ")

    if ($MaxWidth -le 0) {
        return ""
    }

    if ($value.Length -le $MaxWidth) {
        return $value
    }

    if ($MaxWidth -le 3) {
        return $value.Substring(0, $MaxWidth)
    }

    return ($value.Substring(0, $MaxWidth - 3) + "...")
}

function Get-OpenSreFriendlyProgressLabel {
    param(
        [AllowEmptyString()]
        [string]$Label
    )

    if ($Label -like "*Fetching latest main build metadata*" -or
        $Label -like "*Fetching latest release version*" -or
        $Label -like "*Fetching release metadata*") {
        return "fetching metadata"
    }

    # More specific than ``*Preparing opensre*`` below (install warm-up step).
    if ($Label -like "*first launch*") {
        return "preparing first launch"
    }

    if ($Label -like "*Preparing opensre*") {
        return "resolving build"
    }

    if ($Label -like "*Downloading release archive*" -or
        $Label -like "*.zip" -or
        $Label -like "*.tar.gz") {
        return "downloading archive"
    }

    if ($Label -like "*Downloading and verifying checksum*" -or
        $Label -like "*Verifying release archive*" -or
        $Label -like "*.sha256") {
        return "verifying checksum"
    }

    if ($Label -like "*Extracting and verifying binary*") {
        return "verifying binary"
    }

    if ($Label -like "*Installing*binary*" -or
        $Label -like "*Installing*opensre*") {
        return "installing binary"
    }

    return ([System.Text.RegularExpressions.Regex]::Replace([string]$Label, '^\[[0-9]+/[0-9]+\]\s*', ""))
}

function Get-OpenSreProgressFrame {
    param(
        [int]$Step
    )

    $frames = @("-", "\", "|", "/")
    return $frames[$Step % $frames.Count]
}

function New-OpenSreProgressBar {
    param(
        [int]$Step,
        [int]$Width
    )

    if ($Width -lt 1) {
        return ""
    }

    [int]$trail = 8
    [int]$head = $Step % ($Width + $trail)
    $builder = New-Object System.Text.StringBuilder

    for ($i = 0; $i -lt $Width; $i += 1) {
        $age = $head - $i
        if ($age -ge 0 -and $age -lt $trail) {
            if ($age -eq 0 -or $age -eq 1) {
                [void]$builder.Append("#")
            }
            elseif ($age -eq 2 -or $age -eq 3) {
                [void]$builder.Append("=")
            }
            elseif ($age -eq 4 -or $age -eq 5) {
                [void]$builder.Append("+")
            }
            else {
                [void]$builder.Append("-")
            }
        }
        else {
            [void]$builder.Append(".")
        }
    }

    return $builder.ToString()
}

function Write-OpenSreLine {
    param(
        [AllowEmptyString()]
        [string]$Message,
        [string]$Color = ""
    )

    if ((Test-OpenSreInteractiveHost) -and $Color) {
        Write-Host $Message -ForegroundColor $Color
        return
    }

    Write-Host $Message
}

function Write-OpenSreDetail {
    param(
        [AllowEmptyString()]
        [string]$Message
    )

    if (-not $Message) {
        return
    }

    Write-OpenSreLine -Message "  $Message" -Color "DarkGray"
}

function Write-OpenSreHeader {
    param(
        [string]$Channel = "",
        [string]$RequestedVersion = "",
        [string]$InstallDir = "",
        [string]$Repo = ""
    )

    Write-OpenSreLine -Message "OpenSRE installer" -Color "Cyan"
    Write-OpenSreLine -Message "Installing the OpenSRE CLI for Windows." -Color "DarkGray"

    if (Test-OpenSreVerboseInstall) {
        Write-OpenSreDetail -Message "Verbose logging enabled by OPENSRE_INSTALL_VERBOSE=1."
        if ($Repo) {
            Write-OpenSreDetail -Message "Repository: $Repo"
        }
        if ($Channel) {
            Write-OpenSreDetail -Message "Channel: $Channel"
        }
        if ($RequestedVersion) {
            Write-OpenSreDetail -Message "Requested version: $RequestedVersion"
        }
        if ($InstallDir) {
            Write-OpenSreDetail -Message "Install directory: $InstallDir"
        }
    }
}

function Write-OpenSreProgressLine {
    param(
        [string]$Label,
        [Int64]$DownloadedBytes,
        [Int64]$TotalBytes = -1
    )

    if (-not (Test-OpenSreInteractiveHost) -or (Test-OpenSreVerboseInstall)) {
        return
    }

    $width = Get-OpenSreConsoleWidth
    [int]$clearWidth = $width - 1
    if ($clearWidth -lt 1) {
        $clearWidth = 1
    }

    $title = "Installing OpenSRE"
    if ($width -lt 56) {
        $title = "OpenSRE"
    }

    $percentText = ""
    if ($TotalBytes -gt 0) {
        $percent = [Math]::Min(100, [Math]::Floor(($DownloadedBytes * 100) / $TotalBytes))
        $percentText = " $percent%"
    }

    [int]$reserve = 2 + 1 + 1 + 1 + $title.Length + 1 + $percentText.Length
    [int]$available = $clearWidth - $reserve
    [int]$barWidth = 8
    if ($available -lt 12) {
        $barWidth = 4
    }
    else {
        $barWidth = [Math]::Floor($available / 2)
        if ($barWidth -gt 28) {
            $barWidth = 28
        }
        if ($barWidth -lt 8) {
            $barWidth = 8
        }
    }

    [int]$labelWidth = $clearWidth - $reserve - $barWidth
    if ($labelWidth -lt 8 -and $barWidth -gt 4) {
        $barWidth = $clearWidth - $reserve - 8
        if ($barWidth -lt 4) {
            $barWidth = 4
        }
        $labelWidth = $clearWidth - $reserve - $barWidth
    }
    if ($labelWidth -lt 0) {
        $labelWidth = 0
    }

    $script:OpenSreProgressStep += 1
    $frame = Get-OpenSreProgressFrame -Step $script:OpenSreProgressStep
    $bar = New-OpenSreProgressBar -Step $script:OpenSreProgressStep -Width $barWidth
    $status = Limit-OpenSreText -Text (Get-OpenSreFriendlyProgressLabel -Label $Label) -MaxWidth $labelWidth
    $content = "  $frame $bar $title $status$percentText"
    if ($content.Length -gt $clearWidth) {
        $content = $content.Substring(0, $clearWidth)
    }

    # Parenthesize the entire -f expression. Without that, PowerShell treats the
    # comma as a Console.Write argument separator, so -f only receives one value
    # and "{1}" raises: "Error formatting a string: Index ... argument list."
    [System.Console]::Write(("`r{0}`r{1}" -f (" " * $clearWidth), $content))
}

function Clear-OpenSreProgressLine {
    if (-not (Test-OpenSreInteractiveHost) -or (Test-OpenSreVerboseInstall)) {
        return
    }

    $width = Get-OpenSreConsoleWidth
    [int]$clearWidth = $width - 1
    if ($clearWidth -lt 1) {
        $clearWidth = 1
    }

    [System.Console]::Write("`r{0}`r" -f (" " * $clearWidth))
}

function Invoke-OpenSreStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [scriptblock]$Operation,
        [string]$Detail = ""
    )

    Write-OpenSreLine -Message $Name -Color "Cyan"
    Write-OpenSreDetail -Message $Detail

    if ($Operation) {
        try {
            $result = & $Operation
            Write-OpenSreLine -Message "  OK $Name" -Color "Green"
            return $result
        }
        catch {
            Write-OpenSreLine -Message "  FAILED $Name" -Color "Red"
            throw
        }
    }
}

function Invoke-OpenSreStreamDownload {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [string]$OutFile,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $request = [System.Net.HttpWebRequest]::Create($Uri)
    $headers = Get-OpenSreRequestHeaders
    foreach ($key in $headers.Keys) {
        if ($key -eq "User-Agent") {
            $request.UserAgent = [string]$headers[$key]
        }
        elseif ($key -eq "Accept") {
            $request.Accept = [string]$headers[$key]
        }
        else {
            $request.Headers[$key] = [string]$headers[$key]
        }
    }

    $response = $request.GetResponse()
    try {
        $totalBytes = [Int64]$response.ContentLength
        $inputStream = $response.GetResponseStream()
        $outputStream = [System.IO.File]::Open($OutFile, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
        try {
            $buffer = New-Object byte[] 65536
            [Int64]$downloadedBytes = 0

            while ($true) {
                $read = $inputStream.Read($buffer, 0, $buffer.Length)
                if ($read -le 0) {
                    break
                }

                $outputStream.Write($buffer, 0, $read)
                $downloadedBytes += $read
                Write-OpenSreProgressLine -Label $Label -DownloadedBytes $downloadedBytes -TotalBytes $totalBytes
            }
        }
        finally {
            if ($outputStream) {
                $outputStream.Dispose()
            }
            if ($inputStream) {
                $inputStream.Dispose()
            }
            Clear-OpenSreProgressLine
        }
    }
    finally {
        if ($response) {
            $response.Dispose()
        }
    }
}

function Get-OpenSreDefaultInstallDir {
    $userHome = if ($HOME) { $HOME } else { [System.Environment]::GetFolderPath("UserProfile") }
    return Join-Path $userHome ".local\bin"
}

function Initialize-OpenSreNativePathApi {
    if (([System.Management.Automation.PSTypeName]"OpenSre.NativePathApi").Type) {
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
    public static class NativePathApi
    {
        private const uint FileShareRead = 0x00000001;
        private const uint FileShareWrite = 0x00000002;
        private const uint FileShareDelete = 0x00000004;
        private const uint OpenExisting = 3;
        private const uint FileFlagBackupSemantics = 0x02000000;
        private const uint SnapshotProcesses = 0x00000002;
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct ProcessEntry32
        {
            public uint Size;
            public uint Usage;
            public uint ProcessId;
            public IntPtr DefaultHeapId;
            public uint ModuleId;
            public uint Threads;
            public uint ParentProcessId;
            public int BasePriority;
            public uint Flags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
            public string ExecutableName;
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

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetShortPathName(
            string longPath,
            StringBuilder shortPath,
            uint shortPathLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32First(IntPtr snapshot, ref ProcessEntry32 entry);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32Next(IntPtr snapshot, ref ProcessEntry32 entry);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public static int GetParentProcessId(int processId)
        {
            IntPtr snapshot = CreateToolhelp32Snapshot(SnapshotProcesses, 0);
            if (snapshot == new IntPtr(-1))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            try
            {
                ProcessEntry32 entry = new ProcessEntry32();
                entry.Size = (uint)Marshal.SizeOf(typeof(ProcessEntry32));
                if (!Process32First(snapshot, ref entry))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                do
                {
                    if (entry.ProcessId == (uint)processId)
                    {
                        return checked((int)entry.ParentProcessId);
                    }
                }
                while (Process32Next(snapshot, ref entry));
                return 0;
            }
            finally
            {
                CloseHandle(snapshot);
            }
        }

        public static string GetShortPath(string path)
        {
            uint capacity = 512;
            while (true)
            {
                StringBuilder value = new StringBuilder((int)capacity);
                uint length = GetShortPathName(path, value, capacity);
                if (length == 0)
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if (length < capacity)
                {
                    return value.ToString();
                }
                capacity = length + 1;
            }
        }

        public static string GetFinalPath(string path)
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
                        return value.ToString();
                    }
                    capacity = length + 1;
                }
            }
        }

    }
}
'@
}

function Test-OpenSrePathExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $extendedPath = ConvertTo-OpenSreExtendedPath -Path $Path
    return (
        [System.IO.File]::Exists($extendedPath) -or
        [System.IO.Directory]::Exists($extendedPath)
    )
}

function Get-OpenSreCanonicalPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if (-not $fullPath) {
        throw "Invalid empty path."
    }

    $existingPath = $fullPath
    $missingSegments = New-Object System.Collections.Generic.List[string]
    while (-not (Test-OpenSrePathExists -Path $existingPath)) {
        $leaf = [System.IO.Path]::GetFileName($existingPath)
        $parent = [System.IO.Path]::GetDirectoryName($existingPath)
        if (-not $leaf -or -not $parent -or $parent -eq $existingPath) {
            throw "Could not resolve an existing ancestor of '$fullPath'."
        }
        $missingSegments.Insert(0, $leaf)
        $existingPath = $parent
    }

    Initialize-OpenSreNativePathApi
    $canonicalPath = [OpenSre.NativePathApi]::GetFinalPath(
        (ConvertTo-OpenSreExtendedPath -Path $existingPath)
    ).TrimEnd('\', '/')
    if ($canonicalPath.StartsWith('\\?\UNC\')) {
        $canonicalPath = '\\' + $canonicalPath.Substring(8)
    }
    elseif ($canonicalPath.StartsWith('\\?\')) {
        $canonicalPath = $canonicalPath.Substring(4)
    }
    foreach ($segment in $missingSegments) {
        $canonicalPath = [System.IO.Path]::Combine($canonicalPath, $segment)
    }
    return $canonicalPath
}

function Test-OpenSreSamePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Left,
        [Parameter(Mandatory = $true)]
        [string]$Right
    )

    try {
        $leftPath = Get-OpenSreCanonicalPath -Path $Left
        $rightPath = Get-OpenSreCanonicalPath -Path $Right
        return $leftPath.Equals(
            $rightPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

function Test-OpenSrePathContainedBy {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$Candidate
    )

    try {
        $rootPath = (Get-OpenSreCanonicalPath -Path $Root).TrimEnd('\', '/')
        $candidatePath = Get-OpenSreCanonicalPath -Path $Candidate
        return (
            $candidatePath.Equals(
                $rootPath,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            $candidatePath.StartsWith(
                $rootPath + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        )
    }
    catch {
        return $false
    }
}

function Assert-OpenSrePathHasNoReparsePoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Purpose
    )

    if (-not (Test-OpenSrePathExists -Path $Path)) {
        return
    }
    $attributes = [System.IO.File]::GetAttributes(
        (ConvertTo-OpenSreExtendedPath -Path $Path)
    )
    if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing $Purpose because '$Path' is a reparse point."
    }
}

function Assert-OpenSreInstallPathSafe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$InstallDir,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [string]$Purpose = "OpenSRE installation path"
    )

    $installPath = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\', '/')
    $candidatePath = [System.IO.Path]::GetFullPath($Path)
    if (-not (
            $candidatePath.Equals(
                $installPath,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            $candidatePath.StartsWith(
                $installPath + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        )) {
        throw "Refusing $Purpose outside '$installPath': '$candidatePath'."
    }

    $relativePath = $candidatePath.Substring($installPath.Length).TrimStart('\', '/')
    $componentPath = $installPath
    Assert-OpenSrePathHasNoReparsePoint -Path $componentPath -Purpose $Purpose
    if ($relativePath) {
        foreach ($segment in $relativePath.Split(@('\', '/'), [System.StringSplitOptions]::RemoveEmptyEntries)) {
            $componentPath = Join-Path $componentPath $segment
            Assert-OpenSrePathHasNoReparsePoint -Path $componentPath -Purpose $Purpose
        }
    }

    if (-not (Test-OpenSrePathContainedBy -Root $installPath -Candidate $candidatePath)) {
        throw "Refusing $Purpose that escapes '$installPath': '$candidatePath'."
    }
}

function Assert-OpenSreAbsolutePathHasNoReparsePoints {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [string]$Purpose = "OpenSRE installation root"
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $pathRoot = [System.IO.Path]::GetPathRoot($fullPath).TrimEnd('\', '/')
    $componentPath = $pathRoot + [System.IO.Path]::DirectorySeparatorChar
    $relativePath = $fullPath.Substring($componentPath.Length)
    Assert-OpenSrePathHasNoReparsePoint -Path $componentPath -Purpose $Purpose
    foreach ($segment in $relativePath.Split(@('\', '/'), [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $componentPath = Join-Path $componentPath $segment
        Assert-OpenSrePathHasNoReparsePoint -Path $componentPath -Purpose $Purpose
    }
}

function Assert-OpenSreTreeHasNoReparsePoints {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [string]$Purpose = "OpenSRE application bundle"
    )

    $pending = New-Object System.Collections.Generic.Stack[string]
    $pending.Push([System.IO.Path]::GetFullPath($Root))
    while ($pending.Count -gt 0) {
        $entryPath = $pending.Pop()
        Assert-OpenSrePathHasNoReparsePoint -Path $entryPath -Purpose $Purpose
        $extendedEntryPath = ConvertTo-OpenSreExtendedPath -Path $entryPath
        if ([System.IO.Directory]::Exists($extendedEntryPath)) {
            foreach ($child in [System.IO.Directory]::EnumerateFileSystemEntries($extendedEntryPath)) {
                $pending.Push($child)
            }
        }
    }
}

function Get-OpenSreProcessStartedToken {
    param(
        [Parameter(Mandatory = $true)]
        [System.Diagnostics.Process]$Process
    )

    return $Process.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(
        [System.Globalization.CultureInfo]::InvariantCulture
    )
}

function Get-OpenSreImmediateParentContext {
    try {
        Initialize-OpenSreNativePathApi
        $parentProcessId = [OpenSre.NativePathApi]::GetParentProcessId($PID)
        if ($parentProcessId -le 0) {
            return $null
        }

        $parentProcess = Get-Process -Id $parentProcessId -ErrorAction Stop
        $parentPath = [string]$parentProcess.Path
        if (-not $parentPath -or [System.IO.Path]::GetFileName($parentPath) -ine "opensre.exe") {
            return $null
        }

        return [pscustomobject]@{
            ProcessId = $parentProcessId
            ExecutablePath = Get-OpenSreCanonicalPath -Path $parentPath
            Started = Get-OpenSreProcessStartedToken -Process $parentProcess
        }
    }
    catch {
        return $null
    }
}

function Get-OpenSreInstallDirFromExecutable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExecutablePath
    )

    try {
        $resolvedExecutable = Get-OpenSreCanonicalPath -Path $ExecutablePath
    }
    catch {
        throw "Invalid OpenSRE update executable path '$ExecutablePath'."
    }

    if ([System.IO.Path]::GetFileName($resolvedExecutable) -ine "opensre.exe") {
        throw "OpenSRE update executable must be named 'opensre.exe': '$resolvedExecutable'."
    }

    $versionDirectory = Split-Path -Parent $resolvedExecutable
    $versionsRoot = Split-Path -Parent $versionDirectory
    $layoutRoot = Split-Path -Parent $versionsRoot
    $insideManagedLayoutRoot = $false
    $ancestor = $versionDirectory
    while ($ancestor) {
        if ([System.IO.Path]::GetFileName($ancestor) -ieq $script:OpenSreLayoutRootName) {
            $insideManagedLayoutRoot = $true
            break
        }
        $parent = Split-Path -Parent $ancestor
        if (-not $parent -or $parent -eq $ancestor) {
            break
        }
        $ancestor = $parent
    }
    $looksVersioned = `
        ([System.IO.Path]::GetFileName($versionsRoot) -ieq "versions") -or `
        $insideManagedLayoutRoot

    if ($looksVersioned) {
        $isExpectedShape = `
            ([System.IO.Path]::GetFileName($versionsRoot) -ieq "versions") -and `
            ([System.IO.Path]::GetFileName($layoutRoot) -ieq $script:OpenSreLayoutRootName)
        if (-not $isExpectedShape) {
            throw "Refusing malformed OpenSRE versioned update path '$resolvedExecutable'."
        }

        $markerPath = Join-Path $layoutRoot $script:OpenSreLayoutMarkerName
        $installDir = Split-Path -Parent $layoutRoot
        $launcherPath = Join-Path $installDir "opensre.cmd"
        Assert-OpenSreInstallPathSafe `
            -InstallDir $installDir `
            -Path $resolvedExecutable `
            -Purpose "OpenSRE managed update path"
        if (-not (Test-OpenSreManagedLayoutMarker -MarkerPath $markerPath) -or
            -not (Test-OpenSreManagedLauncher -LauncherPath $launcherPath)) {
            throw "Refusing unowned OpenSRE versioned update path '$resolvedExecutable'."
        }
        return $installDir
    }

    # An unpacked standalone onedir artifact is not an installed legacy onefile.
    # Updating it should create the normal user installation instead of deleting
    # the artifact's own entry point and leaving its adjacent _internal behind.
    if (Test-OpenSreInstallDirectoryExists -Path (Join-Path $versionDirectory "_internal")) {
        return ""
    }

    return $versionDirectory
}

function Get-OpenSreVerifiedUpdateExecutablePath {
    param(
        [AllowEmptyString()]
        [string]$UpdateExecutable,
        [int]$ParentProcessId,
        [AllowEmptyString()]
        [string]$ParentStarted
    )

    if (-not $UpdateExecutable -or $ParentProcessId -le 0 -or -not $ParentStarted) {
        return ""
    }

    try {
        $parentContext = Get-OpenSreImmediateParentContext
        if ($null -eq $parentContext -or
            [int]$parentContext.ProcessId -ne $ParentProcessId -or
            [string]$parentContext.Started -cne $ParentStarted -or
            -not (Test-OpenSreSamePath `
                -Left ([string]$parentContext.ExecutablePath) `
                -Right $UpdateExecutable)) {
            return ""
        }
        return Get-OpenSreCanonicalPath -Path $UpdateExecutable
    }
    catch {
        return ""
    }
}

function Get-OpenSreVerifiedLegacyBinaryPath {
    param(
        [AllowEmptyString()]
        [string]$UpdateExecutable,
        [int]$ParentProcessId,
        [AllowEmptyString()]
        [string]$ParentStarted,
        [Parameter(Mandatory = $true)]
        [string]$InstallDir
    )

    $verifiedUpdateExecutable = Get-OpenSreVerifiedUpdateExecutablePath `
        -UpdateExecutable $UpdateExecutable `
        -ParentProcessId $ParentProcessId `
        -ParentStarted $ParentStarted
    if (-not $verifiedUpdateExecutable) {
        return ""
    }

    try {
        $resolvedExecutable = $verifiedUpdateExecutable
        $executableDirectory = [System.IO.Path]::GetDirectoryName($resolvedExecutable)
        if (-not (Test-OpenSreSamePath -Left $executableDirectory -Right $InstallDir) -or
            [System.IO.Path]::GetFileName($resolvedExecutable) -ine "opensre.exe" -or
            (Test-OpenSreInstallDirectoryExists -Path (Join-Path $executableDirectory "_internal"))) {
            return ""
        }

        return $resolvedExecutable
    }
    catch {
        return ""
    }
}

function Get-OpenSreLegacyReplacementRefusalMessage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BinaryPath
    )

    $optIn = $script:OpenSreReplaceExistingBinaryEnv
    return "Refusing to replace unverified pre-existing executable '$BinaryPath'. Nothing was changed. Re-run 'irm https://install.opensre.com | iex' in an interactive PowerShell window and confirm the replacement, or set $optIn=1 for unattended installs."
}

function Test-OpenSreLegacyReplacementOptIn {
    $value = [string]$env:OPENSRE_INSTALL_REPLACE_EXISTING_BINARY
    return $value -ceq "1"
}

function Read-OpenSreConfirmationResponse {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Prompt
    )

    return [string](Read-Host -Prompt $Prompt)
}

function Confirm-OpenSreLegacyBinaryReplacement {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BinaryPath
    )

    if (Test-OpenSreLegacyReplacementOptIn) {
        Write-Host "Replacing the pre-existing OpenSRE executable because $($script:OpenSreReplaceExistingBinaryEnv) is set:"
        Write-Host "    $BinaryPath"
        return $true
    }

    if (-not (Test-OpenSreInteractiveHost)) {
        return $false
    }

    Write-Host ""
    Write-Host "An OpenSRE executable already exists at:"
    Write-Host "    $BinaryPath"
    Write-Host ""
    Write-Host "Installing retires that file and replaces it with the managed launcher."
    Write-Host "The installer never runs it. Answer No if you do not recognize this file."

    $answer = ""
    try {
        $answer = [string](Read-OpenSreConfirmationResponse -Prompt "Replace it? [y/N]")
    }
    catch {
        return $false
    }

    $answer = $answer.Trim()
    return ($answer -ieq "y" -or $answer -ieq "yes")
}

function Resolve-OpenSreInstallContext {
    $explicitInstallDir = [string]$env:OPENSRE_INSTALL_DIR
    $updateExecutable = [string]$env:OPENSRE_UPDATE_EXECUTABLE
    $updateParentStarted = [string]$env:OPENSRE_UPDATE_PARENT_STARTED
    $updateParentProcessId = 0
    if ($env:OPENSRE_UPDATE_PARENT_PID) {
        $parsedParentProcessId = 0
        if ([int]::TryParse(
                [string]$env:OPENSRE_UPDATE_PARENT_PID,
                [ref]$parsedParentProcessId
            ) -and $parsedParentProcessId -gt 0) {
            $updateParentProcessId = $parsedParentProcessId
        }
    }

    $parentContext = $null
    $handoffSupplied = [bool](
        $updateExecutable -or
        $env:OPENSRE_UPDATE_PARENT_PID -or
        $updateParentStarted
    )
    if ($handoffSupplied -and (-not $updateExecutable -or $updateParentProcessId -le 0)) {
        throw "Incomplete OpenSRE update process handoff; executable and parent PID are both required."
    }

    if (-not $handoffSupplied) {
        $parentContext = Get-OpenSreImmediateParentContext
        if ($parentContext) {
            $updateExecutable = [string]$parentContext.ExecutablePath
            $updateParentProcessId = [int]$parentContext.ProcessId
            $updateParentStarted = [string]$parentContext.Started
        }
    }
    else {
        # Older onefile releases supplied PID and executable only. Bind those
        # values to the immediate parent and capture its creation time before
        # trusting the handoff; newer callers also supply the token and must
        # match it exactly.
        $parentContext = Get-OpenSreImmediateParentContext
        if ($null -eq $parentContext -or
            [int]$parentContext.ProcessId -ne $updateParentProcessId -or
            -not (Test-OpenSreSamePath `
                -Left ([string]$parentContext.ExecutablePath) `
                -Right $updateExecutable) -or
            ($updateParentStarted -and
                [string]$parentContext.Started -cne $updateParentStarted)) {
            throw "Refusing unverified OpenSRE update process handoff."
        }
        $updateExecutable = [string]$parentContext.ExecutablePath
        $updateParentStarted = [string]$parentContext.Started
    }

    $verifiedUpdateExecutable = Get-OpenSreVerifiedUpdateExecutablePath `
        -UpdateExecutable $updateExecutable `
        -ParentProcessId $updateParentProcessId `
        -ParentStarted $updateParentStarted

    $installDir = $explicitInstallDir
    if (-not $installDir -and $verifiedUpdateExecutable) {
        $installDir = Get-OpenSreInstallDirFromExecutable `
            -ExecutablePath $verifiedUpdateExecutable
    }
    if (-not $installDir) {
        $installDir = Get-OpenSreDefaultInstallDir
    }

    $legacyBinaryPath = Get-OpenSreVerifiedLegacyBinaryPath `
        -UpdateExecutable $verifiedUpdateExecutable `
        -ParentProcessId $updateParentProcessId `
        -ParentStarted $updateParentStarted `
        -InstallDir $installDir

    return [pscustomobject]@{
        InstallDir = $installDir
        ParentProcessId = $updateParentProcessId
        ParentStarted = $updateParentStarted
        ParentExecutablePath = $verifiedUpdateExecutable
        IsUpdate = [bool]$verifiedUpdateExecutable
        LegacyBinaryPath = $legacyBinaryPath
    }
}

function Get-OpenSreRequestHeaders {
    return @{
        "Accept" = "application/vnd.github+json"
        "User-Agent" = "opensre-install-script"
    }
}

function Invoke-OpenSreWithRetry {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Operation,
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [int]$MaxAttempts = 3
    )

    $attempt = 1

    while ($true) {
        try {
            return & $Operation
        }
        catch {
            $statusCode = Get-OpenSreHttpStatusCodeFromError -ErrorRecord $_
            if ($null -ne $statusCode -and $statusCode -ge 400 -and $statusCode -lt 500) {
                throw "Failed to $Description. $($_.Exception.Message)"
            }

            if ($attempt -ge $MaxAttempts) {
                throw "Failed to $Description after $attempt attempts. $($_.Exception.Message)"
            }

            Write-Warning "Attempt $attempt to $Description failed: $($_.Exception.Message). Retrying..."
            Start-Sleep -Seconds $attempt
            $attempt += 1
        }
    }
}

function Get-OpenSreHttpStatusCodeFromError {
    param(
        [Parameter(Mandatory = $true)]
        [System.Management.Automation.ErrorRecord]$ErrorRecord
    )

    $exception = $ErrorRecord.Exception

    while ($null -ne $exception) {
        if ($exception.PSObject.Properties["Response"] -and $null -ne $exception.Response) {
            $response = $exception.Response
            if ($response.PSObject.Properties["StatusCode"] -and $null -ne $response.StatusCode) {
                try {
                    return [int]$response.StatusCode
                }
                catch {
                    return $null
                }
            }
        }

        if ($exception.PSObject.Properties["StatusCode"] -and $null -ne $exception.StatusCode) {
            try {
                return [int]$exception.StatusCode
            }
            catch {
                return $null
            }
        }

        $exception = $exception.InnerException
    }

    return $null
}

function Enable-OpenSreTls {
    try {
        $protocol = [System.Net.ServicePointManager]::SecurityProtocol
        $availableProtocols = [System.Enum]::GetNames([System.Net.SecurityProtocolType])

        if ($availableProtocols -contains "Tls12") {
            $protocol = $protocol -bor [System.Net.SecurityProtocolType]::Tls12
        }

        if ($availableProtocols -contains "Tls13") {
            $protocol = $protocol -bor [System.Net.SecurityProtocolType]::Tls13
        }

        [System.Net.ServicePointManager]::SecurityProtocol = $protocol
    }
    catch {
        # Best-effort compatibility tweak for older Windows PowerShell runtimes.
    }
}

function Invoke-OpenSreRestMethod {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri
    )

    $params = @{
        Uri = $Uri
        Headers = Get-OpenSreRequestHeaders
    }

    $command = Get-Command Invoke-RestMethod -ErrorAction Stop
    if ($command.Parameters.ContainsKey("UseBasicParsing")) {
        $params.UseBasicParsing = $true
    }

    if (Test-OpenSreVerboseInstall) {
        Write-OpenSreDetail -Message "GET $Uri"
    }

    return Invoke-OpenSreWithRetry -Description "fetch release metadata from GitHub" -Operation {
        Invoke-RestMethod @params
    }
}

function Invoke-OpenSreDownloadFileWithProgress {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [string]$OutFile,
        [string]$Label = ""
    )

    if (-not $Label) {
        $Label = [System.IO.Path]::GetFileName($OutFile)
    }

    if (-not $Label) {
        $Label = "file"
    }

    $params = @{
        Uri = $Uri
        Headers = Get-OpenSreRequestHeaders
        OutFile = $OutFile
    }

    $command = Get-Command Invoke-WebRequest -ErrorAction Stop
    if ($command.Parameters.ContainsKey("UseBasicParsing")) {
        $params.UseBasicParsing = $true
    }

    if (Test-OpenSreVerboseInstall) {
        Write-OpenSreDetail -Message "Download URL: $Uri"
        Write-OpenSreDetail -Message "Destination: $OutFile"
    }
    else {
        Write-OpenSreDetail -Message $Label
    }

    Invoke-OpenSreWithRetry -Description "download '$Uri'" -Operation {
        if ((Test-OpenSreInteractiveHost) -and -not (Test-OpenSreVerboseInstall)) {
            Invoke-OpenSreStreamDownload -Uri $Uri -OutFile $OutFile -Label $Label
        }
        else {
            $previousProgressPreference = $ProgressPreference
            try {
                $ProgressPreference = "SilentlyContinue"
                Invoke-WebRequest @params | Out-Null
            }
            finally {
                $ProgressPreference = $previousProgressPreference
            }
        }
    } | Out-Null
}

function Invoke-OpenSreWebRequest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [string]$OutFile
    )

    Invoke-OpenSreDownloadFileWithProgress -Uri $Uri -OutFile $OutFile
}

function Get-OpenSreRuntimeArchitecture {
    try {
        $runtimeInformation = [System.Runtime.InteropServices.RuntimeInformation]
        return [string]$runtimeInformation::OSArchitecture
    }
    catch {
        return ""
    }
}

function Resolve-OpenSreWindowsArchitecture {
    param(
        [string]$RuntimeArchitecture = (Get-OpenSreRuntimeArchitecture),
        [string]$ProcessorArchitectureW6432 = $env:PROCESSOR_ARCHITEW6432,
        [string]$ProcessorArchitecture = $env:PROCESSOR_ARCHITECTURE,
        [bool]$Is64BitOperatingSystem = [System.Environment]::Is64BitOperatingSystem
    )

    $candidates = @(
        $RuntimeArchitecture,
        $ProcessorArchitectureW6432,
        $ProcessorArchitecture
    ) | Where-Object { $_ -and $_.Trim() }

    foreach ($candidate in $candidates) {
        $normalized = $candidate.Trim().ToUpperInvariant()

        switch ($normalized) {
            { $_ -in @("X64", "AMD64", "X86_64") } { return "x64" }
            { $_ -in @("ARM64", "AARCH64") } { return "arm64" }
            { $_ -in @("X86", "I386", "I686") } {
                throw "Unsupported Windows architecture: $candidate. OpenSRE releases are available only for x64 and arm64."
            }
        }
    }

    if ($Is64BitOperatingSystem) {
        return "x64"
    }

    throw "Unsupported Windows architecture. Could not detect a supported architecture from RuntimeInformation, PROCESSOR_ARCHITEW6432, or PROCESSOR_ARCHITECTURE."
}

function Get-OpenSreArchiveName {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version,
        [Parameter(Mandatory = $true)]
        [ValidateSet("release", "main")]
        [string]$Channel,
        [Parameter(Mandatory = $true)]
        [string]$TargetArch
    )

    $archiveVersion = if ($Channel -eq "main") { "main" } else { $Version }
    return "opensre_${archiveVersion}_windows-$TargetArch.zip"
}

function Get-OpenSreReleaseMetadata {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Repo,
        [ValidateSet("release", "main")]
        [string]$Channel = "release",
        [string]$RequestedVersion = $env:OPENSRE_VERSION
    )

    $normalizedVersion = ""
    if ($RequestedVersion) {
        $normalizedVersion = $RequestedVersion.Trim().TrimStart("v")
    }

    if ($Channel -eq "main" -and $normalizedVersion) {
        throw "OPENSRE_VERSION cannot be combined with the main install channel."
    }

    $mainReleaseTag = if ($env:OPENSRE_MAIN_RELEASE_TAG) { $env:OPENSRE_MAIN_RELEASE_TAG } else { "main-build" }

    $releaseUri = if ($Channel -eq "main") {
        "https://api.github.com/repos/$Repo/releases/tags/$mainReleaseTag"
    }
    elseif ($normalizedVersion) {
        "https://api.github.com/repos/$Repo/releases/tags/v$normalizedVersion"
    }
    else {
        "https://api.github.com/repos/$Repo/releases/latest"
    }

    try {
        $release = Invoke-OpenSreRestMethod -Uri $releaseUri
    }
    catch {
        if ($Channel -eq "main") {
            throw "Failed to fetch main build metadata from GitHub for '$Repo'. $($_.Exception.Message)"
        }

        if ($normalizedVersion) {
            throw "Failed to fetch release metadata for version '$normalizedVersion' from GitHub repo '$Repo'. $($_.Exception.Message)"
        }

        throw "Failed to fetch latest release metadata from GitHub for '$Repo'. $($_.Exception.Message)"
    }

    $version = if ($Channel -eq "main") { "main" } else { [string]$release.tag_name }
    if ($Channel -ne "main" -and $version) {
        $version = $version.Trim().TrimStart("v")
    }

    if (-not $version) {
        if ($Channel -eq "main") {
            throw "Failed to determine the main build tag."
        }

        throw "Failed to determine the latest release version."
    }

    return [pscustomobject]@{
        Release = $release
        Version = $version
    }
}

function Get-OpenSreReleaseAsset {
    param(
        [Parameter(Mandatory = $true)]
        $Release,
        [Parameter(Mandatory = $true)]
        [string]$AssetName
    )

    foreach ($asset in @($Release.assets)) {
        if ([string]$asset.name -eq $AssetName) {
            return $asset
        }
    }

    return $null
}

function Resolve-OpenSreArchiveDownload {
    param(
        [Parameter(Mandatory = $true)]
        $Release,
        [Parameter(Mandatory = $true)]
        [string]$Version,
        [Parameter(Mandatory = $true)]
        [ValidateSet("release", "main")]
        [string]$Channel,
        [Parameter(Mandatory = $true)]
        [string]$TargetArch
    )

    $resolvedArch = $TargetArch
    $archiveName = Get-OpenSreArchiveName -Version $Version -Channel $Channel -TargetArch $resolvedArch
    $archiveAsset = Get-OpenSreReleaseAsset -Release $Release -AssetName $archiveName

    if (-not $archiveAsset -and $TargetArch -eq "arm64") {
        $fallbackArchiveName = Get-OpenSreArchiveName -Version $Version -Channel $Channel -TargetArch "x64"
        $fallbackAsset = Get-OpenSreReleaseAsset -Release $Release -AssetName $fallbackArchiveName

        if ($fallbackAsset) {
            $resolvedArch = "x64"
            $archiveName = $fallbackArchiveName
            $archiveAsset = $fallbackAsset
            if ($Channel -eq "main") {
                Write-Warning "Windows ARM64 artifact is not published for the main build; falling back to the x64 build."
            }
            else {
                Write-Warning "Windows ARM64 artifact is not published for v$Version; falling back to the x64 build."
            }
        }
    }

    if (-not $archiveAsset) {
        $availableAssets = @($Release.assets | ForEach-Object { [string]$_.name } | Where-Object { $_ }) -join ", "
        if ($availableAssets) {
            if ($Channel -eq "main") {
                throw "Main build release does not include asset '$archiveName'. Available assets: $availableAssets"
            }

            throw "Release v$Version does not include asset '$archiveName'. Available assets: $availableAssets"
        }

        if ($Channel -eq "main") {
            throw "Main build release does not include asset '$archiveName'."
        }

        throw "Release v$Version does not include asset '$archiveName'."
    }

    $checksumAsset = Get-OpenSreReleaseAsset -Release $Release -AssetName "$archiveName.sha256"

    return [pscustomobject]@{
        ArchiveName = $archiveName
        ArchiveUrl = [string]$archiveAsset.browser_download_url
        ChecksumName = if ($checksumAsset) { [string]$checksumAsset.name } else { "" }
        ChecksumUrl = if ($checksumAsset) { [string]$checksumAsset.browser_download_url } else { "" }
        ResolvedArch = $resolvedArch
    }
}

function Get-OpenSreExpectedSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ChecksumPath,
        [Parameter(Mandatory = $true)]
        [string]$ArchiveName
    )

    foreach ($line in Get-Content -LiteralPath $ChecksumPath) {
        if (-not $line.Trim()) {
            continue
        }

        $match = [System.Text.RegularExpressions.Regex]::Match(
            $line,
            '^(?<hash>[A-Fa-f0-9]{64})\s+\*?(?<name>.+)$'
        )

        if (-not $match.Success) {
            continue
        }

        $name = [System.IO.Path]::GetFileName($match.Groups["name"].Value.Trim())
        if ($name -eq $ArchiveName) {
            return $match.Groups["hash"].Value.ToLowerInvariant()
        }
    }

    throw "Checksum file '$ChecksumPath' does not contain a SHA256 entry for '$ArchiveName'."
}

function Normalize-OpenSrePath {
    param(
        [string]$PathValue
    )

    if (-not $PathValue) {
        return ""
    }

    $trimmedPath = $PathValue.Trim().TrimEnd("\", "/")
    if (-not $trimmedPath) {
        return ""
    }

    try {
        return [System.IO.Path]::GetFullPath($trimmedPath).TrimEnd("\", "/")
    }
    catch {
        return $trimmedPath
    }
}

function Test-OpenSreDirectoryOnPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Directory,
        [string]$PathValue = $env:PATH
    )

    if (-not $PathValue) {
        return $false
    }

    $normalizedDirectory = Normalize-OpenSrePath -PathValue $Directory

    foreach ($entry in $PathValue -split ";") {
        if (-not $entry) {
            continue
        }

        if ([string]::Equals(
                $normalizedDirectory,
                (Normalize-OpenSrePath -PathValue $entry),
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            return $true
        }
    }

    return $false
}

function Get-OpenSreBinaryPathFromArchive {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExtractionRoot,
        [Parameter(Mandatory = $true)]
        [string]$BinaryName
    )

    $directBinaryPath = Join-Path $ExtractionRoot $BinaryName
    if (Test-Path -LiteralPath $directBinaryPath -PathType Leaf) {
        return $directBinaryPath
    }

    $binaryCandidates = @(
        Get-ChildItem -LiteralPath $ExtractionRoot -Recurse -File -Filter $BinaryName
    )

    if ($binaryCandidates.Count -eq 1) {
        return $binaryCandidates[0].FullName
    }

    if ($binaryCandidates.Count -gt 1) {
        $locations = $binaryCandidates | ForEach-Object { $_.FullName }
        throw "Found multiple '$BinaryName' files after extraction: $($locations -join ', ')"
    }

    throw "Archive did not contain '$BinaryName'."
}

function Test-OpenSreManagedLayoutMarker {
    param(
        [Parameter(Mandatory = $true)]
        [string]$MarkerPath
    )

    $extendedMarkerPath = ConvertTo-OpenSreExtendedPath -Path $MarkerPath
    if (-not [System.IO.File]::Exists($extendedMarkerPath)) {
        return $false
    }

    try {
        $markerText = [System.IO.File]::ReadAllText($extendedMarkerPath).Trim()
        return $markerText -ceq $script:OpenSreLayoutMarkerText
    }
    catch {
        return $false
    }
}

function Get-OpenSreManagedLauncherText {
    return @"
@echo off
$($script:OpenSreLauncherMarker)
setlocal
set "OPENSRE_APP_ROOT=%~dp0$($script:OpenSreLayoutRootName)"
set "OPENSRE_CURRENT_FILE=%OPENSRE_APP_ROOT%\$($script:OpenSreCurrentPointerName)"
if not exist "%OPENSRE_CURRENT_FILE%" (
  echo OpenSRE installation is incomplete: missing "%OPENSRE_CURRENT_FILE%". 1>&2
  exit /b 1
)
"%SystemRoot%\System32\findstr.exe" /r /x /c:"[A-Za-z0-9][A-Za-z0-9._-]*" "%OPENSRE_CURRENT_FILE%" >nul 2>&1
if errorlevel 1 (
  echo OpenSRE installation is incomplete: invalid current version pointer. 1>&2
  exit /b 1
)
"%SystemRoot%\System32\findstr.exe" /v /r /x /c:"[A-Za-z0-9][A-Za-z0-9._-]*" "%OPENSRE_CURRENT_FILE%" >nul 2>&1
if not errorlevel 1 (
  echo OpenSRE installation is incomplete: invalid current version pointer. 1>&2
  exit /b 1
)
"%SystemRoot%\System32\findstr.exe" /v /r /x /c:"[A-Za-z0-9._-]*[A-Za-z0-9]" "%OPENSRE_CURRENT_FILE%" >nul 2>&1
if not errorlevel 1 (
  echo OpenSRE installation is incomplete: invalid current version pointer. 1>&2
  exit /b 1
)
set /p "OPENSRE_INSTALL_ID="<"%OPENSRE_CURRENT_FILE%"
if not defined OPENSRE_INSTALL_ID (
  echo OpenSRE installation is incomplete: empty current version pointer. 1>&2
  exit /b 1
)
set "OPENSRE_BINARY=%OPENSRE_APP_ROOT%\versions\%OPENSRE_INSTALL_ID%\opensre.exe"
if not exist "%OPENSRE_BINARY%" (
  echo OpenSRE installation is incomplete: missing "%OPENSRE_BINARY%". 1>&2
  exit /b 1
)
"%OPENSRE_BINARY%" %*
exit /b %ERRORLEVEL%
"@
}

function Test-OpenSreManagedLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LauncherPath
    )

    $extendedLauncherPath = ConvertTo-OpenSreExtendedPath -Path $LauncherPath
    if (-not [System.IO.File]::Exists($extendedLauncherPath)) {
        return $false
    }

    try {
        $launcherLines = @([System.IO.File]::ReadAllLines($extendedLauncherPath))
        return (
            $launcherLines.Count -ge 2 -and
            $launcherLines[0].Trim() -ieq "@echo off" -and
            $launcherLines[1].Trim() -ceq $script:OpenSreLauncherMarker
        )
    }
    catch {
        return $false
    }
}

function Test-OpenSreCanonicalLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LauncherPath,
        [Parameter(Mandatory = $true)]
        [string]$LauncherText
    )

    try {
        $actualBytes = [System.IO.File]::ReadAllBytes(
            (ConvertTo-OpenSreExtendedPath -Path $LauncherPath)
        )
        $expectedBytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($LauncherText)
        if ($actualBytes.Length -ne $expectedBytes.Length) {
            return $false
        }
        for ($index = 0; $index -lt $actualBytes.Length; $index++) {
            if ($actualBytes[$index] -ne $expectedBytes[$index]) {
                return $false
            }
        }
        return $true
    }
    catch {
        return $false
    }
}

function Write-OpenSreManagedLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [string]$InstallDir
    )

    $launcherPath = Join-Path $InstallDir "opensre.cmd"
    $extendedLauncherPath = ConvertTo-OpenSreExtendedPath -Path $launcherPath
    $launcherExists = [System.IO.File]::Exists($extendedLauncherPath)
    $launcherText = Get-OpenSreManagedLauncherText
    $previousLauncherWasCanonical = $false
    if ($launcherExists) {
        if (-not (Test-OpenSreManagedLauncher -LauncherPath $launcherPath)) {
            throw "Refusing to replace unowned launcher '$launcherPath'. Move it aside and retry."
        }
        $previousLauncherWasCanonical = Test-OpenSreCanonicalLauncher `
            -LauncherPath $launcherPath `
            -LauncherText $launcherText
    }

    $launcherTempPath = "$launcherPath.new-$([System.Guid]::NewGuid().ToString('N'))"
    $launcherBackupPath = Join-Path `
        (Join-Path $InstallDir $script:OpenSreLayoutRootName) `
        ("retired-launcher-$([System.Guid]::NewGuid().ToString('N'))")

    try {
        [System.IO.File]::WriteAllText(
            (ConvertTo-OpenSreExtendedPath -Path $launcherTempPath),
            $launcherText,
            (New-Object System.Text.UTF8Encoding($false))
        )
        if ($launcherExists) {
            [System.IO.File]::Replace(
                (ConvertTo-OpenSreExtendedPath -Path $launcherTempPath),
                $extendedLauncherPath,
                (ConvertTo-OpenSreExtendedPath -Path $launcherBackupPath),
                $true
            )
        }
        else {
            [System.IO.File]::Move(
                (ConvertTo-OpenSreExtendedPath -Path $launcherTempPath),
                $extendedLauncherPath
            )
        }
    }
    finally {
        try {
            Remove-OpenSreInstallPath -Path $launcherTempPath
        }
        catch {
            # A later install can remove an abandoned marker-owned temporary file.
        }
    }

    return [pscustomobject]@{
        Created = -not $launcherExists
        BackupPath = if ($launcherExists) { $launcherBackupPath } else { "" }
        PreviousLauncherWasCanonical = $previousLauncherWasCanonical
    }
}

function Restore-OpenSreManagedLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LauncherPath,
        [Parameter(Mandatory = $true)]
        [psobject]$Transaction
    )

    $extendedLauncherPath = ConvertTo-OpenSreExtendedPath -Path $LauncherPath
    if ([bool]$Transaction.Created) {
        if ([System.IO.File]::Exists($extendedLauncherPath)) {
            [System.IO.File]::Delete($extendedLauncherPath)
        }
        return
    }

    $backupPath = [string]$Transaction.BackupPath
    $extendedBackupPath = if ($backupPath) {
        ConvertTo-OpenSreExtendedPath -Path $backupPath
    }
    else {
        ""
    }
    if (-not [bool]$Transaction.PreviousLauncherWasCanonical) {
        if ($backupPath -and [System.IO.File]::Exists($extendedBackupPath)) {
            try {
                Remove-OpenSreInstallPath -Path $backupPath
            }
            catch {
                # A later install can remove an abandoned noncanonical backup.
            }
        }
        return
    }
    if (-not $backupPath -or -not [System.IO.File]::Exists($extendedBackupPath)) {
        throw "The previous OpenSRE launcher backup is missing."
    }
    if ([System.IO.File]::Exists($extendedLauncherPath)) {
        $discardPath = Join-Path `
            (Split-Path -Parent $backupPath) `
            ("retired-launcher-$([System.Guid]::NewGuid().ToString('N'))")
        [System.IO.File]::Replace(
            $extendedBackupPath,
            $extendedLauncherPath,
            (ConvertTo-OpenSreExtendedPath -Path $discardPath),
            $true
        )
        try {
            Remove-OpenSreInstallPath -Path $discardPath
        }
        catch {
            # A later install can remove an abandoned marker-owned backup.
        }
    }
    else {
        [System.IO.File]::Move($extendedBackupPath, $extendedLauncherPath)
    }
}

function Get-OpenSreCurrentInstallId {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LayoutRoot
    )

    $pointerPath = ConvertTo-OpenSreExtendedPath -Path (
        Join-Path $LayoutRoot $script:OpenSreCurrentPointerName
    )
    if (-not [System.IO.File]::Exists($pointerPath)) {
        return ""
    }

    $pointerText = [System.IO.File]::ReadAllText($pointerPath)
    $pointerMatch = [System.Text.RegularExpressions.Regex]::Match(
        $pointerText,
        '\A(?<id>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?:\r?\n)?\z',
        [System.Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $pointerMatch.Success) {
        throw "OpenSRE installation is incomplete: invalid current version pointer."
    }
    return $pointerMatch.Groups['id'].Value
}

function Set-OpenSreCurrentInstallId {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LayoutRoot,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$')]
        [string]$InstallId
    )

    $pointerPath = ConvertTo-OpenSreExtendedPath -Path (
        Join-Path $LayoutRoot $script:OpenSreCurrentPointerName
    )
    $pointerTempPath = ConvertTo-OpenSreExtendedPath -Path (
        Join-Path $LayoutRoot ("current-$([System.Guid]::NewGuid().ToString('N')).tmp")
    )
    $pointerBackupPath = ConvertTo-OpenSreExtendedPath -Path (
        Join-Path $LayoutRoot ("current-$([System.Guid]::NewGuid().ToString('N')).bak")
    )

    try {
        [System.IO.File]::WriteAllText(
            $pointerTempPath,
            "$InstallId$([System.Environment]::NewLine)",
            (New-Object System.Text.UTF8Encoding($false))
        )

        if ([System.IO.File]::Exists($pointerPath)) {
            [System.IO.File]::Replace($pointerTempPath, $pointerPath, $pointerBackupPath, $true)
            try {
                [System.IO.File]::Delete($pointerBackupPath)
            }
            catch {
                # A later pointer update may reuse neither random artifact name.
            }
        }
        else {
            [System.IO.File]::Move($pointerTempPath, $pointerPath)
        }
    }
    finally {
        foreach ($artifactPath in @($pointerTempPath, $pointerBackupPath)) {
            try {
                [System.IO.File]::Delete($artifactPath)
            }
            catch {
                # Activation or rollback must not be masked by artifact cleanup.
            }
        }
    }
}

function New-OpenSreInstallId {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Fa-f0-9]{64}$')]
        [string]$ArchiveSha256
    )

    $safeVersion = [System.Text.RegularExpressions.Regex]::Replace(
        $Version,
        '[^A-Za-z0-9._-]+',
        '-'
    ).Trim("-", ".")
    if (-not $safeVersion) {
        $safeVersion = "bundle"
    }

    $hashPrefix = $ArchiveSha256.Substring(0, 12).ToLowerInvariant()
    $uniqueSuffix = [System.Guid]::NewGuid().ToString("N").Substring(0, 8)
    return "$safeVersion-$hashPrefix-$uniqueSuffix"
}

function Open-OpenSreInstallLock {
    param(
        [Parameter(Mandatory = $true)]
        [string]$InstallDir,
        [int]$TimeoutSeconds = 30
    )

    $lockPath = Join-Path $InstallDir $script:OpenSreInstallLockName
    Assert-OpenSreInstallPathSafe `
        -InstallDir $InstallDir `
        -Path $lockPath `
        -Purpose "OpenSRE installation lock"
    $deadline = [System.DateTime]::UtcNow.AddSeconds($TimeoutSeconds)

    while ([System.DateTime]::UtcNow -lt $deadline) {
        try {
            $lockHandle = [System.IO.File]::Open(
                (ConvertTo-OpenSreExtendedPath -Path $lockPath),
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            Assert-OpenSreInstallPathSafe `
                -InstallDir $InstallDir `
                -Path $lockPath `
                -Purpose "OpenSRE installation lock"
            return $lockHandle
        }
        catch [System.IO.IOException] {
            Start-Sleep -Milliseconds 250
        }
    }

    throw "Timed out waiting for another OpenSRE installation to finish."
}

function Get-OpenSreObsoleteVersionPaths {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LayoutRoot,
        [Parameter(Mandatory = $true)]
        [string]$ActiveInstallId
    )

    $versionsRoot = Join-Path $LayoutRoot "versions"
    if (-not (Test-OpenSreInstallDirectoryExists -Path $versionsRoot)) {
        return @()
    }

    $paths = @()
    foreach ($versionDirectoryPath in @(
            Get-OpenSreInstallDirectoryPaths -Path $versionsRoot
        )) {
        $versionDirectoryName = [System.IO.Path]::GetFileName(
            $versionDirectoryPath.TrimEnd('\', '/')
        )
        Assert-OpenSrePathHasNoReparsePoint `
            -Path $versionDirectoryPath `
            -Purpose "OpenSRE version cleanup"
        if ($versionDirectoryName -eq $ActiveInstallId) {
            continue
        }
        $paths += $versionDirectoryPath
    }
    foreach ($ownedCleanupPath in @(
            Get-OpenSreInstallEntryPaths -Path $LayoutRoot
        )) {
        $ownedCleanupName = [System.IO.Path]::GetFileName(
            $ownedCleanupPath.TrimEnd('\', '/')
        )
        if ($ownedCleanupName -notlike 'retired-*' -and
            $ownedCleanupName -notlike 'stage-*') {
            continue
        }
        Assert-OpenSrePathHasNoReparsePoint `
            -Path $ownedCleanupPath `
            -Purpose "OpenSRE deferred cleanup"
        $paths += $ownedCleanupPath
    }
    return $paths
}

function ConvertTo-OpenSreExtendedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath.StartsWith('\\?\')) {
        return $fullPath
    }
    if ($fullPath.StartsWith('\\')) {
        return '\\?\UNC\' + $fullPath.Substring(2)
    }
    return '\\?\' + $fullPath
}

function ConvertFrom-OpenSreExtendedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ($Path.StartsWith('\\?\UNC\')) {
        return '\\' + $Path.Substring(8)
    }
    if ($Path.StartsWith('\\?\')) {
        return $Path.Substring(4)
    }
    return $Path
}

function Test-OpenSreInstallFileExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    return [System.IO.File]::Exists((ConvertTo-OpenSreExtendedPath -Path $Path))
}

function Test-OpenSreInstallDirectoryExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    return [System.IO.Directory]::Exists((ConvertTo-OpenSreExtendedPath -Path $Path))
}

function Get-OpenSreInstallFileSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $stream = [System.IO.File]::Open(
        (ConvertTo-OpenSreExtendedPath -Path $Path),
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $hashAlgorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $hashAlgorithm.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $hashAlgorithm.Dispose()
        $stream.Dispose()
    }
}

function Get-OpenSreInstallEntryPaths {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    foreach ($entryPath in [System.IO.Directory]::EnumerateFileSystemEntries(
            (ConvertTo-OpenSreExtendedPath -Path $Path)
        )) {
        ConvertFrom-OpenSreExtendedPath -Path $entryPath
    }
}

function Get-OpenSreInstallDirectoryPaths {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    foreach ($entryPath in [System.IO.Directory]::EnumerateDirectories(
            (ConvertTo-OpenSreExtendedPath -Path $Path)
        )) {
        ConvertFrom-OpenSreExtendedPath -Path $entryPath
    }
}

function Move-OpenSreInstallDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    [System.IO.Directory]::Move(
        (ConvertTo-OpenSreExtendedPath -Path $Source),
        (ConvertTo-OpenSreExtendedPath -Path $Destination)
    )
}

function Move-OpenSreInstallFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    [System.IO.File]::Move(
        (ConvertTo-OpenSreExtendedPath -Path $Source),
        (ConvertTo-OpenSreExtendedPath -Path $Destination)
    )
}

function Copy-OpenSreInstallFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    $destinationParent = [System.IO.Path]::GetDirectoryName($Destination)
    if ($destinationParent) {
        [System.IO.Directory]::CreateDirectory(
            (ConvertTo-OpenSreExtendedPath -Path $destinationParent)
        ) | Out-Null
    }
    [System.IO.File]::Copy(
        (ConvertTo-OpenSreExtendedPath -Path $Source),
        (ConvertTo-OpenSreExtendedPath -Path $Destination),
        $true
    )
}

function Copy-OpenSreInstallTree {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    $sourcePath = [System.IO.Path]::GetFullPath($Source).TrimEnd('\')
    $destinationPath = [System.IO.Path]::GetFullPath($Destination).TrimEnd('\')
    Assert-OpenSreTreeHasNoReparsePoints -Root $sourcePath
    [System.IO.Directory]::CreateDirectory(
        (ConvertTo-OpenSreExtendedPath -Path $destinationPath)
    ) | Out-Null

    $pending = New-Object System.Collections.Generic.Stack[object]
    $pending.Push([pscustomobject]@{
        Source = $sourcePath
        Destination = $destinationPath
    })
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($entry in [System.IO.Directory]::EnumerateFileSystemEntries(
                (ConvertTo-OpenSreExtendedPath -Path ([string]$directory.Source))
            )) {
            Assert-OpenSrePathHasNoReparsePoint `
                -Path $entry `
                -Purpose "OpenSRE application bundle"
            $entryName = [System.IO.Path]::GetFileName($entry)
            $destinationEntry = [System.IO.Path]::Combine(
                [string]$directory.Destination,
                $entryName
            )
            if ([System.IO.Directory]::Exists((ConvertTo-OpenSreExtendedPath -Path $entry))) {
                [System.IO.Directory]::CreateDirectory(
                    (ConvertTo-OpenSreExtendedPath -Path $destinationEntry)
                ) | Out-Null
                $pending.Push([pscustomobject]@{
                    Source = $entry
                    Destination = $destinationEntry
                })
            }
            else {
                Copy-OpenSreInstallFile -Source $entry -Destination $destinationEntry
            }
        }
    }
}

function Remove-OpenSreInstallPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $extendedPath = ConvertTo-OpenSreExtendedPath -Path $Path
    if ([System.IO.Directory]::Exists($extendedPath)) {
        Assert-OpenSreTreeHasNoReparsePoints `
            -Root $Path `
            -Purpose "OpenSRE owned cleanup path"
        [System.IO.Directory]::Delete($extendedPath, $true)
    }
    elseif ([System.IO.File]::Exists($extendedPath)) {
        Assert-OpenSrePathHasNoReparsePoint `
            -Path $Path `
            -Purpose "OpenSRE owned cleanup path"
        [System.IO.File]::Delete($extendedPath)
    }
}

function Move-OpenSreStagedBundle {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StagePath,
        [Parameter(Mandatory = $true)]
        [string]$FinalPath,
        [int]$MaxAttempts = 20,
        [int]$RetryDelayMilliseconds = 250
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            Move-OpenSreInstallDirectory -Source $StagePath -Destination $FinalPath
            return
        }
        catch {
            if (-not (Test-OpenSreInstallDirectoryExists -Path $StagePath) -and
                (Test-OpenSreInstallDirectoryExists -Path $FinalPath)) {
                return
            }

            $exception = $_.Exception
            $transientMoveFailure = $false
            while ($null -ne $exception) {
                if ($exception -is [System.UnauthorizedAccessException] -or
                    $exception -is [System.IO.IOException]) {
                    $transientMoveFailure = $true
                    break
                }
                $exception = $exception.InnerException
            }
            if (-not $transientMoveFailure -or $attempt -eq $MaxAttempts) {
                throw
            }

            Start-Sleep -Milliseconds $RetryDelayMilliseconds
        }
    }
}

function Start-OpenSreDeferredCleanup {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LayoutRoot,
        [Parameter(Mandatory = $true)]
        [string[]]$TargetPaths,
        [Parameter(Mandatory = $true)]
        [int]$ParentProcessId,
        [AllowEmptyString()]
        [string]$ParentExecutablePath = "",
        [AllowEmptyString()]
        [string]$ParentStarted = ""
    )

    if ($TargetPaths.Count -eq 0) {
        return
    }

    if ([string]$PSVersionTable.PSEdition -cne "Desktop" -or
        $PSVersionTable.PSVersion.Major -ne 5 -or
        $PSVersionTable.PSVersion.Minor -ne 1) {
        throw "Detached OpenSRE cleanup requires Windows PowerShell Desktop 5.1."
    }
    $powershellPath = Join-Path $PSHOME "powershell.exe"
    if (-not [System.IO.File]::Exists($powershellPath)) {
        throw "Windows PowerShell Desktop 5.1 was not found at '$powershellPath'."
    }

    $cleanupPath = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) ("opensre-install-cleanup-$([System.Guid]::NewGuid().ToString('N')).ps1")
    $targetJson = ConvertTo-Json -InputObject @($TargetPaths) -Compress
    $targetPayload = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($targetJson))
    $cleanupScript = @'
param(
    [int]$ParentProcessId,
    [string]$ParentExecutablePath,
    [string]$ParentStarted,
    [string]$TargetPayload,
    [string]$CleanupPath,
    [string]$LayoutRoot
)

$ErrorActionPreference = "SilentlyContinue"
if (-not ([System.Management.Automation.PSTypeName]'OpenSreCleanup.NativePathApi').Type) {
    Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace OpenSreCleanup
{
    public static class NativePathApi
    {
        private const uint Share = 0x00000001 | 0x00000002 | 0x00000004;
        private const uint OpenExisting = 3;
        private const uint BackupSemantics = 0x02000000;

        [StructLayout(LayoutKind.Sequential)]
        private struct ByHandleFileInformation
        {
            public uint Attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(
            string name, uint access, uint share, IntPtr security, uint creation,
            uint flags, IntPtr templateFile
        );

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(
            SafeFileHandle file, StringBuilder path, uint length, uint flags
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle file, out ByHandleFileInformation information
        );

        private static SafeFileHandle OpenPath(string path)
        {
            SafeFileHandle handle = CreateFile(
                path, 0, Share, IntPtr.Zero, OpenExisting, BackupSemantics, IntPtr.Zero
            );
            if (handle.IsInvalid)
            {
                handle.Dispose();
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            return handle;
        }

        public static bool SameFile(string left, string right)
        {
            using (SafeFileHandle leftHandle = OpenPath(left))
            using (SafeFileHandle rightHandle = OpenPath(right))
            {
                ByHandleFileInformation leftInfo;
                ByHandleFileInformation rightInfo;
                if (!GetFileInformationByHandle(leftHandle, out leftInfo) ||
                    !GetFileInformationByHandle(rightHandle, out rightInfo))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                return leftInfo.VolumeSerialNumber == rightInfo.VolumeSerialNumber &&
                    leftInfo.FileIndexHigh == rightInfo.FileIndexHigh &&
                    leftInfo.FileIndexLow == rightInfo.FileIndexLow;
            }
        }

        public static string GetFinalPath(string path)
        {
            using (SafeFileHandle handle = OpenPath(path))
            {
                uint capacity = 512;
                while (true)
                {
                    StringBuilder value = new StringBuilder((int)capacity);
                    uint result = GetFinalPathNameByHandle(handle, value, capacity, 0);
                    if (result == 0)
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    if (result < capacity)
                        return value.ToString();
                    capacity = result + 1;
                }
            }
        }
    }
}
"@
}
$targetsJson = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($TargetPayload)
)
$parsedTargets = ConvertFrom-Json -InputObject $targetsJson
$targets = @($parsedTargets)
$installDir = Split-Path -Parent $LayoutRoot
$installLockPath = Join-Path $installDir '.opensre-app.install.lock'

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

function Get-OpenSreCanonicalCleanupPath {
    param([string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedPath = [OpenSreCleanup.NativePathApi]::GetFinalPath(
        (ConvertTo-OpenSreExtendedPath -Path $fullPath)
    ).TrimEnd('\', '/')
    if ($resolvedPath.StartsWith('\\?\UNC\')) {
        return '\\' + $resolvedPath.Substring(8)
    }
    if ($resolvedPath.StartsWith('\\?\')) {
        return $resolvedPath.Substring(4)
    }
    return $resolvedPath
}

function Test-OpenSreCleanupTarget {
    param([string]$Path)

    try {
        if (Test-Path -LiteralPath $Path) {
            return $true
        }
    }
    catch {
        # Fall through to the extended-length path checks.
    }
    $extendedPath = ConvertTo-OpenSreExtendedPath -Path $Path
    return (
        [System.IO.Directory]::Exists($extendedPath) -or
        [System.IO.File]::Exists($extendedPath)
    )
}

function Test-OpenSreCleanupTreeSafe {
    param([string]$Path)

    try {
        $pending = New-Object System.Collections.Generic.Stack[string]
        $pending.Push([System.IO.Path]::GetFullPath($Path))
        while ($pending.Count -gt 0) {
            $entryPath = $pending.Pop()
            $extendedEntryPath = ConvertTo-OpenSreExtendedPath -Path $entryPath
            $attributes = [System.IO.File]::GetAttributes($extendedEntryPath)
            if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                return $false
            }
            if ([System.IO.Directory]::Exists($extendedEntryPath)) {
                foreach ($child in [System.IO.Directory]::EnumerateFileSystemEntries($extendedEntryPath)) {
                    $pending.Push($child)
                }
            }
        }
        return $true
    }
    catch {
        return $false
    }
}

function Remove-OpenSreCleanupTarget {
    param([string]$Path)

    if (-not (Test-OpenSreCleanupTreeSafe -Path $Path)) {
        return
    }

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

function Move-OpenSreCleanupTarget {
    param(
        [string]$Path,
        [string]$Destination,
        [switch]$TreatAsDirectory
    )

    $sourcePath = ConvertTo-OpenSreExtendedPath -Path $Path
    $destinationPath = ConvertTo-OpenSreExtendedPath -Path $Destination
    if ($TreatAsDirectory) {
        [System.IO.Directory]::Move($sourcePath, $destinationPath)
    }
    else {
        [System.IO.File]::Move($sourcePath, $destinationPath)
    }
}

function Test-OpenSrePathContains {
    param(
        [string]$Root,
        [string]$Candidate
    )

    $rootPath = (Get-OpenSreCanonicalCleanupPath -Path $Root).TrimEnd('\', '/')
    $candidatePath = Get-OpenSreCanonicalCleanupPath -Path $Candidate
    if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidatePath.StartsWith(
        $rootPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Test-OpenSreOwnedPathSafe {
    param([string]$Path)

    try {
        if (-not (Test-OpenSrePathContains -Root $LayoutRoot -Candidate $Path)) {
            return $false
        }
        $layoutPath = [System.IO.Path]::GetFullPath($LayoutRoot).TrimEnd('\', '/')
        $candidatePath = [System.IO.Path]::GetFullPath($Path)
        foreach ($rootComponent in @($installDir, $layoutPath)) {
            $rootAttributes = [System.IO.File]::GetAttributes(
                (ConvertTo-OpenSreExtendedPath -Path $rootComponent)
            )
            if (($rootAttributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                return $false
            }
        }
        $relativePath = $candidatePath.Substring($layoutPath.Length).TrimStart('\', '/')
        $componentPath = $layoutPath
        foreach ($segment in $relativePath.Split(@('\', '/'), [System.StringSplitOptions]::RemoveEmptyEntries)) {
            $componentPath = Join-Path $componentPath $segment
            if (-not (Test-OpenSreCleanupTarget -Path $componentPath)) {
                break
            }
            $attributes = [System.IO.File]::GetAttributes(
                (ConvertTo-OpenSreExtendedPath -Path $componentPath)
            )
            if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                return $false
            }
        }
        return $true
    }
    catch {
        return $false
    }
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
            try {
                if (Test-OpenSrePathContains -Root $Path -Candidate $processPath) {
                    return $true
                }
            }
            catch {
                return $true
            }
        }
        else {
            try {
                $sameFile = [OpenSreCleanup.NativePathApi]::SameFile(
                    (ConvertTo-OpenSreExtendedPath -Path $Path),
                    (ConvertTo-OpenSreExtendedPath -Path $processPath)
                )
            }
            catch {
                return $true
            }
            if ($sameFile) {
                return $true
            }
        }
    }
    return $false
}

function Test-OpenSreCurrentVersionTarget {
    param([string]$Path)

    try {
        $targetPath = [System.IO.Path]::GetFullPath($Path)
        $versionsRoot = [System.IO.Path]::GetFullPath((Join-Path $layoutRoot 'versions'))
        $targetParent = [System.IO.Path]::GetDirectoryName($targetPath)
        if (-not $targetParent.Equals($versionsRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        $pointerPath = ConvertTo-OpenSreExtendedPath -Path (
            Join-Path $LayoutRoot 'current.txt'
        )
        if (-not [System.IO.File]::Exists($pointerPath)) {
            return $true
        }
        $pointerText = [System.IO.File]::ReadAllText($pointerPath)
        $pointerMatch = [System.Text.RegularExpressions.Regex]::Match(
            $pointerText,
            '\A(?<id>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?:\r?\n)?\z',
            [System.Text.RegularExpressions.RegexOptions]::CultureInvariant
        )
        if (-not $pointerMatch.Success) {
            return $true
        }
        $currentInstallId = $pointerMatch.Groups['id'].Value
        return [System.IO.Path]::GetFileName($targetPath).Equals(
            $currentInstallId,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $true
    }
}

function Move-OpenSreTargetToRetirement {
    param([string]$Path)

    if (-not (Test-OpenSreCleanupTarget -Path $Path)) {
        return ''
    }
    if (-not (Test-OpenSreOwnedPathSafe -Path $Path) -or
        -not (Test-OpenSreCleanupTreeSafe -Path $Path)) {
        return ''
    }
    if (Test-OpenSreCurrentVersionTarget -Path $Path) {
        return ''
    }
    if (Test-OpenSreTargetInUse -Path $Path) {
        return ''
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
        $extendedGuardPath = ConvertTo-OpenSreExtendedPath -Path $guardPath
        if ([System.IO.File]::Exists($extendedGuardPath)) {
            $guard = [System.IO.File]::Open(
                $extendedGuardPath,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Delete
            )
        }
        if (Test-OpenSreTargetInUse -Path $Path) {
            return ''
        }
        if ($null -ne $guard) {
            $guard.Dispose()
            $guard = $null
        }

        $retiredPath = Join-Path $LayoutRoot (
            "retired-$([System.Guid]::NewGuid().ToString('N'))"
        )
        Move-OpenSreCleanupTarget `
            -Path $Path `
            -Destination $retiredPath `
            -TreatAsDirectory:$targetWasDirectory
        if ((Test-OpenSreTargetInUse -Path $Path -TreatAsDirectory:$targetWasDirectory) -or
            (Test-OpenSreTargetInUse -Path $retiredPath -TreatAsDirectory:$targetWasDirectory)) {
            try {
                Move-OpenSreCleanupTarget `
                    -Path $retiredPath `
                    -Destination $Path `
                    -TreatAsDirectory:$targetWasDirectory
            }
            catch {
                # Leaving the complete tree retired is safer than partial deletion.
            }
            return ''
        }
        return $retiredPath
    }
    catch {
        return ''
    }
    finally {
        if ($null -ne $guard) {
            $guard.Dispose()
        }
    }
}

function Test-OpenSreParentIdentityAlive {
    if ($ParentProcessId -le 0) {
        return $false
    }
    try {
        $parentProcess = Get-Process -Id $ParentProcessId -ErrorAction Stop
        if (-not $ParentExecutablePath -or -not $ParentStarted) {
            # Compatibility for direct calls made by older installers: waiting
            # on the PID is conservative and never authorizes deletion.
            return $true
        }
        $started = $parentProcess.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(
            [System.Globalization.CultureInfo]::InvariantCulture
        )
        $processPath = Get-OpenSreCanonicalCleanupPath -Path ([string]$parentProcess.Path)
        $expectedPath = Get-OpenSreCanonicalCleanupPath -Path $ParentExecutablePath
        $sameExecutable = [OpenSreCleanup.NativePathApi]::SameFile(
            (ConvertTo-OpenSreExtendedPath -Path $processPath),
            (ConvertTo-OpenSreExtendedPath -Path $expectedPath)
        )
        return (
            $started -ceq $ParentStarted -and
            $sameExecutable
        )
    }
    catch {
        return $false
    }
}

if ($ParentProcessId -gt 0) {
    $waitDeadline = [System.DateTime]::UtcNow.AddMinutes(10)
    while (Test-OpenSreParentIdentityAlive) {
        if ([System.DateTime]::UtcNow -ge $waitDeadline) {
            Remove-Item -LiteralPath $CleanupPath -Force -ErrorAction SilentlyContinue
            exit 1
        }
        Start-Sleep -Milliseconds 250
    }
}

$lockHandle = $null
$lockDeadline = [System.DateTime]::UtcNow.AddSeconds(30)
while ($null -eq $lockHandle -and [System.DateTime]::UtcNow -lt $lockDeadline) {
    try {
        $lockHandle = [System.IO.File]::Open(
            $installLockPath,
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
    Remove-Item -LiteralPath $CleanupPath -Force -ErrorAction SilentlyContinue
    exit 1
}
if (-not (Test-OpenSreOwnedPathSafe -Path $LayoutRoot)) {
    $lockHandle.Dispose()
    Remove-Item -LiteralPath $CleanupPath -Force -ErrorAction SilentlyContinue
    exit 1
}

$retiredTargets = @()
try {
    foreach ($targetValue in $targets) {
        $target = [string]$targetValue
        for ($retireAttempt = 0; $retireAttempt -lt 20; $retireAttempt++) {
            if (-not (Test-OpenSreCleanupTarget -Path $target) -or
                (Test-OpenSreCurrentVersionTarget -Path $target)) {
                break
            }
            $retiredPath = Move-OpenSreTargetToRetirement -Path $target
            if ($retiredPath) {
                $retiredTargets += $retiredPath
                break
            }
            Start-Sleep -Milliseconds 250
        }
    }
}
finally {
    $lockHandle.Dispose()
}

$cleanupDeadline = [System.DateTime]::UtcNow.AddMinutes(10)
do {
    foreach ($retiredTarget in $retiredTargets) {
        if (Test-OpenSreCleanupTarget -Path $retiredTarget) {
            try {
                if ((Test-OpenSreOwnedPathSafe -Path $retiredTarget) -and
                    (Test-OpenSreCleanupTreeSafe -Path $retiredTarget)) {
                    Remove-OpenSreCleanupTarget -Path $retiredTarget
                }
            }
            catch {
                # Retried only after the live path has been atomically retired.
            }
        }
    }
    $remaining = @(
        $retiredTargets | Where-Object { Test-OpenSreCleanupTarget -Path ([string]$_) }
    )
    if ($remaining.Count -eq 0) {
        break
    }
    Start-Sleep -Milliseconds 500
} while ([System.DateTime]::UtcNow -lt $cleanupDeadline)

[System.IO.File]::Delete((ConvertTo-OpenSreExtendedPath -Path $CleanupPath))
'@
    $extendedCleanupPath = ConvertTo-OpenSreExtendedPath -Path $cleanupPath
    [System.IO.File]::WriteAllText(
        $extendedCleanupPath,
        $cleanupScript,
        (New-Object System.Text.UTF8Encoding($false))
    )

    try {
        $arguments = @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            ('"{0}"' -f $cleanupPath),
            "-ParentProcessId",
            [string]$ParentProcessId,
            "-ParentExecutablePath",
            ('"{0}"' -f $ParentExecutablePath),
            "-ParentStarted",
            ('"{0}"' -f $ParentStarted),
            "-TargetPayload",
            $targetPayload,
            "-CleanupPath",
            ('"{0}"' -f $cleanupPath),
            "-LayoutRoot",
            ('"{0}"' -f $LayoutRoot)
        )
        Start-Process -FilePath $powershellPath -ArgumentList $arguments -WindowStyle Hidden | Out-Null
        return $cleanupPath
    }
    catch {
        try {
            [System.IO.File]::Delete((ConvertTo-OpenSreExtendedPath -Path $cleanupPath))
        }
        catch {
            # Preserve the cleanup-launch failure that the caller needs to report.
        }
        throw
    }
}

function Install-OpenSreVerifiedBundle {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BinaryPath,
        [Parameter(Mandatory = $true)]
        [string]$InstallDir,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$')]
        [string]$InstallId,
        [int]$ParentProcessId = 0,
        [AllowEmptyString()]
        [string]$ParentExecutablePath = "",
        [AllowEmptyString()]
        [string]$ParentStarted = "",
        [AllowEmptyString()]
        [string]$VerifiedLegacyBinaryPath = "",
        [AllowEmptyString()]
        [string]$ApprovedLegacyBinaryPath = "",
        [AllowEmptyString()]
        [string]$ApprovedLegacyBinarySha256 = ""
    )

    $prospectiveInstallDir = Get-OpenSreCanonicalPath -Path $InstallDir
    $prospectiveLauncherPath = Join-Path $prospectiveInstallDir "opensre.cmd"
    $prospectiveBinaryPath = Join-Path (
        Join-Path (
            Join-Path (
                Join-Path $prospectiveInstallDir $script:OpenSreLayoutRootName
            ) "versions"
        ) $InstallId
    ) "opensre.exe"
    foreach ($commandPath in @($prospectiveLauncherPath, $prospectiveBinaryPath)) {
        if ([System.IO.Path]::GetFullPath($commandPath).Length -gt $script:OpenSreMaxCommandPathLength) {
            throw "OpenSRE's stable Windows command path must be shorter than 260 characters: '$commandPath'. Choose a shorter install directory."
        }
    }

    if (-not (Test-OpenSreInstallFileExists -Path $BinaryPath)) {
        throw "Verified binary '$BinaryPath' no longer exists."
    }

    Assert-OpenSreTreeHasNoReparsePoints `
        -Root (Split-Path -Parent $BinaryPath) `
        -Purpose "verified OpenSRE application bundle"
    [System.IO.Directory]::CreateDirectory(
        (ConvertTo-OpenSreExtendedPath -Path $InstallDir)
    ) | Out-Null
    Assert-OpenSreAbsolutePathHasNoReparsePoints -Path $InstallDir
    $InstallDir = Get-OpenSreCanonicalPath -Path $InstallDir
    $layoutRoot = Join-Path $InstallDir $script:OpenSreLayoutRootName
    $layoutMarkerPath = Join-Path $layoutRoot $script:OpenSreLayoutMarkerName
    $installLock = Open-OpenSreInstallLock -InstallDir $InstallDir
    $versionsRoot = Join-Path $layoutRoot "versions"
    $stagePath = Join-Path $layoutRoot ("stage-$InstallId")
    $finalPath = Join-Path $versionsRoot $InstallId
    $launcherPath = Join-Path $InstallDir "opensre.cmd"
    $currentPointerPath = Join-Path $layoutRoot $script:OpenSreCurrentPointerName
    $previousInstallId = ""
    $hadCurrentPointer = $false
    $launcherTransaction = $null
    $finalPathCreated = $false
    $currentPointerActivationAttempted = $false
    $activationCommitted = $false
    $legacyBinaryPath = Join-Path $InstallDir "opensre.exe"
    $retiredLegacyPath = ""
    $cleanupTargets = @()
    $cleanupWorkerPath = ""
    $cleanupEnumerationFailed = $false
    $stagedVersionInfo = $null
    $installedBinaryPath = ""

    try {
        foreach ($ownedPath in @(
                $layoutRoot,
                $layoutMarkerPath,
                $versionsRoot,
                $stagePath,
                $finalPath,
                $launcherPath,
                $currentPointerPath,
                $legacyBinaryPath
            )) {
            Assert-OpenSreInstallPathSafe `
                -InstallDir $InstallDir `
                -Path $ownedPath
        }

        if (Test-OpenSreInstallFileExists -Path $legacyBinaryPath) {
            $resolvedLegacyBinaryPath = Get-OpenSreCanonicalPath -Path $legacyBinaryPath
            $resolvedVerifiedLegacyPath = if ($VerifiedLegacyBinaryPath) {
                Get-OpenSreCanonicalPath -Path $VerifiedLegacyBinaryPath
            }
            else {
                ""
            }
            $resolvedApprovedLegacyPath = if ($ApprovedLegacyBinaryPath) {
                Get-OpenSreCanonicalPath -Path $ApprovedLegacyBinaryPath
            }
            else {
                ""
            }
            # Replacement is authorized either by an already-running OpenSRE update
            # (process-identity handoff) or by an explicit user confirmation taken
            # before anything was downloaded. Re-checked here because the file may
            # have appeared after that decision was made.
            $verifiedLegacyAuthorized = `
                ($resolvedVerifiedLegacyPath -and (Test-OpenSreSamePath `
                    -Left $resolvedVerifiedLegacyPath `
                    -Right $resolvedLegacyBinaryPath))
            $approvedLegacyAuthorized = `
                ($resolvedApprovedLegacyPath -and
                    $ApprovedLegacyBinarySha256 -match '^[A-Fa-f0-9]{64}$' -and
                    (Test-OpenSreSamePath `
                        -Left $resolvedApprovedLegacyPath `
                        -Right $resolvedLegacyBinaryPath) -and
                    ((Get-OpenSreInstallFileSha256 `
                        -Path $resolvedLegacyBinaryPath
                    ) -ieq $ApprovedLegacyBinarySha256))
            $legacyReplacementAuthorized = `
                $verifiedLegacyAuthorized -or $approvedLegacyAuthorized
            if (-not $legacyReplacementAuthorized) {
                throw (Get-OpenSreLegacyReplacementRefusalMessage -BinaryPath $legacyBinaryPath)
            }
        }

        $layoutRootAlreadyExists = Test-OpenSreInstallDirectoryExists -Path $layoutRoot
        if ($layoutRootAlreadyExists -and
            -not (Test-OpenSreManagedLayoutMarker -MarkerPath $layoutMarkerPath)) {
            $existingEntries = @(Get-OpenSreInstallEntryPaths -Path $layoutRoot)
            if ($existingEntries.Count -gt 0) {
                throw "Refusing to use unowned application directory '$layoutRoot'. Move it aside and retry."
            }
        }

        [System.IO.Directory]::CreateDirectory(
            (ConvertTo-OpenSreExtendedPath -Path $layoutRoot)
        ) | Out-Null
        Assert-OpenSreInstallPathSafe -InstallDir $InstallDir -Path $layoutRoot
        if (-not (Test-OpenSreManagedLayoutMarker -MarkerPath $layoutMarkerPath)) {
            [System.IO.File]::WriteAllText(
                (ConvertTo-OpenSreExtendedPath -Path $layoutMarkerPath),
                "$($script:OpenSreLayoutMarkerText)$([System.Environment]::NewLine)",
                (New-Object System.Text.UTF8Encoding($false))
            )
        }

        [System.IO.Directory]::CreateDirectory(
            (ConvertTo-OpenSreExtendedPath -Path $versionsRoot)
        ) | Out-Null
        Assert-OpenSreInstallPathSafe -InstallDir $InstallDir -Path $versionsRoot
        if (Test-OpenSrePathExists -Path $stagePath) {
            throw "Staging directory '$stagePath' already exists."
        }
        if (Test-OpenSrePathExists -Path $finalPath) {
            throw "Install directory '$finalPath' already exists."
        }

        $hadCurrentPointer = Test-OpenSreInstallFileExists -Path $currentPointerPath
        if ($hadCurrentPointer) {
            $previousInstallId = Get-OpenSreCurrentInstallId -LayoutRoot $layoutRoot
        }

        [System.IO.Directory]::CreateDirectory(
            (ConvertTo-OpenSreExtendedPath -Path $stagePath)
        ) | Out-Null
        Assert-OpenSreInstallPathSafe -InstallDir $InstallDir -Path $stagePath
        $bundleSourceRoot = Split-Path -Parent $BinaryPath
        $bundleInternalPath = Join-Path $bundleSourceRoot "_internal"
        if (Test-OpenSreInstallDirectoryExists -Path $bundleInternalPath) {
            Copy-OpenSreInstallTree -Source $bundleSourceRoot -Destination $stagePath
        }
        else {
            Copy-OpenSreInstallFile `
                -Source $BinaryPath `
                -Destination (Join-Path $stagePath "opensre.exe")
        }

        $stagedBinaryPath = Join-Path $stagePath "opensre.exe"
        $stagedVersionInfo = Get-OpenSreBinaryVersionInfo -BinaryPath $stagedBinaryPath
        Test-OpenSreStagedBundle `
            -BinaryPath $stagedBinaryPath `
            -IsOnedir:(Test-OpenSreInstallDirectoryExists -Path $bundleInternalPath)
        Assert-OpenSreTreeHasNoReparsePoints `
            -Root $stagePath `
            -Purpose "staged OpenSRE application bundle"
        foreach ($activationPath in @($layoutRoot, $versionsRoot, $stagePath, $finalPath)) {
            Assert-OpenSreInstallPathSafe `
                -InstallDir $InstallDir `
                -Path $activationPath `
                -Purpose "OpenSRE bundle activation"
        }
        Move-OpenSreStagedBundle -StagePath $stagePath -FinalPath $finalPath
        $finalPathCreated = $true
        Assert-OpenSreInstallPathSafe -InstallDir $InstallDir -Path $finalPath

        foreach ($switchPath in @($layoutRoot, $finalPath, $launcherPath, $currentPointerPath)) {
            Assert-OpenSreInstallPathSafe `
                -InstallDir $InstallDir `
                -Path $switchPath `
                -Purpose "OpenSRE current-version switch"
        }
        $launcherTransaction = Write-OpenSreManagedLauncher -InstallDir $InstallDir
        $currentPointerActivationAttempted = $true
        Set-OpenSreCurrentInstallId -LayoutRoot $layoutRoot -InstallId $InstallId

        $installedBinaryPath = Join-Path $finalPath "opensre.exe"
        $launcherVersionInfo = Get-OpenSreBinaryVersionInfo -BinaryPath $launcherPath
        if ([string]$launcherVersionInfo.Text -cne [string]$stagedVersionInfo.Text) {
            throw "Installed launcher version output did not match the verified OpenSRE bundle."
        }

        if (Test-OpenSreInstallFileExists -Path $legacyBinaryPath) {
            Assert-OpenSreInstallPathSafe `
                -InstallDir $InstallDir `
                -Path $legacyBinaryPath `
                -Purpose "legacy OpenSRE retirement"
            $retiredLegacyPath = Join-Path $layoutRoot (
                "retired-$([System.Guid]::NewGuid().ToString('N'))"
            )
            Move-OpenSreInstallFile `
                -Source $legacyBinaryPath `
                -Destination $retiredLegacyPath
        }

        $activationCommitted = $true
        if ($null -ne $launcherTransaction -and [string]$launcherTransaction.BackupPath) {
            try {
                Remove-OpenSreInstallPath -Path ([string]$launcherTransaction.BackupPath)
            }
            catch {
                # A later install can remove an abandoned marker-owned backup.
            }
        }
        try {
            $cleanupTargets = @(
                Get-OpenSreObsoleteVersionPaths `
                    -LayoutRoot $layoutRoot `
                    -ActiveInstallId $InstallId
            )
        }
        catch {
            $cleanupEnumerationFailed = $true
            Write-Warning "OpenSRE was activated, but obsolete Windows files could not be enumerated; a later install will retry safe cleanup."
        }
    }
    catch {
        if ($activationCommitted) {
            throw
        }

        $currentInstallId = Get-OpenSreCurrentInstallId -LayoutRoot $layoutRoot
        if ($currentInstallId -eq $InstallId) {
            try {
                if ($hadCurrentPointer -and $previousInstallId) {
                    Set-OpenSreCurrentInstallId -LayoutRoot $layoutRoot -InstallId $previousInstallId
                }
                elseif (-not $hadCurrentPointer) {
                    [System.IO.File]::Delete(
                        (ConvertTo-OpenSreExtendedPath -Path $currentPointerPath)
                    )
                }
            }
            catch {
                Write-Warning "Could not roll back the OpenSRE current-version pointer."
            }
        }

        try {
            Remove-OpenSreInstallPath -Path $stagePath
        }
        catch {
            Write-Warning "Could not remove the failed OpenSRE staging directory; a later install will retry it."
        }
        $currentInstallId = Get-OpenSreCurrentInstallId -LayoutRoot $layoutRoot
        if ($retiredLegacyPath -and
            $currentInstallId -ne $InstallId -and
            (Test-OpenSreInstallFileExists -Path $retiredLegacyPath) -and
            -not (Test-OpenSrePathExists -Path $legacyBinaryPath)) {
            try {
                Move-OpenSreInstallFile `
                    -Source $retiredLegacyPath `
                    -Destination $legacyBinaryPath
            }
            catch {
                Write-Warning "Could not restore the previous flat OpenSRE executable."
            }
        }
        if ($finalPathCreated -and
            $currentInstallId -ne $InstallId -and
            (Test-OpenSreInstallDirectoryExists -Path $finalPath)) {
            if ($currentPointerActivationAttempted) {
                Write-Warning "The failed OpenSRE version directory was retained for a later safe cleanup because it had already been activated."
            }
            else {
                try {
                    Remove-OpenSreInstallPath -Path $finalPath
                }
                catch {
                    Write-Warning "Could not remove the failed OpenSRE version directory; a later install will retry it."
                }
            }
        }
        if ($null -ne $launcherTransaction -and
            (-not [bool]$launcherTransaction.Created -or $currentInstallId -ne $InstallId)) {
            try {
                Restore-OpenSreManagedLauncher `
                    -LauncherPath $launcherPath `
                    -Transaction $launcherTransaction
            }
            catch {
                Write-Warning "Could not restore the previous OpenSRE launcher."
            }
        }
        throw
    }
    finally {
        if ($installLock) {
            $installLock.Dispose()
        }
    }

    $deferredCleanup = $cleanupEnumerationFailed -or $cleanupTargets.Count -gt 0
    if ($cleanupTargets.Count -gt 0) {
        try {
            $cleanupWorkerPath = [string](
                Start-OpenSreDeferredCleanup `
                    -LayoutRoot $layoutRoot `
                    -TargetPaths $cleanupTargets `
                    -ParentProcessId $ParentProcessId `
                    -ParentExecutablePath $ParentExecutablePath `
                    -ParentStarted $ParentStarted
            )
        }
        catch {
            Write-Warning "OpenSRE was activated, but previous Windows files were retained for a later safe cleanup."
        }
    }

    return [pscustomobject]@{
        BinaryPath = $installedBinaryPath
        LauncherPath = $launcherPath
        AppRoot = $finalPath
        LayoutRoot = $layoutRoot
        VersionText = [string]$stagedVersionInfo.Text
        Version = [string]$stagedVersionInfo.Version
        CleanupPath = $cleanupWorkerPath
        DeferredCleanup = $deferredCleanup
    }
}

function Get-OpenSreBinaryVersionInfo {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BinaryPath
    )

    try {
        $versionOutput = & $BinaryPath --version 2>&1
        $versionExitCode = $LASTEXITCODE
    }
    catch {
        throw "Failed to execute '$BinaryPath --version'. $($_.Exception.Message)"
    }

    $versionText = ($versionOutput | Out-String).Trim()
    if ($versionExitCode -ne 0) {
        throw "Failed to execute '$BinaryPath --version' (exit $versionExitCode). $versionText"
    }
    if (-not $versionText) {
        throw "Failed to validate '$BinaryPath --version': OpenSRE returned empty output."
    }

    $openSreVersionMatch = [System.Text.RegularExpressions.Regex]::Match(
        $versionText,
        '(?i)\Aopensre,\s+version\s+[0-9][0-9A-Za-z.+_-]*(?:\s+\([^\r\n]+\))?\s*\z'
    )
    if (-not $openSreVersionMatch.Success) {
        throw "Failed to validate '$BinaryPath --version': expected valid OpenSRE version output, got '$versionText'."
    }

    $detectedVersion = ""
    $match = [System.Text.RegularExpressions.Regex]::Match($versionText, '\d{4}\.\d{1,2}\.\d{1,2}')
    if ($match.Success) {
        $detectedVersion = $match.Value
    }

    return [pscustomobject]@{
        Text = $versionText
        Version = $detectedVersion
    }
}

function Test-OpenSreStagedBundle {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BinaryPath,
        [switch]$IsOnedir
    )

    $verificationHome = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        ("opensre-install-verify-$([System.Guid]::NewGuid().ToString('N'))")
    $environmentOverrides = [ordered]@{
        OPENSRE_HOME = $verificationHome
        OPENSRE_IS_TEST = "1"
        OPENSRE_NO_TELEMETRY = "1"
        OPENSRE_ANALYTICS_DISABLED = "1"
        OPENSRE_SENTRY_DISABLED = "1"
        OPENSRE_DISABLE_KEYRING = "1"
        OPENSRE_PROJECT_ENV_PATH = Join-Path $verificationHome "no-project.env"
        GRAFANA_CONFIG_SKIP_ENV_FILE = "1"
    }
    $savedEnvironment = @{}
    foreach ($name in $environmentOverrides.Keys) {
        $savedEnvironment[$name] = [System.Environment]::GetEnvironmentVariable(
            $name,
            [System.EnvironmentVariableTarget]::Process
        )
    }

    try {
        New-Item -ItemType Directory -Force -Path $verificationHome | Out-Null
        foreach ($name in $environmentOverrides.Keys) {
            [System.Environment]::SetEnvironmentVariable(
                $name,
                [string]$environmentOverrides[$name],
                [System.EnvironmentVariableTarget]::Process
            )
        }

        $smokeArguments = if ($IsOnedir) { @("--help", "_package-smoke") } else { @("--help") }
        foreach ($smokeArgument in $smokeArguments) {
            try {
                $smokeOutput = & $BinaryPath $smokeArgument 2>&1
                $smokeExitCode = $LASTEXITCODE
            }
            catch {
                throw "Failed to execute staged OpenSRE bundle '$smokeArgument' check. $($_.Exception.Message)"
            }
            $smokeText = ($smokeOutput | Out-String).Trim()
            if ($smokeExitCode -ne 0) {
                throw "Staged OpenSRE bundle '$smokeArgument' check failed (exit $smokeExitCode). $smokeText"
            }

            if ($smokeArgument -cne "_package-smoke") {
                continue
            }
            try {
                $smokeResult = ConvertFrom-Json -InputObject $smokeText
            }
            catch {
                throw "Staged OpenSRE package smoke returned invalid JSON."
            }
            if ($null -eq $smokeResult -or [string]$smokeResult.status -cne "ok") {
                throw "Staged OpenSRE package smoke did not report status 'ok'."
            }
        }
    }
    finally {
        foreach ($name in $environmentOverrides.Keys) {
            [System.Environment]::SetEnvironmentVariable(
                $name,
                $savedEnvironment[$name],
                [System.EnvironmentVariableTarget]::Process
            )
        }
        try {
            Remove-OpenSreInstallPath -Path $verificationHome
        }
        catch {
            # Verification state is isolated and never part of the installed bundle.
        }
    }
}

function Invoke-OpenSreFirstLaunchWarmup {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BinaryPath
    )

    # The staged check is strict. Running the package smoke again from its final
    # location warms Defender's scan cache for the path the user will execute.
    Write-OpenSreLine -Message "Preparing OpenSRE for first launch" -Color "Cyan"
    try {
        $null = & $BinaryPath _package-smoke 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-OpenSreLine -Message "  OK Preparing OpenSRE for first launch" -Color "Green"
            return
        }
    }
    catch {
        # Fall through to the warning below.
    }
    Write-Warning "First-launch warm-up did not complete; the first opensre may start slowly."
}

function Ensure-OpenSreGithubCli {
    # Soft dependency for github_cli chat tools. Never fails the OpenSRE install.
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        return
    }

    $skip = [string]$env:OPENSRE_SKIP_GH_INSTALL
    if ($skip -eq "1" -or $skip -eq "true" -or $skip -eq "TRUE" -or $skip -eq "yes" -or $skip -eq "YES" -or $skip -eq "on" -or $skip -eq "ON") {
        Write-Warning "GitHub CLI (gh) is not on PATH; skipped install because OPENSRE_SKIP_GH_INSTALL is set."
        Write-Warning "Install manually: winget install --id GitHub.cli  (or https://cli.github.com/)"
        return
    }

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-OpenSreLine -Message "Installing GitHub CLI (gh) for OpenSRE GitHub tools" -Color "Cyan"
        try {
            winget install --id GitHub.cli --exact --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                Write-OpenSreLine -Message "  OK Installed GitHub CLI (gh) via winget" -Color "Green"
                return
            }
        }
        catch {
            # Soft dependency - fall through to the manual hint.
        }
    }

    Write-Warning "Install manually: winget install --id GitHub.cli  (or https://cli.github.com/) for OpenSRE GitHub chat tools."
}

function Test-OpenSreAutoLaunchEnabled {
    $value = [string]$env:OPENSRE_AUTO_LAUNCH
    return -not ($value -eq "0" -or $value -eq "false" -or $value -eq "FALSE" -or $value -eq "no" -or $value -eq "NO" -or $value -eq "off" -or $value -eq "OFF")
}

function Get-OpenSreCommandName {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BinaryName
    )

    return [System.IO.Path]::GetFileNameWithoutExtension($BinaryName)
}

function Start-OpenSreOnboardingAfterInstall {
    param(
        [string]$BinaryPath,
        [string]$DisplayName
    )

    if (-not (Test-OpenSreAutoLaunchEnabled) -or -not (Test-OpenSreInteractiveHost)) {
        return
    }

    # If stdin is redirected (e.g. the installer is piped), the full-screen
    # onboarding prompt cannot take control of the terminal and exits with a
    # terminal I/O error mid-render (issue #3273). Skip the auto-launch; the
    # "Next steps" output already tells the user to run onboarding themselves.
    try {
        if ([System.Console]::IsInputRedirected) {
            return
        }
    }
    catch {
        return
    }

    if (-not (Test-OpenSreInstallFileExists -Path $BinaryPath)) {
        Write-Warning "Could not auto-launch onboarding; $BinaryPath was not found."
        return
    }

    Write-Host "Launching $DisplayName setup..."
    & $BinaryPath setup
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Setup exited before completion. Run '$DisplayName setup' to retry."
    }
}

function Install-OpenSre {
    $repo = if ($env:OPENSRE_INSTALL_REPO) { $env:OPENSRE_INSTALL_REPO } else { "Tracer-Cloud/opensre" }
    $installContext = Resolve-OpenSreInstallContext
    $installDir = [string]$installContext.InstallDir
    $updateParentProcessId = [int]$installContext.ParentProcessId
    $updateParentExecutablePath = [string]$installContext.ParentExecutablePath
    $updateParentStarted = [string]$installContext.ParentStarted
    $verifiedLegacyBinaryPath = [string]$installContext.LegacyBinaryPath
    $binaryName = "opensre.exe"
    $requestedVersion = if ($env:OPENSRE_VERSION) { $env:OPENSRE_VERSION.Trim().TrimStart("v") } else { "" }
    $resolvedChannel = if ($Channel) { $Channel.Trim().ToLowerInvariant() } else { "release" }
    $channelExplicit = [bool]$script:OpenSreChannelExplicit

    if ($requestedVersion -and $resolvedChannel -eq "main" -and -not $channelExplicit) {
        $resolvedChannel = "release"
    }

    Write-OpenSreHeader -Channel $resolvedChannel -RequestedVersion $requestedVersion -InstallDir $installDir -Repo $repo

    # Decide about a pre-existing flat executable before downloading anything, so a
    # declined or unattended reinstall leaves the executable and layout untouched.
    $existingFlatBinary = Join-Path $installDir $binaryName
    $approvedLegacyBinaryPath = ""
    $approvedLegacyBinarySha256 = ""
    if ((Test-OpenSreInstallFileExists -Path $existingFlatBinary) -and
        -not $verifiedLegacyBinaryPath) {
        Assert-OpenSreAbsolutePathHasNoReparsePoints `
            -Path $existingFlatBinary `
            -Purpose "pre-existing OpenSRE executable"
        $existingFlatBinarySha256 = Get-OpenSreInstallFileSha256 `
            -Path $existingFlatBinary
        if (Confirm-OpenSreLegacyBinaryReplacement -BinaryPath $existingFlatBinary) {
            $approvedLegacyBinaryPath = Get-OpenSreCanonicalPath -Path $existingFlatBinary
            $approvedLegacyBinarySha256 = $existingFlatBinarySha256
        }
        else {
            throw (Get-OpenSreLegacyReplacementRefusalMessage -BinaryPath $existingFlatBinary)
        }
    }

    Enable-OpenSreTls

    $targetArch = Resolve-OpenSreWindowsArchitecture
    $metadataStepName = ""
    if ($resolvedChannel -eq "main") {
        $metadataStepName = "[1/6] Fetching latest main build metadata"
    }
    elseif ($requestedVersion) {
        $metadataStepName = "[1/6] Fetching release metadata for v$requestedVersion"
    }
    else {
        $metadataStepName = "[1/6] Fetching latest release version"
    }
    $releaseMetadata = Invoke-OpenSreStep -Name $metadataStepName -Operation {
        Get-OpenSreReleaseMetadata -Repo $repo -Channel $resolvedChannel -RequestedVersion $requestedVersion
    }
    $version = [string]$releaseMetadata.Version

    $assetStepName = if ($resolvedChannel -eq "main") {
        "[2/6] Preparing opensre main build (windows/$targetArch)"
    }
    else {
        "[2/6] Preparing opensre v$version (windows/$targetArch)"
    }
    $downloadPlan = Invoke-OpenSreStep -Name $assetStepName -Operation {
        Resolve-OpenSreArchiveDownload -Release $releaseMetadata.Release -Version $version -Channel $resolvedChannel -TargetArch $targetArch
    }
    $archive = [string]$downloadPlan.ArchiveName
    $downloadUrl = [string]$downloadPlan.ArchiveUrl
    $checksumUrl = [string]$downloadPlan.ChecksumUrl
    $resolvedArch = [string]$downloadPlan.ResolvedArch
    $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("opensre-install-" + [System.Guid]::NewGuid().ToString("N"))

    New-Item -ItemType Directory -Path $tmpDir | Out-Null

    try {
        $archivePath = Join-Path $tmpDir $archive
        $checksumPath = "$archivePath.sha256"
        $extractionRoot = Join-Path $tmpDir "extracted"

        if ($resolvedArch -ne $targetArch) {
            Write-OpenSreDetail -Message "Using release asset built for windows/$resolvedArch."
        }

        Invoke-OpenSreStep -Name "[3/6] Downloading release archive" -Operation {
            Invoke-OpenSreDownloadFileWithProgress -Uri $downloadUrl -OutFile $archivePath -Label $archive
        }

        if ($checksumUrl) {
            $checksumName = [string]$downloadPlan.ChecksumName
            Invoke-OpenSreStep -Name "[4/6] Downloading and verifying checksum" -Operation {
                Invoke-OpenSreDownloadFileWithProgress -Uri $checksumUrl -OutFile $checksumPath -Label $checksumName

                $expectedHash = Get-OpenSreExpectedSha256 -ChecksumPath $checksumPath -ArchiveName $archive
                $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()

                if ($actualHash -ne $expectedHash) {
                    throw "Checksum verification failed for '$archive'. Expected '$expectedHash' but got '$actualHash'."
                }
            }
        }
        else {
            if ($resolvedChannel -eq "main") {
                throw "Main build release is missing required checksum asset '$archive.sha256'."
            }
            else {
                throw "Release v$version is missing required checksum asset '$archive.sha256'."
            }
        }

        $archiveSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()

        $verifiedBinary = Invoke-OpenSreStep -Name "[5/6] Extracting and verifying binary" -Operation {
            Expand-Archive `
                -LiteralPath $archivePath `
                -DestinationPath $extractionRoot `
                -Force

            $binaryPath = Get-OpenSreBinaryPathFromArchive `
                -ExtractionRoot $extractionRoot `
                -BinaryName $binaryName
            $binaryInternalPath = Join-Path (Split-Path -Parent $binaryPath) "_internal"
            if (-not (Test-Path -LiteralPath $binaryInternalPath -PathType Container)) {
                throw "Windows release archive did not contain the complete OpenSRE onedir bundle."
            }
            Assert-OpenSreTreeHasNoReparsePoints `
                -Root (Split-Path -Parent $binaryPath) `
                -Purpose "extracted OpenSRE release bundle"
            $binaryVersionInfo = Get-OpenSreBinaryVersionInfo -BinaryPath $binaryPath
            Test-OpenSreStagedBundle -BinaryPath $binaryPath -IsOnedir
            $binaryVersionText = [string]$binaryVersionInfo.Text
            $binaryVersion = [string]$binaryVersionInfo.Version
            $installVersion = $version

            if ($resolvedChannel -ne "main" -and $binaryVersionText -notmatch [Regex]::Escape($version)) {
                if ($requestedVersion) {
                    throw "Downloaded binary version mismatch. Expected '$version' but got '$binaryVersionText'."
                }

                if (-not $binaryVersion) {
                    throw "Downloaded binary version mismatch. Expected '$version' but got '$binaryVersionText'."
                }

                Write-Warning "Latest release metadata reports v$version, but the downloaded binary reports v$binaryVersion. Installing the verified binary anyway."
                $installVersion = $binaryVersion
            }

            return [pscustomobject]@{
                Path = $binaryPath
                VersionText = $binaryVersionText
                Version = $binaryVersion
                InstallVersion = $installVersion
            }
        }

        $binaryPath = [string]$verifiedBinary.Path
        $binaryVersionText = [string]$verifiedBinary.VersionText
        $binaryVersion = [string]$verifiedBinary.Version
        $version = [string]$verifiedBinary.InstallVersion
        $installId = New-OpenSreInstallId -Version $version -ArchiveSha256 $archiveSha256

        $installedBundle = Invoke-OpenSreStep -Name "[6/6] Installing application bundle" -Detail (Join-Path $installDir "opensre.cmd") -Operation {
            Install-OpenSreVerifiedBundle `
                -BinaryPath $binaryPath `
                -InstallDir $installDir `
                -InstallId $installId `
                -ParentProcessId $updateParentProcessId `
                -ParentExecutablePath $updateParentExecutablePath `
                -ParentStarted $updateParentStarted `
                -VerifiedLegacyBinaryPath $verifiedLegacyBinaryPath `
                -ApprovedLegacyBinaryPath $approvedLegacyBinaryPath `
                -ApprovedLegacyBinarySha256 $approvedLegacyBinarySha256
        }

        $installedBinaryPath = [string]$installedBundle.BinaryPath
        $installedLauncherPath = [string]$installedBundle.LauncherPath
        $deferredCleanup = [bool]$installedBundle.DeferredCleanup
    }
    finally {
        Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    if ($resolvedChannel -eq "main") {
        if ($binaryVersion) {
            Write-Host "Installed opensre main build ($binaryVersion) to $installedLauncherPath"
        }
        else {
            Write-Host "Installed opensre main build to $installedLauncherPath"
        }
    }
    else {
        Write-Host "Installed opensre $version to $installedLauncherPath"
    }

    if ($deferredCleanup) {
        Write-Host "Previous Windows files are pending safe cleanup; a later install can retry retained files."
    }

    if (-not (Test-OpenSreDirectoryOnPath -Directory $installDir)) {
        Write-Warning "Add $installDir to your PATH to run opensre from any terminal."
    }

    Ensure-OpenSreGithubCli
    Invoke-OpenSreFirstLaunchWarmup -BinaryPath $installedBinaryPath

    $exe = Get-OpenSreCommandName -BinaryName $binaryName
    $sep = "--------------------------------------------"

    Write-Host ""
    Write-Host $sep
    if ($resolvedChannel -eq "main") {
        if ($binaryVersion) {
            Write-Host "  opensre main build ($binaryVersion) installed successfully"
        }
        else {
            Write-Host "  opensre main build installed successfully"
        }
    }
    else {
        Write-Host "  opensre v$version installed successfully"
    }
    Write-Host $sep
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Run  $exe setup"
    Write-Host "     Sign in or create an OpenSRE account, then open the interactive shell."
    Write-Host ""
    Write-Host "  2. Run  $exe  (no subcommand)"
    Write-Host "     This starts the account gate first, then opens the interactive shell."
    Write-Host ""
    Write-Host "  3. Optional - one-shot RCA from a file:"
    Write-Host "     $exe investigate -i path/to/alert.json"
    Write-Host ""
    Write-Host "Docs: https://www.opensre.com/docs"
    Write-Host ""

    if (-not [bool]$installContext.IsUpdate -and -not $deferredCleanup) {
        Start-OpenSreOnboardingAfterInstall -BinaryPath $installedBinaryPath -DisplayName $exe
    }
}

if (-not $SkipMain) {
    Install-OpenSre
}
