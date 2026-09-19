"""Нейтральная точка входа в приложение."""

from __future__ import annotations


def main() -> int:
    from .cli import main as cli_main

    return cli_main()
