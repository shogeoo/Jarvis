"""Create the required on-disk Jarvis runtime layout without replacing user data."""

from __future__ import annotations

import json
from pathlib import Path


_EMPTY_LIST = "[]\n"


def _write_if_missing(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except FileExistsError:
        pass


def _ensure_preset(root: Path, name: str, *, protected: bool = False) -> None:
    preset = root / "presets" / name
    preset.mkdir(parents=True, exist_ok=True)
    if name == "main":
        prompt = (
            "Ты — Jarvis, цифровой ассистент пользователя. Отвечай ясно, "
            "точно и на языке собеседника. Используй только доступные действия.\n"
        )
    elif name == "module_manager":
        prompt = (
            "Ты — Module Manager, разработчик capabilities Jarvis. Выполняй "
            "задачи родителя доступными инструментами и сообщай ему результат.\n"
        )
    else:
        prompt = f"Ты — агент preset {name}. Выполняй порученную задачу.\n"
    _write_if_missing(preset / "personprompt.txt", prompt)
    for filename in ("actions.json", "handlers.json", "modules.json"):
        _write_if_missing(preset / filename, _EMPTY_LIST)
    _write_if_missing(
        preset / "preset.json",
        json.dumps({"protected": protected}, ensure_ascii=False, indent=2) + "\n",
    )
    disabled_path = preset / "disabled_capabilities.json"
    try:
        state = json.loads(disabled_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    changed = not disabled_path.exists()
    for key in ("modules", "actions", "handlers"):
        legacy_path = preset / f"disabled_{key}.json"
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8")) if legacy_path.is_file() else []
        except (OSError, json.JSONDecodeError):
            legacy = []
        current = state.get(key, [])
        if not isinstance(current, list):
            current = []
            changed = True
        merged = list(dict.fromkeys([*current, *(legacy if isinstance(legacy, list) else [])]))
        if merged != current or key not in state:
            state[key] = merged
            changed = True
    if changed:
        temporary = disabled_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(disabled_path)
    for key in ("modules", "actions", "handlers"):
        (preset / f"disabled_{key}.json").unlink(missing_ok=True)


def ensure_runtime_layout(root: Path) -> None:
    """Restore absent runtime directories and structural defaults on startup.

    Existing capability code, presets, memory, logs and other user content are
    left untouched. Missing user-authored capability source cannot be inferred
    or reconstructed, so only its storage directories are created here.
    """

    root = Path(root).expanduser()
    for relative in (
        "actions",
        "handlers",
        "modules",
        "presets",
        "runtime",
        "runtime/logs",
        "runtime/models",
        "runtime/segments",
        "runtime/voices",
        "memory",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)

    state_path = root / "capability_state.json"
    legacy_path = root / "disabled_capabilities.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    paused = state.get("paused", {})
    if not isinstance(paused, dict):
        paused = {}
    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8")) if legacy_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        legacy = {}
    legacy_paused = legacy.get("paused", legacy) if isinstance(legacy, dict) else {}
    merged_paused = {}
    for key in ("modules", "actions", "handlers"):
        current = paused.get(key, [])
        old = legacy_paused.get(key, []) if isinstance(legacy_paused, dict) else []
        merged_paused[key] = list(dict.fromkeys([
            *(current if isinstance(current, list) else []),
            *(old if isinstance(old, list) else []),
        ]))
    normalized = {"paused": merged_paused}
    if normalized != state or legacy_path.exists():
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(state_path)
    legacy_path.unlink(missing_ok=True)

    _ensure_preset(root, "main", protected=True)
    _ensure_preset(root, "module_manager")
    for preset in (root / "presets").iterdir():
        if preset.is_dir() and not preset.name.startswith("."):
            _ensure_preset(root, preset.name, protected=preset.name == "main")
