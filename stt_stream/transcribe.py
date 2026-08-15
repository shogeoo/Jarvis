"""Транскрипция аудио-чанков через faster-whisper (large-v3, CUDA)."""

from __future__ import annotations

import os

from faster_whisper import WhisperModel

CUDA_LIBS = {
    "cuBLAS": os.path.expanduser("~/.local/lib/python3.14/site-packages/nvidia/cublas/lib"),
    "cuDNN": os.path.expanduser("~/.local/lib/python3.14/site-packages/nvidia/cudnn/lib"),
}


class Transcriber:
    def __init__(self, model_name: str = "large-v3", device: str = "cuda",
                 language: str | None = None, compute_type: str | None = None):
        self.model_name = model_name
        self.device = device
        self.language = language
        self.compute_type = compute_type or (
            "float16" if device == "cuda" else "int8"
        )
        self.model = None

    def load(self):
        print(
            f"Загрузка модели {self.model_name} на {self.device} "
            f"({self.compute_type})...",
            flush=True,
        )
        try:
            self.model = WhisperModel(
                self.model_name, device=self.device, compute_type=self.compute_type
            )
        except Exception as exc:
            raise RuntimeError(
                f"Не удалось загрузить модель на {self.device}.\n"
                f"Причина: {exc}\n\n"
                "Для CUDA нужны библиотеки cuBLAS/cuDNN. Установи:\n"
                "  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12\n"
                "и запускай через run_stt.sh (он выставляет LD_LIBRARY_PATH)."
            ) from exc
        print("Модель загружена", flush=True)

    def transcribe_file(self, path: str) -> str:
        segments, _ = self.model.transcribe(
            path,
            language=self.language,
            beam_size=5,
            vad_filter=False,
            without_timestamps=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return text
