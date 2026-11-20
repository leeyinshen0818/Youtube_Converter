"""Convert one YouTube link to MP3 by running yt-dlp.exe as a subprocess.

UI-agnostic: progress is reported through plain callbacks that are called
from the thread that runs ``Conversion.run()``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

from core import saver, tools
from core.errors import FriendlyError, classify_error, missing_tool_error

# Markers printed by yt-dlp through --print / --progress-template.
MARK = "YTMP3"
TITLE_MARK = f"{MARK}:TITLE:"
FILE_MARK = f"{MARK}:FILE:"
DOWNLOAD_MARK = f"{MARK}:DL:"
POSTPROCESS_MARK = f"{MARK}:PP:"

AUDIO_QUALITY = "5"  # VBR, same as the old app (preferredquality "5")
MAX_TITLE_LENGTH = 150
TAIL_LINES = 40

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_WINDOWS = os.name == "nt"


class Phase(str, Enum):
    WAITING = "waiting"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"

    @property
    def is_active(self) -> bool:
        return self in (Phase.WAITING, Phase.DOWNLOADING, Phase.CONVERTING)

    @property
    def is_finished(self) -> bool:
        return not self.is_active


def default_output_dir() -> Path:
    return Path.home() / "Downloads" / "YouTube MP3 Converter"


@dataclass(frozen=True)
class ConverterConfig:
    """Everything a conversion needs. Build it with ``ConverterConfig.from_system``.

    ``command_prefix`` replaces ``[ytdlp]`` at the start of the command; tests
    use it to run a fake yt-dlp script through ``sys.executable``.
    """

    output_dir: Path
    temp_root: Path
    ytdlp: Path | None
    ffmpeg: Path | None
    node: Path | None = None
    cookies: Path | None = None
    command_prefix: tuple[str, ...] | None = None

    @classmethod
    def from_system(
        cls,
        output_dir: Path | None = None,
        env: tools.ToolEnvironment | None = None,
        use_cookies: bool = True,
    ) -> "ConverterConfig":
        env = env or tools.default_environment()
        found = tools.locate_tools(env)
        return cls(
            output_dir=output_dir or default_output_dir(),
            temp_root=tools.temp_root(env),
            ytdlp=found.ytdlp,
            ffmpeg=found.ffmpeg,
            node=found.node,
            cookies=tools.find_cookies(env) if use_cookies else None,
        )


@dataclass(frozen=True)
class ConversionResult:
    phase: Phase  # DONE, FAILED or STOPPED
    title: str = ""
    output_path: Path | None = None
    error: FriendlyError | None = None
    output_tail: tuple[str, ...] = ()


@dataclass
class ConversionCallbacks:
    on_title: Callable[[str], None] | None = None
    on_progress: Callable[[float | None], None] | None = None  # percent 0-100
    on_phase: Callable[[Phase], None] | None = None


# --- command ---------------------------------------------------------------


def build_command(config: ConverterConfig, url: str, work_dir: Path) -> list[str]:
    if config.command_prefix:
        prefix = list(config.command_prefix)
    elif config.ytdlp is not None:
        prefix = [str(config.ytdlp)]
    else:
        raise ValueError("yt-dlp was not found")

    command = prefix + [
        "--ignore-config",
        "--no-playlist",
        "--newline",
        "--progress",
        "--color", "never",
        "--encoding", "utf-8",
        "--no-mtime",
        "--format", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", AUDIO_QUALITY,
        "--paths", str(work_dir),
        # Short ASCII-safe name in the work folder; the real title is used
        # when the finished file is moved to the output folder.
        "--output", "%(id)s.%(ext)s",
        "--print", f"before_dl:{TITLE_MARK}%(title)s",
        "--print", f"after_move:{FILE_MARK}%(filepath)s",
        "--progress-template",
        f"download:{DOWNLOAD_MARK}%(progress.status)s"
        f"|%(progress.downloaded_bytes)s|%(progress.total_bytes)s"
        f"|%(progress.total_bytes_estimate)s",
        "--progress-template",
        f"postprocess:{POSTPROCESS_MARK}%(progress.status)s|%(progress.postprocessor)s",
    ]
    if config.ffmpeg is not None:
        # The folder, so yt-dlp also finds ffprobe next to ffmpeg.
        command += ["--ffmpeg-location", str(config.ffmpeg.parent)]
    if config.node is not None:
        # Same as the old app's js_runtimes={"node": {"path": ...}}.
        command += ["--js-runtimes", f"node:{config.node}"]
    if config.cookies is not None and _exists(config.cookies):
        command += ["--cookies", str(config.cookies)]
    command += ["--", url]
    return command


def _exists(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


# --- output parsing ----------------------------------------------------------


@dataclass(frozen=True)
class OutputEvent:
    kind: str  # "title", "file", "progress", "download_finished", "postprocess"
    value: str = ""
    percent: float | None = None


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_line(line: str) -> OutputEvent | None:
    """Turn one line of yt-dlp output into an event, or None for other text."""
    line = line.rstrip("\r\n")
    index = line.find(f"{MARK}:")
    if index < 0:
        return None
    line = line[index:]

    if line.startswith(TITLE_MARK):
        return OutputEvent("title", line[len(TITLE_MARK):].strip())
    if line.startswith(FILE_MARK):
        return OutputEvent("file", line[len(FILE_MARK):].strip())
    if line.startswith(DOWNLOAD_MARK):
        parts = line[len(DOWNLOAD_MARK):].split("|")
        status = parts[0].strip() if parts else ""
        if status == "finished":
            return OutputEvent("download_finished", percent=100.0)
        if status != "downloading" or len(parts) < 4:
            return None
        done = _number(parts[1])
        total = _number(parts[2]) or _number(parts[3])
        percent = None
        if done is not None and total:
            percent = max(0.0, min(100.0, done * 100.0 / total))
        return OutputEvent("progress", percent=percent)
    if line.startswith(POSTPROCESS_MARK):
        status, _, name = line[len(POSTPROCESS_MARK):].partition("|")
        return OutputEvent("postprocess", value=f"{status.strip()}|{name.strip()}")
    return None


# --- file names --------------------------------------------------------------

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}


def sanitize_filename(title: str, fallback: str = "audio") -> str:
    """Make ``title`` safe as a Windows file name (without extension)."""
    text = unicodedata.normalize("NFC", title or "")
    text = _INVALID_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > MAX_TITLE_LENGTH:
        text = text[:MAX_TITLE_LENGTH].rstrip(" .")
    if not text:
        text = fallback
    if text.split(".")[0].upper() in _RESERVED:
        text = f"{text}_"
    return text


def move_to_output(source: Path, output_dir: Path, title: str, fallback: str) -> Path:
    """Move the finished MP3 to ``output_dir`` as "<title>.mp3" (unique name)."""
    return saver.move_file(source, output_dir, sanitize_filename(title, fallback))


# --- process control -----------------------------------------------------------


def kill_process_tree(process: subprocess.Popen) -> None:
    """Kill the process and everything it started (ffmpeg, node, ...)."""
    if process.poll() is not None:
        return
    if _WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def remove_dir(path: Path, attempts: int = 10, delay: float = 0.2) -> bool:
    """Delete a folder, retrying while Windows still holds files open."""
    for _ in range(attempts):
        if not path.exists():
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(delay)
    return not path.exists()


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for programs the app starts (yt-dlp, which starts ffmpeg
    and node).

    The one-file exe's bootloader leaves ``_PYI_*`` variables behind; yt-dlp.exe
    is itself a PyInstaller one-file program and would take them as its own
    and look for its files in this app's temporary folder. PyInstaller's
    documented fix is ``PYINSTALLER_RESET_ENVIRONMENT=1``; the variables are
    also dropped.
    """
    env = {
        key: value
        for key, value in (os.environ if base is None else base).items()
        if not key.upper().startswith("_PYI_") and key.upper() != "_MEIPASS2"
    }
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _subprocess_env() -> dict[str, str]:
    env = child_env()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# --- conversion ----------------------------------------------------------------


