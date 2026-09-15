"""Вывод текста в консоль.

Расшифровка печатается на отдельной строке, после неё — пустая строка.
Ответ нейросети печатается с префиксом ``Jarvis:`` и тоже отделяется
пустой строкой.
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
