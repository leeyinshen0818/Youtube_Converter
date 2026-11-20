# Build the Windows app: dist\YouTubeMP3Converter\ (onedir) and
# dist\YouTubeMP3Converter-<version>.zip.
#
# Needs: .venv with Python, and ffmpeg, ffprobe and node on PATH.
# Downloads the latest official yt-dlp.exe (verified against SHA2-256SUMS).
# Never bundles cookies.txt or anything from %LOCALAPPDATA%.

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
$helper = Join-Path $projectDir "scripts\build_support.py"
$assets = Join-Path $projectDir ".build-assets"
$mediaDir = Join-Path $assets "bin"          # ffmpeg, ffprobe, node copied from PATH
$ytdlpDir = Join-Path $assets "ytdlp"        # freshly downloaded yt-dlp.exe
$iconPng = Join-Path $projectDir "icon\icon1.png"
$iconIco = Join-Path $assets "app.ico"
$versionFile = Join-Path $projectDir "windows_version_info.txt"
# Kept inside .build-assets so an old build\ folder or .spec file is never touched.
$workDir = Join-Path $assets "pyinstaller-work"
$specDir = Join-Path $assets "pyinstaller-spec"
$distRoot = Join-Path $projectDir "dist"
$appName = "YouTubeMP3Converter"
$appDir = Join-Path $distRoot $appName

function Invoke-Checked([string]$what, [scriptblock]$block) {
    & $block
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." }
}

if (-not (Test-Path $python)) { throw "Project virtual environment not found. Create .venv first (see README)." }
if (-not (Test-Path $iconPng)) { throw "App logo not found at icon\icon1.png." }

Invoke-Checked "Installing requirements" {
    & $python -m pip install -r (Join-Path $projectDir "requirements.txt") -r (Join-Path $projectDir "requirements-build.txt")
}

$version = (& $python $helper version).Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Could not read APP_VERSION from core\version.py." }
Write-Host "Building $appName $version" -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $mediaDir, $ytdlpDir | Out-Null

# ffmpeg, ffprobe, node: copy the real exe behind any PATH entry (WinGet uses links).
foreach ($toolName in @("ffmpeg", "ffprobe", "node")) {
    $command = Get-Command $toolName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { throw "$toolName was not found on PATH. Install it before building." }
    $item = Get-Item $command.Source
    $source = if ($item.LinkType -eq "SymbolicLink") { $item.Target[0] } else { $item.FullName }
    Copy-Item -LiteralPath $source -Destination (Join-Path $mediaDir "$toolName.exe") -Force
}

# yt-dlp.exe: latest official release; the helper exits 1 on any hash or version problem.
Invoke-Checked "Downloading and verifying yt-dlp.exe" { & $python $helper ytdlp $ytdlpDir }
Invoke-Checked "Creating the icon" { & $python $helper icon $iconPng $iconIco }
Invoke-Checked "Writing the version resource" { & $python $helper version-info $versionFile }

# Qt modules the app never imports (it uses QtCore, QtGui and QtWidgets only).
$excluded = @(
    "tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDBus",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtXml", "PySide6.QtPrintSupport", "PySide6.QtConcurrent", "PySide6.QtDesigner",
    "PySide6.QtUiTools", "PySide6.QtHelp", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtBluetooth",
    "PySide6.QtPositioning", "PySide6.QtSerialPort", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech"
)
$excludeArgs = foreach ($name in $excluded) { "--exclude-module"; $name }

Invoke-Checked "PyInstaller" {
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name $appName `
        --distpath $distRoot `
        --workpath $workDir `
        --specpath $specDir `
        --icon $iconIco `
        --version-file $versionFile `
        --add-data "$iconIco;icon" `
        --add-data "$iconPng;icon" `
        @excludeArgs `
        (Join-Path $projectDir "main.py")
}
# ui\app.py uses icon\app.ico for the window (icon1.png is its fallback).
$bundledIcon = Join-Path $appDir "_internal\icon\app.ico"
if (-not (Test-Path $bundledIcon)) { throw "Bundled icon missing: $bundledIcon" }

# Tools go in bin\ next to the exe (core\tools.py looks in <app>\bin first).
$binDir = Join-Path $appDir "bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
foreach ($tool in @(
    (Join-Path $mediaDir "ffmpeg.exe"), (Join-Path $mediaDir "ffprobe.exe"),
    (Join-Path $mediaDir "node.exe"), (Join-Path $ytdlpDir "yt-dlp.exe")
)) {
    if (-not (Test-Path $tool)) { throw "Missing tool: $tool" }
    Copy-Item -LiteralPath $tool -Destination $binDir -Force
}
foreach ($name in @("ffmpeg.exe", "ffprobe.exe", "node.exe", "yt-dlp.exe")) {
    if (-not (Test-Path (Join-Path $binDir $name))) { throw "bin\$name is missing from the build." }
}
foreach ($leak in @("cookies.txt", "settings.json")) {
    $found = Get-ChildItem -Path $appDir -Recurse -Filter $leak -ErrorAction SilentlyContinue
    if ($found) { throw "Refusing to ship $leak found in the build: $($found[0].FullName)" }
}

$zip = Join-Path $distRoot "$appName-$version.zip"
Invoke-Checked "Creating the zip" { & $python $helper zip $appDir $zip }

$folderBytes = (Get-ChildItem -Path $appDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ""
Write-Host ("App folder: {0} ({1:N1} MB)" -f $appDir, ($folderBytes / 1MB)) -ForegroundColor Green
Write-Host ("Zip:        {0} ({1:N1} MB)" -f $zip, ((Get-Item $zip).Length / 1MB)) -ForegroundColor Green
