"""Единая точка речи ядра: TTS-сервер, whisper и VAD-поток микрофона.

При API-запуске сервис грузит модели блокирующе; в режиме подписки может
стартовать в фоне. Вызов speech дожидается готовности фоновой инициализации.
Любой сбой инициализации не останавливает Jarvis: речь помечается
недоступной, действие ``speech`` возвращает ошибку, события не публикуются.
"""

from __future__ import annotations

from ..infrastructure.console import logger

import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from .config import build_config, STT_ENABLED, TTS_ENABLED
from .stt import Pipeline, pipeline
from .tts import Speaker, SpeechInterrupted
from ..capabilities.api import PENDING


SPEECH_HANDLER = "core:speech"
SpeechEvent = Callable[[dict[str, Any]], None]


class SpeechService:
    """Владелец Speaker и Pipeline в процессе Jarvis."""

    def __init__(self) -> None:
        self.available = False
        self._speaker: Speaker | None = None
        self._pipeline: Pipeline | None = None
        self._detach: list[Callable[[], None]] = []
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._ready.set()
        self._background: threading.Thread | None = None
        self._speech_completions: list[tuple[Any, Callable]] = []
        self._input_frozen = threading.Event()

    def begin_background(
        self, jarvis_dir: Path, *, emit: SpeechEvent, debug: Any = None, on_ready=None
    ) -> None:
        """Start speech without delaying the subscription agent's startup event."""
        self._input_frozen.clear()
        self._ready.clear()

        def run():
            try:
                self.start(jarvis_dir, emit=emit, debug=debug)
            except Exception as exc:  # noqa: BLE001
                self._report(debug, "speech_start_failed", exc)
            finally:
                self._ready.set()
                if on_ready is not None:
                    try:
                        on_ready()
                    except Exception as exc:  # noqa: BLE001
                        self._report(debug, "speech_ready_callback_failed", exc)

        self._background = threading.Thread(
            target=run, name="jarvis-speech-init", daemon=True
        )
        self._background.start()

    @property
    def can_speak(self) -> bool:
        return self._speaker is not None

    def start(
        self,
        jarvis_dir: Path,
        *,
        emit: SpeechEvent,
        debug: Any = None,
    ) -> None:
        """Грузит модели в VRAM и запускает VAD-поток. Блокирующе."""

        with self._lock:
            if self.available:
                return
            config = build_config(jarvis_dir)
            speaker = None
            try:
                config.runtime_dir.mkdir(parents=True, exist_ok=True)
                if TTS_ENABLED and shutil.which(str(config.tts_server_bin)):
                    speaker = Speaker(config)
                    speaker.start()
                elif TTS_ENABLED:
                    logger.info(
                        "s2 binary not found; speech action is unavailable. STT remains independent."
                    )
            except Exception as exc:  # noqa: BLE001
                self._report(debug, "speech_tts_failed", exc)
                speaker = None
            pipe = None
            detach: list[Callable[[], None]] = []
            try:
                if not STT_ENABLED or self._input_frozen.is_set():
                    return self._finish_start(speaker, None, [])
                pipe = pipeline()
                pipe.ensure_running(jarvis_dir)
                if speaker is not None:
                    pipe.interrupt_speech = speaker.interrupt
                detach.append(
                    pipe.attach(
                        "speech",
                        lambda data: self._recognized_speech(data, emit),
                    )
                )
                detach.append(
                    pipe.attach(
                        "error",
                        lambda data: self._log_stt_error(debug, data["message"]),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self._report(debug, "speech_stt_failed", exc)
                if pipe is not None:
                    pipe.shutdown()
                pipe = None
            self._finish_start(speaker, pipe, detach)

    def _finish_start(self, speaker, pipe, detach):
        if self._input_frozen.is_set():
            for callback in detach:
                callback()
            if pipe is not None:
                pipe.interrupt_speech = None
                pipe.shutdown()
            pipe, detach = None, []
        self._speaker = speaker
        self._pipeline = pipe
        self._detach = detach
        self.available = speaker is not None or pipe is not None
        if self.available:
            logger.info("Речь: инициализация завершена.")

    def _recognized_speech(self, data: dict, emit: SpeechEvent) -> None:
        """Release interrupted results together with the recognized utterance."""
        if self._input_frozen.is_set():
            return
        callbacks = []
        with self._lock:
            remaining = []
            for future, complete in self._speech_completions:
                if future.done() and isinstance(future.exception(), SpeechInterrupted):
                    callbacks.append(complete)
                else:
                    remaining.append((future, complete))
            self._speech_completions = remaining
        event = {"text": data["text"]}
        if callbacks:
            event["_interrupted_completions"] = callbacks
        emit(event)

    def speak_result(self, text: str, *, interrupted_complete=None) -> Any:
        """Озвучить text. Результат — только статус; сбой является ошибкой."""

        self._ready.wait()
        if not self.available or self._speaker is None:
            raise RuntimeError("speech_unavailable")
        text = (text or "").strip()
        if not text:
            return {"status": "successful"}
        speaker = self._speaker
        with self._lock:
            future = speaker.submit(text)
            if interrupted_complete is not None:
                self._speech_completions.append((future, interrupted_complete))
        try:
            result = future.result()
        except SpeechInterrupted:
            if interrupted_complete is not None:
                return PENDING
            return {"status": "interrupted"}
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._speech_completions = [
                    (item, callback)
                    for item, callback in self._speech_completions
                    if item is not future
                ]
            raise RuntimeError(f"speech_failed: {exc}") from exc
        with self._lock:
            self._speech_completions = [
                (item, callback)
                for item, callback in self._speech_completions
                if item is not future
            ]
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"speech_failed: {result['error']}")
        return {"status": "successful"}

    def interrupt(self) -> None:
        # Reading the current speaker must not wait for background model loading.
        speaker = self._speaker
        if speaker is not None:
            speaker.interrupt()

    def freeze_input(self):
        self._input_frozen.set()
        self.interrupt()
        pipe = self._pipeline
        if pipe is not None:
            pipe.interrupt_speech = None
        if not self._lock.acquire(blocking=False):
            # Initialization observes the frozen flag in _finish_start.
            return
        try:
            detach, self._detach = self._detach, []
            pipe, self._pipeline = self._pipeline, None
        finally:
            self._lock.release()
        if pipe is not None:
            pipe.interrupt_speech = None
        for callback in detach:
            callback()
        if pipe is not None:
            pipe.shutdown()

    def start_output(self, jarvis_dir):
        self._ready.wait()
        if self._speaker is not None:
            return
        config = build_config(jarvis_dir)
        if TTS_ENABLED and shutil.which(str(config.tts_server_bin)):
            speaker = Speaker(config)
            try:
                speaker.start()
            except Exception:
                speaker.stop()
                raise
            self._speaker = speaker
            self.available = True

    def shutdown(self) -> None:
        background = self._background
        if background is not None and background is not threading.current_thread():
            background.join()
        self._background = None
        with self._lock:
            speaker, self._speaker = self._speaker, None
            detach, self._detach = self._detach, []
            self.available = False
            self._speech_completions.clear()
        for callback in detach:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass
        if speaker is not None:
            speaker.stop()
        self._input_frozen.clear()

    @staticmethod
    def _log_stt_error(debug: Any, message: str) -> None:
        """Фоновые ошибки STT видны только в логах, не модели."""

        if debug is not None:
            debug.log("speech_stt_error", error=message)
        logger.error(f"Ошибка речи: {message}")

    @staticmethod
    def _report(debug: Any, event: str, exc: Exception) -> None:
        if debug is not None:
            debug.log(event, error=str(exc))
        logger.error(f"Речь недоступна: {exc}")


service = SpeechService()
