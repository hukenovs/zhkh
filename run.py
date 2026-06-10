#!/usr/bin/env -S uv run python
"""Дефолтный запуск: обходит все сайты, для которых есть креды в ~/.zhkh.keys,
складывает квитанции в ./receipts/<YYYY-MM>/<site>/, скриншоты — в ./debug/.

Запуск (любой из вариантов):
    ./run.py                       # благодаря shebang `uv run python`
    uv run run.py
    .venv/bin/python run.py
    source .venv/bin/activate && python run.py

Флаги:
    --debug          подробные логи + DEBUG-скриншоты
    --headed         видимое окно браузера
    --site moek      один сайт; флаг можно повторить --site mosvodokanal …
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
