"""Общий конвейер микрофон -> VAD -> транскрипция слоя речи ядра."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Callable

from .config import (
    STT_CHUNK_SILENCE,
    STT_COMPUTE_TYPE,
    STT_DEVICE,
    STT_INPUT_DEVICE,
    STT_KEEP_AUDIO,
    STT_LANGUAGES,
    STT_LANGUAGE,
    STT_MODEL,
    STT_PRE_ROLL,
    STT_THRESHOLD,
)


class Pipeline:
    """Владеет микрофоном, VAD, транскрибером и их потоками."""

    def __init__(self):
        self.mic = None
        self.segmenter = None
        self.worker = None
        self.queue: "queue.Queue[dict | None]" = queue.Queue()
        self.cleanup_lock = threading.Lock()
        self.interrupt_speech: Callable[[], None] | None = None
        self._subscribers: dict[str, list] = {"speech": [], "error": []}
        self._refs = 0

    def attach(self, kind: str, emit):
        with self.cleanup_lock:
            self._subscribers[kind].append(emit)
            self._refs += 1

        def detach():
            with self.cleanup_lock:
                if emit in self._subscribers[kind]:
                    self._subscribers[kind].remove(emit)
                self._refs -= 1
                if self._refs <= 0:
                    self._cleanup_locked()

        return detach

    def ensure_running(self, jarvis_dir: Path) -> None:
        """Загрузить модели в VRAM и запустить потоки. Блокирующе."""

        with self.cleanup_lock:
            if self.worker is not None:
                return
            from .audio import FRAME_SIZE, MicStream
            from .transcribe import Transcriber
            from .vad import Segmenter, SileroVAD, ensure_model

            runtime_dir = Path(jarvis_dir) / "runtime"
            model_path = runtime_dir / "models" / "silero_vad.onnx"
            ensure_model(str(model_path))
            out_dir = runtime_dir / "segments"
            vad = SileroVAD(model_path=str(model_path), threshold=STT_THRESHOLD)
            self.segmenter = Segmenter(
                vad,
                out_dir=str(out_dir),
                pre_roll=STT_PRE_ROLL,
                chunk_silence=STT_CHUNK_SILENCE,
                keep_audio=STT_KEEP_AUDIO,
            )
            transcriber = Transcriber(
                STT_MODEL,
                STT_DEVICE,
                STT_LANGUAGE,
                STT_COMPUTE_TYPE,
                languages=STT_LANGUAGES,
            )
            transcriber.load()
            self.worker = threading.Thread(
                target=self._transcribe,
                args=(transcriber,),
                name="jarvis-speech-transcriber",
                daemon=True,
            )
            self.worker.start()
            self.mic = MicStream(
                16000,
                FRAME_SIZE,
                STT_INPUT_DEVICE,
            ).start()
            listener = threading.Thread(
                target=self._listen,
                name="jarvis-speech-listener",
                daemon=True,
            )
            listener.start()

    def _emit(self, kind: str, data: dict) -> None:
        with self.cleanup_lock:
            subscribers = list(self._subscribers[kind])
        for emit in subscribers:
            try:
                emit(data)
            except Exception:
                pass

    def _listen(self) -> None:
        while self.mic is not None:
            try:
                frame = self.mic.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            except AttributeError:
                return
            segment = self.segmenter.feed(frame)
            if self.segmenter.consume_speech_started():
                interrupt = self.interrupt_speech
                if interrupt is not None:
                    try:
                        interrupt()
                    except Exception:
                        pass
            if segment:
                self.queue.put(segment)

    def _transcribe(self, transcriber) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            try:
                text = transcriber.transcribe_file(item["path"])
                if text.strip():
                    self._emit("speech", {"text": text.strip()})
            except Exception as exc:
                self._emit("error", {"message": str(exc)})
            finally:
                self._discard(item)

    def shutdown(self) -> None:
        with self.cleanup_lock:
            self._subscribers = {"speech": [], "error": []}
            self._refs = 0
            self._cleanup_locked()

    def _cleanup_locked(self):
        mic, self.mic = self.mic, None
        segmenter, self.segmenter = self.segmenter, None
        worker, self.worker = self.worker, None
        if mic is not None:
            mic.stop()
        if segmenter is not None:
            self._discard(segmenter.flush())
        self.queue.put(None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)

    @staticmethod
    def _discard(item):
        import os

        if item is not None and not item["keep"]:
            try:
                os.remove(item["path"])
            except FileNotFoundError:
                pass


_instance: Pipeline | None = None
_lock = threading.Lock()


def pipeline() -> Pipeline:
    global _instance
    with _lock:
        if _instance is None:
            _instance = Pipeline()
        return _instance
