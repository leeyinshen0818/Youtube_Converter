from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = APP_DIR / "downloads"
MAX_BATCH_SIZE = 50
MAX_WORKERS = 2
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8080


@dataclass
class ConversionItem:
    id: str
    url: str
    status: str = "queued"
    title: str = ""
    message: str = "Waiting"
    progress: str = ""
    files: list[str] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class BatchJob:
    id: str
    created_at: float
    items: list[ConversionItem]


jobs: dict[str, BatchJob] = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def yt_dlp_command() -> list[str] | None:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return None

    return [sys.executable, "-m", "yt_dlp"]


def ffmpeg_location() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return str(Path(ffmpeg).parent)

    winget_link_dir = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links"
    winget_ffmpeg = (
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
    )
    try:
        if winget_ffmpeg.exists():
            return str(winget_link_dir)
    except OSError:
        return str(winget_link_dir)

    return None


def validate_youtube_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Use a full YouTube URL starting with https://")

    host = parsed.netloc.lower().split(":")[0]
    is_youtube = host == "youtube.com" or host.endswith(".youtube.com")
    is_short = host == "youtu.be"
    if not (is_youtube or is_short):
        raise ValueError("Only YouTube URLs are supported")

    return url


def parse_urls(payload: dict[str, Any]) -> list[str]:
    raw = str(payload.get("urls", ""))
    candidates = [part.strip() for part in re.split(r"[\r\n]+", raw) if part.strip()]
    if not candidates:
        raise ValueError("Paste at least one YouTube URL")

    if len(candidates) > MAX_BATCH_SIZE:
        raise ValueError(f"Batch is limited to {MAX_BATCH_SIZE} URLs at a time")

    seen: set[str] = set()
    urls: list[str] = []
    for candidate in candidates:
        url = validate_youtube_url(candidate)
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def snapshot_job(job: BatchJob) -> dict[str, Any]:
    queued = running = done = error = 0
    items: list[dict[str, Any]] = []
    for item in job.items:
        if item.status == "queued":
            queued += 1
        elif item.status == "running":
            running += 1
        elif item.status == "done":
            done += 1
        elif item.status == "error":
            error += 1

        item_data = asdict(item)
        item_data["downloadUrls"] = [
            f"/api/download/{job.id}/{item.id}/{quote(filename)}" for filename in item.files
        ]
        items.append(item_data)

    return {
        "id": job.id,
        "createdAt": job.created_at,
        "counts": {
            "queued": queued,
            "running": running,
            "done": done,
            "error": error,
            "total": len(job.items),
        },
        "items": items,
        "zipUrl": f"/api/download/{job.id}.zip" if done else "",
    }


def set_item_state(batch_id: str, item_id: str, **changes: Any) -> None:
    with jobs_lock:
        job = jobs[batch_id]
        item = next(entry for entry in job.items if entry.id == item_id)
        for key, value in changes.items():
            setattr(item, key, value)


def list_mp3_files(item_dir: Path) -> list[str]:
    return sorted(path.name for path in item_dir.glob("*.mp3") if path.is_file())


