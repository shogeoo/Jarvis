import os
import queue
import threading
from pathlib import Path

from jarvis.capabilities import event_definition, handler_definition
from jarvis.core.protocol import object_schema


def _bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class SpeechInput:
    def __init__(self):
        self.mic = None
        self.segmenter = None
        self.worker = None
        self.queue = queue.Queue()
        self.cleanup_lock = threading.Lock()

    def start(self, ctx):
        if not _bool("STT_ENABLED", True):
            while not ctx.stop_event.wait(0.2):
                pass
            return
        try:
            from ..audio import FRAME_SIZE, MicStream
            from ..transcribe import Transcriber
            from ..vad import DEFAULT_MODEL_PATH, Segmenter, SileroVAD, ensure_model

            model_path = os.environ.get("STT_VAD_MODEL", DEFAULT_MODEL_PATH)
            ensure_model(model_path)
            sample_rate = 16000
            out_dir = Path(
                os.environ.get(
                    "STT_SEGMENTS_DIR",
                    str(ctx.config.jarvis_dir / "runtime" / "segments"),
                )
            )
            vad = SileroVAD(
                model_path=model_path,
                threshold=float(os.environ.get("STT_THRESHOLD", "0.5")),
            )
            self.segmenter = Segmenter(
                vad,
                out_dir=str(out_dir),
                pre_roll=float(os.environ.get("STT_PRE_ROLL", "0.5")),
                chunk_silence=float(os.environ.get("STT_CHUNK_SILENCE", "2.0")),
                keep_audio=_bool("STT_KEEP_AUDIO", False),
            )
            transcriber = Transcriber(
                os.environ.get("STT_MODEL", "large-v3-turbo"),
                os.environ.get("STT_DEVICE", "cuda"),
                os.environ.get("STT_LANGUAGE") or None,
                os.environ.get("STT_COMPUTE_TYPE") or None,
                languages=tuple(
                    item.strip()
                    for item in os.environ.get("STT_LANGUAGES", "ru,en").split(",")
                    if item.strip()
                ),
            )
            transcriber.load()
            self.worker = threading.Thread(
                target=self._transcribe,
                args=(ctx, transcriber),
                name="jarvis-speech-transcriber",
                daemon=True,
            )
            self.worker.start()
            self.mic = MicStream(
                sample_rate,
                FRAME_SIZE,
                os.environ.get("STT_INPUT_DEVICE") or None,
            ).start()
            while not ctx.stop_event.is_set():
                try:
                    frame = self.mic.frames.get(timeout=0.1)
                except queue.Empty:
                    continue
                segment = self.segmenter.feed(frame)
                if self.segmenter.consume_speech_started():
                    speaker = ctx.services.get("speech_output")
                    if speaker is not None:
                        speaker.interrupt()
                if segment:
                    self.queue.put(segment)
        except Exception as exc:
            if not ctx.stop_event.is_set():
                ctx.emit("speech_input.error", {"message": str(exc)})
        finally:
            self._cleanup()

    def _transcribe(self, ctx, transcriber):
        while not ctx.stop_event.is_set():
            item = self.queue.get()
            if item is None:
                return
            try:
                text = transcriber.transcribe_file(item["path"])
                if text.strip():
                    ctx.emit("speech_input.speech", {"text": text.strip()})
            except Exception as exc:
                if not ctx.stop_event.is_set():
                    ctx.emit("speech_input.error", {"message": str(exc)})
            finally:
                self._discard(item)

    def stop(self, ctx):
        self._cleanup()

    def _cleanup(self):
        with self.cleanup_lock:
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
        if item is not None and not item["keep"]:
            try:
                os.remove(item["path"])
            except FileNotFoundError:
                pass


def create_handler():
    controller = SpeechInput()
    speech = event_definition(
        "speech_input.speech",
        "Новая завершённая реплика пользователя с микрофона. text содержит "
        "распознанную речь.",
        object_schema({"text": {"type": "string"}}),
    )
    error = event_definition(
        "speech_input.error",
        "Фоновая ошибка микрофона, VAD или распознавания. message содержит "
        "причину, которую следует сообщить пользователю или передать на "
        "исправление.",
        object_schema({"message": {"type": "string"}}),
    )
    return handler_definition(
        "speech_input.microphone",
        "Непрерывно слушает микрофон, режет речь по VAD и публикует "
        "завершённые транскрипции и ошибки.",
        (speech, error),
        controller.start,
        stop=controller.stop,
    )
