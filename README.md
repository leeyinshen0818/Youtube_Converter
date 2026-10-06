# YouTube to MP3 Batch Converter

A local app for converting YouTube links to MP3 in batches. Current version: **v1.1.0**.

Use it only for videos you own, public-domain or Creative Commons content, or material you have permission to download.

## Use the Windows App

Open `dist\YouTubeMP3Converter.exe`. It opens the converter in your browser and stays in the system tray. You can copy the single `.exe` to your Desktop or another Windows computer; Python, FFmpeg, FFprobe, Node.js, and yt-dlp are bundled.

To rebuild it on Windows, run `./build_windows.ps1` in PowerShell. Close any running copies of the app first so the executable can be replaced.

## Run from Source

Install Python 3.10 or newer, Node.js 22 or newer, and FFmpeg. FFmpeg must be on `PATH` (`winget install Gyan.FFmpeg` on Windows; reopen your terminal afterward).

From this folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

The app opens in your browser at `http://127.0.0.1:8080`. To use VS Code's Run button, select `.\.venv\Scripts\python.exe` with **Python: Select Interpreter** first.

## Convert and Manage the Queue

1. Paste a YouTube link. Press Enter for another link, or paste multiple links at once.
2. Click **Convert Batch**. New batches stay in the queue with earlier results.
3. Download an MP3 from its row, or use **Download All** for all available completed MP3s in the visible queue.

**Stop** cancels a waiting item immediately or requests cancellation of a running item. **Retry** is available for failed or stopped items. **Clear Links** empties the input fields. **Clear List** asks for confirmation and removes completed, failed, and stopped rows; active conversions stay visible. Clearing the list does not delete MP3 files.

Source downloads are saved in `downloads/`. Standalone app downloads are saved in `%USERPROFILE%\Downloads\YouTube MP3 Converter`.

## YouTube Cookies

If YouTube requires sign-in for a video, export your own cookies in Netscape format as `cookies.txt`. Put it beside `app.py` when running from source, or in `%LOCALAPPDATA%\YouTube MP3 Converter\` for the Windows app. Restart the app after replacing it. Never share or commit `cookies.txt`; it contains private session information and is ignored by Git.

## Limits

- Up to 50 links per batch, with two conversions running at a time. Playlists are not downloaded in full.
- Queue history and download indicators last only until the app closes. MP3 files remain on disk.
- A running item may take a moment to stop while yt-dlp finishes a network request or FFmpeg step.
- The app marks a file as downloaded after serving it to the browser. It cannot confirm that the browser saved it. Browsers may also ask permission for multiple downloads.
- Missing MP3 files are shown as unavailable and excluded from Download All. Some YouTube videos require cookies or take longer because of YouTube checks.
