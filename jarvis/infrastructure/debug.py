"""Вывод сообщений, которые добавляются в контекст модели."""

from __future__ import annotations

import json
import sys
import threading
from typing import Any


_MAIN_COLOR = "\033[32m"  # зелёный: всё, что связано с main
_SUB_COLOR = "\033[38;5;208m"  # оранжевый: субагенты и их handlers
_RESET = "\033[0m"


class Debugger:
    """Печатает только протокольный JSON event-action формата.

    Вход: ровно {"type":..., "data":...} как его задаёт модуль.
    Результат: ровно {"type":"action_result", "action_id":..., "data":...}.
    Выход: сырой content assistant message как его вернула модель.
    OpenAI-обёртка (списки text/image_url, data URL с base64) и
    переформатирование JSON намеренно не выводятся: base64 текстом
    в лог попадать не должен.

    Всё, что связано с main, печатается зелёным; всё, что печатают
    субагенты (включая их handlers и action_result), — оранжевым и
    отделяется пустой строкой с обеих сторон. Ответ модели с массивом
    actions всегда отбивается пустыми строками; ивенты и action_result
    пишутся без пустых строк. Цвет включается только на терминале.

    Внутренние события доставки, состояния агентов и этапы действий намеренно
    не выводятся: они дублируют сообщения, которые модель уже увидит через
    event-action протокол.
    """

    def __init__(self, *, enabled: bool = True, stream=None):
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()

    def _color(self, agent_id: str) -> str:
        detect = getattr(self.stream, "isatty", None)
        if detect is not None and not detect():
            return ""
        return _MAIN_COLOR if agent_id == "main" else _SUB_COLOR

    def _write(self, agent_id: str, kind: str, rendered: str) -> None:
        blank = kind == "model" or agent_id != "main"
        color = self._color(agent_id)
        with self._lock:
            if blank:
                self.stream.write("\n")
            if color:
                self.stream.write(color)
            self.stream.write(rendered)
            self.stream.write("\n")
            if color:
                self.stream.write(_RESET)
            if blank:
                self.stream.write("\n")
            self.stream.flush()

    def log(self, kind: str, **data: Any) -> None:
        """Совместимость со старыми диагностическими вызовами.

        Технические логи не являются частью представления модели и поэтому
        намеренно подавляются.
        """

    def message(
        self, content: Any, *, agent_id: str = "main", kind: str = "event"
    ) -> None:
        """Вывести протокольный JSON с форматированием, без OpenAI-обёртки."""

        if not self.enabled:
            return
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                pass
        rendered = (
            json.dumps(content, ensure_ascii=False, indent=2)
            if not isinstance(content, str)
            else content
        )
        self._write(agent_id, kind, rendered)

    def input(self, event: Any, capabilities: Any = None, agent_id: str = "main") -> None:
        """Вывести входное событие в протокольном формате type/data."""

        self.message(event.model_value(), agent_id=agent_id, kind="event")

    def result(self, result: Any, agent_id: str = "main") -> None:
        """Вывести результат действия как событие: type/action_id/data."""

        self.message(result.model_value(), agent_id=agent_id, kind="result")

    def event(self, direction: str, agent_id: str, event: Any) -> None:
        """Старый API: доставка события не является выводом для модели."""

    def action(self, stage: str, agent_id: str, action: Any, **extra: Any) -> None:
        """Старый API: действие уже присутствует в ответе модели."""

    def model(self, agent_id: str, output: str, *, actions: Any = None) -> None:
        """Вывести сырой content assistant message как есть."""

        self.message(output, agent_id=agent_id, kind="model")

    def state(self, agent_id: str, state: str, **extra: Any) -> None:
        """Старый API: внутреннее состояние не выводится в model trace."""
