from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .base import ParseResult


@dataclass
class SummaryRow:
    site: str
    label: str  # объект: счёт и/или адрес
    period: str  # "—" если неизвестно
    amount: float | None  # сумма к оплате, ₽
    status: str  # "ok" | "fail" | "empty"
    note: str  # текст ошибки или пусто


def _receipt_label(meta: dict) -> str:
    """Человекочитаемая метка объекта из meta квитанции (см. cli._print_summary)."""
    account = meta.get("account")
    address = meta.get("address")
    if account:
        return f"{account}  ({address or '—'})"
    return address or "—"


def iter_summary_rows(results: list[ParseResult]) -> Iterator[SummaryRow]:
    """Единый источник строк итога — общий для CLI-таблицы и текста бота."""
    for r in results:
        if not r.ok:
            yield SummaryRow(r.site, "—", "—", None, "fail", (r.error or "")[:200])
            continue
        if not r.receipts:
            yield SummaryRow(r.site, "—", "—", None, "empty", "")
            continue
        for rcpt in r.receipts:
            yield SummaryRow(
                r.site,
                _receipt_label(rcpt.meta),
                rcpt.period or "—",
                rcpt.amount_rub,
                "ok",
                "",
            )


def grand_total(results: list[ParseResult]) -> float:
    return sum(
        row.amount for row in iter_summary_rows(results) if row.amount is not None
    )
