"""Registration of built-in services; implementation lives in focused modules."""

from dataclasses import replace
from ..capabilities.api import action_definition
from .protocol import object_schema
from .agent_actions import register_agent_actions
from .preset_actions import register_preset_actions
from .capability_actions import register_capability_actions


def system_capabilities(*, primary=False, preset=""):
    """Intrinsic assignments; never persisted as editable preset capabilities."""
    values = ["no_action", "send_message_to_agent", "semantic_memory"]
    if primary:
        values += [
            "agents",
            "shutdown",
            "speech_detected",
            "list_assigned_capabilities",
            "list_available_capabilities",
            "capability_info",
            "enable_capability",
            "disable_capability",
        ]
    if preset == "capability_manager":
        values += [
            "capability_development",
            "list_capabilities",
            "capability_info",
            "toggle_capability",
        ]
    if preset == 'memory_manager':
        values += ['memory', 'list_assigned_capabilities', 'list_available_capabilities', 'capability_info', 'enable_capability', 'disable_capability']
    return values


def register_memory_actions(registry):
    def write(data, context):
        return {
            "id": context.agent_manager._manager.require_agent(context.agent_id).semantic_memory.write(data["content"])
        }

    def edit(data, context):
        context.agent_manager._manager.require_agent(context.agent_id).semantic_memory.edit(data["id"], data["content"])
        return {"status": "updated"}

    def delete(data, context):
        context.agent_manager._manager.require_agent(context.agent_id).semantic_memory.delete(data["id"])
        return {"status": "deleted"}

    definitions = (
        (
            "write",
            "Save a confirmed, stable fact in Jarvis semantic memory. Returns its generated ID.",
            object_schema(
                {
                    "content": {
                        "type": "string",
                        "description": "One concise, confirmed semantic fact to remember.",
                    }
                }
            ),
            object_schema(
                {
                    "id": {
                        "type": "string",
                        "description": "Generated semantic memory entry ID.",
                    }
                }
            ),
            write,
        ),
        (
            "edit",
            "Replace one semantic memory entry by its ID.",
            object_schema(
                {
                    "id": {
                        "type": "string",
                        "description": "ID of the semantic memory entry to replace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete replacement fact.",
                    },
                }
            ),
            object_schema({"status": {"type": "string", "enum": ["updated"]}}),
            edit,
        ),
        (
            "delete",
            "Delete one semantic memory entry by its ID.",
            object_schema(
                {
                    "id": {
                        "type": "string",
                        "description": "ID of the semantic memory entry to delete.",
                    }
                }
            ),
            object_schema({"status": {"type": "string", "enum": ["deleted"]}}),
            delete,
        ),
    )
    for identifier, description, arguments, result, handler in definitions:
        registry.register(
            replace(
                action_definition(description, arguments, result, handler),
                id="semantic_memory." + identifier,
                owner="core:module:semantic_memory",
            )
        )


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
    from .memory_actions import register_archive_actions
    register_archive_actions(registry)
    def shutdown(data, context):
        if context.agent_id != 'main':
            raise ValueError('Only Main can shut down the logical session')
        return context.agent_manager._manager.session_shutdown.start(context.call_id)
    registry.register(replace(action_definition(
        'Complete the entire logical session: interrupt running work, archive all experience, then give one final farewell and exit. Only Main may call this; use it as the sole action.',
        object_schema({}), object_schema({'status': {}}), shutdown), id='shutdown', owner='core:primary'))
    registry.register(
        replace(
            action_definition(
                "Reply to the user through the JSON console trace when s2 is unavailable.",
                object_schema({"text": {"type": "string"}}),
                object_schema({"status": {"type": "string"}}),
                reply,
            ),
            id="reply",
            owner="core:reply",
        )
    )
