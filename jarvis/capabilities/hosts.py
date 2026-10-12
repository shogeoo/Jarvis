"""Capability host records; no lifecycle or dispatch logic."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .api import ActionDefinition, HandlerDefinition, ModuleDefinition


@dataclass(slots=True)
class UnitHost:
    """Живой процесс одной единицы (действие, handler или модуль)."""

    key: str
    kind: str
    unit_id: str
    module_id: str | None
    path: Path
    catalog: dict[str, Any]
    action_ids: list[str] = field(default_factory=list)
    handler_ids: list[str] = field(default_factory=list)
    process: subprocess.Popen | None = None
    reader_thread: threading.Thread | None = None
    writer_lock: threading.Lock = field(default_factory=threading.Lock)
    start_lock: threading.Lock = field(default_factory=threading.Lock)
    stderr_stream: Any = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    closing: bool = False


@dataclass(slots=True)
class LoadedAction:
    id: str
    definition: ActionDefinition
    path: Path
    host: UnitHost
    module_id: str | None


@dataclass(slots=True)
class LoadedHandler:
    id: str
    definition: HandlerDefinition
    path: Path
    host: UnitHost
    module_id: str | None


@dataclass(slots=True)
class LoadedModule:
    module_id: str
    definition: ModuleDefinition
    path: Path
    host: UnitHost
    action_ids: list[str] = field(default_factory=list)
    handler_ids: list[str] = field(default_factory=list)
