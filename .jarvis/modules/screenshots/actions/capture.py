"""Action, который запускает hyprshot fire-and-forget.

Команда отсоединяется double-fork: родитель никого не ждёт, зомби-процессов
не остаётся, таймаутов и проверок кода возврата нет.
"""

import os
from datetime import datetime
from pathlib import Path


def build_capture(data, context):
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
        return
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
