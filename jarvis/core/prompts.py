"""Сборка актуального systemprompt агента в памяти."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..infrastructure.model_capabilities import ModelCapabilities
from .protocol import json_text
from .registry import ActionRegistry


def read_environment_prompt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Не удалось прочитать описание среды {path}: {exc}") from exc
    if not text:
        raise RuntimeError(f"Описание среды пусто: {path}")
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
    environment_prompt: str,
    action_specs: dict[str, Any],
    event_specs: dict[str, Any],
    module_catalog: list[dict[str, Any]],
    *,
    capabilities: ModelCapabilities | None = None,
) -> str:
    """Собрать systemprompt из постоянных и динамических частей."""

    module_action_names = {
        action["type"]
        for module in module_catalog
        for action in module["actions"]
    }
    module_event_names = {
        event["type"]
        for module in module_catalog
        for event in module["events"]
    }
    catalog = {
        "core_protocol": {
            "actions": ActionRegistry.catalog(
                {
                    name: spec
                    for name, spec in action_specs.items()
                    if name not in module_action_names
                }
            ),
            "events": _event_catalog(
                {
                    name: spec
                    for name, spec in event_specs.items()
                    if name not in module_event_names
                }
            ),
        },
        "modules": module_catalog,
    }
    parts = [person_prompt.strip(), environment_prompt.strip()]
    if capabilities is not None:
        parts.append(capabilities.prompt_block())
    parts.append("Доступный контракт модулей:\n" + json_text(catalog, indent=2))
    return "\n\n".join(part for part in parts if part)
