<#
Creates an isolated runtime under a supplied workspace. Does not alter existing Python
environments or machine PATH and never downloads model weights. FFmpeg ZIPs should come
from https://ffmpeg.org/download.html (Windows builds linked by the FFmpeg project).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Workspace,
    [string]$Python,
    [string]$FfmpegArchive,
    [string]$FfmpegSha256
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$workspacePath = [IO.Path]::GetFullPath($Workspace)
if ($workspacePath.TrimEnd('\', '/') -eq [IO.Path]::GetPathRoot($workspacePath).TrimEnd('\', '/')) {
    throw 'Workspace must be a project directory, not a drive root.'
}
if ($workspacePath.TrimEnd('\', '/') -eq [Environment]::GetFolderPath('UserProfile').TrimEnd('\', '/')) {
    throw 'Choose a project directory instead of the home directory.'
}
New-Item -ItemType Directory -Path $workspacePath -Force | Out-Null
$runtimePath = Join-Path $workspacePath '.douyin-notes-runtime'
$markerPath = Join-Path $runtimePath 'runtime-owner.txt'
$venvPath = Join-Path $runtimePath 'venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
if ((Test-Path -LiteralPath $runtimePath) -and -not (Test-Path -LiteralPath $markerPath)) {
    throw "Existing directory has no skill ownership marker; choose a different workspace: $runtimePath"
}
if ((Test-Path -LiteralPath $markerPath) -and (Get-Content -LiteralPath $markerPath -Raw).Trim() -ne 'douyin-video-notes isolated runtime') {
    throw "Existing runtime ownership marker belongs to another tool: $runtimePath"
}
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
'douyin-video-notes isolated runtime' | Set-Content -LiteralPath $markerPath -Encoding utf8

if (-not $Python) {
    $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
    if ($pythonCommand) { $Python = $pythonCommand.Source }
    else { throw 'Python 3.11 or 3.12 is required by these pinned packages. Pass its path with -Python.' }
}
& $Python -c 'import sys; assert (3, 11) <= sys.version_info[:2] <= (3, 12), "Use Python 3.11 or 3.12 with these pinned packages"'
if ($LASTEXITCODE -ne 0) { throw 'Python version check failed.' }
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated Python environment.' }
}

$probe = 'import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(x) for x in ["faster_whisper", "ctranslate2", "numpy", "PIL", "yt_dlp", "requests"]) else 1)'
& $venvPython -c $probe
if ($LASTEXITCODE -ne 0) {
    # Known baseline versions. Site extractor updates can be made explicitly inside this venv.
    $requirementsPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'requirements.txt'
    if (-not (Test-Path -LiteralPath $requirementsPath)) { throw "Missing requirements file: $requirementsPath" }
    & $venvPython -m pip install --disable-pip-version-check -r $requirementsPath
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; rerun after restoring package access.' }
}

$ffmpegPath = $null
$ffprobePath = $null
if ($FfmpegArchive) {
    $archivePath = (Resolve-Path -LiteralPath $FfmpegArchive).Path
    if ([IO.Path]::GetExtension($archivePath) -ne '.zip') { throw '-FfmpegArchive must be a ZIP archive.' }
    if ($FfmpegSha256) {
        $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
        if ($actualHash -ne $FfmpegSha256) { throw 'FFmpeg archive SHA256 does not match.' }
    }
    $ffmpegRoot = Join-Path $runtimePath 'ffmpeg'
    if (Test-Path -LiteralPath $ffmpegRoot) {
        throw "FFmpeg directory already exists; reuse its executables: $ffmpegRoot"
    }
    # Validate member paths before extracting; nothing may leave the isolated directory.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $prefix = [IO.Path]::GetFullPath($ffmpegRoot).TrimEnd('\') + '\'
        foreach ($entry in $archive.Entries) {
            $targetPath = [IO.Path]::GetFullPath((Join-Path $ffmpegRoot $entry.FullName))
            if (-not $targetPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Archive contains a path outside the target directory: $($entry.FullName)"
            }
        }
    } finally { $archive.Dispose() }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $ffmpegRoot
    $ffmpegFiles = @(Get-ChildItem -LiteralPath $ffmpegRoot -Filter ffmpeg.exe -File -Recurse)
    $ffprobeFiles = @(Get-ChildItem -LiteralPath $ffmpegRoot -Filter ffprobe.exe -File -Recurse)
    if ($ffmpegFiles.Count -ne 1 -or $ffprobeFiles.Count -ne 1) { throw 'Archive must contain one ffmpeg.exe and one ffprobe.exe.' }
    $ffmpegPath = $ffmpegFiles[0].FullName
    $ffprobePath = $ffprobeFiles[0].FullName
}

$discovery = Join-Path $PSScriptRoot 'runtime_support.py'
if ($ffmpegPath) {
    # Command-specific configuration file; no environment or PATH mutation.
    @{ python = $venvPython; ffmpeg = $ffmpegPath; ffprobe = $ffprobePath } | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $runtimePath 'runtime.json') -Encoding utf8
}
$discoveryArgs = @('--runtime-dir', $runtimePath)
if ($ffmpegPath) { $discoveryArgs += @('--ffmpeg', $ffmpegPath, '--ffprobe', $ffprobePath) }
& $venvPython $discovery @discoveryArgs
if ($LASTEXITCODE -ne 0) { throw 'Runtime inspection failed.' }
Write-Host "Isolated Python: $venvPython"
if ($ffmpegPath) {
    Write-Host "FFmpeg: $ffmpegPath"
    Write-Host "FFprobe: $ffprobePath"
    Write-Host 'Pass these paths with --ffmpeg and --ffprobe, or set DOUYIN_NOTES_FFMPEG / DOUYIN_NOTES_FFPROBE for this session.'
} else {
    Write-Host 'If FFmpeg is missing, get a Windows ZIP via https://ffmpeg.org/download.html and rerun with -FfmpegArchive. Existing FFmpeg is reused.'
}
Write-Host "Project runtime: $runtimePath"
Write-Host 'Run commands from the workspace, or set DOUYIN_NOTES_RUNTIME to the project runtime directory.'
Write-Host "Default model directory: $(Join-Path $runtimePath 'models\whisper-small')"
Write-Host 'Model weights are not downloaded. Supply a complete local CTranslate2 Whisper model with --model or DOUYIN_NOTES_MODEL.'
