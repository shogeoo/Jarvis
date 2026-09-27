"""Сборка актуального systemprompt агента в памяти."""

from __future__ import annotations

from pathlib import Path
from importlib.resources import files
from typing import Any

from ..infrastructure.model_capabilities import ModelCapabilities
from .protocol import json_text
from .registry import ActionRegistry


def read_master_prompt(path: Path | None = None) -> str:
    """Прочитать общий мастер-промпт среды.

    Текст содержит только общие для всех агентов правила среды и протокола.
    """

    try:
        resource = path if path is not None else files("jarvis").joinpath("assets", "master_prompt.txt")
        text = resource.read_text(encoding="utf-8").strip()
        if path is None:
            text += "\n\n" + files("jarvis").joinpath("assets", "capability_sdk.txt").read_text(encoding="utf-8").strip()
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

    Итоговый system prompt — строго упорядоченная склейка четырёх отдельных
    блоков: person_prompt, master_prompt, model_info и capabilities.
    """

    standalone_action_ids = {
        action["type"] for action in capability_catalog.get("actions", [])
    }
    standalone_event_types = {
        event["type"] for event in capability_catalog.get("events", [])
    }
    catalog = {
        "storage_root": capability_catalog.get("storage_root", ""),
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
            "call_result": {
                "type": "call_result",
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
    model_info = (
        model_capabilities.prompt_block()
        if model_capabilities is not None
        else "Информация о возможностях модели недоступна."
    )
    capabilities = json_text(catalog, indent=2)
    blocks = (
        ("person_prompt", person_prompt.strip()),
        ("master_prompt", master_prompt.strip()),
        ("model_info", model_info),
        ("capabilities", capabilities),
    )
    return "\n\n".join(f"{name}:\n{content}" for name, content in blocks)
