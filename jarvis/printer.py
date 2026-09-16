"""Вывод текста в консоль.

Используется только в режиме без LLM. В режиме event-action вход и ответ
выводятся отладчиком в точном model-visible JSON-виде.
"""

from __future__ import annotations


class Printer:
    def print_segment(self, text: str):
        text = text.strip()
        if not text:
            return
        print(text, flush=True)
        print(flush=True)

    def print_reply(self, text: str):
        text = text.strip()
        if not text:
            return
        print(f"Jarvis: {text}", flush=True)
        print(flush=True)
