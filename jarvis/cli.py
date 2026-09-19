"""Минимальный CLI жизненного цикла Jarvis."""

from __future__ import annotations

import argparse
import signal
import threading

from .application import JarvisApplication
from .infrastructure.config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Модульная event-thinking-action система Jarvis",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Файл переменных окружения (по умолчанию .env в корне проекта)",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    stop = threading.Event()
    received_signal = None

    def interrupt(signum, frame):
        nonlocal received_signal
        received_signal = signum
        stop.set()

    previous = {
        sig: signal.signal(sig, interrupt)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    app = None
    try:
        app = JarvisApplication(load_config(args.env_file)).start()
        print("Jarvis: система запущена. Ctrl+C — штатное завершение.", flush=True)
        while not stop.wait(0.2) and not app.shutdown_requested.is_set():
            pass
        if received_signal is None:
            return 0
        return 128 + (received_signal or signal.SIGINT)
    finally:
        if app is not None:
            app.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
