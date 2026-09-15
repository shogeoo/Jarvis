"""CLI: реальное время STT.

Микрофон -> VAD -> запись чанка в WAV -> whisper -> строка текста.
Если задана модель OpenAI-совместимого API, расшифровка также уходит в
нейросеть, а её ответ печатается в консоль.
"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import threading

from .audio import FRAME_SIZE, MicStream
from .printer import Printer
from .transcribe import Transcriber
from .vad import Segmenter, SileroVAD, ensure_model


def _list_devices(args):
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        print(
            "pactl не найден. Установи pulseaudio-utils "
            "(Arch: sudo pacman -S pulseaudio-utils).",
            file=os.sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"pactl завершился с ошибкой: {exc}", file=os.sys.stderr)
        return 1
    print("Источники PulseAudio (имя из 2-й колонки — для --input-device):")
    print(out.stdout.strip() or "  (нет источников)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jarvis",
        description="STT в реальном времени: VAD -> чанки -> whisper -> строки",
    )
    p.add_argument("--model", default="large-v3-turbo", help="Модель faster-whisper")
    p.add_argument("--device", default="cuda", choices=["cuda", "cpu"],
                   help="Устройство (без фолбека: если CUDA нет — ошибка с подсказкой)")
    p.add_argument("--compute-type", default=None,
                   choices=["float16", "float32", "int8"],
                   help="Точность вычислений; по умолчанию float16 на CUDA")
    p.add_argument("--language", default=None,
                   help="Жёстко заданный язык, напр. ru; по умолчанию — авто из --languages")
    p.add_argument("--languages", default="ru,en",
                   help="Допустимые языки через запятую; мисдетект перегоняется на первый")
    p.add_argument("--pre-roll", type=float, default=0.5,
                   help="Запас аудио перед стартом речи, с")
    p.add_argument("--chunk-silence", type=float, default=2.0,
                   help="Сколько секунд VAD=false, чтобы завершить запись сегмента, с")
    p.add_argument("--threshold", type=float, default=0.5, help="Порог VAD")
    p.add_argument("--sample-rate", type=int, default=16000)
    p.add_argument("--out-dir", default="segments", help="Каталог WAV-чанков")
    p.add_argument("--keep-audio", action="store_true",
                   help="Не удалять WAV-чанки после расшифровки")
    p.add_argument("--input-device", default=None, help="Имя источника PulseAudio")
    p.add_argument("--list-devices", action="store_true",
                   help="Список источников PulseAudio и выход")
    p.add_argument("--llm-model", default=os.environ.get("OPENAI_MODEL"),
                   help="Модель OpenAI-совместимого API (env OPENAI_MODEL); "
                        "если не задана — нейросеть выключена")
    p.add_argument("--llm-base-url", default=os.environ.get("OPENAI_BASE_URL"),
                   help="Эндпоинт API (env OPENAI_BASE_URL)")
    p.add_argument("--llm-api-key", default=os.environ.get("OPENAI_API_KEY"),
                   help="API-ключ (env OPENAI_API_KEY)")
    p.add_argument("--llm-system", default=os.environ.get("JARVIS_SYSTEM"),
                   help="System-промпт ассистента (env JARVIS_SYSTEM)")
    p.add_argument("--no-llm", action="store_true",
                   help="Отключить нейросеть, только расшифровка")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_devices:
        return _list_devices(args)

    ensure_model()
    printer = Printer()

    assistant = None
    if not args.no_llm and args.llm_model:
        from .assistant import Assistant

        assistant = Assistant(
            model=args.llm_model,
            base_url=args.llm_base_url,
            api_key=args.llm_api_key,
            system=args.llm_system,
            on_reply=printer.print_reply,
        ).start()

    vad = SileroVAD(threshold=args.threshold)
    segmenter = Segmenter(
        vad, out_dir=args.out_dir, pre_roll=args.pre_roll,
        chunk_silence=args.chunk_silence, keep_audio=args.keep_audio,
    )
    out_queue: "queue.Queue[dict]" = queue.Queue()
    stop = threading.Event()

    def worker():
        tr = Transcriber(
            args.model, args.device, args.language, args.compute_type,
            languages=tuple(x.strip() for x in args.languages.split(",") if x.strip()),
        )
        try:
            tr.load()
        except RuntimeError as exc:
            print(exc, file=os.sys.stderr)
            stop.set()
            return
        while True:
            item = out_queue.get()
            if item is None:
                break
            text = tr.transcribe_file(item["path"])
            if not item["keep"]:
                try:
                    os.remove(item["path"])
                except OSError:
                    pass
            printer.print_segment(text)
            if assistant is not None:
                assistant.submit(text)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    mic = MicStream(args.sample_rate, FRAME_SIZE, args.input_device)
    try:
        mic.start()
        print("Говорите. Ctrl+C — выход.", flush=True)
        while not stop.is_set():
            try:
                frame = mic.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            result = segmenter.feed(frame)
            if result:
                out_queue.put(result)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        mic.stop()
        final = segmenter.flush()
        if final:
            out_queue.put(final)
        out_queue.put(None)
        thread.join(timeout=60)
        if assistant is not None:
            assistant.stop()
    return 0
