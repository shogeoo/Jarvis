def run(tasks, ctx):
    while not ctx.stop_event.is_set():
        task = tasks.get()
        if task is None:
            continue
        try:
            result = _execute(task, ctx)
        except Exception as exc:
            _emit(ctx, task, "error", None, str(exc))
        else:
            _emit(ctx, task, "success", result, None)


def stop(tasks, ctx):
    tasks.clear()
    tasks.close()


def _execute(task, ctx):
    manager = ctx.agent_manager
    if task.action_type == "module_control.list_existing":
        active = set(manager.require_agent(task.agent_id).modules())
        return {
            "modules": [
                {**item, "active_for_agent": item["module_id"] in active}
                for item in ctx.modules.list_existing()
            ]
        }
    if task.action_type == "module_control.list_active":
        return {"modules": sorted(manager.require_agent(task.agent_id).modules())}
    if task.action_type == "module_control.enable":
        result = manager.enable_module(task.agent_id, task.data["module_id"])
        if task.agent_id == "main":
            preset = manager.presets.add_module("main", task.data["module_id"])
            result["persistent"] = True
            result["preset_modules"] = list(preset.modules)
        else:
            result["persistent"] = False
        return result
    if task.action_type == "module_control.disable":
        module_id = task.data["module_id"]
        if module_id == "module_control":
            raise ValueError("Управляющий модуль не может выключить сам себя")
        return manager.disable_module(task.agent_id, module_id)
    raise ValueError(f"Неизвестное действие: {task.action_type}")


def _emit(ctx, task, status, result, error):
    ctx.emit(
        "module_control.operation_result",
        {"action_type": task.action_type, "status": status, "result": result, "error": error},
        target=task.agent_id,
    )
