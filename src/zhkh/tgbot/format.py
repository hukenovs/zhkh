from __future__ import annotations

import html

from ..base import ParseResult
from ..summary import grand_total, iter_summary_rows

# Telegram режет сообщения на 4096 символов; берём с запасом.
TELEGRAM_LIMIT = 3500


def _fmt_amount(amount: float | None) -> str:
    return f"{amount:,.2f} ₽".replace(",", " ") if amount is not None else "—"


def build_text_summary(results: list[ParseResult]) -> list[str]:
    """Текст саммари для Telegram (parse_mode=HTML).

    Возвращает список сообщений: длинный итог режется по границам сайтов,
    чтобы не упереться в лимит Telegram.
    """
    if not results:
        return ["Нет данных: ни один сайт не был обработан."]

    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append("\n".join(buf))
            buf = []
            size = 0

    for r in results:
        rows = [row for row in iter_summary_rows(results) if row.site == r.site]
        block_lines = [f"<b>{html.escape(r.site)}</b>"]
        if not r.ok:
            block_lines.append(f"  ✗ ошибка: {html.escape((r.error or '')[:200])}")
        elif not r.receipts:
            block_lines.append("  — квитанций нет")
        else:
            for row in rows:
                period = "" if row.period == "—" else f" — {row.period}"
                block_lines.append(
                    f"  {html.escape(row.label)}: "
                    f"{_fmt_amount(row.amount)}{html.escape(period)}"
                )
        block = "\n".join(block_lines)

        if size + len(block) > TELEGRAM_LIMIT:
            flush()
        buf.append(block)
        size += len(block) + 1

    flush()

    total = grand_total(results)
    footer = f"\n<b>Итого по всем объектам: {_fmt_amount(total)}</b>"
    if chunks and len(chunks[-1]) + len(footer) <= TELEGRAM_LIMIT:
        chunks[-1] += footer
    else:
        chunks.append(footer.strip())
    return chunks
