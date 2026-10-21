"""Create missing runtime files and migrate paths without replacing PERSON."""
from __future__ import annotations

from .console import logger
import json
import traceback
from pathlib import Path
from importlib.resources import files
from .storage_migration import migrate_capability_directories

_EMPTY = {"modules": [], "actions": [], "handlers": []}


def _write_if_missing(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except FileExistsError:
        pass


def _state_if_missing(path: Path, default: dict) -> None:
    _write_if_missing(path, json.dumps(default, ensure_ascii=False, indent=2) + "\n")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        state = value.get("paused") if "paused" in default else value
        if not isinstance(state, dict) or any(
            not isinstance(state.get(key), list)
            or not all(isinstance(item, str) for item in state[key])
            for key in _EMPTY
        ):
            raise ValueError("Invalid capability state")
    except Exception:
        logger.error(f"Jarvis cannot read {path}; original file preserved:\n{traceback.format_exc()}")


def _ensure_preset(root: Path, name: str) -> None:
    preset = root / "presets" / name
    preset.mkdir(parents=True, exist_ok=True)
    prompt = (files("jarvis").joinpath("assets", name + "_person.txt").read_text(encoding="utf-8")
              if name in {"main", "capability_manager", "memory_manager"} else f"Ты — агент preset {name}.\n")
    prompt_path = preset / "person.txt"
    _write_if_missing(prompt_path, prompt)
    default = ([value for value in ("send_notification", "set_timer", "take_screenshot")
                if (root / "capabilities/actions" / value).is_dir()] if name == "main" else [])
    # Existing legacy metadata is migrated by PresetStore without overwriting PERSON.
    metadata = {} if (preset / "capabilities.json").exists() else {"description": {"main": "The central JARVIS personality and human interlocutor.", "capability_manager": "Develops and maintains the capabilities of JARVIS.", "memory_manager": "Researches archived experience of J.A.R.V.I.S. instances."}.get(name, ""), "model": None, "capabilities": default}
    _write_if_missing(preset / "preset.json", json.dumps(metadata, indent=2) + "\n")


def ensure_runtime_layout(root: Path) -> None:
    root = Path(root).expanduser()
    migrate_capability_directories(root)
    for relative in (
        "capabilities/actions", "capabilities/handlers", "capabilities/modules", "presets", "runtime", "runtime/logs",
        "runtime/models", "runtime/segments", "runtime/voices", "memory",
        "memory/instances", "memory/sessions",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    _write_if_missing(root / 'memory/instances/main/semantic.json', json.dumps({'entries': []}, indent=2) + '\n')
    _state_if_missing(root / "capability_state.json", {"paused": _EMPTY})
    old_manager = root / "presets" / "capabilities_manager"
    new_manager = root / "presets" / "capability_manager"
    if old_manager.exists() and new_manager.exists():
        raise ValueError("Cannot merge conflicting old and new capability_manager data")
    if old_manager.exists() and not new_manager.exists():
        old_manager.rename(new_manager)
    _ensure_preset(root, "main")
    _ensure_preset(root, "capability_manager")
    _ensure_preset(root, "memory_manager")
    for preset in (root / "presets").iterdir():
        if preset.is_dir() and not preset.name.startswith("."):
            _ensure_preset(root, preset.name)
