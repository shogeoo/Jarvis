"""CLI: реальное время STT.

Микрофон -> VAD -> запись чанка в WAV -> whisper -> строка текста.
"""

from __future__ import annotations

import argparse
import os
import queue
import threading

import sounddevice as sd

from .audio import FRAME_SIZE, MicStream
from .printer import Printer
from .transcribe import Transcriber
from .vad import Segmenter, SileroVAD, ensure_model


def _list_devices(args):
    print(sd.query_devices())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stt_stream",
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
    p.add_argument("--input-device", default=None, help="ID устройства ввода")
    p.add_argument("--list-devices", action="store_true",
                   help="Список аудиоустройств и выход")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_devices:
        return _list_devices(args)

    ensure_model()
    printer = Printer()

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
    return 0
