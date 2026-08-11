# YouTube to MP3 Batch Converter

A small local web app for converting multiple YouTube links to MP3 files in one batch.

Use this only for videos you own, public-domain content, Creative Commons content, or material you have permission to download.

## Requirements

- Python 3.10 or newer
- `ffmpeg` installed and available on your `PATH`
- Node.js 22 or newer, used by yt-dlp to solve YouTube JavaScript challenges
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

The converter automatically opens this address in your default browser:

```text
http://127.0.0.1:8080
```

Paste one YouTube URL per line, click **Convert Batch**, and wait for each item to become ready. Converted files are stored in the local `downloads/` folder and can also be downloaded from the browser UI.

## YouTube Cookies

Some YouTube requests require authentication. Export your own YouTube cookies in Netscape format, name the file `cookies.txt`, and put it beside `app.py`:

```text
Youtube_Converter\
|-- app.py
|-- cookies.txt
|-- requirements.txt
```

Restart `python app.py` after adding or replacing the file. The program detects it automatically and passes it to `yt-dlp`.

Never share `cookies.txt` or commit it to GitHub. It contains private session information and is excluded by `.gitignore`.

## Notes

- Batches are limited to 50 links.
- The app downloads only the individual video URL you paste, not an entire playlist.
- Two conversions run at the same time by default. You can change `MAX_WORKERS` in `app.py`.
