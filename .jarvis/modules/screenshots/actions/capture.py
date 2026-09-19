"""Action, который непосредственно выполняет команду создания PNG."""

import subprocess
from datetime import datetime
from pathlib import Path


def build_capture(data, context):
    directory = Path.home() / "Images" / "Screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("%Y-%m-%d-%H%M%S_jarvis.png")
    completed = subprocess.run(
        [
            "hyprshot",
            "-m",
            "output",
            "-m",
            "active",
            "-o",
            str(directory),
            "-f",
            filename,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        reason = f"hyprshot завершился с кодом {completed.returncode}"
        if stderr:
            reason = f"{reason}; stderr: {stderr}"
        raise RuntimeError(reason)
