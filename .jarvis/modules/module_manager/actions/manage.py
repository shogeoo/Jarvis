import shutil
import subprocess


def _report(ctx, action_type, function):
    try:
        result = function()
    except Exception as exc:
        ctx.emit(
            "module_manager.operation_result",
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
        "module_manager.operation_result",
        {
            "action_id": ctx.action_id,
            "action_type": action_type,
            "status": "success",
            "result": result,
            "error": None,
        },
    )


def _path(ctx, value):
    root = ctx.modules.modules_dir.resolve()
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Путь выходит за пределы .jarvis/modules")
    return candidate


def list_workspace(data, ctx):
    def run():
        path = _path(ctx, data["path"])
        if not path.exists():
            return {"path": str(path), "files": []}
        if not path.is_dir():
            raise ValueError(f"Не каталог: {path}")
        return {
            "path": str(path),
            "files": sorted(str(item.relative_to(path)) for item in path.rglob("*")),
        }

    _report(ctx, "module_manager.workspace_list", run)


def read_file(data, ctx):
    _report(
        ctx,
        "module_manager.workspace_read",
        lambda: {
            "path": str(_path(ctx, data["path"])),
            "content": _path(ctx, data["path"]).read_text(encoding="utf-8"),
        },
    )


def write_file(data, ctx):
    def run():
        path = _path(ctx, data["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data["content"], encoding="utf-8")
        return {"path": str(path), "bytes": path.stat().st_size}

    _report(ctx, "module_manager.workspace_write", run)


def delete_path(data, ctx):
    def run():
        path = _path(ctx, data["path"])
        if path == ctx.modules.modules_dir.resolve():
            raise ValueError("Корневой каталог модулей удалить нельзя")
        if path.is_dir():
            if data["recursive"]:
                shutil.rmtree(path)
            else:
                path.rmdir()
        else:
            path.unlink(missing_ok=True)
        return {"path": str(path), "deleted": True}

    _report(ctx, "module_manager.workspace_delete", run)


def run_process(data, ctx):
    def run():
        cwd = _path(ctx, data["cwd"] or ".")
        proc = ctx.agent_manager.processes.start(
            data["command"],
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=data["timeout_seconds"])
            return {
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            from jarvis.core.lifecycle import terminate_process

            terminate_process(proc, group=True)
            stdout, stderr = proc.communicate(timeout=2)
            return {
                "returncode": None,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": True,
            }
        finally:
            ctx.agent_manager.processes.finish(proc)

    _report(ctx, "module_manager.process_run", run)


def validate_module(data, ctx):
    _report(
        ctx,
        "module_manager.validate",
        lambda: ctx.modules.validate(data["module_id"]),
    )


def complete_module(data, ctx):
    def run():
        summary = ctx.modules.apply(
            data["operation"],
            data["module_id"],
            requested_by=ctx.module_id,
            requester_agent_id=ctx.agent_id,
        )
        return {"summary": data["summary"], **summary}

    _report(ctx, "module_manager.complete", run)


def add_to_preset(data, ctx):
    def run():
        module_id = data["module_id"]
        if module_id not in ctx.modules.loaded_names():
            raise ValueError(f"Модуль не загружен: {module_id}")
        preset = ctx.agent_manager.presets.add_module(data["preset"], module_id)
        return {"preset": preset.name, "modules": list(preset.modules)}

    _report(ctx, "module_manager.add_to_preset", run)
