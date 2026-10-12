"""Live preset bases: person.txt plus model and global capability IDs."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class AgentPreset:
    name: str
    person_prompt: str
    capabilities: tuple[str, ...]
    model: str | None = None
    _categories: tuple[str, ...] = ()

    def _of_kind(self, kind):
        return tuple(value for value, category in zip(self.capabilities, self._categories) if category == kind)

    @property
    def modules(self):
        return self._of_kind("module")

    @property
    def actions(self):
        return self._of_kind("action")

    @property
    def handlers(self):
        return self._of_kind("handler")


class PresetStore:
    def __init__(self, root: Path, *, resolve_kind=None):
        self.root = Path(root)
        self.resolve_kind = resolve_kind
        self._lock = threading.RLock()

    @staticmethod
    def validate_name(name):
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ValueError(f"Invalid snake_case preset_id: {name!r}")

    @staticmethod
    def validate_capability(kind, capability_id):
        if kind not in {"module", "action", "handler"} or not isinstance(capability_id, str) or not IDENTIFIER.fullmatch(capability_id):
            raise ValueError(f"Invalid capability_id: {capability_id!r}")

    def path(self, name):
        self.validate_name(name)
        return self.root / name

    def _kind(self, capability_id):
        if self.resolve_kind is not None:
            return self.resolve_kind(capability_id)
        if capability_id == "capability_development":
            return "module"
        for kind, category in (("module", "modules"), ("handler", "handlers")):
            if (self.root.parent / "capabilities" / category / capability_id).is_dir():
                return kind
        return "action"

    @classmethod
    def _normalize(cls, capabilities):
        if isinstance(capabilities, dict):
            capabilities = [item for key in ("modules", "actions", "handlers") for item in capabilities.get(key, [])]
        if not isinstance(capabilities, (list, tuple)):
            raise ValueError("capabilities must be an array of global IDs")
        for value in capabilities:
            cls.validate_capability("action", value)
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("Duplicate capability IDs")
        return list(capabilities)

    def load(self, name):
        with self._lock:
            path = self.path(name)
            person = (path / "person.txt").read_text(encoding="utf-8").strip()
            metadata_path = path / "preset.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
            if not isinstance(metadata, dict):
                raise ValueError(f"Invalid preset.json: {name}")
            if "capabilities" not in metadata:
                # Only the migration path reads the obsolete split files.
                legacy = json.loads((path / "capabilities.json").read_text(encoding="utf-8"))
                disabled_file = path / "disabled_capabilities.json"
                disabled = json.loads(disabled_file.read_text(encoding="utf-8")) if disabled_file.exists() else {}
                values = [item for key in ("modules", "actions", "handlers") for item in legacy.get(key, []) if item not in disabled.get(key, [])]
                if name == "capability_manager":
                    values = list(dict.fromkeys([*values, "capability_development", "list_capabilities", "capability_info", "toggle_capability"]))
                self._replace(name, person, {"model": metadata.get("model"), "capabilities": self._normalize(values)})
                metadata = {"model": metadata.get("model"), "capabilities": values}
            values = self._normalize(metadata["capabilities"])
            model = metadata.get("model")
            if model is not None and (not isinstance(model, str) or not model.strip()):
                raise ValueError("model must be a nonempty ID or null")
            if not person:
                raise ValueError("person.txt must not be empty")
            return AgentPreset(name, person, tuple(values), model, tuple(self._kind(value) for value in values))

    def list(self):
        return [self.load(path.name) for path in sorted(self.root.iterdir()) if path.is_dir() and not path.name.startswith(".")] if self.root.exists() else []

    @staticmethod
    def _write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _replace(self, name, person, metadata):
        path = self.path(name)
        staging = Path(tempfile.mkdtemp(prefix=".edit-", dir=self.root))
        replacement = staging / "replacement"
        try:
            shutil.copytree(path, replacement)
            (replacement / "person.txt").write_text(person.strip() + "\n", encoding="utf-8")
            self._write(replacement / "preset.json", metadata)
            for obsolete in ("capabilities.json", "disabled_capabilities.json", "disabled_actions.json", "disabled_handlers.json", "disabled_modules.json"):
                (replacement / obsolete).unlink(missing_ok=True)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.renameat2(-100, os.fsencode(path), -100, os.fsencode(replacement), 2) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        finally:
            shutil.rmtree(staging)

    def create(self, name, person_prompt, capabilities=None, model=None):
        path = self.path(name)
        values = self._normalize(capabilities or [])
        if not isinstance(person_prompt, str) or not person_prompt.strip():
            raise ValueError("person must not be empty")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a nonempty ID or null")
        with self._lock:
            if path.exists():
                raise ValueError(f"Пресет уже существует: {name}")
            self.root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=self.root))
            try:
                (staging / "person.txt").write_text(person_prompt.strip() + "\n", encoding="utf-8")
                self._write(staging / "preset.json", {"model": model, "capabilities": values})
                staging.rename(path)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return self.load(name)

    def edit(self, name, person_prompt=None, capabilities=None, model=None, *, changes=None):
        with self._lock:
            current = self.load(name)
            update = changes if changes is not None else {"person": person_prompt, "capabilities": capabilities, "model": model}
            if set(update) - {"person", "model", "capabilities", "capabilities_add", "capabilities_remove"}:
                raise ValueError("Unknown preset changes")
            person = update.get("person", current.person_prompt)
            selected_model = update.get("model", current.model)
            values = self._normalize(update.get("capabilities", current.capabilities))
            for value in self._normalize(update.get("capabilities_add", [])):
                if value not in values:
                    values.append(value)
            removed = self._normalize(update.get("capabilities_remove", []))
            values = [value for value in values if value not in removed]
            if not isinstance(person, str) or not person.strip():
                raise ValueError("person must not be empty")
            if selected_model is not None and (not isinstance(selected_model, str) or not selected_model.strip()):
                raise ValueError("model must be a nonempty ID or null")
            self._replace(name, person, {"model": selected_model, "capabilities": values})
            return self.load(name)

    def add_capability(self, name, kind, capability_id):
        self.validate_capability(kind, capability_id)
        return self.edit(name, changes={"capabilities_add": [capability_id]})

    def delete(self, name):
        with self._lock:
            path = self.path(name)
            if not path.is_dir():
                raise ValueError(f"Пресет не найден: {name}")
            shutil.rmtree(path)
