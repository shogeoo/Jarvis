"""Вывод сообщений, которые добавляются в контекст модели."""

from __future__ import annotations

import json
import colorsys
import secrets
import sys
import threading
from typing import Any
from .console import logger


_MAIN_COLOR = "\033[32m"  # зелёный: всё, что связано с main
_ERROR_COLOR = "\033[31m"  # красный: structure_error и иные ошибки
_RESET = "\033[0m"

_ERROR_EVENT_IDS = frozenset({"structure_error", "capability_error"})


class Debugger:
    """Печатает только протокольный JSON event-action формата.

    Печатается ровно то, что видит модель в своём контексте, и строго
    как JSON-структура: вход — {"event_id":..., "data":...} как его задаёт
    модуль, результат — {"event_id":"call_result", "call_id":...,
    "data":...}, выход — content assistant message.

    Сломанный (невалидный) ответ модели в консоль не выводится вовсе:
    модель видит только structure_error, содержащий его текст.
    structure_error и capability_error подсвечиваются красным. Технические
    ошибки и текст reply сохраняются в файловой диагностике, не в trace.

    Между блоками — одна пустая строка. Завершающие переводы строк убираются,
    чтобы соседние сообщения не создавали двойной интервал. Всё, что связано
    с main, печатается зелёным; каждый субагент (включая его handlers и
    call_result) получает собственный случайный цвет. Цвет включается только на терминале.

    OpenAI-обёртка (списки text/image_url, data URL с base64) и
    переформатирование JSON намеренно не выводятся: base64 текстом
    в лог попадать не должен.

    Внутренние события доставки, состояния агентов и этапы действий намеренно
    не выводятся: они дублируют сообщения, которые модель уже увидит через
    event-action протокол.
    """

    def __init__(self, *, enabled: bool = True, stream=None, buffered: bool = False):
        self.enabled = enabled
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()
        self._started = False
        self._buffered = buffered
        self._buffer = []
        self._agent_colors: dict[str, str] = {}
        self._used_colors: set[tuple[int, int, int]] = set()

    def initializing(self) -> None:
        if self.enabled:
            with self._lock:
                self.stream.write("Инициализация системы Jarvis....\n")
                self.stream.flush()

    def initialization_notice(self, message: str) -> None:
        """Show authentication progress before the JSON-only runtime trace."""
        if self.enabled:
            with self._lock:
                self.stream.write(message.rstrip("\r\n") + "\n")
                self.stream.flush()

    def initialized(self) -> None:
        with self._lock:
            if self.enabled:
                self.stream.write("Система инициализирована.\n")
                self.stream.flush()
                self._started = True
            buffered = self._buffer
            self._buffer = []
            self._buffered = False
            for agent_id, error, rendered in buffered:
                self._write_locked(agent_id, error=error, rendered=rendered)

    def _color(self, agent_id: str, *, error: bool) -> str:
        detect = getattr(self.stream, "isatty", None)
        if detect is not None and not detect():
            return ""
        if error:
            return _ERROR_COLOR
        if agent_id == "main":
            return _MAIN_COLOR
        if agent_id not in self._agent_colors:
            while True:
                hue = secrets.randbelow(360000) / 360000
                saturation = 0.55 + secrets.randbelow(3000) / 10000
                brightness = 0.8 + secrets.randbelow(2000) / 10000
                rgb = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, saturation, brightness))
                if rgb not in self._used_colors:
                    break
            self._used_colors.add(rgb)
            self._agent_colors[agent_id] = "\033[38;2;" + ";".join(map(str, rgb)) + "m"
        return self._agent_colors[agent_id]

    def _write(self, agent_id: str, *, error: bool, rendered: str) -> None:
        with self._lock:
            if self._buffered:
                self._buffer.append((agent_id, error, rendered))
                return
            self._write_locked(agent_id, error=error, rendered=rendered)

    def _write_locked(self, agent_id: str, *, error: bool, rendered: str) -> None:
        color = self._color(agent_id, error=error)
        separator = "\n" if self._started else ""
        self._started = True
        # Keep the separator, color and complete JSON in one stream operation.
        # A lock protects concurrent agents; a single write also avoids exposing
        # partial blocks to stream wrappers and terminal capture integrations.
        self.stream.write(
            separator + color + rendered.rstrip("\r\n")
            + (_RESET if color else "") + "\n"
        )
        self.stream.flush()

    def log(self, event_name: str, **data: Any) -> None:
        """Техническая диагностика сохраняется только в файл."""
        logger.info("%s %s", event_name, json.dumps(data, ensure_ascii=False, default=str))

    def reply(self, text: str, *, agent_id: str = "main") -> None:
        logger.info("reply %s: %s", agent_id, text)

    def error(self, message: str) -> None:
        """Ошибки API не смешиваются с model-facing JSON."""
        logger.error("%s", message)

    def message(self, content: Any, *, agent_id: str = "main") -> None:
        """Вывести блок строго как отформатированный JSON.

        Печатается ровно то, что видит модель. Сломанный (невалидный)
        ответ модели в консоль не выводится вовсе: вместо него ядро
        доставляет structure_error, содержащий его текст.
        """

        if not self.enabled:
            return
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                return
        rendered = json.dumps(content, ensure_ascii=False, indent=2)
        error = isinstance(content, dict) and (
            content.get("event_id") in _ERROR_EVENT_IDS
        )
        self._write(agent_id, error=error, rendered=rendered)

    def input(self, event: Any, capabilities: Any = None, agent_id: str = "main") -> None:
        """Вывести входное событие в формате event_id/data."""

        self.message(event.model_value(), agent_id=agent_id)

    def result(self, result: Any, agent_id: str = "main") -> None:
        """Вывести результат как событие: event_id/call_id/data."""

        self.message(result.model_value(), agent_id=agent_id)

    def event(self, direction: str, agent_id: str, event: Any) -> None:
        """Старый API: доставка события не является выводом для модели."""

    def action(self, stage: str, agent_id: str, action: Any, **extra: Any) -> None:
        """Старый API: действие уже присутствует в ответе модели."""

    def model(self, agent_id: str, output: str, *, actions: Any = None) -> None:
        """Вывести content assistant message строго как JSON."""

        self.message(output, agent_id=agent_id)

    def state(self, agent_id: str, state: str, **extra: Any) -> None:
        """Старый API: внутреннее состояние не выводится в model trace."""
