"""A stand-in for yt-dlp.exe used by the converter tests (no network).

Usage: python fake_ytdlp.py <scenario> <title> <real yt-dlp arguments...>

It honours the arguments the converter passes that matter for the tests:
--paths, --print and --progress-template (simple %(field)s substitution).

Scenarios:
  success         download + convert, writes <id>.mp3, exit 0
  fail            print the title, then "ERROR: <title>" to stderr, exit 1
  no_mp3          exit 0 without writing a file
  sleep_download  start a sleeping child process, then hang while "downloading"
  sleep_convert   finish downloading, write a partial file, hang while "converting"

Environment:
  FAKE_YTDLP_ARGS  if set, the full argument list is written there as JSON
  FAKE_YTDLP_PIDS  if set, the PIDs of this process and its child are written there
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

FIELD = re.compile(r"%\(([^)]+)\)s")


def fill(template: str, values: dict[str, object]) -> str:
    return FIELD.sub(lambda match: str(values.get(match.group(1), "NA")), template)


def option_values(args: list[str], name: str) -> list[str]:
    return [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == name]


def by_prefix(values: list[str], prefix: str) -> list[str]:
    return [value[len(prefix):] for value in values if value.startswith(prefix)]


def main() -> int:
    scenario, title, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    if os.environ.get("FAKE_YTDLP_ARGS"):
        Path(os.environ["FAKE_YTDLP_ARGS"]).write_text(json.dumps(args), encoding="utf-8")

    work_dir = Path(option_values(args, "--paths")[0])
    prints = option_values(args, "--print")
    templates = option_values(args, "--progress-template")
    url = args[-1]
    video_id = url.rsplit("=", 1)[-1]
    info = {"title": title, "id": video_id}

    def out(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def progress(status: str, done: object, total: object) -> None:
        values = {
            "progress.status": status,
            "progress.downloaded_bytes": done,
            "progress.total_bytes": total,
            "progress.total_bytes_estimate": "NA",
        }
        for template in by_prefix(templates, "download:"):
            out(fill(template, values))

    def postprocess(status: str) -> None:
        values = {"progress.status": status, "progress.postprocessor": "ExtractAudio"}
        for template in by_prefix(templates, "postprocess:"):
            out(fill(template, values))

    out(f"[youtube] Extracting URL: {url}")
    for template in by_prefix(prints, "before_dl:"):
        out(fill(template, info))

    if scenario == "fail":
        sys.stderr.write(f"ERROR: {title}\n")
        sys.stderr.flush()
        return 1
    if scenario == "no_mp3":
        return 0

    if scenario == "sleep_download":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if os.environ.get("FAKE_YTDLP_PIDS"):
            Path(os.environ["FAKE_YTDLP_PIDS"]).write_text(
                json.dumps([os.getpid(), child.pid]), encoding="utf-8"
            )
        (work_dir / f"{video_id}.webm.part").write_bytes(b"partial")
        progress("downloading", 10, 1000)
        time.sleep(120)
        return 0

    for done in (0, 250, 500, 1000):
        progress("downloading", done, 1000)
    (work_dir / f"{video_id}.webm").write_bytes(b"audio")
    progress("finished", 1000, 1000)
    postprocess("started")

    if scenario == "sleep_convert":
        (work_dir / f"{video_id}.mp3").write_bytes(b"half an mp3")
        if os.environ.get("FAKE_YTDLP_PIDS"):
            Path(os.environ["FAKE_YTDLP_PIDS"]).write_text(
                json.dumps([os.getpid()]), encoding="utf-8"
            )
        time.sleep(120)
        return 0

    mp3 = work_dir / f"{video_id}.mp3"
    mp3.write_bytes(b"ID3 fake mp3")
    (work_dir / f"{video_id}.webm").unlink()
    postprocess("finished")
    for template in by_prefix(prints, "after_move:"):
        out(fill(template, {**info, "filepath": str(mp3)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