class Conversion:
    """One conversion. Call ``run()`` on a worker thread; ``stop()`` from any thread."""

    def __init__(
        self,
        config: ConverterConfig,
        url: str,
        job_id: str,
        callbacks: ConversionCallbacks | None = None,
        fallback_name: str = "audio",
    ) -> None:
        self.config = config
        self.url = url
        self.job_id = job_id
        self.callbacks = callbacks or ConversionCallbacks()
        self.fallback_name = fallback_name
        self.work_dir = config.temp_root / job_id
        self._lock = threading.Lock()
        self._stop_requested = False
        self._process: subprocess.Popen | None = None
        self._phase = Phase.WAITING
        self._title = ""
        self._last_percent: int | None = None

    # public

    @property
    def stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            process = self._process
        if process is not None:
            kill_process_tree(process)

    def run(self) -> ConversionResult:
        try:
            return self._run()
        finally:
            remove_dir(self.work_dir)

    # internals

    def _emit_phase(self, phase: Phase) -> None:
        if phase == self._phase:
            return
        self._phase = phase
        if self.callbacks.on_phase:
            self.callbacks.on_phase(phase)

    def _emit_progress(self, percent: float | None) -> None:
        bucket = None if percent is None else int(percent)
        if bucket == self._last_percent:
            return
        self._last_percent = bucket
        if self.callbacks.on_progress:
            self.callbacks.on_progress(percent)

    def _emit_title(self, title: str) -> None:
        if not title or title == self._title:
            return
        self._title = title
        if self.callbacks.on_title:
            self.callbacks.on_title(title)

    def _finish(self, phase: Phase, **kwargs) -> ConversionResult:
        if phase is not Phase.DONE:
            self._emit_progress(None)
        self._emit_phase(phase)
        return ConversionResult(phase=phase, title=self._title, **kwargs)

    def _stopped(self) -> ConversionResult:
        return self._finish(Phase.STOPPED)

    def _run(self) -> ConversionResult:
        if self.stop_requested:
            return self._stopped()
        if self.config.command_prefix is None and self.config.ytdlp is None:
            return self._finish(Phase.FAILED, error=missing_tool_error("yt-dlp"))
        if self.config.ffmpeg is None:
            return self._finish(Phase.FAILED, error=missing_tool_error("ffmpeg"))

        remove_dir(self.work_dir)
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._finish(Phase.FAILED, error=classify_error(str(exc)))

        command = build_command(self.config, self.url, self.work_dir)
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        output_file: Path | None = None

        with self._lock:
            if self._stop_requested:
                return self._stopped()
            try:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.work_dir),
                    env=_subprocess_env(),
                    creationflags=CREATE_NO_WINDOW if _WINDOWS else 0,
                )
            except OSError as exc:
                return self._finish(Phase.FAILED, error=classify_error(str(exc)))
            process = self._process

        self._emit_phase(Phase.DOWNLOADING)
        assert process.stdout is not None
        try:
            for raw in iter(process.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if self.stop_requested:
                    break
                event = parse_line(line)
                if event is None:
                    if line.strip():
                        tail.append(line.strip())
                    continue
                if event.kind == "title":
                    self._emit_title(event.value)
                elif event.kind == "file":
                    output_file = Path(event.value)
                elif event.kind == "progress":
                    self._emit_phase(Phase.DOWNLOADING)
                    self._emit_progress(event.percent)
                elif event.kind in ("download_finished", "postprocess"):
                    self._emit_progress(None)
                    self._emit_phase(Phase.CONVERTING)
        finally:
            if self.stop_requested:
                kill_process_tree(process)
            process.stdout.close()
            return_code = process.wait()
            with self._lock:
                self._process = None

        if self.stop_requested:
            return self._stopped()

        if return_code != 0:
            return self._finish(
                Phase.FAILED, error=classify_error(_error_text(tail)), output_tail=tuple(tail)
            )

        mp3 = self._find_mp3(output_file)
        if mp3 is None:
            text = _error_text(tail) or "Conversion finished without creating an MP3 file"
            return self._finish(Phase.FAILED, error=classify_error(text), output_tail=tuple(tail))

        try:
            target = move_to_output(mp3, self.config.output_dir, self._title, self.fallback_name)
        except OSError as exc:
            return self._finish(Phase.FAILED, error=classify_error(str(exc)), output_tail=tuple(tail))

        self._emit_progress(100.0)
        return self._finish(Phase.DONE, output_path=target, output_tail=tuple(tail))

    def _find_mp3(self, reported: Path | None) -> Path | None:
        if reported is not None and reported.suffix.lower() == ".mp3" and _exists(reported):
            return reported
        matches = sorted(self.work_dir.glob("*.mp3"))
        return matches[0] if matches else None


def _error_text(tail: Sequence[str]) -> str:
    """Prefer yt-dlp's ERROR lines; otherwise use the last lines of output."""
    errors = [line for line in tail if line.startswith("ERROR")]
    lines = errors or list(tail)[-10:]
    return "\n".join(lines)


def convert(
    config: ConverterConfig,
    url: str,
    job_id: str,
    callbacks: ConversionCallbacks | None = None,
) -> ConversionResult:
    """Convenience wrapper: run one conversion on the current thread."""
    return Conversion(config, url, job_id, callbacks).run()


__all__ = [
    "Phase",
    "ConverterConfig",
    "ConversionCallbacks",
    "ConversionResult",
    "Conversion",
    "OutputEvent",
    "build_command",
    "parse_line",
    "sanitize_filename",
    "move_to_output",
    "kill_process_tree",
    "remove_dir",
    "default_output_dir",
    "convert",
]
