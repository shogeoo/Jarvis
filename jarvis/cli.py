"""Минимальный CLI жизненного цикла Jarvis."""

from __future__ import annotations

import argparse
import signal
import threading
import sys

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
    parser.add_argument(
        "--download-models",
        action="store_true",
        help="Скачать модели речи и выйти; s2 устанавливается отдельно",
    )
    parser.add_argument("--message", help="Передать main стартовое текстовое поручение")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.env_file)
    if args.download_models:
        from .infrastructure.models import download_models

        try:
            download_models(config.jarvis_dir)
            return 0
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 1
    from .speech.cuda import prepare

    prepare()
    from .application import JarvisApplication
    from .core.protocol import Event

    stop = threading.Event()
    received_signal = None

    def interrupt(signum, frame):
        nonlocal received_signal
        received_signal = signum
        stop.set()

    previous = {
        sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    app = None
    try:
        app = JarvisApplication(config)
        app.start()
        if app.main_agent is None:
            print("Main restore failed; persisted data preserved.", file=sys.stderr)
            return 1
        if args.message and app.main_agent is not None:
            app.bus.publish(
                Event(
                    "user_message",
                    {"text": args.message},
                    target=app.main_agent.agent_id,
                    source="cli",
                )
            )
        print("Jarvis: система запущена. Ctrl+C — штатное завершение.", flush=True)
        while not stop.wait(0.2):
            pass
        return 128 + (received_signal or signal.SIGINT)
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        if app is not None:
            app.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
