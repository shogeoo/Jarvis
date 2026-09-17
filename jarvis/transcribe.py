"""Транскрипция аудио-чанков через faster-whisper (large-v3-turbo, CUDA).

Язык ограничен списком допустимых (по умолчанию ru/en): если
автоопределение whisper выдало язык вне списка — сегмент транскрибируется
повторно с принудительным фолбек-языком (по умолчанию русский).
"""

from __future__ import annotations

from faster_whisper import WhisperModel


class Transcriber:
    def __init__(self, model_name: str = "large-v3-turbo", device: str = "cuda",
                 language: str | None = None, compute_type: str | None = None,
                 languages: tuple[str, ...] = ("ru", "en")):
        self.model_name = model_name
        self.device = device
        self.language = language
        self.languages = tuple(languages) or ("ru", "en")
        self.fallback = self.languages[0]
        self.compute_type = compute_type or (
            "float16" if device == "cuda" else "int8"
        )
        self.model = None

    def load(self):
        print(
            f"STT Whisper: загрузка модели {self.model_name} на {self.device} "
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
                "или переустанови пакет с extra: pip install -e '.[cuda]'."
            ) from exc
        print(f"STT Whisper: модель {self.model_name} готова.", flush=True)

    def _transcribe(self, path: str, language: str | None) -> tuple[str, str]:
        segments, info = self.model.transcribe(
            path,
            language=language,
            beam_size=5,
            vad_filter=False,
            without_timestamps=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, info.language

    def transcribe_file(self, path: str) -> str:
        if self.language is not None:
            text, _ = self._transcribe(path, self.language)
            return text
        text, detected = self._transcribe(path, None)
        if detected in self.languages:
            return text
        text, _ = self._transcribe(path, self.fallback)
        return text
