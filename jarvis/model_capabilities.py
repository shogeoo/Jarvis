"""Возможности входа выбранной модели OpenRouter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


SUPPORTED_INPUT_MODALITIES = ("text", "image", "audio", "video")


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    model: str
    input_modalities: tuple[str, ...]
    discovery_error: str | None = None

    def supports(self, modality: str) -> bool:
        return modality in self.input_modalities

    def prompt_block(self) -> str:
        supported = ", ".join(self.input_modalities) or "нет"
        unsupported = [
            name for name in SUPPORTED_INPUT_MODALITIES
            if name not in self.input_modalities
        ]
        lines = [
            "Входные модальности текущей модели:",
            f"- поддерживаются: {supported}",
        ]
        if unsupported:
            lines.extend(
                [
                    f"- не поддерживаются: {', '.join(unsupported)}",
                    "Не пытайся самостоятельно воспринимать неподдерживаемые "
                    "модальности. Если задача требует их, сообщи об ограничении "
                    "через доступное действие.",
                ]
            )
        return "\n".join(lines)


def discover_model_capabilities(
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    *,
    timeout: float = 10.0,
) -> ModelCapabilities:
    """Получить input_modalities из ``GET /models``.

    При недоступности каталога используется безопасный text-only режим: ядро
    не передаст модели бинарные мультимодальные данные, если не знает, что она
    их поддерживает.
    """

    model_name = model or "unknown"
    if not base_url:
        return ModelCapabilities(model_name, ("text",), "LLM_BASE_URL не задан")
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
        return ModelCapabilities(model_name, modalities[:4])
    except (OSError, ValueError, requests.RequestException) as exc:
        return ModelCapabilities(model_name, ("text",), str(exc))