def cookies_file() -> Path | None:
    candidate = APP_DIR / "cookies.txt"
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def run_conversion(batch_id: str, item_id: str, url: str) -> None:
    item_dir = DOWNLOAD_DIR / batch_id / item_id
    item_dir.mkdir(parents=True, exist_ok=True)

    command_prefix = yt_dlp_command()
    if command_prefix is None:
        set_item_state(
            batch_id,
            item_id,
            status="error",
            message="yt-dlp is not installed. Run: python -m pip install -r requirements.txt",
            finished_at=time.time(),
        )
        return

    ffmpeg_path = ffmpeg_location()
    if ffmpeg_path is None:
        set_item_state(
            batch_id,
            item_id,
            status="error",
            message="ffmpeg is not installed or is not on PATH. Install ffmpeg, then try again.",
            finished_at=time.time(),
        )
        return

    output_template = str(item_dir / "%(title).180B [%(id)s].%(ext)s")
    command = [
        *command_prefix,
        "--newline",
        "--no-playlist",
        "--no-mtime",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--ffmpeg-location",
        ffmpeg_path,
        "--add-metadata",
        "--output",
        output_template,
    ]
    node_path = shutil.which("node")
    if node_path:
        command.extend(["--js-runtimes", f"node:{node_path}"])
    cookie_path = cookies_file()
    if cookie_path:
        command.extend(["--cookies", str(cookie_path)])
    command.append(url)

    set_item_state(
        batch_id,
        item_id,
        status="running",
        message="Starting conversion",
        started_at=time.time(),
    )

    try:
        process = subprocess.Popen(
            command,
            cwd=APP_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        last_line = ""
        cookie_required = False
        assert process.stdout is not None
        for line in process.stdout:
            clean = line.strip()
            if not clean:
                continue
            last_line = clean
            lowered = clean.lower()
            if "cookies" in lowered or "sign in to confirm" in lowered:
                cookie_required = True

            title_match = re.search(r"\[download\] Destination: (.+)", clean)
            if title_match:
                set_item_state(batch_id, item_id, title=Path(title_match.group(1)).stem)

            progress_match = re.search(r"\[download\]\s+([0-9.]+%)", clean)
            changes: dict[str, Any] = {"message": clean[-240:]}
            if progress_match:
                changes["progress"] = progress_match.group(1)
            set_item_state(batch_id, item_id, **changes)

        exit_code = process.wait()
        files = list_mp3_files(item_dir)
        if exit_code == 0 and files:
            set_item_state(
                batch_id,
                item_id,
                status="done",
                message="Ready to download",
                progress="100%",
                files=files,
                finished_at=time.time(),
            )
            return

        if cookie_required and cookie_path:
            reason = (
                "YouTube rejected cookies.txt. Export fresh YouTube cookies, replace the "
                "file beside app.py, restart the server, and try again."
            )
        elif cookie_required:
            reason = (
                "YouTube requires cookies. Export YouTube cookies to cookies.txt, put the "
                "file beside app.py, restart the server, and try again."
            )
        else:
            reason = last_line or f"yt-dlp exited with code {exit_code}"
        set_item_state(
            batch_id,
            item_id,
            status="error",
            message=reason[-240:],
            files=files,
            finished_at=time.time(),
        )
    except Exception as exc:  # pragma: no cover - defensive background task guard
        set_item_state(
            batch_id,
            item_id,
            status="error",
            message=str(exc),
            finished_at=time.time(),
        )


def create_batch(payload: dict[str, Any]) -> dict[str, Any]:
    urls = parse_urls(payload)
    batch_id = uuid.uuid4().hex
    items = [ConversionItem(id=uuid.uuid4().hex, url=url) for url in urls]
    job = BatchJob(id=batch_id, created_at=time.time(), items=items)

    with jobs_lock:
        jobs[batch_id] = job

    for item in items:
        executor.submit(run_conversion, batch_id, item.id, item.url)

    return snapshot_job(job)


def safe_lookup_file(batch_id: str, item_id: str, filename: str) -> Path:
    filename = Path(unquote(filename)).name
    item_dir = (DOWNLOAD_DIR / batch_id / item_id).resolve()
    target = (item_dir / filename).resolve()
    if item_dir not in target.parents:
        raise FileNotFoundError("Invalid file path")
    if target.suffix.lower() != ".mp3" or not target.is_file():
        raise FileNotFoundError("MP3 file not found")
    return target


def build_zip(batch_id: str) -> Path:
    with jobs_lock:
        job = jobs.get(batch_id)
        if job is None:
            raise FileNotFoundError("Batch not found")
        item_file_pairs = [
            (item.id, filename)
            for item in job.items
            if item.status == "done"
            for filename in item.files
        ]

    if not item_file_pairs:
        raise FileNotFoundError("No converted MP3 files are ready")

    batch_dir = DOWNLOAD_DIR / batch_id
    zip_path = batch_dir / "youtube-mp3-batch.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        used_names: set[str] = set()
        for item_id, filename in item_file_pairs:
            source = safe_lookup_file(batch_id, item_id, filename)
            archive_name = source.name
            if archive_name in used_names:
                archive_name = f"{item_id[:8]}-{archive_name}"
            used_names.add(archive_name)
            archive.write(source, archive_name)
    return zip_path


class AppHandler(BaseHTTPRequestHandler):
    server_version = "YouTubeBatchConverter/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return

        if parsed.path.startswith("/api/jobs/"):
            batch_id = parsed.path.removeprefix("/api/jobs/").strip("/")
            with jobs_lock:
                job = jobs.get(batch_id)
                if job is None:
                    self.send_json({"error": "Batch not found"}, HTTPStatus.NOT_FOUND)
                    return
                data = snapshot_job(job)
            self.send_json(data)
            return

        if parsed.path.startswith("/api/download/") and parsed.path.endswith(".zip"):
            batch_id = Path(parsed.path.removeprefix("/api/download/")).stem
            try:
                self.send_file(build_zip(batch_id), "application/zip")
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return

        if parsed.path.startswith("/api/download/"):
            parts = parsed.path.removeprefix("/api/download/").split("/", 2)
            if len(parts) != 3:
                self.send_json({"error": "Invalid download URL"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                target = safe_lookup_file(parts[0], parts[1], parts[2])
                self.send_file(target, "audio/mpeg")
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return

        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/jobs":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body or "{}")
            data = create_batch(payload)
            self.send_json(data, HTTPStatus.CREATED)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: Any) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {format % args}")

    def send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_file(self, path: Path, content_type: str) -> None:
        filename = path.name
        data = path.read_bytes()
        ascii_filename = filename.encode("ascii", "ignore").decode("ascii").strip()
        if not ascii_filename:
            ascii_filename = f"download{path.suffix}"
        ascii_filename = ascii_filename.replace("\\", "_").replace("/", "_").replace('"', "")
        utf8_filename = quote(filename)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{utf8_filename}',
        )
        self.end_headers()
        self.wfile.write(data)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube to MP3 Batch Converter</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #15171c;
      --muted: #5c6370;
      --line: #d9dee8;
      --paper: #fbfbfd;
      --surface: #ffffff;
      --accent: #0e7c66;
      --accent-dark: #075f4d;
      --danger: #b83232;
      --warn: #956400;
      --ok: #19724f;
      --blue: #1c5fb8;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--paper);
      color: var(--ink);
      font-family: Arial, Helvetica, sans-serif;
    }

    main {
      width: min(1120px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 56px;
    }

    header {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 24px;
    }

    h1 {
      margin: 0 0 8px;
      font-size: clamp(30px, 4vw, 52px);
      line-height: 1;
      letter-spacing: 0;
    }

    p {
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }

    .shell {
      display: grid;
      grid-template-columns: minmax(0, 420px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }

    .panel,
    .queue {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(30, 38, 56, 0.06);
    }

    .panel {
      padding: 18px;
      position: sticky;
      top: 18px;
    }

    label {
      display: block;
      font-weight: 700;
      margin-bottom: 10px;
    }

    textarea {
      width: 100%;
      min-height: 290px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 12px;
      font: 14px/1.45 Consolas, "Courier New", monospace;
      color: var(--ink);
      background: #fff;
    }

    textarea:focus,
    button:focus-visible,
    a.button:focus-visible {
      outline: 3px solid rgba(14, 124, 102, 0.22);
      outline-offset: 2px;
    }

    .actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 14px;
    }

    button,
    .button {
      border: 0;
      border-radius: 6px;
      min-height: 42px;
      padding: 0 16px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
      white-space: nowrap;
    }

    button:hover,
    .button:hover {
      background: var(--accent-dark);
    }

    .secondary-button {
      background: #eef1f6;
      color: #334155;
    }

    .secondary-button:hover {
      background: #dfe5ee;
    }

    button:disabled {
      cursor: wait;
      opacity: 0.65;
    }

    .quiet {
      color: var(--muted);
      font-size: 13px;
    }

    .notice {
      margin-top: 16px;
      padding: 12px;
      border: 1px solid #ead5a5;
      border-radius: 6px;
      background: #fff8e6;
      color: #5f4400;
      font-size: 13px;
      line-height: 1.45;
    }

    .queue {
      min-height: 480px;
      overflow: hidden;
    }

    .queue-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
    }

    h2 {
      margin: 0;
      font-size: 18px;
      letter-spacing: 0;
    }

    .stats {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }

    .stat {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 9px;
      background: #fff;
    }

    .empty {
      padding: 80px 24px;
      text-align: center;
    }

    .items {
      display: grid;
      gap: 0;
    }

    .item {
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      display: grid;
      gap: 10px;
    }

    .item:last-child {
      border-bottom: 0;
    }

    .item-main {
      display: grid;
      grid-template-columns: 88px minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
    }

    .badge {
      min-height: 28px;
      border-radius: 999px;
      padding: 6px 10px;
      text-align: center;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0;
      background: #eef1f6;
      color: #445063;
    }

    .badge.running {
      background: #eaf2ff;
      color: var(--blue);
    }

    .badge.done {
      background: #e8f6ef;
      color: var(--ok);
    }

    .badge.error {
      background: #ffecec;
      color: var(--danger);
    }

    .url {
      overflow-wrap: anywhere;
      font-size: 14px;
    }

    .message {
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
      margin-top: 4px;
    }

    progress {
      width: 100%;
      height: 10px;
      accent-color: var(--accent);
    }

    .downloads {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }

    .downloads .button {
      min-height: 34px;
      padding: 0 12px;
      font-size: 13px;
      background: #243043;
    }

    .top-download {
      background: #243043;
    }

    .error-text {
      color: var(--danger);
      font-weight: 700;
      margin-top: 10px;
      min-height: 20px;
    }

    @media (max-width: 860px) {
      main {
        width: min(100% - 24px, 680px);
        padding-top: 22px;
      }

      header,
      .shell,
      .item-main {
        grid-template-columns: 1fr;
      }

      header {
        display: block;
      }

      .panel {
        position: static;
      }

      .queue-head,
      .actions {
        align-items: stretch;
        flex-direction: column;
      }

      button,
      .button {
        width: 100%;
      }

      .item-main {
        display: grid;
      }

      .badge {
        width: max-content;
      }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>YouTube to MP3 Batch Converter</h1>
        <p>Convert multiple permitted YouTube videos into MP3 files from one local queue.</p>
      </div>
    </header>

    <section class="shell" aria-label="Converter">
      <form class="panel" id="batchForm">
        <label for="urls">YouTube links</label>
        <textarea id="urls" name="urls" spellcheck="false" placeholder="https://www.youtube.com/watch?v=...&#10;https://youtu.be/..."></textarea>
        <div class="actions">
          <button id="convertButton" type="submit">Convert Batch</button>
          <button class="secondary-button" id="clearLinksButton" type="button">Clear Links</button>
          <span class="quiet" id="linkCount">0 links inserted</span>
        </div>
        <div class="error-text" id="errorText" role="alert"></div>
        <div class="notice">
          Use this only for videos you own, public-domain content, Creative Commons content, or material you have permission to download.
        </div>
      </form>

      <section class="queue" aria-live="polite">
        <div class="queue-head">
          <div>
            <h2>Queue</h2>
            <div class="stats" id="stats"></div>
          </div>
          <a class="button top-download" id="zipLink" href="#" hidden>Download ZIP</a>
        </div>
        <div id="items" class="items">
          <div class="empty">
            <p>No batch is running yet.</p>
          </div>
        </div>
      </section>
    </section>
  </main>

  <script>
    const form = document.querySelector("#batchForm");
    const textarea = document.querySelector("#urls");
    const button = document.querySelector("#convertButton");
    const clearLinksButton = document.querySelector("#clearLinksButton");
    const linkCount = document.querySelector("#linkCount");
    const errorText = document.querySelector("#errorText");
    const itemsNode = document.querySelector("#items");
    const statsNode = document.querySelector("#stats");
    const zipLink = document.querySelector("#zipLink");
    let currentBatchId = "";
    let pollTimer = 0;

    textarea.addEventListener("input", updateLinkCount);
    clearLinksButton.addEventListener("click", () => {
      textarea.value = "";
      errorText.textContent = "";
      updateLinkCount();
      textarea.focus();
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      errorText.textContent = "";
      button.disabled = true;
      button.textContent = "Starting...";

      try {
        const response = await fetch("/api/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ urls: textarea.value })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Unable to start batch");

        currentBatchId = data.id;
        render(data);
        startPolling();
      } catch (error) {
        errorText.textContent = error.message;
      } finally {
        button.disabled = false;
        button.textContent = "Convert Batch";
      }
    });

    function startPolling() {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(refresh, 1500);
      refresh();
    }

    function updateLinkCount() {
      const count = textarea.value
        .split(/\r?\n/)
        .map((link) => link.trim())
        .filter(Boolean).length;
      linkCount.textContent = `${count} ${count === 1 ? "link" : "links"} inserted`;
    }

    async function refresh() {
      if (!currentBatchId) return;
      const response = await fetch(`/api/jobs/${currentBatchId}`);
      const data = await response.json();
      if (!response.ok) {
        errorText.textContent = data.error || "Unable to refresh batch";
        clearInterval(pollTimer);
        return;
      }

      render(data);
      const active = data.counts.queued + data.counts.running;
      if (active === 0) clearInterval(pollTimer);
    }

    function render(data) {
      const counts = data.counts;
      statsNode.innerHTML = `
        <span class="stat">${counts.total} total</span>
        <span class="stat">${counts.running} running</span>
        <span class="stat">${counts.done} ready</span>
        <span class="stat">${counts.error} failed</span>
      `;

      zipLink.hidden = !data.zipUrl;
      zipLink.href = data.zipUrl || "#";

      itemsNode.innerHTML = data.items.map((item) => {
        const progressValue = Number((item.progress || "0").replace("%", "")) || 0;
        const fileName = item.files && item.files.length ? item.files.join(", ") : "";
        const displayName = fileName || item.title || item.url;
        const downloads = item.downloadUrls.map((url, index) => (
          `<a class="button" href="${url}">Download MP3 ${index + 1}</a>`
        )).join("");

        return `
          <article class="item">
            <div class="item-main">
              <span class="badge ${item.status}">${item.status}</span>
              <div>
                <div class="url">${escapeHtml(displayName)}</div>
                <div class="message">${escapeHtml(item.message || "")}</div>
              </div>
              <div class="downloads">${downloads}</div>
            </div>
            <progress max="100" value="${progressValue}"></progress>
          </article>
        `;
      }).join("");
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }
  </script>
</body>
</html>
"""


def main() -> None:
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((SERVER_HOST, SERVER_PORT), AppHandler)
    url = f"http://{SERVER_HOST}:{SERVER_PORT}"
    print(f"Opening {url} in your browser")
    print("Press Ctrl+C to stop the converter")
    browser_timer = threading.Timer(1.0, lambda: webbrowser.open(url))
    browser_timer.daemon = True
    browser_timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()
        executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
