$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
$assetDir = Join-Path $projectDir ".build-assets\bin"
$iconPng = Join-Path $projectDir "icon\icon1.png"
$iconIco = Join-Path $projectDir ".build-assets\icon.ico"
$versionFile = Join-Path $projectDir "windows_version_info.txt"

if (-not (Test-Path $python)) {
    throw "Project virtual environment not found. Create .venv before building."
}
if (-not (Test-Path $iconPng)) {
    throw "App logo not found at icon\icon1.png."
}

New-Item -ItemType Directory -Force -Path $assetDir | Out-Null

& $python -m pip install --upgrade -r (Join-Path $projectDir "requirements.txt") -r (Join-Path $projectDir "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Unable to install build dependencies." }

foreach ($toolName in @("ffmpeg", "ffprobe", "node")) {
    $command = Get-Command $toolName -ErrorAction Stop
    $item = Get-Item $command.Source
    $source = if ($item.LinkType -eq "SymbolicLink") { $item.Target[0] } else { $item.FullName }
    Copy-Item -LiteralPath $source -Destination (Join-Path $assetDir "$toolName.exe") -Force
}

& $python -c "from PIL import Image; image=Image.open(r'$iconPng').convert('RGBA'); image.save(r'$iconIco', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE -ne 0) { throw "Unable to create the Windows icon." }

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "YouTubeMP3Converter" `
    --icon $iconIco `
    --version-file $versionFile `
    --add-data "$iconPng;icon" `
    --add-binary "$(Join-Path $assetDir 'ffmpeg.exe');bin" `
    --add-binary "$(Join-Path $assetDir 'ffprobe.exe');bin" `
    --add-binary "$(Join-Path $assetDir 'node.exe');bin" `
    --hidden-import pystray._win32 `
    (Join-Path $projectDir "app.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$output = Join-Path $projectDir "dist\YouTubeMP3Converter.exe"
Write-Host ""
Write-Host "Standalone app created: $output" -ForegroundColor Green
