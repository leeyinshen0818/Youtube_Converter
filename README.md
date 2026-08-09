# YouTube to MP3 Batch Converter

A small local web app for converting multiple permitted YouTube links to MP3 files in one batch.

Use this only for videos you own, public-domain content, Creative Commons content, or material you have permission to download.

## Requirements

- Python 3.10 or newer
- `ffmpeg` installed and available on your `PATH`
- Python dependencies from `requirements.txt`

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Install `ffmpeg` if it is not already available:

```powershell
winget install Gyan.FFmpeg
```

Close and reopen PowerShell after installing `ffmpeg`, then check:

```powershell
ffmpeg -version
```

If Windows still cannot find `ffmpeg`, add this folder to your user `Path`:

```text
C:\Users\Nitro\AppData\Local\Microsoft\WinGet\Links
```

Then close and reopen your terminal and restart `python app.py`.

## Run

```powershell
python app.py
```

Open:

```text
http://127.0.0.1:8080
```

Paste one YouTube URL per line, click **Convert Batch**, and wait for each item to become ready. Converted files are stored in the local `downloads/` folder and can also be downloaded from the browser UI.

Start the app from your own PowerShell or Git Bash terminal. If the app is started by Codex's sandboxed terminal, the page can open but `yt-dlp` may be blocked from reaching YouTube.

## Build a Windows App

Install the build dependency and create one standalone app file:

```powershell
.\build_app.ps1
```

The finished app will be here:

```text
dist\YoutubeConverter.exe
```

To use it on another Windows computer, send only this file:

```text
dist\YoutubeConverter.exe
```

The app opens your browser automatically and stores converted files in a `downloads` folder beside the executable. Keep the small black app window open while converting; close it to stop the app. The first launch can take a few seconds because the app extracts bundled tools into a temporary Windows folder.

The build bundles `ffmpeg.exe` and `ffprobe.exe` inside the single executable when they exist in the local `bin` folder.

## Notes

- Batches are limited to 50 links.
- The app downloads only the individual video URL you paste, not an entire playlist.
- Two conversions run at the same time by default. You can change `MAX_WORKERS` in `app.py`.
