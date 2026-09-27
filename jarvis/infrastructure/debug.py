"""Вывод сообщений, которые добавляются в контекст модели."""

from __future__ import annotations

import json
import sys
import threading
from typing import Any


_MAIN_COLOR = "\033[32m"  # зелёный: всё, что связано с main
_SUB_COLOR = "\033[33m"  # жёлтый: субагенты и их handlers
_ERROR_COLOR = "\033[31m"  # красный: structure_error и иные ошибки
_RESET = "\033[0m"

_ERROR_EVENT_TYPES = frozenset({"structure_error", "capability_error"})


class Debugger:
    """Печатает только протокольный JSON event-action формата.

    Печатается ровно то, что видит модель в своём контексте, и строго
    как JSON-структура: вход — {"type":..., "data":...} как его задаёт
    модуль, результат — {"type":"call_result", "call_id":...,
    "data":...}, выход — content assistant message.

    Сломанный (невалидный) ответ модели в консоль не выводится вовсе:
    модель видит только structure_error, содержащий его текст.
    structure_error и capability_error подсвечиваются красным. Ошибки LLM API
    печатаются отдельными красными строками без заголовка и JSON-обёртки.

    Между блоками — одна пустая строка. Завершающие переводы строк убираются,
    чтобы соседние сообщения не создавали двойной интервал. Всё, что связано
    с main, печатается зелёным; субагенты (включая их handlers и
    call_result) — жёлтым. Цвет включается только на терминале.

    OpenAI-обёртка (списки text/image_url, data URL с base64) и
    переформатирование JSON намеренно не выводятся: base64 текстом
    в лог попадать не должен.

    Внутренние события доставки, состояния агентов и этапы действий намеренно
    не выводятся: они дублируют сообщения, которые модель уже увидит через
    event-action протокол.
    """

    def __init__(self, *, enabled: bool = True, stream=None):
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()
        self._started = False

    def _color(self, agent_id: str, *, error: bool) -> str:
        detect = getattr(self.stream, "isatty", None)
        if detect is not None and not detect():
            return ""
        if error:
            return _ERROR_COLOR
        return _MAIN_COLOR if agent_id == "main" else _SUB_COLOR

    def _write(self, agent_id: str, *, error: bool, rendered: str) -> None:
        color = self._color(agent_id, error=error)
        with self._lock:
            if self._started:
                self.stream.write("\n")
            self._started = True
            if color:
                self.stream.write(color)
            self.stream.write(rendered)
            if color:
                self.stream.write(_RESET)
            self.stream.write("\n")
            self.stream.flush()

    def log(self, event_name: str, **data: Any) -> None:
        """Совместимость со старыми диагностическими вызовами.

        Технические логи не являются частью представления модели и поэтому
        намеренно подавляются.
        """

    def reply(self, text: str, *, agent_id: str = "main") -> None:
        if self.enabled:
            self._write(agent_id, error=False, rendered=text)

    def error(self, message: str) -> None:
        """Напечатать исходный текст ошибки красным, без заголовка."""

        if not self.enabled:
            return
        rendered = str(message).rstrip("\r\n")
        if rendered:
            self._write("main", error=True, rendered=rendered)

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
            content.get("type") in _ERROR_EVENT_TYPES
        )
        self._write(agent_id, error=error, rendered=rendered)

    def input(self, event: Any, capabilities: Any = None, agent_id: str = "main") -> None:
        """Вывести входное событие в протокольном формате type/data."""

        self.message(event.model_value(), agent_id=agent_id)

    def result(self, result: Any, agent_id: str = "main") -> None:
        """Вывести результат действия как событие: type/call_id/data."""

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
