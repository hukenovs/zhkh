#!/usr/bin/env -S uv run python
"""Запуск Telegram-бота для сбора квитанций ЖКХ.

Требует секцию `telegram` в ~/.zhkh.keys (token + allowed_chat_ids).

Запуск (любой из вариантов):
    ./run_bot.py                   # благодаря shebang `uv run python`
    uv run run_bot.py
    .venv/bin/python run_bot.py

Флаги:
    --debug                подробные логи
    --keys PATH            другой путь к файлу с кредами
"""

from __future__ import annotations

import sys
from pathlib import Path

# Делает запуск из корня проекта (без `uv pip install -e .`) тоже рабочим.
SRC = Path(__file__).resolve().parent / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zhkh.tgbot import main_bot  # noqa: E402

if __name__ == "__main__":
    main_bot()
