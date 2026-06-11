from __future__ import annotations

import logging
from datetime import datetime

from .base import ParseResult
from .config import Config
from .log import fail, ok, stage
from .sites import PARSERS
from .storage import debug_dir as mk_debug_dir, receipts_dir


def run_parsers(
    cfg: Config,
    chosen: list[str],
    *,
    logger: logging.Logger,
    headed: bool = False,
    when: datetime | None = None,
) -> list[ParseResult]:
    """Прогоняет выбранные парсеры и возвращает их результаты.

    Общая логика для CLI и Telegram-бота. Синхронная (парсеры на
    sync_playwright) — из async-кода вызывать через asyncio.to_thread.
    """
    run_stamp = when or datetime.now()
    results: list[ParseResult] = []

    for name in chosen:
        ParserCls = PARSERS[name]
        try:
            creds = cfg.for_site(name)
        except KeyError as e:
            fail(logger, name, str(e))
            results.append(ParseResult(site=name, error=str(e)))
            continue

        out_dir = receipts_dir(name, when=run_stamp)
        dbg_dir = mk_debug_dir(name, when=run_stamp)
        stage(logger, name, f"старт ({ParserCls.url})")
        stage(logger, name, f"квитанции → {out_dir}")
        stage(logger, name, f"debug → {dbg_dir}")

        parser = ParserCls(
            creds=creds,
            output_dir=out_dir,
            debug_dir=dbg_dir,
            logger=logger,
            headless=not headed,
        )
        result = parser.run()
        results.append(result)
        if result.ok:
            ok(logger, name, f"скачано квитанций: {len(result.receipts)}")
        else:
            fail(logger, name, result.error or "неизвестная ошибка")

    return results
