# YouTube to MP3 Batch Converter

A Windows app for converting multiple permitted YouTube links into MP3 files in one batch.

Use this only for videos you own, public-domain content, Creative Commons content, or material you have permission to download.

## Downloadable App

The standalone Windows app is:

```text
dist\YoutubeConverter.exe
```

You can send only this `.exe` file to another Windows computer. No extra folder, Python install, `yt-dlp`, or FFmpeg setup is required because the build bundles the needed tools into the executable.

When opened, the app starts a local server and automatically opens:

```text
http://127.0.0.1:8080
```

Keep the small black app window open while converting. Closing that window stops the app.

Converted MP3 files are saved in a `downloads` folder beside the executable.

## Features

- Batch conversion: paste one YouTube URL per line.
- Live link counter.
- Clear all links button.
- Queue status for each conversion.
- Individual MP3 download buttons.
- Download ZIP button for completed files.
- App icon from `icon\100.ico`.

## Build The App

From PowerShell:

```powershell
.\build_app.ps1
```

If PowerShell blocks the script:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build_app.ps1
```

The build output is:

```text
dist\YoutubeConverter.exe
```

The executable includes `ffmpeg.exe` and `ffprobe.exe` when they exist in the local `bin` folder.

## Run From Source

Requirements:

- Python 3.10 or newer
- FFmpeg installed and available on `PATH`
- Python dependencies from `requirements.txt`

Setup:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Install FFmpeg if needed:

```powershell
winget install Gyan.FFmpeg
```

Run:

```powershell
python app.py
```

Then open:

```text
http://127.0.0.1:8080
```

## Notes

- Batches are limited to 50 links.
- The app downloads only the individual video URL you paste, not an entire playlist.
- Two conversions run at the same time by default. Change `MAX_WORKERS` in `app.py` to adjust this.
