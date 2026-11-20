# YouTube to MP3 Converter

A small Windows app that turns YouTube video links into MP3 files.

Use it only for videos you own, public-domain content, Creative Commons
content, or material you have permission to download.

## How to use

1. **Paste links**: one YouTube video link per line, up to 50 at a time.
   Playlist and channel links are skipped; paste the single videos instead.
2. **Convert**: each link gets a row that shows its progress.
3. **Download**: press **Download** on a finished row, or **Download All**.
   **Show in folder** then opens the saved file.

## Where your files go

- MP3s are saved in `Downloads\YouTube MP3 Converter` unless you choose
  another folder in **Settings**.
- Until you press Download, converted files wait in
  `%LOCALAPPDATA%\YouTube MP3 Converter\ready`. The app's settings and its
  updated download component are also kept in
  `%LOCALAPPDATA%\YouTube MP3 Converter`.

## Settings and Update

- **Settings** (top right): change the save folder and see the version
  numbers. Cookies are under **Advanced**.
- **Downloads not working? Update** (bottom of the window): YouTube changes
  often. If downloads stop working, this installs the newest official
  `yt-dlp.exe`, the part that does the downloading. The download is checked
  against the official checksums before it replaces the old one.

## Cookies (optional)

You usually don't need cookies. Import a `cookies.txt` file under
**Settings > Advanced** only if YouTube asks you to sign in. The file holds
your login session, so keep it private and never share it.

## Run from source

Requires Windows and Python 3.10 or newer.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python main.py
```

The app needs `yt-dlp.exe`, `ffmpeg.exe`, `ffprobe.exe` and `node.exe`. It
looks for them in a `bin` folder next to `main.py`, then on `PATH`. If
`yt-dlp.exe` is missing, use **Downloads not working? Update** to install it.

Run the tests. They don't use the network or the real app folders:

```powershell
.venv\Scripts\python -m unittest discover -s . -t .
```

## Build the Windows app

You need `ffmpeg`, `ffprobe` and `node` on `PATH`, plus the `.venv` from
above. Then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

The script:

- installs `requirements.txt` and `requirements-build.txt`;
- downloads the latest official `yt-dlp.exe` and checks it against the
  release's `SHA2-256SUMS` (the build stops on any mismatch);
- copies `ffmpeg`, `ffprobe` and `node` from `PATH`;
- writes the icon and the version resource (the version comes from
  `core\version.py`).

Results:

- `dist\YouTubeMP3Converter\`: the app folder. Run
  `YouTubeMP3Converter.exe`; the four tools are in `bin\`.
- `dist\YouTubeMP3Converter-<version>.zip`: the same folder, zipped for sharing.

The built app doesn't need Python. The exe isn't code-signed, so Windows
SmartScreen may warn the first time it runs.
