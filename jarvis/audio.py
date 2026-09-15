"""Захват звука с микрофона в реальном времени.

Бэкенд: parec (PulseAudio/PipeWire) — он корректно коннектится к реальному
источнику (в отличие от PortAudio в некоторых PipeWire-конфигурациях).
Аудио режется на короткие чанки по 32 мс (512 сэмплов @16 кГц) и
складывается в очередь для VAD-обработки.
"""

from __future__ import annotations

import queue
import subprocess
import threading

import numpy as np

SAMPLE_RATE = 16000
FRAME_SIZE = 512


class MicStream:
    """Поток микрофона, отдающий чанки int16 (моно, 16 кГц) в очередь."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, frame_size: int = FRAME_SIZE,
                 device: str | None = None):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.device = device
        self.frames: "queue.Queue[np.ndarray]" = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()

    def start(self) -> "MicStream":
        cmd = [
            "parec",
            "--format=s16le",
            f"--rate={self.sample_rate}",
            "--channels=1",
            "--file-format=raw",
        ]
        if self.device:
            cmd.append(f"--device={self.device}")
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        return self

    def _read_loop(self):
        nbytes = self.frame_size * 2
        while not self._stop.is_set() and self._proc is not None \
                and self._proc.poll() is None:
            raw = self._proc.stdout.read(nbytes)
            if len(raw) < nbytes:
                if raw:
                    self.frames.put(np.frombuffer(raw, dtype=np.int16))
                break
            self.frames.put(np.frombuffer(raw, dtype=np.int16))
        if not self._stop.is_set():
            self._stop.set()

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
