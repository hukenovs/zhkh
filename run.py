#!/usr/bin/env python3
"""Дефолтный запуск: обходит все сайты, для которых есть креды в ~/.zhkh.keys,
складывает квитанции в ./receipts/<YYYY-MM>/<site>/, скриншоты — в ./debug/.

Запуск:
    uv run run.py                  # все сайты, headless, info-логи
    uv run run.py --debug          # подробные логи и больше скриншотов
    uv run run.py --headed         # видимое окно браузера
    uv run run.py --site moek      # один сайт; можно повторять --site
    uv run run.py --site moek --debug --headed   # любой набор флагов
"""

from __future__ import annotations

import sys
from pathlib import Path

# Делает запуск из корня проекта (без `uv pip install -e .`) тоже рабочим.
SRC = Path(__file__).resolve().parent / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zhkh.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
