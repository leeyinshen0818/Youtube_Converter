# Build the Windows app as ONE file: dist\YouTubeMP3Converter.exe
# (plus dist\YouTubeMP3Converter.exe.sha256).
#
# Needs: .venv with Python, node on PATH, and ffmpeg/ffprobe either in
# .build-assets\ffmpeg-essentials\ (preferred, much smaller) or on PATH.
# Downloads the latest official yt-dlp.exe (verified against SHA2-256SUMS).
# Never bundles cookies.txt or anything from %LOCALAPPDATA%.

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
$helper = Join-Path $projectDir "scripts\build_support.py"
$assets = Join-Path $projectDir ".build-assets"
$essentialsDir = Join-Path $assets "ffmpeg-essentials"   # optional, placed by hand
$ytdlpDir = Join-Path $assets "ytdlp"                    # freshly downloaded yt-dlp.exe
$logo = Join-Path $projectDir "icon\logo.png"
$iconIco = Join-Path $assets "app.ico"
$iconSheet = Join-Path $projectDir "screenshots\icon_sheet.png"
$versionFile = Join-Path $projectDir "windows_version_info.txt"
# Kept inside .build-assets so an old build\ folder or .spec file is never touched.
$workDir = Join-Path $assets "pyinstaller-work"
$specDir = Join-Path $assets "pyinstaller-spec"
$distRoot = Join-Path $projectDir "dist"
$appName = "YouTubeMP3Converter"
$exe = Join-Path $distRoot "$appName.exe"
$hashFile = "$exe.sha256"

function Invoke-Checked([string]$what, [scriptblock]$block) {
    & $block
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." }
}

function Resolve-OnPath([string]$name) {
    $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { throw "$name was not found on PATH. Install it before building." }
    $item = Get-Item $command.Source
    # WinGet puts links on PATH; bundle the real file behind them.
    if ($item.LinkType -eq "SymbolicLink") { return (Get-Item $item.Target[0]).FullName }
    return $item.FullName
}

if (-not (Test-Path $python)) { throw "Project virtual environment not found. Create .venv first (see README)." }
if (-not (Test-Path $logo)) { throw "App logo not found at icon\logo.png." }

Invoke-Checked "Installing requirements" {
    & $python -m pip install -r (Join-Path $projectDir "requirements.txt") -r (Join-Path $projectDir "requirements-build.txt")
}

$version = (& $python $helper version).Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Could not read APP_VERSION from core\version.py." }
Write-Host "Building $appName $version (one file)" -ForegroundColor Cyan

# --- tools ------------------------------------------------------------------
$essentialFfmpeg = Join-Path $essentialsDir "ffmpeg.exe"
$essentialFfprobe = Join-Path $essentialsDir "ffprobe.exe"
if ((Test-Path $essentialFfmpeg) -and (Test-Path $essentialFfprobe)) {
    $ffmpeg = $essentialFfmpeg
    $ffprobe = $essentialFfprobe
} else {
    Write-Warning ("ffmpeg.exe and ffprobe.exe were not both found in $essentialsDir. " +
        "Using the copies on PATH instead; if those are 'full' builds the exe will be MUCH larger " +
        "(about 200 MB each instead of about 100 MB). See README, 'Build the Windows app', step 2.")
    $ffmpeg = Resolve-OnPath "ffmpeg"
    $ffprobe = Resolve-OnPath "ffprobe"
}
$node = Resolve-OnPath "node"

New-Item -ItemType Directory -Force -Path $ytdlpDir | Out-Null
# yt-dlp.exe: latest official release; the helper exits 1 on any hash or version problem.
Invoke-Checked "Downloading and verifying yt-dlp.exe" { & $python $helper ytdlp $ytdlpDir }
$ytdlp = Join-Path $ytdlpDir "yt-dlp.exe"

$tools = [ordered]@{ "ffmpeg.exe" = $ffmpeg; "ffprobe.exe" = $ffprobe; "node.exe" = $node; "yt-dlp.exe" = $ytdlp }
Write-Host ""
Write-Host "Tools embedded in bin\:" -ForegroundColor Cyan
foreach ($entry in $tools.GetEnumerator()) {
    if (-not (Test-Path $entry.Value)) { throw "Missing tool: $($entry.Value)" }
    $file = Get-Item $entry.Value
    if ($file.Name -ne $entry.Key) { throw "Expected a file named $($entry.Key), got $($file.FullName)" }
    Write-Host ("  {0,-12} {1,8:N1} MB  {2}" -f $entry.Key, ($file.Length / 1MB), $file.FullName)
}
Write-Host ""

# --- icon and version resource ------------------------------------------------
Invoke-Checked "Creating the icon" { & $python $helper icon $logo $iconIco }
Invoke-Checked "Rendering the icon sheet" { & $python $helper icon-sheet $iconIco $iconSheet }
Invoke-Checked "Writing the version resource" { & $python $helper version-info $versionFile }

# --- PyInstaller ----------------------------------------------------------------
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
$binaryArgs = foreach ($path in $tools.Values) { "--add-binary"; "$path;bin" }

foreach ($old in @($exe, $hashFile)) { if (Test-Path $old) { Remove-Item -LiteralPath $old -Force } }

Invoke-Checked "PyInstaller" {
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --noupx `
        --name $appName `
        --distpath $distRoot `
        --workpath $workDir `
        --specpath $specDir `
        --icon $iconIco `
        --version-file $versionFile `
        --add-data "$iconIco;icon" `
        @binaryArgs `
        @excludeArgs `
        (Join-Path $projectDir "main.py")
}
if (-not (Test-Path $exe)) { throw "PyInstaller did not produce $exe" }

# Safety check on the finished exe: the four tools and the icon are inside,
# and no cookies.txt or settings.json was swept in.
Invoke-Checked "Checking the exe contents" { & $python $helper check-exe $exe }

$hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText($hashFile, "$hash  $appName.exe`n", [System.Text.Encoding]::ASCII)

Write-Host ""
Write-Host ("App:    {0} ({1:N1} MB)" -f $exe, ((Get-Item $exe).Length / 1MB)) -ForegroundColor Green
Write-Host ("SHA256: {0}" -f $hash) -ForegroundColor Green
