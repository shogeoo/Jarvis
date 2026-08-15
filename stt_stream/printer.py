"""Вывод расшифрованного текста строками.

Каждая расшифрованная фраза печатается с новой строки. Если пауза перед
фразой была не меньше blank_pause секунд (заведомо больше средней речевой
паузы), перед ней выводится пустая строка.
"""

from __future__ import annotations


class Printer:
    def __init__(self, blank_pause: float = 2.0):
        self.blank_pause = blank_pause

    def print_segment(self, text: str, pause_before: float | None):
        text = text.strip()
        if not text:
            return
        if pause_before is not None and pause_before >= self.blank_pause:
            print()
        print(text, flush=True)
