"""Persistent semantic facts for the primary Jarvis agent."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path


class SemanticMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> list[dict[str, str]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            value = {"entries": []}
        if (
            not isinstance(value, dict)
            or set(value) != {"entries"}
            or not isinstance(value["entries"], list)
        ):
            raise ValueError(f"Invalid semantic memory: {self.path}")
        entries = value["entries"]
        if any(
            not isinstance(item, dict)
            or set(item) != {"id", "content"}
            or not isinstance(item["id"], str)
            or not isinstance(item["content"], str)
            or not item["content"].strip()
            for item in entries
        ):
            raise ValueError(f"Invalid semantic memory entries: {self.path}")
        numbers = []
        for item in entries:
            if not re.fullmatch(r"mem_[0-9]{6,}", item["id"]):
                raise ValueError(f"Invalid semantic memory ID: {item['id']}")
            numbers.append(int(item["id"][4:]))
        if numbers != sorted(set(numbers)):
            raise ValueError(f"Semantic memory IDs must be unique and increasing: {self.path}")
        return entries

    def snapshot(self) -> dict[str, list[dict[str, str]]]:
        with self._lock:
            return {"entries": [dict(item) for item in self._read()]}

    def _save(self, entries: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump({"entries": entries}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def write(self, content: str) -> str:
        if not content.strip():
            raise ValueError("Memory content must not be empty")
        with self._lock:
            entries = self._read()
            last = entries[-1]["id"] if entries else "mem_000000"
            if not last.startswith("mem_") or not last[4:].isdigit():
                raise ValueError(f"Invalid last semantic memory ID: {last}")
            number = int(last[4:]) + 1
            identifier = f"mem_{number:06d}"
            self._save([*entries, {"id": identifier, "content": content}])
            return identifier

    def edit(self, identifier: str, content: str) -> None:
        if not content.strip():
            raise ValueError("Memory content must not be empty")
        with self._lock:
            entries = self._read()
            if not any(item["id"] == identifier for item in entries):
                raise ValueError(f"Unknown semantic memory ID: {identifier}")
            self._save(
                [
                    {**item, "content": content} if item["id"] == identifier else item
                    for item in entries
                ]
            )

    def delete(self, identifier: str) -> None:
        with self._lock:
            entries = self._read()
            remaining = [item for item in entries if item["id"] != identifier]
            if len(remaining) == len(entries):
                raise ValueError(f"Unknown semantic memory ID: {identifier}")
            self._save(remaining)
