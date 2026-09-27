"""Единая точка речи ядра: TTS-сервер, whisper и VAD-поток микрофона.

Сервис стартует блокирующе при запуске Jarvis и грузит все модели в VRAM.
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
                    logger.info("s2 binary not found; speech action is unavailable. STT remains independent.")
            except Exception as exc:  # noqa: BLE001
                self._report(debug, "speech_tts_failed", exc)
                speaker = None
            pipe = None
            detach: list[Callable[[], None]] = []
            try:
                if not STT_ENABLED:
                    return self._finish_start(speaker, None, [])
                pipe = pipeline()
                pipe.ensure_running(jarvis_dir)
                if speaker is not None:
                    pipe.interrupt_speech = speaker.interrupt
                detach.append(
                    pipe.attach(
                        "speech",
                        lambda data: emit({"text": data["text"]}),
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
        self._speaker = speaker
        self._pipeline = pipe
        self._detach = detach
        self.available = speaker is not None or pipe is not None
        if self.available:
            logger.info("Речь: инициализация завершена.")

    def speak_result(self, text: str) -> dict[str, Any]:
        """Озвучить text. Результат — только статус; сбой является ошибкой."""

        if not self.available or self._speaker is None:
            raise RuntimeError("speech_unavailable")
        text = (text or "").strip()
        if not text:
            return {"status": "successful"}
        speaker = self._speaker
        try:
            result = speaker.submit(text).result()
        except SpeechInterrupted:
            return {"status": "interrupted"}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"speech_failed: {exc}") from exc
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"speech_failed: {result['error']}")
        return {"status": "successful"}

    def interrupt(self) -> None:
        with self._lock:
            speaker = self._speaker
        if speaker is not None:
            speaker.interrupt()

    def shutdown(self) -> None:
        with self._lock:
            speaker, self._speaker = self._speaker, None
            detach, self._detach = self._detach, []
            self.available = False
        for callback in detach:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass
        if speaker is not None:
            speaker.stop()

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
