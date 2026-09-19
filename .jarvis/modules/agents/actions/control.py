from jarvis.core.protocol import Event


def _report(ctx, action_type, function):
    try:
        result = function()
    except Exception as exc:
        ctx.emit(
            "agents.operation_result",
            {
                "action_id": ctx.action_id,
                "action_type": action_type,
                "status": "error",
                "result": None,
                "error": str(exc),
            },
        )
        return
    ctx.emit(
        "agents.operation_result",
        {
            "action_id": ctx.action_id,
            "action_type": action_type,
            "status": "success",
            "result": result,
            "error": None,
        },
    )


def spawn_agent(data, ctx):
    _report(
        ctx,
        "agents.spawn",
        lambda: ctx.agent_manager.spawn(
            parent_id=ctx.agent_id,
            name=data["name"],
            preset=data["preset"],
        ),
    )


def send_message(data, ctx):
    def send():
        sender = ctx.agent_manager.require_agent(ctx.agent_id)
        target = ctx.agent_manager.require_agent(data["agent_id"])
        if not target.accepts_module("agents"):
            raise ValueError("Получателю недоступен модуль agents")
        delivered = ctx.event_bus.publish(
            Event(
                type="agents.message",
                data={
                    "from_agent_id": sender.agent_id,
                    "from_name": sender.name,
                    "text": data["text"],
                },
                source=f"agent:{sender.agent_id}",
                target=target.agent_id,
                module_id="agents",
            )
        )
        return {"delivered": delivered, "agent_id": target.agent_id}

    _report(ctx, "agents.message", send)


def interrupt_agent(data, ctx):
    _report(
        ctx,
        "agents.interrupt",
        lambda: ctx.agent_manager.interrupt(
            agent_id=data["agent_id"], reason=data["reason"]
        ),
    )


def delete_agent(data, ctx):
    _report(
        ctx,
        "agents.delete",
        lambda: ctx.agent_manager.delete(
            agent_id=data["agent_id"], reason=data["reason"]
        ),
    )


def list_agents(data, ctx):
    _report(ctx, "agents.list", lambda: {"agents": ctx.agent_manager.list_agents()})


def create_preset(data, ctx):
    def create():
        available = ctx.modules.loaded_names()
        unknown = set(data["modules"]) - available
        if unknown:
            raise ValueError(f"Неизвестные модули: {sorted(unknown)}")
        preset = ctx.agent_manager.presets.create(
            data["name"], data["person_prompt"], data["modules"]
        )
        return {
            "name": preset.name,
            "modules": list(preset.modules),
            "protected": preset.protected,
        }

    _report(ctx, "agents.preset_create", create)


def delete_preset(data, ctx):
    def delete():
        users = ctx.agent_manager.agents_using_preset(data["name"])
        if users:
            raise ValueError(
                f"Пресет используется активными агентами: {sorted(users)}"
            )
        ctx.agent_manager.presets.delete(data["name"])
        return {"name": data["name"], "deleted": True}

    _report(ctx, "agents.preset_delete", delete)


def list_presets(data, ctx):
    _report(
        ctx,
        "agents.preset_list",
        lambda: {
            "presets": [
                {
                    "name": preset.name,
                    "modules": list(preset.modules),
                    "protected": preset.protected,
                }
                for preset in ctx.agent_manager.presets.list()
            ]
        },
    )
