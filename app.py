from __future__ import annotations

import json
import os
import re
import shutil
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


SOURCE_DIR = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
if IS_FROZEN:
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    APP_DIR = local_app_data / "YouTube MP3 Converter"
    DOWNLOAD_DIR = Path.home() / "Downloads" / "YouTube MP3 Converter"
else:
    APP_DIR = SOURCE_DIR
    DOWNLOAD_DIR = APP_DIR / "downloads"
MAX_BATCH_SIZE = 50
MAX_WORKERS = 2
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8080
APP_VERSION = "1.1.0"


@dataclass
class ConversionItem:
    id: str
    url: str
    status: str = "queued"
    title: str = ""
    message: str = "Waiting"
    progress: str = ""
    files: list[str] = field(default_factory=list)
    downloaded_files: list[str] = field(default_factory=list)
    downloaded_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class BatchJob:
    id: str
    created_at: float
    items: list[ConversionItem]


jobs: dict[str, BatchJob] = {}
jobs_lock = threading.Lock()
cancel_events: dict[tuple[str, str], threading.Event] = {}
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


class ConversionStopped(Exception):
    pass


def ffmpeg_location() -> str | None:
    bundled = BUNDLE_DIR / "bin" / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled.parent)

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
    queued = running = stopping = stopped = done = error = 0
    items: list[dict[str, Any]] = []
    for item in job.items:
        if item.status == "queued":
            queued += 1
        elif item.status == "running":
            running += 1
        elif item.status == "stopping":
            stopping += 1
        elif item.status == "stopped":
            stopped += 1
        elif item.status == "done":
            done += 1
        elif item.status == "error":
            error += 1

        item_data = asdict(item)
        item_data["downloadedFiles"] = item_data.pop("downloaded_files")
        item_data["downloadedAt"] = item_data.pop("downloaded_at")
        available_files = []
        missing_files = []
        for filename in item.files:
            try:
                safe_lookup_file(job.id, item.id, filename)
                available_files.append(filename)
            except FileNotFoundError:
                missing_files.append(filename)
        item_data["downloadUrls"] = [
            f"/api/download/{job.id}/{item.id}/{quote(filename)}"
            for filename in available_files
        ]
        item_data["missingFiles"] = missing_files
        items.append(item_data)

    return {
        "id": job.id,
        "createdAt": job.created_at,
        "counts": {
            "queued": queued,
            "running": running,
            "stopping": stopping,
            "stopped": stopped,
            "done": done,
            "error": error,
            "total": len(job.items),
        },
        "items": items,
        "zipUrl": f"/api/download/{job.id}.zip" if done else "",
    }


def snapshot_jobs() -> dict[str, Any]:
    with jobs_lock:
        return {"jobs": [snapshot_job(job) for job in jobs.values()]}


def clear_job_items(statuses: set[str]) -> dict[str, Any]:
    removed = 0
    preserved_active = 0
    with jobs_lock:
        empty_batch_ids: list[str] = []
        for batch_id, job in jobs.items():
            kept_items: list[ConversionItem] = []
            for item in job.items:
                if item.status in statuses:
                    removed += 1
                else:
                    kept_items.append(item)
                    if item.status in {"queued", "running", "stopping"}:
                        preserved_active += 1
            job.items = kept_items
            if not job.items:
                empty_batch_ids.append(batch_id)

        for batch_id in empty_batch_ids:
            del jobs[batch_id]
        for key in list(cancel_events):
            if key[0] not in jobs or all(item.id != key[1] for item in jobs[key[0]].items):
                del cancel_events[key]

        return {
            "jobs": [snapshot_job(job) for job in jobs.values()],
            "removed": removed,
            "preservedActive": preserved_active,
        }


