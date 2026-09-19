"""Handler, который рассылает новые PNG-снимки всем подписанным агентам."""

import base64
import re
from pathlib import Path

from jarvis.modules import input_part


SCREENSHOTS_DIR = Path.home() / "Images" / "Screenshots"
FILENAME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}_jarvis\.png$")
POLL_SECONDS = 0.5


def _files(directory):
    try:
        return {
            entry.name: entry.stat().st_size
            for entry in directory.iterdir()
            if entry.is_file()
        }
    except OSError:
        return {}


def build_monitor(ctx):
    seen = set(_files(SCREENSHOTS_DIR))
    stable_sizes = {}
    while not ctx.stop_event.is_set():
        current = _files(SCREENSHOTS_DIR)
        for name, size in sorted(current.items()):
            if name in seen or not FILENAME_PATTERN.fullmatch(name):
                continue
            if stable_sizes.get(name) != size:
                stable_sizes[name] = size
                continue
            stable_sizes.pop(name, None)
            seen.add(name)
            path = SCREENSHOTS_DIR / name
            try:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError as exc:
                ctx.emit(
                    "screenshots.error",
                    {"name": name, "reason": str(exc)},
                )
                continue
            ctx.emit(
                "screenshots.captured",
                {"name": name},
                parts=(input_part("image", "image/png", encoded),),
            )
        ctx.stop_event.wait(POLL_SECONDS)
