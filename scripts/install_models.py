#!/usr/bin/env python3
"""Prepare every Jarvis model; --check is strictly offline and read-only."""

from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Установка всех локальных моделей J.A.R.V.I.S."
    )
    parser.add_argument(
        "--jarvis-dir",
        type=Path,
        default=PROJECT / ".jarvis",
        help="Каталог данных J.A.R.V.I.S.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Заново скачать веса, не доверяя прежним копиям и кэшу",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Только проверить файлы и контрольные суммы; ничего не скачивать",
    )
    args = parser.parse_args(argv)
    if args.check and args.force:
        parser.error("--check и --force нельзя использовать вместе")
    try:
        from jarvis.infrastructure.models import download_models, model_status
        from jarvis.infrastructure.console import configure_logging
        from jarvis.speech.config import build_config

        root = args.jarvis_dir.expanduser().resolve()
        if not args.check:
            configure_logging(root)
            print(
                "Подготовка STT, VAD, TTS и Qwen. Незавершённые загрузки можно продолжить повторным запуском.",
                flush=True,
            )
            download_models(root, force=args.force)
        status = model_status(root)
        for item in status:
            print(
                f"{'OK' if item['ready'] else 'MISSING/INVALID'} {item['name']}: {item['path']}"
            )
            for problem in item["problems"]:
                print("  " + problem)
        speech = build_config(root)
        references_ready = (
            speech.tts_reference.is_file() and speech.tts_reference_text.is_file()
        )
        print(f"Голосовой референс: {'OK' if references_ready else 'MISSING'}")
        print(
            f"s2: {shutil.which(str(speech.tts_server_bin)) or 'не найден; веса TTS устанавливаются независимо от бинарника'}"
        )
        return 0 if all(item["ready"] for item in status) and references_ready else 1
    except KeyboardInterrupt:
        print("Установка прервана. Повторите запуск для продолжения.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        print(
            "Запускайте скрипт Python-интерпретатором окружения проекта. Подробности: runtime/logs/core.log.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
