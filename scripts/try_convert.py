"""Dev-only: run ONE real conversion through core.converter and print what happens.

Examples (from the project folder):
  .venv\\Scripts\\python.exe scripts\\try_convert.py "https://youtu.be/<id>"
  .venv\\Scripts\\python.exe scripts\\try_convert.py "<url>" --out D:\\Temp\\mp3
  .venv\\Scripts\\python.exe scripts\\try_convert.py "<url>" --stop-after 3
  .venv\\Scripts\\python.exe scripts\\try_convert.py "<url>" --stop-when-converting
  .venv\\Scripts\\python.exe scripts\\try_convert.py "<url>" --no-cookies

Press Ctrl+C to stop the conversion (same as the app's Stop button).
Only use links you have permission to download.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import tools  # noqa: E402
from core.converter import (  # noqa: E402
    Conversion,
    ConversionCallbacks,
    ConverterConfig,
    Phase,
    default_output_dir,
)
from core.urls import parse_links  # noqa: E402


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert one YouTube link to MP3 (dev test).")
    parser.add_argument("url")
    parser.add_argument("--out", type=Path, default=None, help="output folder")
    parser.add_argument("--no-cookies", action="store_true", help="ignore any cookies.txt")
    parser.add_argument("--stop-after", type=float, default=None, metavar="SECONDS",
                        help="press Stop automatically after this many seconds")
    parser.add_argument("--stop-when-converting", action="store_true",
                        help="press Stop automatically as soon as converting starts")
    args = parser.parse_args()

    parsed = parse_links(args.url)
    if parsed.skipped:
        item = parsed.skipped[0]
        log(f"Link not accepted ({item.reason.value}): {item.message}")
        return 2
    link = parsed.valid[0]
    log(f"Link: {link.url}")

    env = tools.default_environment()
    config = ConverterConfig.from_system(output_dir=args.out, env=env, use_cookies=not args.no_cookies)
    log(f"yt-dlp:  {config.ytdlp or 'NOT FOUND'}")
    log(f"ffmpeg:  {config.ffmpeg or 'NOT FOUND'}")
    log(f"node:    {config.node or 'not found (optional)'}")
    log(f"cookies: {config.cookies or 'none'}")
    log(f"output:  {config.output_dir}")
    log(f"temp:    {config.temp_root}")

    if config.ytdlp is None:
        target = tools.user_tools_dir(env) / "yt-dlp.exe"
        log("yt-dlp.exe was not found.")
        log(f"Download yt-dlp.exe from https://github.com/yt-dlp/yt-dlp/releases/latest and save it as:\n    {target}")
        return 3
    if config.ffmpeg is None:
        log("ffmpeg was not found. Install it (winget install Gyan.FFmpeg) and reopen the terminal.")
        return 3

    conversion: Conversion | None = None

    def on_phase(phase: Phase) -> None:
        log(f"phase: {phase.value}")
        if phase is Phase.CONVERTING and args.stop_when_converting and conversion:
            log("auto Stop (converting started)")
            threading.Thread(target=conversion.stop, daemon=True).start()

    callbacks = ConversionCallbacks(
        on_title=lambda title: log(f"title: {title}"),
        on_progress=lambda percent: log(
            "progress: (unknown)" if percent is None else f"progress: {percent:5.1f}%"
        ),
        on_phase=on_phase,
    )
    conversion = Conversion(
        config, link.url, f"try-{uuid.uuid4().hex[:8]}", callbacks, fallback_name=link.video_id
    )

    if args.stop_after is not None:
        timer = threading.Timer(args.stop_after, lambda: (log("auto Stop (timer)"), conversion.stop()))
        timer.daemon = True
        timer.start()

    results = []
    worker = threading.Thread(target=lambda: results.append(conversion.run()), daemon=True)
    started = time.monotonic()
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.2)
    except KeyboardInterrupt:
        log("Ctrl+C: stopping")
        conversion.stop()
        worker.join()

    result = results[0]
    log(f"result: {result.phase.value} after {time.monotonic() - started:.1f}s")
    if result.output_path:
        log(f"saved: {result.output_path}")
    if result.error:
        log(f"message: {result.error.message}")
        log(f"hint:    {result.error.hint}")
        log(f"details: {result.error.details}")
    log(f"work folder removed: {not conversion.work_dir.exists()}  ({conversion.work_dir})")
    return 0 if result.phase is Phase.DONE else 1


if __name__ == "__main__":
    sys.exit(main())
