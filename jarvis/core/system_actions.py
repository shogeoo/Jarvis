"""Registration of built-in services; implementation lives in focused modules."""

from dataclasses import replace
from ..capabilities.api import action_definition
from .protocol import object_schema
from .agent_actions import register_agent_actions, require_main
from .preset_actions import register_preset_actions
from .capability_actions import register_capability_actions

SYSTEM_ALL = frozenset({"send_message_to_agent"})
SYSTEM_MAIN = frozenset({
    "spawn_agent", "interrupt_agent", "delete_agent", "list_agents", "get_agent", "edit_agent",
    "list_agent_presets", "get_preset", "create_preset", "edit_preset", "remove_preset",
    "enable_capability", "disable_capability", "list_active_capabilities", "list_available_capabilities",
    "list_capabilities", "capability_info", "toggle_capability",
    "reply", "memory_write", "memory_edit", "memory_delete",
})

def register_memory_actions(registry):
    def write(data, context):
        require_main(context)
        return {"id": context.agent_manager._manager.semantic_memory.write(data["content"])}

    def edit(data, context):
        require_main(context)
        context.agent_manager._manager.semantic_memory.edit(data["id"], data["content"])
        return {"status": "updated"}

    def delete(data, context):
        require_main(context)
        context.agent_manager._manager.semantic_memory.delete(data["id"])
        return {"status": "deleted"}

    definitions = (
        ("memory_write", "Save a confirmed, stable fact in Jarvis semantic memory. Returns its generated ID.",
         object_schema({"content": {"type": "string", "description": "One concise, confirmed semantic fact to remember."}}),
         object_schema({"id": {"type": "string", "description": "Generated semantic memory entry ID."}}), write),
        ("memory_edit", "Replace one semantic memory entry by its ID.",
         object_schema({"id": {"type": "string", "description": "ID of the semantic memory entry to replace."},
                        "content": {"type": "string", "description": "Complete replacement fact."}}),
         object_schema({"status": {"type": "string", "enum": ["updated"]}}), edit),
        ("memory_delete", "Delete one semantic memory entry by its ID.",
         object_schema({"id": {"type": "string", "description": "ID of the semantic memory entry to delete."}}),
         object_schema({"status": {"type": "string", "enum": ["deleted"]}}), delete),
    )
    for identifier, description, arguments, result, handler in definitions:
        registry.register(replace(action_definition(description, arguments, result, handler), id=identifier, owner="core:primary"))


def reply(data, context):
    context.agent_manager._manager.debug.reply(data["text"], agent_id=context.agent_id)
    return {"status": "successful"}



def register_system_actions(registry):
    from .developer_actions import register_developer_actions
    register_developer_actions(registry)
    register_agent_actions(registry)
    register_preset_actions(registry)
    register_capability_actions(registry)
    register_memory_actions(registry)
    registry.register(replace(action_definition(
        "Reply to the user through the JSON console trace when s2 is unavailable.",
        object_schema({"text": {"type": "string"}}),
        object_schema({"status": {"type": "string"}}), reply),
        id="reply", owner="core:reply"))
