"""CLI: реальное время STT.

Микрофон -> VAD -> запись чанка в WAV -> whisper -> строка текста.
Если задана модель LLM, расшифровка уходит в неё, а ответ озвучивается через
Fish Audio S2 Pro (s2.cpp). Model-visible JSON выводится в CLI без изменений.
"""

from __future__ import annotations

import argparse
import os
import queue
import signal
import subprocess
import threading
from types import SimpleNamespace

from .audio import FRAME_SIZE, MicStream
from .builtin import register_builtin_actions, register_builtin_events
from .config import load_config
from .debug import Debugger
from .module_manager import ModuleManager
from .model_capabilities import discover_model_capabilities
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


def publish_stt_event(event_bus, text: str) -> None:
    """Передать распознанную речь main без повторного перебивания TTS."""

    event_bus.publish(
        Event(
            type="speech",
            data={"text": text},
            source="stt",
            target="main",
        )
    )


def _run(args, resources) -> int:
    ensure_model()
    printer = Printer()
    config = load_config(args.env_file, args.system_prompt)

    speaker = None
    if not args.no_tts and config.tts_enabled and config.tts_url:
        from .tts import Speaker

        try:
            speaker = Speaker(config)
            resources.speaker = speaker
            speaker.start()
        except RuntimeError as exc:
            print(exc, file=os.sys.stderr)
            if speaker is not None:
                speaker.stop()
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
        resources.modules = module_manager
        capabilities = discover_model_capabilities(
            config.model,
            config.base_url,
            config.api_key,
        )
        client = OpenAI(
            base_url=config.base_url or None,
            api_key=config.api_key or None,
            timeout=60.0,
        )
        resources.client = client
        agent_manager = AgentManager(
            model=config.model,
            client=client,
            actions=action_registry,
            events=event_registry,
            bus=event_bus,
            module_manager=module_manager,
            config=config,
            debug=debug,
            capabilities=capabilities,
        )
        resources.agents = agent_manager
        register_builtin_actions(
            action_registry,
            agent_manager,
            printer=None,
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
    resources.segmenter = segmenter
    out_queue: "queue.Queue[dict]" = queue.Queue()
    resources.out_queue = out_queue
    stop = resources.stop
    failed = threading.Event()
    transcriber_ready = threading.Event()

    def worker():
        tr = Transcriber(
            args.model, args.device, args.language, args.compute_type,
            languages=tuple(x.strip() for x in args.languages.split(",") if x.strip()),
        )
        try:
            tr.load()
        except RuntimeError as exc:
            print(exc, file=os.sys.stderr)
            failed.set()
            stop.set()
            transcriber_ready.set()
            return
        transcriber_ready.set()
        while not stop.is_set():
            item = out_queue.get()
            if item is None:
                break
            if stop.is_set():
                _discard_segment(item)
                break
            try:
                text = tr.transcribe_file(item["path"])
            except Exception as exc:
                if not stop.is_set():
                    print(f"STT: {exc}", file=os.sys.stderr)
                    failed.set()
                    stop.set()
                break
            finally:
                _discard_segment(item)
            if stop.is_set():
                break
            if event_bus is not None:
                publish_stt_event(event_bus, text)
            else:
                printer.print_segment(text)

    thread = threading.Thread(target=worker, daemon=True)
    resources.worker = thread
    thread.start()

    while not transcriber_ready.wait(timeout=0.1):
        if stop.is_set():
            return 1
    if failed.is_set():
        return 1

    mic = MicStream(args.sample_rate, FRAME_SIZE, args.input_device)
    resources.mic = mic
    mic.start()
    print("Jarvis: ассистент готов. Говорите. Ctrl+C — выход.", flush=True)
    while not stop.is_set():
        try:
            frame = mic.frames.get(timeout=0.1)
        except queue.Empty:
            continue
        result = segmenter.feed(frame)
        if segmenter.consume_speech_started() and speaker is not None:
            if speaker.has_pending() and agent_manager is not None:
                agent_manager.hold(agent_id="main", until_event="speech")
            speaker.interrupt()
        if result:
            out_queue.put(result)
    return 1 if failed.is_set() else 0


def _discard_segment(item):
    if item is not None and not item["keep"]:
        try:
            os.remove(item["path"])
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"Не удалось удалить временный сегмент {item['path']}: {exc}",
                  file=os.sys.stderr)


def _cleanup(resources):
    resources.stop.set()

    def close(name, callback, timeout):
        def run():
            try:
                callback()
            except Exception as exc:
                print(f"Завершение {name}: {exc}", file=os.sys.stderr)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            print(f"Завершение {name}: истекло время ожидания.", file=os.sys.stderr)

    if resources.agents is not None:
        close("агентов", resources.agents.begin_shutdown, 1)
    if resources.speaker is not None:
        close("озвучки", resources.speaker.stop, 12)
    if resources.mic is not None:
        close("микрофона", resources.mic.stop, 5)
    if resources.segmenter is not None:
        close("записи сегмента", lambda: _discard_segment(resources.segmenter.flush()), 1)
    if resources.out_queue is not None:
        while True:
            try:
                _discard_segment(resources.out_queue.get_nowait())
            except queue.Empty:
                break
        resources.out_queue.put(None)
    if resources.modules is not None:
        close("обработчиков", resources.modules.shutdown, 4)
    if resources.client is not None:
        close("соединений модели", resources.client.close, 2)
    if resources.agents is not None:
        close("действий", resources.agents.shutdown, 10)
    if resources.worker is not None and resources.worker.ident is not None:
        resources.worker.join(timeout=1)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_devices:
        return _list_devices(args)
    resources = SimpleNamespace(
        stop=threading.Event(), speaker=None, agents=None, modules=None,
        client=None, mic=None, segmenter=None, out_queue=None, worker=None,
    )
    received_signal = None

    def interrupt(signum, frame):
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum
            raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return _run(args, resources)
    except KeyboardInterrupt:
        print("\nJarvis: завершение работы…", flush=True)
        return 128 + (received_signal or signal.SIGINT)
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            _cleanup(resources)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
