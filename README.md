# YouTube to MP3 Batch Converter

A small local web app for converting multiple YouTube links to MP3 files in one batch.

Use this only for videos you own, public-domain content, Creative Commons content, or material you have permission to download.

## Requirements

- Python 3.10 or newer. Python 3.11+ is recommended because yt-dlp warns that Python 3.10 support is deprecated.
- `ffmpeg` installed and available on your `PATH`
- Node.js 22 or newer, used by yt-dlp to solve YouTube JavaScript challenges
- Python dependencies from `requirements.txt`

This project is a source-only local app. It is intended to be opened in VS Code and run with the project virtual environment. It is not currently packaged as a standalone Windows application.

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

Recommended:

```powershell
.\.venv\Scripts\python.exe app.py
```

This makes sure the app uses the project virtual environment and the correct `yt-dlp[default]` installation.

You can also use the VS Code **Run Python Script** button after selecting the project interpreter:

1. Press `Ctrl + Shift + P`
2. Search `Python: Select Interpreter`
3. Choose `.\.venv\Scripts\python.exe`

VS Code remembers this interpreter for this project until you change it.

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

If YouTube starts showing authentication, cookie, or "page needs to be reloaded" errors again, export a fresh `cookies.txt`, replace the old file, and restart the server.

## Speed Notes

Some videos are slower because YouTube now requires extra checks before the media can be downloaded:

```text
Downloading web creator client config
[jsc:node] Solving JS challenges using node
```

Those steps come from `yt-dlp` and YouTube, not from the app UI. Node.js support makes more videos work, but it can make startup slower for each link.

## Notes

- Batches are limited to 50 links.
- The app downloads only the individual video URL you paste, not an entire playlist.
- Two conversions run at the same time by default. You can change `MAX_WORKERS` in `app.py`.
- Converted files are saved in the local `downloads/` folder.
