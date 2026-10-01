"""Возможности входа выбранной модели OpenRouter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from .prompt_templates import render_template


SUPPORTED_INPUT_MODALITIES = ("text", "image", "audio", "video", "file")


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    model: str
    input_modalities: tuple[str, ...]
    discovery_error: str | None = None

    def supports(self, modality: str) -> bool:
        return modality in self.input_modalities

    def prompt_block(self) -> str:
        supported = ", ".join(self.input_modalities) or "нет"
        return render_template("modalities.txt", modalities=supported)


def discover_model_capabilities(
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    *,
    timeout: float = 10.0,
) -> ModelCapabilities:
    """Получить input_modalities из ``GET /models``.

    Если провайдер не вернул список входных модальностей (нет base_url,
    модели нет в каталоге, нет поля input_modalities, ошибка запроса),
    считается, что модель поддерживает все модальности: ядро не станет
    урезать возможности модели из-за неполного каталога.
    """

    model_name = model or "unknown"
    if not base_url:
        return ModelCapabilities(model_name, SUPPORTED_INPUT_MODALITIES, "LLM_BASE_URL не задан")
    endpoint = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}" if api_key else ""}
    headers = {name: value for name, value in headers.items() if value}
    try:
        response = requests.get(endpoint, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload: Any = response.json()
        models = payload.get("data", []) if isinstance(payload, dict) else []
        selected = next(
            (item for item in models if isinstance(item, dict) and item.get("id") == model),
            None,
        )
        if selected is None:
            raise ValueError(f"Модель {model!r} отсутствует в /models")
        architecture = selected.get("architecture", {})
        raw = selected.get("input_modalities", architecture.get("input_modalities"))
        if not isinstance(raw, list):
            raise ValueError(f"У модели {model!r} нет input_modalities")
        modalities = tuple(
            name for name in SUPPORTED_INPUT_MODALITIES if name in raw
        )
        if "text" not in modalities:
            modalities = ("text",) + modalities
        return ModelCapabilities(model_name, modalities)
    except (OSError, ValueError, requests.RequestException) as exc:
        return ModelCapabilities(model_name, SUPPORTED_INPUT_MODALITIES, str(exc))
