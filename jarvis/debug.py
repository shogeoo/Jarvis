"""Вывод сообщений, которые добавляются в контекст модели."""

from __future__ import annotations

import sys
import threading
from typing import Any


class Debugger:
    """Печатает только точное содержимое model-visible сообщений.

    Внутренние события доставки, состояния агентов и этапы действий намеренно
    не выводятся: они дублируют сообщения, которые модель уже увидит через
    event-action протокол.
    """

    def __init__(self, *, enabled: bool = True, stream=None):
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()

    def log(self, kind: str, **data: Any) -> None:
        """Совместимость со старыми диагностическими вызовами.

        Технические логи не являются частью представления модели и поэтому
        намеренно подавляются.
        """

    def message(self, content: str) -> None:
        """Вывести содержимое одного сообщения без изменений."""

        if not self.enabled:
            return
        with self._lock:
            self.stream.write(content)
            self.stream.write("\n")
            self.stream.flush()

    def input(self, event: Any) -> None:
        """Вывести ровно JSON события, добавляемого как user message."""

        self.message(event.model_content())

    def event(self, direction: str, agent_id: str, event: Any) -> None:
        """Старый API: доставка события не является выводом для модели."""

    def action(self, stage: str, agent_id: str, action: Any, **extra: Any) -> None:
        """Старый API: действие уже присутствует в ответе модели."""

    def model(self, agent_id: str, output: str, *, actions: Any = None) -> None:
        """Вывести точный content assistant message."""

        self.message(output)

    def state(self, agent_id: str, state: str, **extra: Any) -> None:
        """Старый API: внутреннее состояние не выводится в model trace."""
