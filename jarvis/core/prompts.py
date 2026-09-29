"""Build one dynamic system message from explicitly labeled blocks."""

from importlib.resources import files
from pathlib import Path
from typing import Any

from ..infrastructure.model_capabilities import ModelCapabilities
from .protocol import json_text, validate_catalog_text
from .registry import ActionRegistry


def read_master_prompt(path: Path | None = None) -> str:
    resource = (
        path
        if path is not None
        else files("jarvis").joinpath("assets", "master_prompt.txt")
    )
    text = resource.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"Empty masterprompt: {resource}")
    return text


def _ordered(
    items: list[dict[str, Any]], field: str, key: str, order: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """Retain the current prefix and append newly enabled definitions."""
    by_id = {item[field]: item for item in items}
    previous = [name for name in order.get(key, []) if name in by_id]
    seen = set(previous)
    previous.extend(name for name in by_id if name not in seen)
    order[key] = previous
    return [by_id[name] for name in previous]


def agent_system_prompt(
    person_prompt: str,
    master_prompt: str,
    action_specs: dict[str, Any],
    event_specs: dict[str, Any],
    capability_catalog: dict[str, Any],
    *,
    model_capabilities: ModelCapabilities | None = None,
    catalog_order: dict[str, list[str]] | None = None,
    semantic_memory: dict[str, Any] | None = None,
) -> str:
    order = catalog_order if catalog_order is not None else {}
    modules = capability_catalog.get("modules", [])
    module_actions = {
        action["action_id"] for module in modules for action in module["actions"]
    }
    module_events = {
        event["event_id"] for module in modules for event in module["events"]
    }
    catalog = {
        "actions": _ordered(
            ActionRegistry.catalog(
                {
                    name: spec
                    for name, spec in action_specs.items()
                    if name not in module_actions
                }
            ),
            "action_id",
            "actions",
            order,
        ),
        "events": _ordered(
            [
                {
                    "event_id": spec.event_id,
                    "description": spec.description,
                    "data_schema": spec.data_schema,
                }
                for name, spec in event_specs.items()
                if name not in module_events
            ],
            "event_id",
            "events",
            order,
        ),
        "modules": _ordered(modules, "module_id", "modules", order),
    }
    for module in catalog["modules"]:
        for kind, field in (("actions", "action_id"), ("events", "event_id")):
            module[kind] = _ordered(
                module[kind], field, module["module_id"] + "." + kind, order
            )
    validate_catalog_text(catalog)
    modalities = (
        model_capabilities.prompt_block()
        if model_capabilities
        else "Текущая модель поддерживает следующие модальности: text, image, audio, video, file."
    )
    blocks = [
        "ENVIRONMENT:\n" + master_prompt.strip(),
        "PERSON:\n" + person_prompt.strip(),
    ]
    if semantic_memory is not None:
        blocks.append(
            "MEMORY:\n"
            "Сохраняй в семантической памяти только подтверждённые и достаточно устойчивые факты о людях, отношениях, окружении, устройствах, предпочтениях, привычках и биографии. "
            "Не сохраняй текущие задачи, временные проекты, планы, обсуждаемые возможности и сведения, в достоверности которых не уверен. "
            "Изменившийся факт обновляй, устаревший удаляй; записи делай короткими, самостоятельными и понятными. "
            "Ниже — записи, которые ты, JARVIS, сохранил для себя в прошлом: знания от тебя прошлого для тебя нынешнего.\n"
            + json_text(semantic_memory, indent=2)
        )
    model_id = model_capabilities.model if model_capabilities else "unknown"
    blocks.extend(
        [
            "MODEL INFO:\nModel ID: " + model_id + "\n" + modalities,
            "CAPABILITIES:\n" + json_text(catalog, indent=2),
        ]
    )
    return "\n\n".join(blocks)
