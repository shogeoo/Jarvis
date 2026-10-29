"""Durable live instances; completed sessions are committed separately."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.request import urlopen

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_FILE = re.compile(r"^file_[0-9]{6,}$")
KINDS = {
    "image_url": "image",
    "input_audio": "audio",
    "video_url": "video",
    "file": "file",
}


def valid_name(value):
    return isinstance(value, str) and bool(_NAME.fullmatch(value))


def valid_capability(value):
    return valid_name(value)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return deepcopy(default)


def hydrate_part(part, directory):
    if part.get("type") not in KINDS or not part.get("file_id"):
        return deepcopy(part)
    filename = part["filename"]
    if Path(filename).name != filename:
        raise ValueError("Invalid attachment filename")
    raw = (Path(directory) / "files" / filename).read_bytes()
    mime = (
        mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    data = base64.b64encode(raw).decode("ascii")
    kind = part["type"]
    if kind == "input_audio":
        return {
            "type": kind,
            kind: {"data": data, "format": Path(filename).suffix.lstrip(".")},
        }
    if kind == "file":
        return {
            "type": kind,
            kind: {"filename": filename, "file_data": f"data:{mime};base64,{data}"},
        }
    return {
        "type": kind,
        kind: {"url": f"data:{mime};base64,{data}"},
    }


def context_files(context):
    """Attachment metadata has one source of truth: the context tree."""
    def walk(value):
        if isinstance(value, dict):
            if value.get("type") in KINDS and value.get("file_id"):
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)
    return list(walk(context))


class MemoryStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.instances = self.root / "instances"
        self.sessions = self.root / "sessions"
        self._save_lock = threading.RLock()
        self._migrate_file_indexes()

    def _migrate_file_indexes(self):
        """Fold the previous duplicate registry into its owning context once."""
        for index_path in self.root.rglob("files.json"):
            directory = index_path.parent
            if directory.parent.name != "instances":
                continue
            self._recover(directory)
            context = read_json(directory / "context.json", [])
            registry = {f["file_id"]: f for f in read_json(index_path, {"files": []})["files"]}
            obsolete = {"source", "sha256", "native_options", "mime_type", "format", "agent_id", "session_id", "interpreted"}

            def clean(part, ancestors=()):
                if part.get("file_id") in ancestors:
                    return
                for key in obsolete:
                    part.pop(key, None)
                identifiers = re.findall(r"memory://[^/]+/[^/]+/(file_[0-9]+)\?", part.get("document_markdown", ""))
                nested = list(part.get("embedded_files", []))
                known = {f["file_id"] for f in nested}
                for identifier in dict.fromkeys(identifiers):
                    if identifier != part["file_id"] and identifier not in known and identifier in registry:
                        nested.append(deepcopy(registry[identifier]))
                        known.add(identifier)
                if nested:
                    part["embedded_files"] = nested
                    for child in nested:
                        clean(child, (*ancestors, part["file_id"]))

            for part in context_files(context):
                clean(part)
            self._write_json(directory / "context.json", context)
            if directory.parent != self.instances:
                metadata = read_json(directory / "instance.json")
                if metadata is not None:
                    self._write_json(directory / "instance.json", {k: v for k, v in metadata.items() if k in {"agent_id", "preset_id", "parent_id", "description", "deleted"}})
            index_path.unlink()
            sync_directory(directory)

    _read_json = staticmethod(read_json)

    @staticmethod
    def _write_json(path, value):
        write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def state(self):
        with self._save_lock:
            state = read_json(self.root / "state.json")
            if state is None:
                state = {
                    "session_id": "session_000001",
                    "next_session": 2,
                    "next_agent": 1,
                    "shutdown": None,
                    "started": False,
                }
                self._write_json(self.root / "state.json", state)
            return state

    def update_state(self, **changes):
        with self._save_lock:
            state = {**self.state(), **changes}
            self._write_json(self.root / "state.json", state)
            return state

    def claim_start_event(self):
        """Runtime directories and empty surviving instances do not define a session."""
        with self._save_lock:
            state = self.state()
            started = state.get("started")
            if started is None:
                # Existing sessions created before the durable startup marker.
                started = any(
                    read_json(p, []) for p in self.instances.glob("*/context.json")
                )
            self.update_state(started=True)
            return "system_restarted" if started else "system_started"

    def allocate_agent_id(self):
        with self._save_lock:
            state = self.state()
            number = max(
                state["next_agent"],
                1
                + max(
                    (
                        int(r["agent_id"][6:])
                        for r in self.records(include_deleted=True)
                        if re.fullmatch(r"agent-[0-9]+", r["agent_id"])
                    ),
                    default=0,
                ),
            )
            self.update_state(next_agent=number + 1)
            return f"agent-{number:05d}"

    def has_existing_state(self):
        return self.instances.exists() and (
            any(self.instances.glob("*/instance.json"))
            or any(self.instances.glob("*/context.json"))
            or any(self.instances.glob("*/.save-journal.json"))
        )

    def agent_dir(self, preset, agent_id):
        if not valid_name(agent_id) or not valid_name(preset):
            raise ValueError("Invalid preset/agent_id")
        return self.instances / agent_id

    def records(self, *, include_deleted=False):
        with self._save_lock:
            result = []
            directories = sorted(
                {
                    p.parent
                    for pattern in (
                        "*/instance.json",
                        "*/context.json",
                        "*/.save-journal.json",
                    )
                    for p in self.instances.glob(pattern)
                }
            )
            for directory in directories:
                self._recover(directory)
                record = read_json(directory / "instance.json")
                if record is None:
                    if directory.name != "main":
                        raise ValueError(f"Missing instance metadata: {directory}")
                    record = {
                        "agent_id": "main",
                        "preset_id": "main",
                        "parent_id": None,
                        "restore_from_preset": True,
                    }

                if include_deleted or not record.get("deleted"):
                    result.append(record)
            return result

    def _recover(self, directory):
        # Redo a prepared transaction. Both values belong to the same new state.
        journal = read_json(directory / ".save-journal.json")
        if journal is not None:
            for key in ("context", "instance"):
                self._write_json(directory / (key + ".json"), journal[key])
            (directory / ".save-journal.json").unlink()
            sync_directory(directory)

    def register_file(self, agent_id, raw, filename, kind, mime, *, siblings=()):
        """Write original bytes; the caller commits metadata in its context tree."""
        with self._save_lock:
            if not valid_name(agent_id) or kind not in KINDS:
                raise ValueError("Invalid file owner/type")
            directory = self.instances / agent_id
            known = [*self.files(agent_id), *context_files(list(siblings))]
            number = 1 + max((int(f["file_id"][5:]) for f in known), default=0)
            filename = Path(filename or ("attachment" + (mimetypes.guess_extension(mime) or ".bin"))).name
            candidate, suffix = filename, 1
            while (directory / "files" / candidate).exists():
                candidate = f"{Path(filename).stem}_{suffix}{Path(filename).suffix}"
                suffix += 1
            files = directory / "files"
            files.mkdir(parents=True, exist_ok=True)
            with (files / candidate).open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(files)
            return {"type": kind, "file_id": f"file_{number:06d}",
                    "filename": candidate, "text_representation": ""}

    def files(self, agent_id):
        return context_files(read_json(self.instances / agent_id / "context.json", []))

    def update_file(self, agent_id, item):
        with self._save_lock:
            path = self.instances / agent_id / "context.json"
            context = read_json(path, [])
            found = False
            for attachment in context_files(context):
                if attachment["file_id"] == item["file_id"]:
                    attachment.clear()
                    attachment.update(deepcopy(item))
                    found = True
            if not found:
                raise ValueError("Unknown context attachment")
            self._write_json(path, context)

    def store_messages(self, agent_id, messages):
        result = deepcopy(messages)
        previous = read_json(self.instances / agent_id / "context.json", [])
        for message_index, message in enumerate(result):
            if message.get("role") not in {
                "system",
                "user",
                "assistant",
                "tool",
                "developer",
            }:
                raise ValueError("Invalid context role")
            content = message.get("content")
            if not isinstance(content, list):
                if content is not None and not isinstance(content, str):
                    raise ValueError("Invalid context content")
                continue
            for j, part in enumerate(content):
                kind = part.get("type")
                if kind not in KINDS:
                    continue
                if part.get("file_id"):
                    # Preserve the context itself, including manual edits.
                    continue
                else:
                    inner = part.get(kind) or {}
                    field = {
                        "image_url": "url",
                        "video_url": "url",
                        "input_audio": "data",
                        "file": "file_data",
                    }[kind]
                    value = inner.get(field)
                    if not isinstance(value, str):
                        raise ValueError(
                            "Attachment must contain original bytes or a URL"
                        )
                    mime = "application/octet-stream"
                    if kind == "input_audio":
                        mime = "audio/" + inner.get("format", "wav")
                        raw = base64.b64decode(value, validate=True)
                    elif value.startswith("data:"):
                        header, data = value.split(",", 1)
                        mime = header[5:].split(";")[0]
                        raw = base64.b64decode(data, validate=True)
                    elif value.startswith(("https://", "http://")):
                        with urlopen(value, timeout=60) as response:
                            raw = response.read()
                            mime = response.headers.get_content_type()
                    else:
                        raise ValueError("Unsupported attachment source")
                    old_parts = (previous[message_index].get("content", []) if message_index < len(previous) else [])
                    old = old_parts[j] if isinstance(old_parts, list) and j < len(old_parts) else {}
                    if old.get("type") == kind and old.get("file_id") and (self.instances / agent_id / "files" / old["filename"]).read_bytes() == raw:
                        item = old
                    else:
                        item = self.register_file(
                            agent_id, raw, inner.get("filename") or inner.get("name"),
                            kind, mime, siblings=context_files(result),
                        )
                content[j] = deepcopy(item)
        return result

    def save(self, record):
        with self._save_lock:
            self.state()
            agent_id = record["agent_id"]
            preset = record.get("preset_id", record.get("preset", "main"))
            directory = self.agent_dir(preset, agent_id)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "files").mkdir(exist_ok=True)
            self._recover(directory)
            old = read_json(directory / "instance.json", {})
            instance = {
                key: deepcopy(record.get(key, default))
                for key, default in (
                    ("agent_id", agent_id),
                    ("preset_id", preset),
                    ("parent_id", None),
                    ("description", None),
                    ("model", None),
                    ("additional_instructions", ""),
                    ("capabilities_add", []),
                    ("pending_turn", False),
                )
            }
            if old.get("deleted"):
                instance["deleted"] = True
            messages = self.store_messages(agent_id, record.get("messages", []))
            self._write_json(
                directory / ".save-journal.json",
                {"instance": instance, "context": messages},
            )
            self._recover(directory)
            if not (directory / "semantic.json").exists():
                self._write_json(directory / "semantic.json", {"entries": []})
            # Return the persisted representation to prevent repeated URL downloads.
            return messages

    def load(self, preset, agent_id):
        with self._save_lock:
            directory = self.agent_dir(preset, agent_id)
            self._recover(directory)
            record = read_json(directory / "instance.json")
            if record is None:
                if agent_id == "main" and (directory / "context.json").exists():
                    record = {
                        "agent_id": agent_id,
                        "preset_id": preset,
                        "parent_id": None,
                        "restore_from_preset": True,
                    }
                else:
                    return None
            if not (directory / "context.json").exists():
                record["restore_missing_context"] = True
            record["messages"] = read_json(directory / "context.json", [])
            return record

    def load_all(self):
        return [self.load(r["preset_id"], r["agent_id"]) for r in self.records()]

    def delete(self, preset, agent_id):
        # Tombstones retain this session's activity until the completed archive exists.
        with self._save_lock:
            directory = self.agent_dir(preset, agent_id)
            instance = read_json(directory / "instance.json")
            if instance is not None:
                instance["deleted"] = True
                self._write_json(directory / "instance.json", instance)

    def delete_preset(self, preset):
        for record in self.records(include_deleted=True):
            if record["preset_id"] == preset:
                self.delete(preset, record["agent_id"])

    def clear_after_archive(self):
        with self._save_lock:
            for record in self.records(include_deleted=True):
                directory = self.instances / record["agent_id"]
                if record.get("deleted"):
                    shutil.rmtree(directory)
                    continue
                self._write_json(directory / "context.json", [])
                record["pending_turn"] = False
                self._write_json(directory / "instance.json", record)
                (directory / "files.json").unlink(missing_ok=True)
                shutil.rmtree(directory / "files", ignore_errors=True)
                (directory / "files").mkdir()
            sync_directory(self.instances)
