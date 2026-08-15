"""Захват звука с микрофона в реальном времени.

Аудио режется на короткие чанки по 32 мс (512 сэмплов @16 кГц) и
складывается в очередь для VAD-обработки.
"""

from __future__ import annotations

import queue

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
FRAME_SIZE = 512


class MicStream:
    """Поток микрофона, отдающий чанки int16 (моно, 16 кГц) в очередь."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, frame_size: int = FRAME_SIZE,
                 device=None):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.device = device
        self.frames: "queue.Queue[np.ndarray]" = queue.Queue()
        self._stream = None
        self._decim = 1
        self._actual_rate = sample_rate

    def _callback(self, indata, frames, time_info, status):
        audio = np.frombuffer(indata, dtype=np.int16)
        if self._decim > 1:
            audio = audio[:: self._decim]
        self.frames.put(audio)

    def start(self) -> "MicStream":
        candidates = [self.sample_rate]
        if 48000 not in candidates:
            candidates.append(48000)
        last_error = None
        for rate in candidates:
            decim = rate // self.sample_rate
            block = self.frame_size * decim
            try:
                self._stream = sd.RawInputStream(
                    samplerate=rate,
                    channels=1,
                    dtype="int16",
                    blocksize=block,
                    device=self.device,
                    callback=self._callback,
                )
                self._stream.start()
                self._decim = decim
                self._actual_rate = rate
                return self
            except Exception as exc:
                last_error = exc
        raise RuntimeError(
            f"Не удалось открыть микрофон на {self.sample_rate} Гц: {last_error}"
        )

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
