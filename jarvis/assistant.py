"""Диалог с OpenAI-совместимым API.

Ассистент хранит system-промпт и полную историю сообщений (без ручной
обрезки). Транскрипции складываются в очередь; отдельный поток забирает все
накопившиеся реплики, как только освободится, отправляет их одним запросом и
передаёт ответ в ``on_reply``.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Callable

from openai import OpenAI

DEFAULT_SYSTEM = "Ты — Jarvis, голосовой ассистент. Отвечай кратко и по делу."


class Assistant:
    """Обёртка над chat/completions с контекстом диалога в отдельном потоке."""

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        system: str | None = None,
        on_reply: Callable[[str], None] | None = None,
        timeout: float = 60.0,
    ):
        self.model = model
        self.system = system or DEFAULT_SYSTEM
        self.on_reply = on_reply
        self.messages: list[dict] = [{"role": "system", "content": self.system}]
        self.client = OpenAI(
            base_url=base_url or None,
            api_key=api_key or None,
            timeout=timeout,
        )
        self._pending: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "Assistant":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def submit(self, text: str) -> None:
        """Добавить реплику пользователя в очередь на отправку."""
        text = (text or "").strip()
        if text:
            self._pending.put(text)

    def stop(self, timeout: float = 60.0) -> None:
        """Дообработать очередь и остановить поток."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _drain_pending(self) -> list[str]:
        batch = []
        while True:
            try:
                batch.append(self._pending.get_nowait())
            except queue.Empty:
                return batch

    def _run(self) -> None:
        while not (self._stop.is_set() and self._pending.empty()):
            try:
                first = self._pending.get(timeout=0.1)
            except queue.Empty:
                continue
            self._ask([first, *self._drain_pending()])

    def _ask(self, texts: list[str]) -> None:
        self.messages.extend({"role": "user", "content": text} for text in texts)
        try:
            response = self.client.chat.completions.create(
                model=self.model, messages=self.messages
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"Ошибка запроса к нейросети: {exc}",
                file=os.sys.stderr, flush=True,
            )
            return
        answer = (response.choices[0].message.content or "").strip()
        self.messages.append({"role": "assistant", "content": answer})
        if answer and self.on_reply is not None:
            self.on_reply(answer)
