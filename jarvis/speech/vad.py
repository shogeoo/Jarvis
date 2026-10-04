"""VAD (silero-vad) + нарезка речи на аудио-чанки.

Конечный автомат:
    тишина -> (речь) -> запись в WAV с пред-роллом -> (тишина >= chunk_silence) -> файл готов

При готовности возвращается словарь с путём к WAV и длительностью паузы
перед началом этого чанка (для логики пустой строки).
"""

from __future__ import annotations

from ..infrastructure.console import logger

import os
import time
import urllib.request
import wave
from collections import deque

import numpy as np
import onnxruntime as ort

from .audio import FRAME_SIZE, SAMPLE_RATE

MODEL_URL = (
    "https://huggingface.co/onnx-community/silero-vad/resolve/main/"
    "onnx/model.onnx"
)


def ensure_model(path: str) -> str:
    if os.path.exists(path) and os.path.getsize(path) > 100_000:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    logger.info(f"Скачивание {MODEL_URL}")
    urllib.request.urlretrieve(MODEL_URL, path)
    return path


class SileroVAD:
    def __init__(self, model_path: str, threshold: float = 0.5):
        self.session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        self.threshold = threshold
        self.sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.last_prob = 0.0

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.last_prob = 0.0

    def is_speech(self, frame_int16) -> bool:
        x = frame_int16.astype(np.float32) / 32768.0
        x = x.reshape(1, -1)
        out = self.session.run(
            None, {"input": x, "state": self.state, "sr": self.sr}
        )
        prob = float(out[0].ravel()[0])
        self.state = out[1]
        self.last_prob = prob
        return prob > self.threshold


class Segmenter:
    def __init__(self, vad: SileroVAD, out_dir: str = "segments",
                 pre_roll: float = 0.5, chunk_silence: float = 1.0,
                 keep_audio: bool = False):
        self.vad = vad
        self.out_dir = out_dir
        self.keep_audio = keep_audio
        os.makedirs(out_dir, exist_ok=True)
        self.pre_frames = max(1, int(pre_roll * SAMPLE_RATE / FRAME_SIZE))
        self.chunk_silence_frames = max(1, int(chunk_silence * SAMPLE_RATE / FRAME_SIZE))

        self._roll: deque = deque(maxlen=self.pre_frames)
        self._recording = False
        self._wav = None
        self._path = None
        self._pause = None
        self._silence = 0
        self._last_speech_idx = 0
        self._prev_end_idx = None
        self._idx = 0
        self._seq = 0
        self._speech_started = False

    def feed(self, frame_int16):
        """Подать один чанк. Возвращает словарь сегмента или None."""
        self._speech_started = False
        speech = self.vad.is_speech(frame_int16)
        result = None
        self._roll.append(frame_int16)

        if not self._recording:
            if speech:
                self._speech_started = True
                self.vad.reset()
                if self._prev_end_idx is not None:
                    self._pause = (
                        (self._idx - self._prev_end_idx)
                        * FRAME_SIZE / SAMPLE_RATE
                    )
                else:
                    self._pause = None
                self._path = os.path.join(
                    self.out_dir,
                    f"seg_{self._seq:06d}_{time.time() * 1000:.0f}.wav",
                )
                self._seq += 1
                self._wav = wave.open(self._path, "wb")
                self._wav.setnchannels(1)
                self._wav.setsampwidth(2)
                self._wav.setframerate(SAMPLE_RATE)
                for f in self._roll:
                    self._wav.writeframes(f.tobytes())
                self._roll.clear()
                self._recording = True
                self._silence = 0
                self._last_speech_idx = self._idx
        else:
            if speech:
                self._silence = 0
                self._last_speech_idx = self._idx
            else:
                self._silence += 1
            self._wav.writeframes(frame_int16.tobytes())
            if self._silence >= self.chunk_silence_frames:
                self._wav.close()
                self._wav = None
                self._prev_end_idx = self._last_speech_idx
                result = {
                    "path": self._path,
                    "pause": self._pause,
                    "keep": self.keep_audio,
                }
                self._path = None
                self._recording = False

        self._idx += 1
        return result

    def consume_speech_started(self) -> bool:
        """Забрать одноразовый сигнал начала нового речевого сегмента."""

        started = self._speech_started
        self._speech_started = False
        return started

    def flush(self):
        """Завершить незакрытый сегмент при выходе. Возвращает словарь или None."""
        if self._recording and self._wav is not None:
            self._wav.close()
            result = {"path": self._path, "pause": self._pause,
                      "keep": self.keep_audio}
            self._wav = None
            self._path = None
            self._recording = False
            return result
        return None
