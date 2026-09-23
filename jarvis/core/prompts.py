"""Сборка актуального systemprompt агента в памяти."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..infrastructure.model_capabilities import ModelCapabilities
from .protocol import json_text
from .registry import ActionRegistry


def read_master_prompt(path: Path) -> str:
    """Прочитать общий мастер-промпт среды.

    Текст одинаков для всех агентов; блок модальностей конкретной нейронки
    подставляется билдером вместо плейсхолдера ``{modalities}``, а не
    хардкодится в файле.
    """

    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Не удалось прочитать мастер-промпт {path}: {exc}") from exc
    if not text:
        raise RuntimeError(f"Мастер-промпт пуст: {path}")
    return text


def _event_catalog(event_specs: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": spec.type,
            "description": spec.description,
            "data_schema": spec.data_schema,
        }
        for spec in sorted(event_specs.values(), key=lambda item: item.type)
    ]


def agent_system_prompt(
    person_prompt: str,
    master_prompt: str,
    action_specs: dict[str, Any],
    event_specs: dict[str, Any],
    capability_catalog: dict[str, Any],
    *,
    model_capabilities: ModelCapabilities | None = None,
) -> str:
    """Собрать systemprompt из постоянных и динамических частей.

    System prompt — конкатенация personprompt, мастер-промпта с подставленным
    блоком модальностей и каталога capabilities. Именно он отправляется в API.
    """

    standalone_action_ids = {
        action["type"] for action in capability_catalog.get("actions", [])
    }
    standalone_event_types = {
        event["type"] for event in capability_catalog.get("events", [])
    }
    catalog = {
        "core_protocol": {
            "actions": ActionRegistry.catalog(
                {
                    name: spec
                    for name, spec in action_specs.items()
                    if name not in standalone_action_ids
                    and name
                    not in {
                        action["type"]
                        for module in capability_catalog.get("modules", [])
                        for action in module["actions"]
                    }
                }
            ),
            "events": _event_catalog(
                {
                    name: spec
                    for name, spec in event_specs.items()
                    if name not in standalone_event_types
                    and name
                    not in {
                        event["type"]
                        for module in capability_catalog.get("modules", [])
                        for event in module["events"]
                    }
                }
            ),
            "action_result": {
                "type": "action_result",
                "call_id": "строка, выбранная моделью для конкретного вызова",
                "data": "объект по схеме результата конкретного действия",
            },
        },
        "standalone": {
            "actions": capability_catalog.get("actions", []),
            "handlers": capability_catalog.get("handlers", []),
            "events": capability_catalog.get("events", []),
        },
        "modules": capability_catalog.get("modules", []),
    }
    parts = [person_prompt.strip(), _master_text(master_prompt, model_capabilities)]
    parts.append("Доступный контракт capabilities:\n" + json_text(catalog, indent=2))
    return "\n\n".join(part for part in parts if part)


def _master_text(
    master_prompt: str, model_capabilities: ModelCapabilities | None
) -> str:
    """Подставить блок модальностей в мастер-промпт вместо плейсхолдера."""

    block = model_capabilities.prompt_block() if model_capabilities is not None else ""
    text = master_prompt.strip()
    if "{modalities}" in text:
        return text.replace("{modalities}", block)
    if block:
        return text + "\n\n" + block
    return text
