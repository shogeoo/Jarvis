"""Единая точка речи ядра: TTS-сервер, whisper и VAD-поток микрофона.

Сервис стартует блокирующе при запуске Jarvis и грузит все модели в VRAM.
Любой сбой инициализации не останавливает Jarvis: речь помечается
недоступной, действие ``speech`` возвращает ошибку, события не публикуются.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Callable

from .config import build_config
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
                speaker = Speaker(config)
                speaker.start()
            except Exception as exc:  # noqa: BLE001
                self._report(debug, "speech_tts_failed", exc)
                speaker = None
            pipe = None
            detach: list[Callable[[], None]] = []
            try:
                pipe = pipeline()
                pipe.ensure_running(jarvis_dir)
                if speaker is not None:
                    pipe.interrupt_speech = speaker.interrupt
                detach.append(
                    pipe.attach(
                        "speech",
                        lambda data: emit(
                            {"text": data["text"], "error": None}
                        ),
                    )
                )
                detach.append(
                    pipe.attach(
                        "error",
                        lambda data: emit(
                            {"text": "", "error": data["message"]}
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self._report(debug, "speech_stt_failed", exc)
                pipe = None
            if speaker is None and pipe is None:
                return
            self._speaker = speaker
            self._pipeline = pipe
            self._detach = detach
            self.available = True
            print("Речь: инициализация завершена.", flush=True)

    def speak_result(self, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not self.available or self._speaker is None:
            return {"spoken": False, "text": text, "error": "speech_unavailable"}
        try:
            result = self._speaker.submit(text).result()
        except SpeechInterrupted:
            return {"spoken": False, "text": text, "error": "interrupted"}
        except Exception as exc:  # noqa: BLE001
            return {"spoken": False, "text": text, "error": str(exc)}
        if isinstance(result, dict):
            return {
                "spoken": bool(result.get("spoken", True)),
                "text": text,
                "error": result.get("error"),
            }
        return {"spoken": True, "text": text, "error": None}

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
    def _report(debug: Any, event: str, exc: Exception) -> None:
        if debug is not None:
            debug.log(event, error=str(exc))
        print(f"Речь недоступна: {exc}", file=sys.stderr, flush=True)


service = SpeechService()
