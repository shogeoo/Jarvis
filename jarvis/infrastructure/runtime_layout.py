"""Create missing runtime structure while preserving existing persisted files."""
from __future__ import annotations
import json
import sys
import traceback
from pathlib import Path

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
        print(f"Jarvis cannot read {path}; original file preserved:\n{traceback.format_exc()}", file=sys.stderr)


def _ensure_preset(root: Path, name: str, *, protected: bool = False) -> None:
    preset = root / "presets" / name
    preset.mkdir(parents=True, exist_ok=True)
    prompt = (
        "Ты — Jarvis, цифровой ассистент пользователя. Отвечай ясно и точно.\n"
        if name == "main" else
        "Ты — Module Manager, разработчик capabilities Jarvis. Выполняй задачи родителя.\n"
        if name == "module_manager" else f"Ты — агент preset {name}.\n"
    )
    _write_if_missing(preset / "personprompt.txt", prompt)
    _write_if_missing(preset / "capabilities.json", json.dumps(_EMPTY, indent=2) + "\n")
    _write_if_missing(preset / "preset.json", json.dumps({"protected": protected}) + "\n")
    _state_if_missing(preset / "disabled_capabilities.json", _EMPTY)


def ensure_runtime_layout(root: Path) -> None:
    root = Path(root).expanduser()
    for relative in (
        "actions", "handlers", "modules", "presets", "runtime", "runtime/logs",
        "runtime/models", "runtime/segments", "runtime/voices", "memory",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    _write_if_missing(root / "automations.json", "[]\n")
    _state_if_missing(root / "capability_state.json", {"paused": _EMPTY})
    _ensure_preset(root, "main", protected=True)
    _ensure_preset(root, "module_manager")
    for preset in (root / "presets").iterdir():
        if preset.is_dir() and not preset.name.startswith("."):
            _ensure_preset(root, preset.name, protected=preset.name == "main")
