"""Action, который запускает hyprshot и возвращает подтверждение."""

import os
from datetime import datetime
from pathlib import Path

from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    directory = Path.home() / "Images" / "Screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("%Y-%m-%d-%H%M%S_jarvis.png")
    argv = (
        "hyprshot",
        "-m", "output",
        "-m", "active",
        "-o", str(directory),
        "-f", filename,
    )
    pid = os.fork()
    if pid != 0:
        os.waitpid(pid, 0)
        return {"accepted": True, "filename": filename}
    try:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        with open(os.devnull, "rb") as devnull_r, open(os.devnull, "ab") as devnull_w:
            os.dup2(devnull_r.fileno(), 0)
            os.dup2(devnull_w.fileno(), 1)
            os.dup2(devnull_w.fileno(), 2)
        os.execvp(argv[0], argv)
    except BaseException:
        os._exit(127)


def create_action():
    return action_definition(
        "screenshots.capture",
        "Запустить hyprshot для активного монитора и сохранить PNG в "
        "~/Images/Screenshots с именем YYYY-MM-DD-HHMMSS_jarvis.png. "
        "Возвращает подтверждение запуска и имя файла; сам снимок позже "
        "приходит отдельным событием screenshot от handler.",
        object_schema({}),
        object_schema(
            {
                "accepted": {"type": "boolean"},
                "filename": {"type": "string"},
            }
        ),
        run,
    )
