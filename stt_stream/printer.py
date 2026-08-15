"""Вывод расшифрованного текста.

Каждая расшифровка печатается на отдельной строке, после неё — пустая
строка (между расшифровками всегда пустая строка).
"""

from __future__ import annotations


class Printer:
    def print_segment(self, text: str):
        text = text.strip()
        if not text:
            return
        print(text, flush=True)
        print(flush=True)
