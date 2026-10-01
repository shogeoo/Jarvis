"""Build one dynamic system message from explicitly labeled blocks."""

from importlib.resources import files
from pathlib import Path
from typing import Any

from ..infrastructure.model_capabilities import ModelCapabilities, SUPPORTED_INPUT_MODALITIES
from ..infrastructure.prompt_templates import render_template
from .protocol import json_text, validate_catalog_text
from .registry import ActionRegistry


def read_environment(path: Path | None = None) -> str:
    resource = (
        path
        if path is not None
        else files("jarvis").joinpath("assets", "environment.txt")
    )
    text = resource.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"Empty environment: {resource}")
    return text


def agent_system_prompt(
    person_prompt: str,
    environment: str,
    action_specs: dict[str, Any],
    event_specs: dict[str, Any],
    capability_catalog: dict[str, Any],
    *,
    agent_id: str,
    agent_name: str,
    model_capabilities: ModelCapabilities | None = None,
    semantic_memory: dict[str, Any] | None = None,
) -> str:
    modules = capability_catalog.get("modules", [])
    module_actions = {
        action["action_id"] for module in modules for action in module["actions"]
    }
    module_events = {
        event["event_id"] for module in modules for event in module["events"]
    }
    catalog = {
        "actions": ActionRegistry.catalog(
            {
                name: spec
                for name, spec in action_specs.items()
                if name not in module_actions
            }
        ),
        "events": [
            {
                "event_id": spec.event_id,
                "description": spec.description,
                "data_schema": spec.data_schema,
            }
            for name, spec in event_specs.items()
            if name not in module_events
        ],
        "modules": modules,
    }
    validate_catalog_text(catalog)
    model = model_capabilities or ModelCapabilities("unknown", SUPPORTED_INPUT_MODALITIES)
    blocks = [("ENVIRONMENT", environment.strip()), ("PERSON", person_prompt.strip())]
    if semantic_memory is not None:
        blocks.append(("MEMORY", render_template("memory_instruction.txt") + "\n" + json_text(semantic_memory, indent=2)))
    blocks.extend(
        [
            ("MODEL INFO", render_template("model_info.txt", model_id=model.model, modalities=model.prompt_block())),
            ("AGENT_INFO", render_template("agent_info.txt", name=agent_name, agent_id=agent_id)),
            ("CAPABILITIES", json_text(catalog, indent=2)),
        ]
    )
    return "\n\n".join(render_template("system_section.txt", title=title, content=content) for title, content in blocks)
