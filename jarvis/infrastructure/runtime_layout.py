"""Create runtime structure and synchronize built-in preset personalities."""
from __future__ import annotations

from .console import logger
import json
import traceback
from pathlib import Path
from importlib.resources import files

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


def _ensure_preset(root: Path, name: str, *, protected: bool = False) -> None:
    preset = root / "presets" / name
    preset.mkdir(parents=True, exist_ok=True)
    prompt = (files("jarvis").joinpath("assets", name + ".txt").read_text(encoding="utf-8")
              if name in {"main", "module_manager"} else f"Ты — агент preset {name}.\n")
    prompt_path = preset / "personprompt.txt"
    if name in {"main", "module_manager"}:
        if not prompt_path.exists() or prompt_path.read_text(encoding="utf-8") != prompt:
            temporary = preset / "personprompt.txt.tmp"
            temporary.write_text(prompt, encoding="utf-8")
            temporary.replace(prompt_path)
    else:
        _write_if_missing(prompt_path, prompt)
    _write_if_missing(preset / "capabilities.json", json.dumps(_EMPTY, indent=2) + "\n")
    _write_if_missing(preset / "preset.json", json.dumps({"protected": protected}, indent=2) + "\n")
    _state_if_missing(preset / "disabled_capabilities.json", _EMPTY)


def ensure_runtime_layout(root: Path) -> None:
    root = Path(root).expanduser()
    for relative in (
        "actions", "handlers", "modules", "presets", "runtime", "runtime/logs",
        "runtime/models", "runtime/segments", "runtime/voices", "memory",
        "memory/main", "memory/main/files", "memory/main/long_term/sessions",
        "memory/main/long_term/files",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    _write_if_missing(root / "automations.json", "[]\n")
    _write_if_missing(root / "memory" / "main" / "semantic.json", json.dumps({"entries": []}, indent=2) + "\n")
    _state_if_missing(root / "capability_state.json", {"paused": _EMPTY})
    _ensure_preset(root, "main", protected=True)
    _ensure_preset(root, "module_manager")
    for preset in (root / "presets").iterdir():
        if preset.is_dir() and not preset.name.startswith("."):
            _ensure_preset(root, preset.name, protected=preset.name == "main")
