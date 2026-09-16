"""CLI: реальное время STT.

Микрофон -> VAD -> запись чанка в WAV -> whisper -> строка текста.
Если задана модель LLM, расшифровка уходит в неё, а ответ печатается и
озвучивается через Fish Audio S2 Pro (s2.cpp).
"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import threading

from .audio import FRAME_SIZE, MicStream
from .builtin import register_builtin_actions, register_builtin_events
from .config import load_config
from .debug import Debugger
from .module_manager import ModuleManager
from .printer import Printer
from .protocol import Event
from .registry import ActionRegistry, EventRegistry
from .runtime import AgentManager, EventBus
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
    p.add_argument("--env-file", default=None,
                   help="Файл с параметрами API (по умолчанию .env в корне проекта)")
    p.add_argument("--system-prompt", default=None,
                   help="Файл мастер-промпта (по умолчанию system_prompt.txt)")
    p.add_argument("--no-llm", action="store_true",
                   help="Отключить нейросеть, только расшифровка")
    p.add_argument("--no-tts", action="store_true",
                   help="Не озвучивать ответы через Fish Audio")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_devices:
        return _list_devices(args)

    ensure_model()
    printer = Printer()
    config = load_config(args.env_file, args.system_prompt)

    speaker = None
    if not args.no_tts and config.tts_enabled and config.tts_url:
        from .tts import Speaker

        try:
            speaker = Speaker(config).start()
        except RuntimeError as exc:
            print(exc, file=os.sys.stderr)
            speaker = None

    agent_manager = None
    event_bus = None
    if not args.no_llm and config.llm_enabled:
        from openai import OpenAI

        debug = Debugger(enabled=True)
        event_registry = EventRegistry()
        register_builtin_events(event_registry)
        action_registry = ActionRegistry()
        event_bus = EventBus(event_registry, debug=debug)
        module_manager = ModuleManager(
            event_bus,
            action_registry,
            event_registry,
            config=config,
            debug=debug,
        )
        client = OpenAI(
            base_url=config.base_url or None,
            api_key=config.api_key or None,
            timeout=60.0,
        )
        agent_manager = AgentManager(
            model=config.model,
            client=client,
            actions=action_registry,
            events=event_registry,
            bus=event_bus,
            module_manager=module_manager,
            config=config,
            debug=debug,
        )
        register_builtin_actions(
            action_registry,
            agent_manager,
            printer=printer,
            speaker=speaker,
        )
        module_manager.load_all()
        agent_manager.create_main(config.system)
        module_manager.start_all()

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
            if event_bus is not None:
                event_bus.publish(
                    Event(
                        type="speech",
                        data={"text": text},
                        source="stt",
                        target="main",
                    )
                )

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
        if agent_manager is not None:
            agent_manager.shutdown()
        if speaker is not None:
            speaker.stop()
    return 0
