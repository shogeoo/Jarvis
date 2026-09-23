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
    for filename in ("disabled_actions.json", "disabled_handlers.json", "disabled_modules.json"):
        _write_if_missing(preset / filename, _EMPTY_LIST)


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

    _write_if_missing(
        root / "disabled_capabilities.json",
        json.dumps(
            {"modules": [], "actions": [], "handlers": [], "targets": {}},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
    )

    _ensure_preset(root, "main", protected=True)
    _ensure_preset(root, "module_manager")
    for preset in (root / "presets").iterdir():
        if preset.is_dir() and not preset.name.startswith("."):
            _ensure_preset(root, preset.name, protected=preset.name == "main")
