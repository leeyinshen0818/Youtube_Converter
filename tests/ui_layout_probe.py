"""Offscreen layout probe for one device pixel ratio (run in its own process).

Usage: python tests/ui_layout_probe.py <dpr> <WxH> [<WxH> ...] [--grab DIR]

QT_SCALE_FACTOR is process-wide, so every DPR needs its own process. It is
set here, in the test harness only; the app never sets any QT_* variable.
Sizes are logical pixels of the window's client area. Prints one JSON line
per size.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

DPR = sys.argv[1]
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = DPR
os.environ.pop("QT_ENABLE_HIGHDPI_SCALING", None)
fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
if fonts.is_dir():
    os.environ.setdefault("QT_QPA_FONTDIR", str(fonts))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QLabel,
    QLayout,
    QWidget,
)

from core.converter import ConversionResult, Phase  # noqa: E402
from core.errors import classify_error  # noqa: E402
from core.jobqueue import JobQueue  # noqa: E402
from core.urls import parse_links  # noqa: E402
from ui import theme  # noqa: E402


class FakeConversion:
    """Puts items into every visible state and waits (no processes)."""

    def __init__(self, job_id, url, video_id, callbacks) -> None:
        self.callbacks = callbacks
        self.video_id = video_id
        self.stopped = threading.Event()

    def run(self) -> ConversionResult:
        number = int(self.video_id[-1])
        self.callbacks.on_phase(Phase.DOWNLOADING)
        self.callbacks.on_title(f"A long song title number {number} (Official Music Video) [HD Remaster 2024]")
        self.callbacks.on_progress(40.0)
        if number == 1:
            self.callbacks.on_phase(Phase.CONVERTING)
        if number in (2, 4):  # 2 stays Ready, 4 is marked Saved below
            return ConversionResult(Phase.DONE, title=f"Done song {number}", output_path=Path(f"song{number}.mp3"))
        if number == 3:
            return ConversionResult(
                Phase.FAILED, error=classify_error("ERROR: Sign in to confirm you're not a bot")
            )
        self.stopped.wait(60)
        return ConversionResult(Phase.STOPPED)

    def stop(self) -> None:
        self.stopped.set()


def layout_widgets(layout: QLayout) -> list[QWidget]:
    found: list[QWidget] = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is not None:
            found.append(item.widget())
        elif item.layout() is not None:
            found.extend(layout_widgets(item.layout()))
    return found


def name_of(widget: QWidget) -> str:
    text = ""
    if isinstance(widget, (QLabel, QAbstractButton)):
        text = widget.text()[:30]
    return f"{type(widget).__name__}({widget.objectName() or text})"


def check(window: QWidget) -> dict:
    problems: list[str] = []
    # 1. Sibling overlap in every layout.
    for widget in [window, *window.findChildren(QWidget)]:
        if not widget.isVisible() or widget.layout() is None:
            continue
        children = [child for child in layout_widgets(widget.layout()) if child.isVisible()]
        for i, first in enumerate(children):
            for second in children[i + 1:]:
                if first.geometry().intersects(second.geometry()):
                    problems.append(f"overlap: {name_of(first)} / {name_of(second)}")
    # 2. Labels and buttons not smaller than they need.
    for label in window.findChildren(QLabel):
        if not label.isVisible() or not label.text():
            continue
        if label.wordWrap():
            need = label.heightForWidth(label.width())
            if label.height() + 1 < need:
                problems.append(f"clipped wrap label: {name_of(label)} {label.height()} < {need}")
        else:
            hint = label.minimumSizeHint()
            if label.width() + 1 < hint.width() or label.height() + 1 < hint.height():
                problems.append(f"clipped label: {name_of(label)} {label.size().toTuple()} < {hint.toTuple()}")
    for button in window.findChildren(QAbstractButton):
        if not button.isVisible() or not button.text():
            continue
        hint = button.minimumSizeHint()
        if button.width() + 1 < hint.width() or button.height() + 1 < max(hint.height(), button.minimumHeight()):
            problems.append(f"clipped button: {name_of(button)} {button.size().toTuple()} < {hint.toTuple()}")
    return {"problems": problems}


def main() -> int:
    sizes = [arg for arg in sys.argv[2:] if "x" in arg and not arg.startswith("--")]
    grab_dir = None
    if "--grab" in sys.argv:
        grab_dir = Path(sys.argv[sys.argv.index("--grab") + 1])
        grab_dir.mkdir(parents=True, exist_ok=True)

    theme.configure_high_dpi()
    app = QApplication([])
    theme.apply_theme(app)
    from ui.main_window import MainWindow

    window = MainWindow(
        queue_factory=lambda bridge: JobQueue(FakeConversion, max_workers=5, **bridge.callbacks())
    )
    window.confirm = lambda *args: True  # answer "Quit" (no modal dialog)
    window.show()
    links = "\n".join(f"https://youtu.be/vid0000000{index}" for index in range(6))
    window.input_panel.input.setPlainText(links + "\nnot a link\nhttps://www.youtube.com/@channel")
    window.input_panel.convert()

    deadline = 200
    while deadline and (window.queue.counts()[Phase.FAILED] < 1 or window.queue.counts()[Phase.DONE] < 2):
        app.processEvents()
        threading.Event().wait(0.01)
        deadline -= 1
    saved = [item for item in window.queue.snapshot() if item.title == "Done song 4"]
    for item in saved:
        window.queue.mark_saved(item.id, Path("C:/Users/x/Downloads/YouTube MP3 Converter/Done song 4.mp3"))
    # Worst case for the header: summary, Download All and the "All done" line.
    window.queue_panel.set_done_message("All done: 2 ready to download · 1 saved · 1 failed · 3 stopped")
    for _ in range(5):
        app.processEvents()

    for size in sizes:
        width, height = (int(value) for value in size.split("x"))
        window.resize(width, height)
        for _ in range(5):
            app.processEvents()
        result = check(window)
        convert = window.input_panel.convert_button
        top_left = convert.mapTo(window, convert.rect().topLeft())
        convert_rect = QRect(top_left, convert.size())
        view = window.queue_panel.view
        first_row = view.sizeHintForRow(0) if window.model.rowCount() else 0
        result.update(
            size=size,
            dpr=window.devicePixelRatioF(),
            window=[window.width(), window.height()],
            minimum=list(window.minimumSizeHint().toTuple()),
            convert=[convert_rect.x(), convert_rect.y(), convert_rect.width(), convert_rect.height()],
            convert_visible=convert.isVisible() and window.rect().contains(convert_rect),
            convert_min_height=convert.minimumHeight(),
            input_lines=window.input_panel.input.viewport().height()
            // max(1, theme.line_height(window.input_panel.input)),
            queue_rows=view.viewport().height() // max(1, first_row),
            compact_notice=window.input_panel.notice.compact,
            header_buttons_visible=all(
                button.isVisible()
                for button in (
                    window.queue_panel.download_all_button,
                    window.queue_panel.open_folder_button,
                    window.queue_panel.clear_button,
                )
            ),
            summary=window.queue_panel.summary.text(),
        )
        if grab_dir is not None:
            window.grab().save(str(grab_dir / f"dpr{DPR}-{size}.png"))
        print(json.dumps(result), flush=True)

    window.queue.shutdown()
    window.queue.join(5)
    window.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
