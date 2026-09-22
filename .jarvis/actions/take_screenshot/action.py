"""Action: снять экран и вернуть PNG прямо в результате."""

import base64
import subprocess
import time
from datetime import datetime
from pathlib import Path

from jarvis.capabilities import PENDING, action_definition, input_part
from jarvis.core.protocol import object_schema


WAIT_SECONDS = 15.0
POLL_SECONDS = 0.2


def run(data, context):
    directory = Path.home() / "Images" / "Screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("%Y-%m-%d-%H%M%S_jarvis.png")
    try:
        subprocess.run(
            (
                "hyprshot",
                "-m", "output",
                "-m", "active",
                "-o", str(directory),
                "-f", filename,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=WAIT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"filename": None, "error": "hyprshot timed out"}
    deadline = time.monotonic() + WAIT_SECONDS
    target = directory / filename
    while time.monotonic() < deadline:
        if target.is_file() and target.stat().st_size > 0:
            break
        time.sleep(POLL_SECONDS)
    else:
        return {"filename": None, "error": "screenshot file did not appear"}
    encoded = base64.b64encode(target.read_bytes()).decode("ascii")
    context.complete(
        {"filename": filename, "error": None},
        parts=(input_part("image", "image/png", encoded),),
    )
    return PENDING


def create_action():
    return action_definition(
        "Снять активный монитор через hyprshot и вернуть PNG прямо в "
        "результате действия отдельной image-частью; base64 в data нет.",
        object_schema({}),
        object_schema(
            {
                "filename": {"type": ["string", "null"]},
                "error": {"type": ["string", "null"]},
            }
        ),
        run,
    )
