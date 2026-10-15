"""The built-in, indivisible capability-development module."""

from dataclasses import replace
from ..capabilities.file_tools import TOOL_NAMES, file_action

MODULE_ID = "capability_development"


def register_developer_actions(registry):
    for name in TOOL_NAMES:
        definition = file_action(
            f"{MODULE_ID}.{name}",
            lambda context: context.capabilities.root / "capabilities",
            process_manager=lambda context: context.agent_manager._manager.processes,
        )
        registry.register(replace(definition, owner=f"core:module:{MODULE_ID}"))
