"""Простое действие: показать десктопное уведомление через notify-send."""

import os

from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    message = data["message"]
    argv = (
        "notify-send",
        "-a", "Jarvis",
        "-u", "normal",
        "Jarvis",
        message,
    )
    pid = os.fork()
    if pid != 0:
        _, status = os.waitpid(pid, 0)
        return {"accepted": os.waitstatus_to_exitcode(status) == 0}
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
        "Показать экранное уведомление через notify-send. Команда "
        "отсоединяется double-fork: действие возвращает подтверждение "
        "запуска и не ждёт показа уведомления.",
        object_schema({"message": {"type": "string"}}),
        object_schema({"accepted": {"type": "boolean"}}),
        run,
    )
