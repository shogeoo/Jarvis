import shutil
import subprocess

from jarvis.core.lifecycle import terminate_process


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


def _path(ctx, value):
    root = ctx.modules.modules_dir.resolve()
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Путь выходит за пределы .jarvis/modules")
    if ".venv" in candidate.relative_to(root).parts:
        raise ValueError("Содержимое module-local .venv управляется системой")
    return candidate


def _execute(task, ctx):
    data = task.data
    if task.action_type == "module_manager.workspace_list":
        path = _path(ctx, data["path"])
        if not path.exists():
            return {"path": str(path), "files": []}
        if not path.is_dir():
            raise ValueError(f"Не каталог: {path}")
        files = []
        for item in path.rglob("*"):
            relative = item.relative_to(path)
            if any(part in {".venv", "__pycache__"} for part in relative.parts):
                continue
            files.append(str(relative))
        return {"path": str(path), "files": sorted(files)}
    if task.action_type == "module_manager.workspace_read":
        path = _path(ctx, data["path"])
        return {"path": str(path), "content": path.read_text(encoding="utf-8")}
    if task.action_type == "module_manager.workspace_write":
        path = _path(ctx, data["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data["content"], encoding="utf-8")
        return {"path": str(path), "bytes": path.stat().st_size}
    if task.action_type == "module_manager.workspace_delete":
        path = _path(ctx, data["path"])
        if path == ctx.modules.modules_dir.resolve():
            raise ValueError("Корневой каталог модулей удалить нельзя")
        if path.is_dir():
            shutil.rmtree(path) if data["recursive"] else path.rmdir()
        else:
            path.unlink(missing_ok=True)
        return {"path": str(path), "deleted": True}
    if task.action_type == "module_manager.process_run":
        return _run_process(task, ctx)
    if task.action_type == "module_manager.validate":
        return ctx.modules.validate(data["module_id"])
    if task.action_type == "module_manager.disable":
        if data["module_id"] == "module_manager":
            raise ValueError("module_manager не может выключить сам себя")
        return ctx.modules.disable_for_edit(data["module_id"])
    if task.action_type == "module_manager.enable":
        return ctx.modules.enable_after_edit(data["module_id"])
    if task.action_type == "module_manager.prepare_environment":
        return ctx.modules.create_environment(data["module_id"])
    if task.action_type == "module_manager.add_to_agent":
        return ctx.agent_manager.enable_module(data["agent_id"], data["module_id"])
    raise ValueError(f"Неизвестное действие: {task.action_type}")


def _run_process(task, ctx):
    data = task.data
    cwd = _path(ctx, data["cwd"] or ".")
    proc = ctx.agent_manager.processes.start(
        data["command"], shell=True, cwd=str(cwd), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=data["timeout_seconds"])
        return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr, "timed_out": False}
    except subprocess.TimeoutExpired:
        terminate_process(proc, group=True)
        stdout, stderr = proc.communicate(timeout=2)
        return {"returncode": None, "stdout": stdout, "stderr": stderr, "timed_out": True}
    finally:
        ctx.agent_manager.processes.finish(proc)


def _emit(ctx, task, status, result, error):
    ctx.emit(
        "module_manager.operation_result",
        {"action_type": task.action_type, "status": status, "result": result, "error": error},
        target=task.agent_id,
    )
