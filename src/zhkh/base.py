from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .config import SiteCreds


@dataclass
class Receipt:
    """Скачанная квитанция."""

    path: Path
    period: str | None = None  # "2026-05" если удалось определить
    amount_rub: float | None = None  # сумма к оплате
    meta: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    site: str
    receipts: list[Receipt] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class BaseParser(ABC):
    """Базовый интерфейс парсера ЛК.

    Поток: login() -> collect() -> возвращаем ParseResult.
    Конкретные реализации могут переопределить run() целиком, если удобнее.
    """

    name: str  # короткий идентификатор сайта, напр. "moek"
    url: str

    def __init__(
        self,
        creds: SiteCreds,
        output_dir: Path,
        debug_dir: Path,
        logger: logging.Logger,
        headless: bool = True,
    ) -> None:
        self.creds = creds
        self.output_dir = output_dir
        self.debug_dir = debug_dir
        self.log = logger
        self.headless = headless

    @abstractmethod
    def run(self) -> ParseResult: ...