def mark_files_downloaded(file_keys: list[tuple[str, str, str]]) -> None:
    downloaded_at = time.time()
    with jobs_lock:
        for batch_id, item_id, filename in file_keys:
            job = jobs.get(batch_id)
            if job is None:
                continue
            item = next((entry for entry in job.items if entry.id == item_id), None)
            if item is None or filename not in item.files:
                continue
            if filename not in item.downloaded_files:
                item.downloaded_files.append(filename)
            item.downloaded_at = downloaded_at


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
    with jobs_lock:
        job = jobs.get(batch_id)
        item = next((entry for entry in job.items if entry.id == item_id), None) if job else None
        if item is None or item.status != "queued":
            return
        cancel_event = cancel_events[(batch_id, item_id)]
        item.status = "running"
        item.message = "Starting conversion"
        item.started_at = time.time()

    def check_cancelled() -> None:
        if cancel_event.is_set():
            raise ConversionStopped()

    item_dir = DOWNLOAD_DIR / batch_id / item_id
    item_dir.mkdir(parents=True, exist_ok=True)

    try:
        import yt_dlp
    except ImportError:
        with jobs_lock:
            item = next(entry for entry in jobs[batch_id].items if entry.id == item_id)
            item.status = "stopped" if cancel_event.is_set() else "error"
            item.message = (
                "Stopped" if cancel_event.is_set()
                else "yt-dlp is not installed. Run: python -m pip install -r requirements.txt"
            )
            item.finished_at = time.time()
        return

    if cancel_event.is_set():
        set_item_state(batch_id, item_id, status="stopped", message="Stopped", finished_at=time.time())
        return

    ffmpeg_path = ffmpeg_location()
    if cancel_event.is_set():
        set_item_state(batch_id, item_id, status="stopped", message="Stopped", finished_at=time.time())
        return
    if ffmpeg_path is None:
        with jobs_lock:
            item = next(entry for entry in jobs[batch_id].items if entry.id == item_id)
            item.status = "stopped" if cancel_event.is_set() else "error"
            item.message = (
                "Stopped" if cancel_event.is_set()
                else "ffmpeg is not installed or is not on PATH. Install ffmpeg, then try again."
            )
            item.finished_at = time.time()
        return

    output_template = str(item_dir / "%(title).180B [%(id)s].%(ext)s")
    bundled_node = BUNDLE_DIR / "bin" / "node.exe"
    node_path = str(bundled_node) if bundled_node.is_file() else shutil.which("node")
    cookie_path = cookies_file()

    last_line = ""
    cookie_required = False

    def clean_message(message: Any) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", str(message)).strip()

    def record_message(message: Any) -> None:
        nonlocal last_line, cookie_required
        check_cancelled()
        clean = clean_message(message)
        if not clean:
            return
        last_line = clean
        lowered = clean.lower()
        if "cookies" in lowered or "sign in to confirm" in lowered:
            cookie_required = True
        set_item_state(batch_id, item_id, message=clean[-240:])

    class QueueLogger:
        def debug(self, message: Any) -> None:
            if not str(message).startswith("[debug]"):
                record_message(message)

        def info(self, message: Any) -> None:
            record_message(message)

        def warning(self, message: Any) -> None:
            record_message(message)

        def error(self, message: Any) -> None:
            record_message(message)

    def progress_hook(data: dict[str, Any]) -> None:
        check_cancelled()
        info = data.get("info_dict") or {}
        title = str(info.get("title") or "")
        status = data.get("status")
        if status == "downloading":
            percent = clean_message(data.get("_percent_str") or "")
            changes: dict[str, Any] = {
                "message": f"Downloading {percent}".strip(),
            }
            match = re.search(r"([0-9.]+%)", percent)
            if match:
                changes["progress"] = match.group(1)
            if title:
                changes["title"] = title
            set_item_state(batch_id, item_id, **changes)
        elif status == "finished":
            set_item_state(
                batch_id,
                item_id,
                title=title,
                message="Converting to MP3",
                progress="100%",
            )

    def postprocessor_hook(data: dict[str, Any]) -> None:
        check_cancelled()
        if data.get("status") == "started":
            set_item_state(batch_id, item_id, message="Converting to MP3")

    options: dict[str, Any] = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "updatetime": False,
        "outtmpl": output_template,
        "ffmpeg_location": ffmpeg_path,
        "logger": QueueLogger(),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "5",
            }
        ],
    }
    if node_path:
        options["js_runtimes"] = {"node": {"path": node_path}}
    if cookie_path:
        options["cookiefile"] = str(cookie_path)

    try:
        check_cancelled()
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.extract_info(url, download=True)
        check_cancelled()
        files = list_mp3_files(item_dir)
        if files:
            with jobs_lock:
                item = next(entry for entry in jobs[batch_id].items if entry.id == item_id)
                item.status = "stopped" if cancel_event.is_set() else "done"
                item.message = "Stopped" if cancel_event.is_set() else "Ready to download"
                item.progress = "" if cancel_event.is_set() else "100%"
                item.files = [] if cancel_event.is_set() else files
                item.finished_at = time.time()
            return
        raise RuntimeError(last_line or "Conversion finished without creating an MP3 file")
    except Exception as exc:  # pragma: no cover - defensive background task guard
        if cancel_event.is_set():
            set_item_state(
                batch_id, item_id, status="stopped", message="Stopped",
                progress="", files=[], finished_at=time.time(),
            )
            return
        if cookie_required and cookie_path:
            reason = (
                "YouTube rejected cookies.txt. Export fresh YouTube cookies, replace the "
                "file, restart the app, and try again."
            )
        elif cookie_required:
            reason = (
                "YouTube requires cookies. Export YouTube cookies to cookies.txt, add the "
                "file to the app data folder, restart the app, and try again."
            )
        else:
            reason = last_line or str(exc)
        set_item_state(
            batch_id,
            item_id,
            status="error",
            message=reason[-240:],
            files=list_mp3_files(item_dir),
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
            cancel_events[(batch_id, item.id)] = threading.Event()

    for item in items:
        executor.submit(run_conversion, batch_id, item.id, item.url)

    return snapshot_job(job)


def retry_item(batch_id: str, item_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(batch_id)
        if job is None:
            raise FileNotFoundError("Batch not found")

        item = next((entry for entry in job.items if entry.id == item_id), None)
        if item is None:
            raise FileNotFoundError("Queue item not found")
        if item.status in {"queued", "running", "stopping"}:
            raise ValueError("This item is already queued or running")

        item.status = "queued"
        item.title = ""
        item.message = "Waiting"
        item.progress = ""
        item.files = []
        item.downloaded_files = []
        item.downloaded_at = None
        item.started_at = None
        item.finished_at = None
        cancel_events[(batch_id, item_id)] = threading.Event()
        url = item.url

    executor.submit(run_conversion, batch_id, item_id, url)

    with jobs_lock:
        job = jobs[batch_id]
        return snapshot_job(job)


def stop_item(batch_id: str, item_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(batch_id)
        if job is None:
            raise FileNotFoundError("Batch not found")
        item = next((entry for entry in job.items if entry.id == item_id), None)
        if item is None:
            raise FileNotFoundError("Queue item not found")
        if item.status not in {"queued", "running"}:
            raise ValueError("Only waiting or running items can be stopped")

        cancel_events[(batch_id, item_id)].set()
        if item.status == "queued":
            item.status = "stopped"
            item.message = "Stopped before conversion"
            item.finished_at = time.time()
        else:
            item.status = "stopping"
            item.message = "Stopping conversion"
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


def write_zip(
    zip_path: Path, file_keys: list[tuple[str, str, str]]
) -> tuple[Path, list[tuple[str, str, str]]]:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_zip = zip_path.with_name(f".{zip_path.name}.{uuid.uuid4().hex}.tmp")
    included: list[tuple[str, str, str]] = []
    used_names: set[str] = set()
    with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for batch_id, item_id, filename in file_keys:
            try:
                source = safe_lookup_file(batch_id, item_id, filename)
            except FileNotFoundError:
                continue
            archive_name = source.name
            if archive_name in used_names:
                archive_name = f"{item_id[:8]}-{archive_name}"
            used_names.add(archive_name)
            archive.write(source, archive_name)
            included.append((batch_id, item_id, filename))

    if not included:
        temporary_zip.unlink(missing_ok=True)
        raise FileNotFoundError("No converted MP3 files are available")

    os.replace(temporary_zip, zip_path)
    return zip_path, included


def build_zip(batch_id: str) -> tuple[Path, list[tuple[str, str, str]]]:
    with jobs_lock:
        job = jobs.get(batch_id)
        if job is None:
            raise FileNotFoundError("Batch not found")
        file_keys = [
            (batch_id, item.id, filename)
            for item in job.items
            if item.status == "done"
            for filename in item.files
        ]

    return write_zip(DOWNLOAD_DIR / batch_id / "youtube-mp3-batch.zip", file_keys)


class AppHandler(BaseHTTPRequestHandler):
    server_version = f"YouTubeBatchConverter/{APP_VERSION}"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return

        if parsed.path == "/api/jobs":
            self.send_json(snapshot_jobs())
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
                target, file_keys = build_zip(batch_id)
                if self.send_file(target, "application/zip"):
                    mark_files_downloaded(file_keys)
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
                if self.send_file(target, "audio/mpeg"):
                    mark_files_downloaded([(parts[0], parts[1], target.name)])
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return

        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs/clear-history":
            self.send_json(clear_job_items({"done", "error", "stopped"}))
            return

        if parsed.path == "/api/jobs/clear-completed":
            self.send_json(clear_job_items({"done"}))
            return

        stop_match = re.fullmatch(r"/api/jobs/([^/]+)/items/([^/]+)/stop", parsed.path)
        if stop_match:
            try:
                self.send_json(stop_item(stop_match.group(1), stop_match.group(2)))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return

        retry_match = re.fullmatch(r"/api/jobs/([^/]+)/items/([^/]+)/retry", parsed.path)
        if retry_match:
            try:
                data = retry_item(retry_match.group(1), retry_match.group(2))
                self.send_json(data)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return

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

    def send_file(self, path: Path, content_type: str) -> bool:
        filename = path.name
        ascii_filename = filename.encode("ascii", "ignore").decode("ascii").strip()
        if not ascii_filename:
            ascii_filename = f"download{path.suffix}"
        ascii_filename = ascii_filename.replace("\\", "_").replace("/", "_").replace('"', "")
        utf8_filename = quote(filename)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{utf8_filename}',
        )
        self.end_headers()
        try:
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False


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

    [hidden] {
      display: none !important;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background: linear-gradient(180deg, #f7f9fc 0, var(--paper) 280px);
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
      line-height: 0.98;
      letter-spacing: -1.5px;
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
      border-radius: 10px;
      box-shadow: 0 10px 28px rgba(30, 38, 56, 0.07);
    }

    .panel {
      padding: 18px;
      display: flex;
      flex-direction: column;
    }

    label {
      display: block;
      font-weight: 700;
      margin-bottom: 10px;
    }

    .url-inputs {
      width: 100%;
      height: 180px;
      overflow-y: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
    }

    .url-input {
      display: block;
      width: 100%;
      min-width: 0;
      height: 44px;
      border: 0;
      border-bottom: 1px solid #edf0f5;
      padding: 0 12px;
      background: transparent;
      color: var(--ink);
      font: 14px/1 Consolas, "Courier New", monospace;
      white-space: nowrap;
    }

    .url-input:last-child {
      border-bottom: 0;
    }

    .url-input:focus {
      outline: 0;
      background: #f9fbfb;
    }

    .url-inputs:focus-within,
    button:focus-visible,
    a.button:focus-visible {
      outline: 3px solid rgba(14, 124, 102, 0.22);
      outline-offset: 2px;
    }

    .input-hint {
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
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
      transition: background-color 160ms ease, box-shadow 160ms ease;
    }

    button:hover,
    .button:hover {
      background: var(--accent-dark);
      box-shadow: 0 5px 14px rgba(14, 124, 102, 0.18);
    }

    .secondary-button {
      background: #eef1f6;
      color: #334155;
    }

    .secondary-button:hover {
      background: #dfe5ee;
      box-shadow: 0 5px 14px rgba(51, 65, 85, 0.12);
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
      margin-top: auto;
      padding: 12px;
      border: 1px solid #ead5a5;
      border-radius: 6px;
      background: #fff8e6;
      color: #5f4400;
      font-size: 13px;
      line-height: 1.45;
    }

    .queue {
      min-height: clamp(360px, calc(100vh - 230px), 480px);
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }

    .queue-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 10px;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
    }

    .queue-summary {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px 12px;
    }

    .queue-tools {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      flex-wrap: wrap;
      gap: 6px;
    }

    .queue-action {
      min-height: 32px;
      padding: 0 10px;
      border: 1px solid var(--line);
      background: #f7f8fa;
      color: #334155;
      font-size: 12px;
    }

    .queue-action:hover {
      background: #eef1f6;
      box-shadow: 0 4px 12px rgba(51, 65, 85, 0.1);
    }

    .danger-button {
      color: var(--danger);
    }

    h2 {
      margin: 0;
      font-size: 18px;
      letter-spacing: 0;
    }

    .stats {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
    }

    .stat {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 8px;
      background: #fff;
    }

    .empty {
      min-height: 100%;
      padding: 48px 24px;
      display: grid;
      place-items: center;
      text-align: center;
    }

    .items {
      display: flex;
      flex-direction: column;
      gap: 0;
      flex: 1;
    }

    .batch-group + .batch-group {
      border-top: 4px solid #f1f3f7;
    }

    .batch-label {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 7px 16px;
      border-bottom: 1px solid var(--line);
      background: #f7f8fa;
      color: #445063;
      font-size: 12px;
      font-weight: 700;
    }

    .item {
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      border-left: 3px solid transparent;
      display: grid;
      gap: 8px;
      content-visibility: auto;
      contain-intrinsic-size: 104px;
    }

    .item.running {
      border-left-color: var(--blue);
      background: #fbfdff;
    }

    .item.stopping,
    .item.stopped {
      border-left-color: #718096;
    }

    .item.done {
      border-left-color: var(--ok);
    }

    .item.error {
      border-left-color: var(--danger);
      background: #fffdfd;
    }

    .item:last-child {
      border-bottom: 0;
    }

    .item-main {
      display: grid;
      grid-template-columns: 92px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }

    .badge {
      min-height: 26px;
      border-radius: 999px;
      padding: 5px 8px;
      text-align: center;
      font-size: 11px;
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

    .badge.stopping,
    .badge.stopped {
      background: #eef1f6;
      color: #445063;
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
      line-height: 1.3;
    }

    .message {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.3;
      overflow-wrap: anywhere;
      margin-top: 4px;
    }

    .download-state {
      display: inline-flex;
      margin-top: 6px;
      color: var(--ok);
      font-size: 12px;
      font-weight: 700;
    }

    .missing-file {
      color: var(--danger);
      font-size: 12px;
      font-weight: 700;
    }

    progress {
      width: 100%;
      height: 7px;
      accent-color: var(--accent);
    }

    .downloads {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }

    .downloads .button {
      min-height: 32px;
      padding: 0 10px;
      font-size: 12px;
      background: #243043;
    }

    .top-download {
      background: #243043;
      min-height: 36px;
      padding: 0 13px;
      font-size: 13px;
    }

    .downloads .button:hover,
    .top-download:hover {
      background: #172033;
      box-shadow: 0 5px 14px rgba(36, 48, 67, 0.18);
    }

    .error-text {
      color: var(--danger);
      font-weight: 700;
      margin-top: 10px;
      margin-bottom: 16px;
      min-height: 20px;
    }

    @media (min-width: 861px) and (max-height: 700px) {
      main {
        padding-top: 18px;
        padding-bottom: 28px;
      }

      header {
        margin-bottom: 16px;
      }

      h1 {
        font-size: clamp(30px, 4vw, 44px);
      }

      .url-inputs {
        height: 150px;
      }
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

      .queue-head {
        grid-template-columns: 1fr;
        align-items: stretch;
      }

      .actions {
        align-items: stretch;
        flex-direction: column;
      }

      .queue-tools {
        justify-content: stretch;
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
        <label for="url-1">YouTube links</label>
        <div class="url-inputs" id="urlInputs">
          <input class="url-input" id="url-1" type="url" inputmode="url" autocomplete="off" spellcheck="false" aria-describedby="urlHint" placeholder="Paste a YouTube link">
        </div>
        <p class="input-hint" id="urlHint" aria-live="polite">Paste a YouTube link above.</p>
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
          <div class="queue-summary">
            <h2>Queue</h2>
            <div class="stats" id="stats"></div>
          </div>
          <div class="queue-tools">
            <button class="queue-action danger-button" id="clearHistoryButton" type="button" hidden>Clear List</button>
            <button class="button top-download" id="downloadAllButton" type="button" hidden>Download All</button>
          </div>
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
    const urlInputs = document.querySelector("#urlInputs");
    const button = document.querySelector("#convertButton");
    const clearLinksButton = document.querySelector("#clearLinksButton");
    const linkCount = document.querySelector("#linkCount");
    const urlHint = document.querySelector("#urlHint");
    const errorText = document.querySelector("#errorText");
    const itemsNode = document.querySelector("#items");
    const statsNode = document.querySelector("#stats");
    const downloadAllButton = document.querySelector("#downloadAllButton");
    const clearHistoryButton = document.querySelector("#clearHistoryButton");
    const batches = new Map();
    let pollTimer = 0;
    let renderedSignature = "";

    let nextUrlInputId = 2;

    urlInputs.addEventListener("input", updateLinkCount);
    urlInputs.addEventListener("keydown", (event) => {
      if (!event.target.classList.contains("url-input") || event.key !== "Enter") return;
      event.preventDefault();

      const nextInput = event.target.nextElementSibling;
      if (nextInput) {
        nextInput.focus();
        return;
      }

      addUrlInput("", true);
    });
    urlInputs.addEventListener("paste", (event) => {
      if (!event.target.classList.contains("url-input")) return;
      const pastedLinks = event.clipboardData.getData("text")
        .split(/\r?\n/)
        .map((link) => link.trim())
        .filter(Boolean);
      if (pastedLinks.length < 2) return;

      event.preventDefault();
      event.target.value = pastedLinks[0];
      let previousInput = event.target;
      pastedLinks.slice(1).forEach((link) => {
        const input = createUrlInput(link);
        previousInput.after(input);
        previousInput = input;
      });
      updateLinkCount();
      previousInput.focus();
    });
    clearLinksButton.addEventListener("click", () => {
      resetUrlInputs();
      errorText.textContent = "";
    });
    downloadAllButton.addEventListener("click", downloadAll);
    clearHistoryButton.addEventListener("click", clearHistory);
    itemsNode.addEventListener("click", (event) => {
      if (event.target.closest(".download-link")) {
        setTimeout(refreshHistory, 1000);
      }
    });
    loadHistory();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      errorText.textContent = "";
      button.disabled = true;
      button.textContent = "Starting...";

      try {
        const response = await fetch("/api/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ urls: getUrlValues().join("\n") })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Unable to start batch");

        batches.set(data.id, data);
        renderedSignature = "";
        resetUrlInputs();
        renderHistory();
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
      pollTimer = setInterval(refreshHistory, 1500);
      refreshHistory();
    }

    function createUrlInput(value = "") {
      const input = document.createElement("input");
      input.className = "url-input";
      input.id = `url-${nextUrlInputId++}`;
      input.type = "url";
      input.inputMode = "url";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.setAttribute("aria-describedby", "urlHint");
      input.placeholder = "Paste a YouTube link";
      input.value = value;
      return input;
    }

    function addUrlInput(value = "", focus = false) {
      const input = createUrlInput(value);
      urlInputs.appendChild(input);
      if (focus) input.focus();
      updateLinkCount();
      return input;
    }

    function resetUrlInputs() {
      urlInputs.innerHTML = "";
      const input = addUrlInput();
      input.focus();
    }

    function getUrlValues() {
      return Array.from(urlInputs.querySelectorAll(".url-input"))
        .map((input) => input.value.trim())
        .filter(Boolean);
    }

    function updateLinkCount() {
      const count = getUrlValues().length;
      linkCount.textContent = `${count} ${count === 1 ? "link" : "links"} inserted`;
      urlHint.textContent = count
        ? "Press Enter to add another link."
        : "Paste a YouTube link above.";
    }

    async function loadHistory() {
      try {
        const active = await refreshHistory();
        if (active > 0) startPolling();
      } catch (error) {
        errorText.textContent = error.message;
      }
    }

    async function refreshHistory() {
      const response = await fetch("/api/jobs");
      const data = await response.json();
      if (!response.ok) {
        errorText.textContent = data.error || "Unable to refresh queue";
        clearInterval(pollTimer);
        return 0;
      }

      const nextSignature = JSON.stringify(data.jobs);
      if (nextSignature !== renderedSignature) {
        batches.clear();
        data.jobs.forEach((batch) => batches.set(batch.id, batch));
        renderedSignature = nextSignature;
        renderHistory();
      }
      const active = data.jobs.reduce(
        (total, batch) => total + batch.counts.queued + batch.counts.running + batch.counts.stopping,
        0
      );
      if (active === 0) clearInterval(pollTimer);
      return active;
    }

    async function retryItem(batchId, itemId) {
      errorText.textContent = "";

      try {
        const response = await fetch(`/api/jobs/${batchId}/items/${itemId}/retry`, {
          method: "POST"
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Unable to retry item");

        batches.set(batchId, data);
        renderedSignature = "";
        renderHistory();
        startPolling();
      } catch (error) {
        errorText.textContent = error.message;
      }
    }

    async function stopItem(batchId, itemId) {
      errorText.textContent = "";
      try {
        const response = await fetch(`/api/jobs/${batchId}/items/${itemId}/stop`, {
          method: "POST"
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Unable to stop item");
        batches.set(batchId, data);
        renderedSignature = "";
        renderHistory();
        if (data.counts.stopping > 0) startPolling();
      } catch (error) {
        errorText.textContent = error.message;
      }
    }

    async function clearHistory() {
      const confirmed = window.confirm(
        "Remove completed, failed, and stopped items from the current list? Active conversions will stay, and MP3 files will not be deleted."
      );
      if (!confirmed) return;

      clearHistoryButton.disabled = true;
      errorText.textContent = "";

      try {
        const response = await fetch("/api/jobs/clear-history", { method: "POST" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Unable to clear queue items");

        batches.clear();
        data.jobs.forEach((batch) => batches.set(batch.id, batch));
        renderedSignature = "";
        renderHistory();
        if (data.preservedActive > 0) startPolling();
      } catch (error) {
        errorText.textContent = error.message;
      } finally {
        clearHistoryButton.disabled = false;
      }
    }

    function downloadAll() {
      const urls = Array.from(batches.values()).flatMap((batch) => (
        batch.items.flatMap((item) => item.downloadUrls || [])
      ));
      if (!urls.length) return;

      downloadAllButton.disabled = true;
      urls.forEach((url, index) => {
        setTimeout(() => {
          const link = document.createElement("a");
          link.href = url;
          link.download = "";
          document.body.appendChild(link);
          link.click();
          link.remove();
        }, index * 350);
      });

      setTimeout(async () => {
        try {
          await refreshHistory();
        } catch (error) {
          errorText.textContent = error.message;
        } finally {
          downloadAllButton.disabled = false;
        }
      }, urls.length * 350 + 1000);
    }

    function renderHistory() {
      const history = Array.from(batches.values());
      const counts = history.reduce((total, batch) => ({
        total: total.total + batch.counts.total,
        queued: total.queued + batch.counts.queued,
        running: total.running + batch.counts.running,
        stopping: total.stopping + batch.counts.stopping,
        stopped: total.stopped + batch.counts.stopped,
        done: total.done + batch.counts.done,
        error: total.error + batch.counts.error
      }), { total: 0, queued: 0, running: 0, stopping: 0, stopped: 0, done: 0, error: 0 });

      statsNode.innerHTML = `
        <span class="stat">${counts.total} total</span>
        <span class="stat">${counts.queued} waiting</span>
        <span class="stat">${counts.running} running</span>
        <span class="stat">${counts.stopping} stopping</span>
        <span class="stat">${counts.stopped} stopped</span>
        <span class="stat">${counts.done} completed</span>
        <span class="stat">${counts.error} failed</span>
      `;

      const readyDownloads = history.flatMap((batch) => (
        batch.items.flatMap((item) => item.downloadUrls || [])
      ));
      downloadAllButton.hidden = readyDownloads.length === 0;
      clearHistoryButton.hidden = counts.done + counts.error + counts.stopped === 0;

      if (!history.length) {
        itemsNode.innerHTML = `<div class="empty"><p>No batch is running yet.</p></div>`;
        return;
      }

      itemsNode.innerHTML = history.map((batch, batchIndex) => {
        const batchTime = new Date(batch.createdAt * 1000).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit"
        });
        const items = batch.items.map((item) => {
        const progressValue = Number((item.progress || "0").replace("%", "")) || 0;
        const fileName = item.files && item.files.length ? item.files.join(", ") : "";
        const displayName = fileName || item.title || item.url;
        const downloads = item.downloadUrls.map((url, index) => (
          `<a class="button download-link" href="${url}" download>Download MP3 ${index + 1}</a>`
        )).join("");
        const retry = item.status === "error" || item.status === "stopped"
          ? `<button class="button retry-button" type="button" onclick="retryItem('${batch.id}', '${item.id}')">Retry</button>`
          : "";
        const stop = item.status === "queued" || item.status === "running"
          ? `<button class="button retry-button" type="button" onclick="stopItem('${batch.id}', '${item.id}')">Stop</button>`
          : "";
        const downloaded = item.files.length > 0
          && item.downloadedFiles.length === item.files.length
          ? `<span class="download-state">Downloaded</span>`
          : "";
        const missing = item.missingFiles.length
          ? `<div class="missing-file">${item.missingFiles.length === 1 ? "File is" : "Files are"} no longer available on disk</div>`
          : "";
        const statusLabel = item.status === "done" ? "completed" : item.status;

        return `
          <article class="item ${item.status}">
            <div class="item-main">
              <span class="badge ${item.status}">${statusLabel}</span>
              <div>
                <div class="url">${escapeHtml(displayName)}</div>
                <div class="message">${escapeHtml(item.message || "")}</div>
                ${downloaded}${missing}
              </div>
              <div class="downloads">${downloads}${retry}${stop}</div>
            </div>
            <progress max="100" value="${progressValue}"></progress>
          </article>
        `;
        }).join("");

        return `
          <section class="batch-group">
            <div class="batch-label">
              <span>Batch ${batchIndex + 1} · ${batchTime}</span>
              <span>${batch.items.length} ${batch.items.length === 1 ? "item" : "items"}</span>
            </div>
            ${items}
          </section>
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
    APP_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server = ThreadingHTTPServer((SERVER_HOST, SERVER_PORT), AppHandler)
    except OSError:
        server = ThreadingHTTPServer((SERVER_HOST, 0), AppHandler)
    port = int(server.server_address[1])
    url = f"http://{SERVER_HOST}:{port}"
    print(f"Opening {url} in your browser")
    print("Press Ctrl+C to stop the converter")
    browser_timer = threading.Timer(1.0, lambda: webbrowser.open(url))
    browser_timer.daemon = True
    browser_timer.start()

    if IS_FROZEN:
        try:
            import pystray
            from PIL import Image

            tray_image = Image.open(BUNDLE_DIR / "icon" / "icon1.png")

            def open_converter(icon: Any = None, item: Any = None) -> None:
                webbrowser.open(url)

            def open_downloads(icon: Any = None, item: Any = None) -> None:
                os.startfile(DOWNLOAD_DIR)  # type: ignore[attr-defined]

            def exit_converter(icon: Any, item: Any = None) -> None:
                icon.stop()

            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            tray = pystray.Icon(
                "youtube_mp3_converter",
                tray_image,
                "YouTube MP3 Converter",
                menu=pystray.Menu(
                    pystray.MenuItem("Open Converter", open_converter, default=True),
                    pystray.MenuItem("Open Downloads", open_downloads),
                    pystray.MenuItem("Exit", exit_converter),
                ),
            )
            tray.run()
            server.shutdown()
            server_thread.join(timeout=3)
            return
        except Exception as exc:
            print(f"Tray icon unavailable: {exc}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()
        executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
