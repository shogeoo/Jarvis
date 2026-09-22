"""Долговременная память экземпляров агентов в ``.jarvis/memory``.

Раскладка::

    memory/<preset_id>/agent.json
    memory/<preset_id>/context.json
    memory/<preset_id>/parts/            # бинарные части корневого агента
    memory/<preset_id>/<agent_id>/agent.json
    memory/<preset_id>/<agent_id>/context.json
    memory/<preset_id>/<agent_id>/parts/

Корневой агент preset (``main``) хранится без подпапки ``agent_id``.
``agent.json`` описывает экземпляр, ``context.json`` содержит историю
сообщений без system message: он пересобирается при запуске.

Все пять модальностей переживают перезапуск: бинарные части (image, audio,
video, file) пишутся в ``parts/`` под тем же именем, которое им дал автор
части, а в context.json остаётся ссылка ``jarvis_part``. При загрузке ссылка
разворачивается обратно в исходную OpenAI-часть.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# Точка в ID корневой единицы запрещена: она означает единицу модуля.
_CAPABILITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_PART_REF = "jarvis_part"
_PART_EXTENSIONS = {
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


def valid_name(value: Any) -> bool:
    return isinstance(value, str) and bool(_NAME.fullmatch(value))


def valid_capability(value: Any) -> bool:
    return isinstance(value, str) and bool(_CAPABILITY.fullmatch(value))


def _valid_message(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("role") not in {
        "user",
        "assistant",
    }:
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return all(isinstance(part, dict) for part in content)
    return False


def _parse_data_url(url: Any) -> tuple[str, str] | None:
    """Достать mime и base64 из data-URL. Без префикса — вернуть как есть."""

    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        head, separator, payload = url.partition(",")
        if not separator:
            return None
        mime = head[5:].split(";", 1)[0].strip() or "application/octet-stream"
        return mime, payload
    return None


def _safe_part_name(name: Any, mime: str, payload: str) -> str:
    """Имя файла в parts/: ровно то, что дал автор части, без переименования.

    Отсекаются только компоненты пути (безопасность), само имя не меняется.
    Безымянным частям имя даётся из содержимого и mime.
    """

    raw = str(name or "").strip().replace("\\", "/").split("/")[-1].strip()
    raw = raw.lstrip(".")
    if raw:
        return raw
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()[:12]
    extension = _PART_EXTENSIONS.get(mime) or mime.split("/", 1)[-1] or "bin"
    return f"{digest}.{extension}"


def _extract_part(part: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """(kind, mime, name, base64) из OpenAI-части; текст — None."""

    ptype = part.get("type")
    if ptype == "image_url":
        inner = part.get("image_url") or {}
        parsed = _parse_data_url(inner.get("url"))
        if parsed is None:
            return None
        mime, payload = parsed
        return "image", mime, str(inner.get("name") or ""), payload
    if ptype == "input_audio":
        inner = part.get("input_audio") or {}
        payload = inner.get("data")
        if not isinstance(payload, str) or not payload:
            return None
        fmt = str(inner.get("format") or "wav").strip() or "wav"
        return "audio", f"audio/{fmt}", str(inner.get("name") or ""), payload
    if ptype == "file":
        inner = part.get("file") or {}
        parsed = _parse_data_url(inner.get("file_data"))
        if parsed is None:
            return None
        mime, payload = parsed
        return "file", mime, str(inner.get("filename") or ""), payload
    if ptype == "video_url":
        inner = part.get("video_url") or {}
        parsed = _parse_data_url(inner.get("url"))
        if parsed is None:
            return None
        mime, payload = parsed
        return "video", mime, str(inner.get("name") or ""), payload
    return None


def _externalize_modalities(messages: list[Any], parts_dir: Path) -> list[Any]:
    """Бинарные части — в parts/ под исходным именем; в JSON — ссылка."""

    stored: list[Any] = []
    for message in messages:
        if not _valid_message(message):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            stored.append(message)
            continue
        new_content: list[dict[str, Any]] = []
        for part in content:
            extracted = _extract_part(part)
            if extracted is None:
                new_content.append(part)
                continue
            kind, mime, name, payload = extracted
            try:
                raw = base64.b64decode(payload)
            except (ValueError, TypeError):
                continue
            filename = _safe_part_name(name, mime, payload)
            parts_dir.mkdir(parents=True, exist_ok=True)
            path = parts_dir / filename
            if not path.exists() or path.read_bytes() != raw:
                path.write_bytes(raw)
            new_content.append(
                {
                    _PART_REF: {
                        "type": kind,
                        "mime_type": mime,
                        "name": name,
                        "file": f"parts/{filename}",
                    }
                }
            )
        if new_content:
            stored.append({**message, "content": new_content})
    return stored


def _hydrate_part(ref: dict[str, Any], parts_dir: Path) -> dict[str, Any] | None:
    info = ref.get(_PART_REF)
    if not isinstance(info, dict):
        return None
    filename = str(info.get("file") or "").replace("\\", "/").split("/")[-1]
    path = parts_dir / filename
    raw = path.read_bytes()
    payload = base64.b64encode(raw).decode("ascii")
    kind = info.get("type")
    mime = str(info.get("mime_type") or "application/octet-stream")
    name = str(info.get("name") or "")
    url = f"data:{mime};base64,{payload}"
    if kind == "image":
        inner: dict[str, Any] = {"url": url}
        if name:
            inner["name"] = name
        return {"type": "image_url", "image_url": inner}
    if kind == "audio":
        fmt = mime.split("/", 1)[-1] or "wav"
        inner = {"data": payload, "format": fmt}
        if name:
            inner["name"] = name
        return {"type": "input_audio", "input_audio": inner}
    if kind == "file":
        return {
            "type": "file",
            "file": {"filename": name or "document", "file_data": url},
        }
    if kind == "video":
        inner = {"url": url}
        if name:
            inner["name"] = name
        return {"type": "video_url", "video_url": inner}
    return None


def _hydrate_modalities(messages: list[Any], parts_dir: Path) -> list[Any]:
    """Развернуть ссылки jarvis_part обратно в OpenAI-части."""

    restored: list[Any] = []
    for message in messages:
        if not _valid_message(message):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            restored.append(message)
            continue
        new_content: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and _PART_REF in part:
                try:
                    rebuilt = _hydrate_part(part, parts_dir)
                except OSError:
                    continue
                if rebuilt is not None:
                    new_content.append(rebuilt)
            else:
                new_content.append(part)
        if new_content:
            restored.append({**message, "content": new_content})
    return restored


def _clean_capabilities(values: Any, pattern) -> list[str]:
    if not isinstance(values, list) or not all(
        isinstance(item, str) and pattern.fullmatch(item) for item in values
    ):
        return []
    return sorted(set(values))


class MemoryStore:
    """Читает и атомарно пишет память экземпляров по пресетам."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def agent_dir(self, preset: str, agent_id: str) -> Path:
        if not valid_name(preset):
            raise ValueError(f"Некорректный preset для памяти: {preset!r}")
        if not valid_name(agent_id):
            raise ValueError(f"Некорректный agent_id для памяти: {agent_id!r}")
        if agent_id == "main":
            return self.root / preset
        return self.root / preset / agent_id

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @staticmethod
    def _read_json(path: Path) -> Any | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def save(self, record: dict[str, Any]) -> None:
        agent_id = record.get("agent_id")
        preset = record.get("preset") or "main"
        directory = self.agent_dir(preset, agent_id)
        self._write_json(
            directory / "agent.json",
            {
                "agent_id": agent_id,
                "name": record.get("name"),
                "preset": preset,
                "parent_id": record.get("parent_id"),
                "modules": record.get("modules", []),
                "actions": record.get("actions", []),
                "handlers": record.get("handlers", []),
            },
        )
        messages = record.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        self._write_json(
            directory / "context.json",
            _externalize_modalities(messages, directory / "parts"),
        )

    def load(self, preset: str, agent_id: str) -> dict[str, Any] | None:
        directory = self.agent_dir(preset, agent_id)
        metadata = self._read_json(directory / "agent.json")
        messages = self._read_json(directory / "context.json")
        if not isinstance(metadata, dict) or not isinstance(messages, list):
            return None
        return self._clean(preset, agent_id, directory, metadata, messages)

    def load_all(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        records = []
        for preset_dir in sorted(self.root.iterdir()):
            if not preset_dir.is_dir() or preset_dir.name.startswith("."):
                continue
            preset = preset_dir.name
            if not valid_name(preset):
                continue
            main = self._load_agent_dir(preset_dir, preset, "main")
            if main is not None:
                records.append(main)
            for child in sorted(preset_dir.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if not valid_name(child.name) or child.name == "main":
                    continue
                record = self._load_agent_dir(child, preset, child.name)
                if record is not None:
                    records.append(record)
        return records

    def _load_agent_dir(
        self, directory: Path, preset: str, agent_id: str
    ) -> dict[str, Any] | None:
        metadata = self._read_json(directory / "agent.json")
        messages = self._read_json(directory / "context.json")
        if not isinstance(metadata, dict) or not isinstance(messages, list):
            return None
        return self._clean(preset, agent_id, directory, metadata, messages)

    def delete(self, preset: str, agent_id: str) -> None:
        try:
            directory = self.agent_dir(preset, agent_id)
        except ValueError:
            return
        try:
            if agent_id == "main":
                for name in ("agent.json", "context.json"):
                    try:
                        (directory / name).unlink()
                    except FileNotFoundError:
                        pass
                shutil.rmtree(directory / "parts", ignore_errors=True)
            else:
                shutil.rmtree(directory, ignore_errors=True)
        except OSError:
            return

    @staticmethod
    def _clean(
        preset: str,
        agent_id: str,
        directory: Path,
        metadata: Any,
        messages: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(metadata, dict) or not isinstance(messages, list):
            return None
        parent_id = metadata.get("parent_id")
        if parent_id is not None and not valid_name(parent_id):
            return None
        name = metadata.get("name")
        return {
            "agent_id": agent_id,
            "name": name if isinstance(name, str) and name.strip() else agent_id,
            "preset": preset,
            "parent_id": parent_id,
            "modules": _clean_capabilities(metadata.get("modules", []), _NAME),
            "actions": _clean_capabilities(metadata.get("actions", []), _CAPABILITY),
            "handlers": _clean_capabilities(metadata.get("handlers", []), _CAPABILITY),
            "messages": _hydrate_modalities(messages, directory / "parts"),
        }
