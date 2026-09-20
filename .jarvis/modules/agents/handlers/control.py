def run(tasks, ctx):
    while not ctx.stop_event.is_set():
        task = tasks.get()
        if task is None:
            continue
        try:
            result = _execute(task, ctx)
        except Exception as exc:
            _result(ctx, task, "error", None, str(exc))
        else:
            _result(ctx, task, "success", result, None)


def stop(tasks, ctx):
    tasks.clear()
    tasks.close()


def _execute(task, ctx):
    data = task.data
    manager = ctx.agent_manager
    if task.action_type == "agents.spawn":
        return manager.spawn(parent_id=task.agent_id, name=data["name"], preset=data["preset"])
    if task.action_type == "agents.message":
        sender = manager.require_agent(task.agent_id)
        target = manager.require_agent(data["agent_id"])
        if not target.accepts_module("agents"):
            raise ValueError("Получателю недоступен модуль agents")
        ctx.emit(
            "agents.message",
            {"from_agent_id": sender.agent_id, "from_name": sender.name, "text": data["text"]},
            target=target.agent_id,
        )
        return {"delivered": True, "agent_id": target.agent_id}
    if task.action_type == "agents.interrupt":
        return manager.interrupt(agent_id=data["agent_id"], reason=data["reason"])
    if task.action_type == "agents.delete":
        return manager.delete(agent_id=data["agent_id"], reason=data["reason"])
    if task.action_type == "agents.list":
        return {"agents": manager.list_agents()}
    if task.action_type == "agents.preset_list":
        return {
            "presets": [
                {"name": preset.name, "modules": list(preset.modules), "protected": preset.protected}
                for preset in manager.presets.list()
            ]
        }
    raise ValueError(f"Неизвестное действие: {task.action_type}")


def _result(ctx, task, status, result, error):
    ctx.emit(
        "agents.operation_result",
        {"action_type": task.action_type, "status": status, "result": result, "error": error},
        target=task.agent_id,
    )
