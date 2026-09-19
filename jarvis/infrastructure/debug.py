"""Вывод сообщений, которые добавляются в контекст модели."""

from __future__ import annotations

import json
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

    def message(self, content: Any) -> None:
        """Вывести содержимое сообщения как читаемую JSON-структуру."""

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
        with self._lock:
            self.stream.write(rendered)
            self.stream.write("\n")
            self.stream.flush()

    def input(self, event: Any, capabilities: Any = None) -> None:
        """Вывести model-visible content входного сообщения."""

        self.message(event.model_visible_content(capabilities))

    def event(self, direction: str, agent_id: str, event: Any) -> None:
        """Старый API: доставка события не является выводом для модели."""

    def action(self, stage: str, agent_id: str, action: Any, **extra: Any) -> None:
        """Старый API: действие уже присутствует в ответе модели."""

    def model(self, agent_id: str, output: str, *, actions: Any = None) -> None:
        """Вывести точный content assistant message."""

        self.message(output)

    def state(self, agent_id: str, state: str, **extra: Any) -> None:
        """Старый API: внутреннее состояние не выводится в model trace."""
