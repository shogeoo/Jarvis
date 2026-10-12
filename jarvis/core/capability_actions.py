"""Capability lookup and assignment through global IDs, never public kind."""

from dataclasses import replace
from ..capabilities.api import action_definition
from .agent_actions import OPEN, STRING
from .protocol import object_schema


def run(data, context):
    manager = context.agent_manager._manager
    capabilities = context.capabilities
    name = context.action_id
    if name == "list_capabilities":
        return capabilities.list_existing()
    if name == "list_active_capabilities":
        return manager.require_agent(context.agent_id).capabilities_snapshot_lists()
    if name == "list_available_capabilities":
        return capabilities.list_available(manager.require_agent(context.agent_id).capabilities_snapshot())
    capability_id = data["capability_id"]
    kind = capabilities.resolve_kind(capability_id)
    if name == "capability_info":
        return {"capability_id": capability_id, "info": capabilities.capability_info(capability_id)}
    if name == "toggle_capability":
        spec = capabilities.actions.get(capability_id)
        if capability_id in capabilities.builtin_modules() or (spec is not None and spec.owner.startswith("core")):
            raise ValueError("Built-in capabilities cannot be globally paused")
        return {"capability_id": capability_id, **capabilities.toggle_global_state(kind, capability_id)}
    enabled = name == "enable_capability"
    if not enabled:
        agent = manager.require_agent(context.agent_id)
        if capability_id in manager.presets.load(agent.preset).capabilities:
            raise ValueError("Cannot remove a mandatory preset capability; edit the preset instead")
        spec = capabilities.actions.get(capability_id)
        if capability_id in capabilities.builtin_modules() or (spec is not None and spec.owner.startswith("core")):
            raise ValueError("Built-in capabilities cannot be disabled")
    operation = getattr(manager, ("enable_" if enabled else "disable_") + kind)
    operation(context.agent_id, capability_id)
    return {"capability_id": capability_id, "enabled": enabled}


def register_capability_actions(registry):
    for name in ("list_capabilities", "list_active_capabilities", "list_available_capabilities", "capability_info", "toggle_capability", "enable_capability", "disable_capability"):
        lists = {key: {} for key in ("actions", "handlers", "modules")}
        if name.startswith("list_"):
            arguments, result = {}, lists
        elif name == "capability_info":
            arguments, result = {"capability_id": STRING}, {"capability_id": {}, "info": OPEN}
        elif name == "toggle_capability":
            arguments, result = {"capability_id": STRING}, {"capability_id": {}, "state": {}, "affected_agent_ids": {}}
        else:
            arguments, result = {"capability_id": STRING}, {"capability_id": {}, "enabled": {}}
        description = name + ": inspect or manage capabilities by their globally unique ID. Modules are assigned whole. Preset capabilities are mandatory; only individual additions may be removed. Global pause affects user capabilities, not built-ins. Wait for pause result before resuming, never toggle the same ID twice in one batch."
        registry.register(replace(action_definition(description, object_schema(arguments), object_schema(result), run), id=name, owner="core:primary"))
