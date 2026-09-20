"""Handler, который рассылает новые PNG-снимки всем подписанным агентам."""

import base64
import re
from pathlib import Path

from jarvis.capabilities import event_definition, handler_definition, input_part
from jarvis.core.protocol import object_schema


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


def run(ctx):
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
            encoded = base64.b64encode(
                (SCREENSHOTS_DIR / name).read_bytes()
            ).decode("ascii")
            ctx.emit(
                "screenshot",
                {"text": name},
                parts=(input_part("image", "image/png", encoded),),
            )
        ctx.stop_event.wait(POLL_SECONDS)


def create_handler():
    captured = event_definition(
        "screenshot",
        "Новый PNG-снимок обнаружен в Images/Screenshots. В data присутствует "
        "только text с именем файла. Сам снимок прикреплён отдельной "
        "image-частью, а не текстом base64.",
        object_schema({"text": {"type": "string"}}),
    )
    return handler_definition(
        "screenshots.monitor",
        "Мониторит Images/Screenshots и публикует каждый новый файл, имя "
        "которого соответствует YYYY-MM-DD-HHMMSS_jarvis.png. Старые файлы "
        "игнорируются.",
        (captured,),
        run,
    )
