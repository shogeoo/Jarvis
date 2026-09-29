"""Agent instances and model-shaped contexts in .jarvis/memory.

Binary parts are stored in files/; persisted context parts refer to file_id.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import traceback
from pathlib import Path
from typing import Any

from .console import logger

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_CAPABILITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/webm": "webm",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "application/pdf": "pdf",
    "text/plain": "txt",
}
_MIME = {value: key for key, value in _EXT.items()}
_MIME.update({"wav": "audio/wav", "webm": "audio/webm"})


def valid_name(value: Any) -> bool:
    return isinstance(value, str) and bool(_NAME.fullmatch(value))


def valid_capability(value: Any) -> bool:
    return isinstance(value, str) and bool(_CAPABILITY.fullmatch(value))


def _valid_message(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("role") not in {
        "system",
        "user",
        "assistant",
    }:
        return False
    content = value.get("content")
    return isinstance(content, str) or (
        isinstance(content, list) and all(isinstance(part, dict) for part in content)
    )


def _binary_part(part: dict[str, Any]) -> tuple[str, str, bytes] | None:
    kind = part.get("type")
    field = {
        "image_url": "url",
        "video_url": "url",
        "file": "file_data",
        "input_audio": "data",
    }.get(kind)
    if field is None:
        return None
    inner = part.get(kind) or {}
    value = inner.get(field)
    if not isinstance(value, str) or not value:
        return None
    if kind == "input_audio":
        mime, payload = "audio/" + str(inner.get("format") or "wav"), value
    elif value.startswith("data:") and "," in value:
        header, payload = value.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "application/octet-stream"
    else:
        return None
    return kind, mime, base64.b64decode(payload, validate=True)


def _extension(mime: str) -> str:
    if mime in _EXT:
        return _EXT[mime]
    guessed = mimetypes.guess_extension(mime)
    return (
        guessed[1:] if guessed and re.fullmatch(r"\.[A-Za-z0-9]+", guessed) else "bin"
    )


def _hydrate_part(part: dict[str, Any], files_dir: Path) -> dict[str, Any]:
    kind = part.get("type")
    inner = part.get(kind)
    if (
        kind not in {"image_url", "video_url", "file", "input_audio"}
        or not isinstance(inner, dict)
        or "file_id" not in inner
    ):
        return part
    file_id = inner["file_id"]
    if not isinstance(file_id, str) or not re.fullmatch(r"file_[0-9]{6,}", file_id):
        raise ValueError(f"Invalid file_id: {file_id!r}")
    matches = list(files_dir.glob(file_id + ".*"))
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected one file for {file_id}: {files_dir}")
    path = matches[0]
    mime = (
        _MIME.get(path.suffix[1:])
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    if kind == "video_url" and path.suffix == ".webm":
        mime = "video/webm"
    elif kind == "input_audio" and path.suffix == ".webm":
        mime = "audio/webm"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    url = f"data:{mime};base64,{payload}"
    if kind == "input_audio":
        return {"type": kind, kind: {"data": payload, "format": mime.split("/", 1)[-1]}}
    if kind == "file":
        return {"type": kind, kind: {"filename": path.name, "file_data": url}}
    return {"type": kind, kind: {"url": url}}


def _clean_capabilities(values: Any, pattern) -> list[str]:
    if not isinstance(values, list) or not all(
        isinstance(item, str) and pattern.fullmatch(item) for item in values
    ):
        return []
    return sorted(set(values))


class MemoryStore:
    """Persist a single current state per agent, without snapshot directories."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._save_lock = threading.RLock()

    def has_existing_state(self) -> bool:
        return self.root.exists() and any(
            (preset / "instance.json").is_file()
            or (preset / ".save-journal.json").is_file()
            or any(
                (child / "instance.json").is_file()
                or (child / ".save-journal.json").is_file()
                for child in preset.iterdir()
                if child.is_dir()
            )
            for preset in self.root.iterdir()
            if preset.is_dir() and valid_name(preset.name)
        )

    def agent_dir(self, preset: str, agent_id: str) -> Path:
        if not valid_name(preset) or not valid_name(agent_id):
            raise ValueError(f"Invalid preset/agent_id: {preset!r}/{agent_id!r}")
        return (
            self.root / preset if agent_id == "main" else self.root / preset / agent_id
        )

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> Any | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _allocate_file_id(self) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".file_index.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            index = self.root / "file_index.json"
            state = self._read_json(index)
            if state is None:
                highest = max(
                    (
                        int(path.stem[5:])
                        for path in self.root.rglob("file_*.*")
                        if path.is_file() and re.fullmatch(r"file_[0-9]{6,}", path.stem)
                    ),
                    default=0,
                )
                state = {"next_file_id": highest + 1}
            number = state["next_file_id"]
            if not isinstance(number, int) or number < 1:
                raise ValueError("Invalid file ID index")
            self._write_json(index, {"next_file_id": number + 1})
            return f"file_{number:06d}"

    def _store_parts(
        self, messages: list[dict[str, Any]], files_dir: Path
    ) -> list[dict[str, Any]]:
        by_digest: dict[tuple[str, str], str] = {}
        if files_dir.exists():
            for path in files_dir.iterdir():
                if path.is_file() and re.fullmatch(
                    r"file_[0-9]{6,}\.[A-Za-z0-9]+", path.name
                ):
                    by_digest[
                        (hashlib.sha256(path.read_bytes()).hexdigest(), path.suffix)
                    ] = path.stem
        result = []
        for message in messages:
            if not _valid_message(message):
                raise ValueError("Invalid model context message")
            content = message["content"]
            if not isinstance(content, list):
                result.append(dict(message))
                continue
            parts = []
            for part in content:
                binary = _binary_part(part)
                if binary is None:
                    parts.append(part)
                    continue
                kind, mime, raw = binary
                suffix = "." + _extension(mime)
                key = (hashlib.sha256(raw).hexdigest(), suffix)
                file_id = by_digest.get(key)
                if file_id is None:
                    file_id = self._allocate_file_id()
                    files_dir.mkdir(parents=True, exist_ok=True)
                    with (files_dir / (file_id + suffix)).open("xb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    by_digest[key] = file_id
                parts.append({"type": kind, kind: {"file_id": file_id}})
            result.append({**message, "content": parts})
        return result

    def _rollback_if_needed(self, directory: Path) -> None:
        journal = directory / ".save-journal.json"
        saved = self._read_json(journal)
        if saved is None:
            return
        if not isinstance(saved, dict) or set(saved) != {"instance", "context"}:
            raise ValueError(f"Invalid save journal: {journal}")
        for name, key in (("instance.json", "instance"), ("context.json", "context")):
            path = directory / name
            if saved[key] is None:
                path.unlink(missing_ok=True)
            else:
                self._write_json(path, saved[key])
        journal.unlink()

    def save(self, record: dict[str, Any]) -> None:
        with self._save_lock:
            self._save_locked(record)

    def _save_locked(self, record: dict[str, Any]) -> None:
        agent_id = record.get("agent_id")
        preset = record.get("preset") or "main"
        directory = self.agent_dir(preset, agent_id)
        (directory / "files").mkdir(parents=True, exist_ok=True)
        self._rollback_if_needed(directory)
        modules = _clean_capabilities(record.get("modules", []), _NAME)
        actions = [
            name
            for name in _clean_capabilities(record.get("actions", []), _CAPABILITY)
            if not any(name.startswith(module + ".") for module in modules)
        ]
        handlers = [
            name
            for name in _clean_capabilities(record.get("handlers", []), _CAPABILITY)
            if not any(name.startswith(module + ".") for module in modules)
        ]
        disabled = [
            f"{kind}:{name}"
            for kind, field in (
                ("module", "disabled_modules"),
                ("action", "disabled_actions"),
                ("handler", "disabled_handlers"),
            )
            for name in _clean_capabilities(record.get(field, []), _CAPABILITY)
            if not any(name.startswith(module + ".") for module in modules)
        ]
        instance = {
            "agent_id": agent_id,
            "name": record.get("name"),
            "preset": preset,
            "person_prompt": record.get("person_prompt"),
            "protected": record.get("protected"),
            "parent_id": record.get("parent_id"),
            "modules": modules,
            "actions": actions,
            "handlers": handlers,
            "disabled_capabilities": disabled,
            "catalog_order": record.get("catalog_order", {}),
            "automated_call_ids": record.get("automated_call_ids", []),
        }
        messages = record.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("Agent context must be a list")
        stored = self._store_parts(messages, directory / "files")
        journal = directory / ".save-journal.json"
        self._write_json(
            journal,
            {
                "instance": self._read_json(directory / "instance.json"),
                "context": self._read_json(directory / "context.json"),
            },
        )
        try:
            self._write_json(directory / "context.json", stored)
            self._write_json(directory / "instance.json", instance)
        except Exception:
            try:
                self._rollback_if_needed(directory)
            except Exception:
                logger.error(
                    "Jarvis save rollback failed for %s; journal preserved:\n%s",
                    directory,
                    traceback.format_exc(),
                )
            raise
        journal.unlink()

    def load(self, preset: str, agent_id: str) -> dict[str, Any] | None:
        directory = self.agent_dir(preset, agent_id)
        with self._save_lock:
            self._rollback_if_needed(directory)
        instance = self._read_json(directory / "instance.json")
        context = self._read_json(directory / "context.json")
        if instance is None and context is None:
            return None
        return self._clean(preset, agent_id, directory, instance, context)

    def load_all(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        records = []
        for preset in sorted(self.root.iterdir()):
            if not preset.is_dir() or not valid_name(preset.name):
                continue
            if (preset / "instance.json").exists() or (
                preset / ".save-journal.json"
            ).exists():
                try:
                    records.append(self.load(preset.name, "main"))
                except Exception:
                    logger.error(
                        "Jarvis restore failed for %s/main; saved data preserved:\n%s",
                        preset.name,
                        traceback.format_exc(),
                    )
            for child in sorted(preset.iterdir()):
                if (
                    child.is_dir()
                    and valid_name(child.name)
                    and (
                        (child / "instance.json").exists()
                        or (child / ".save-journal.json").exists()
                    )
                ):
                    try:
                        records.append(self.load(preset.name, child.name))
                    except Exception:
                        logger.error(
                            "Jarvis restore failed for %s/%s; saved data preserved:\n%s",
                            preset.name,
                            child.name,
                            traceback.format_exc(),
                        )
        return [record for record in records if record is not None]

    def delete(self, preset: str, agent_id: str) -> None:
        with self._save_lock:
            directory = self.agent_dir(preset, agent_id)
            if agent_id == "main":
                for name in ("instance.json", "context.json"):
                    (directory / name).unlink(missing_ok=True)
                shutil.rmtree(directory / "files", ignore_errors=True)
            elif directory.exists():
                shutil.rmtree(directory)

    def delete_preset(self, preset: str) -> None:
        with self._save_lock:
            directory = self.agent_dir(preset, "main")
            if directory.exists():
                shutil.rmtree(directory)

    @staticmethod
    def _clean(
        preset: str, agent_id: str, directory: Path, instance: Any, context: Any
    ) -> dict[str, Any]:
        if not isinstance(instance, dict) or not isinstance(context, list):
            raise ValueError(f"Invalid persisted instance/context: {directory}")
        disabled = instance.get("disabled_capabilities", [])
        if not isinstance(disabled, list) or not all(
            isinstance(item, str) for item in disabled
        ):
            raise ValueError(f"Invalid disabled capabilities: {directory}")
        messages = []
        for message in context:
            if not _valid_message(message):
                raise ValueError(f"Invalid context message: {directory}")
            content = message["content"]
            messages.append(
                {
                    **message,
                    "content": [
                        _hydrate_part(part, directory / "files") for part in content
                    ],
                }
                if isinstance(content, list)
                else dict(message)
            )
        parent_id = instance.get("parent_id")
        if parent_id is not None and not valid_name(parent_id):
            raise ValueError(f"Invalid parent ID: {directory}")
        return {
            "agent_id": agent_id,
            "name": instance.get("name") or agent_id,
            "preset": preset,
            "person_prompt": instance.get("person_prompt"),
            "protected": instance.get("protected"),
            "parent_id": parent_id,
            "modules": _clean_capabilities(instance.get("modules", []), _NAME),
            "actions": _clean_capabilities(instance.get("actions", []), _CAPABILITY),
            "handlers": _clean_capabilities(instance.get("handlers", []), _CAPABILITY),
            "disabled_modules": [
                item[7:] for item in disabled if item.startswith("module:")
            ],
            "disabled_actions": [
                item[7:] for item in disabled if item.startswith("action:")
            ],
            "disabled_handlers": [
                item[8:] for item in disabled if item.startswith("handler:")
            ],
            "catalog_order": instance.get("catalog_order", {}),
            "automated_call_ids": instance.get("automated_call_ids", []),
            "messages": messages,
        }
