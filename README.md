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

The build makes **one file**, `dist\YouTubeMP3Converter.exe`, that you can
copy to any Windows 10/11 PC. Python doesn't need to be installed there.

1. Have the `.venv` from above, and `node` on `PATH`.
2. Optional, but it makes the exe about 200 MB smaller: download an
   "essentials" build of ffmpeg (for example
   `ffmpeg-release-essentials.zip` from gyan.dev). Copy its `ffmpeg.exe` and
   `ffprobe.exe` into `.build-assets\ffmpeg-essentials\`. Without them, the
   build uses `ffmpeg` and `ffprobe` from `PATH` and prints a warning.
3. The logo is `icon\logo.png`. The build turns it into the exe and window
   icon at every size from 16 to 256 px. A preview of the small sizes is
   saved to `screenshots\icon_sheet.png`.
4. Run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
   ```

The script:

- downloads the latest official `yt-dlp.exe` and checks it against the
  release's `SHA2-256SUMS` (the build stops on any mismatch);
- embeds ffmpeg, ffprobe, node and yt-dlp in the exe, and prints which files
  it used and their sizes;
- checks that no `cookies.txt` or `settings.json` ended up inside;
- writes `dist\YouTubeMP3Converter.exe.sha256`.

Notes:

- Each start unpacks the app to a temporary folder (removed again when the
  app closes), so the first window takes a few seconds to appear.
- Only one copy runs at a time; opening it again does nothing while it's
  already open.
- The exe isn't code-signed, so Windows SmartScreen may warn the first time
  it runs.
