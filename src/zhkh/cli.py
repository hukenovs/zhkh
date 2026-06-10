from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.table import Table

from .base import ParseResult
from .config import load_config
from .log import console, fail, ok, setup_logging, stage
from .sites import PARSERS


@click.command()
@click.option(
    "--site",
    "sites",
    multiple=True,
    type=click.Choice(sorted(PARSERS.keys())),
    help="Какие сайты обходить. Можно указать несколько раз. По умолчанию — все доступные.",
)
@click.option(
    "--keys",
    "keys_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Путь к YAML с кредами (по умолчанию ~/.zhkh.keys).",
)
@click.option(
    "--headed/--headless",
    default=False,
    help="Запустить браузер с видимым окном (по умолчанию headless).",
)
@click.option("--debug", is_flag=True, help="Подробный лог + DEBUG-screenshots.")
def main(sites: tuple[str, ...], keys_path: Path | None, headed: bool, debug: bool) -> None:
    """Авто-сбор квитанций ЖКХ."""
    logger = setup_logging(debug=debug)

    try:
        cfg = load_config(keys_path)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(2)

    chosen = list(sites) if sites else sorted(PARSERS.keys())
    logger.info("сайты в работе: %s", ", ".join(chosen))

    from datetime import datetime

    from .storage import debug_dir as mk_debug_dir, receipts_dir

    run_stamp = datetime.now()
    results: list[ParseResult] = []

    for name in chosen:
        ParserCls = PARSERS[name]
        try:
            creds = cfg.for_site(name)
        except KeyError as e:
            fail(logger, name, str(e))
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

    _print_summary(results)


def _print_summary(results: list[ParseResult]) -> None:
    table = Table(title="Итог")
    table.add_column("Сайт")
    table.add_column("Объект")
    table.add_column("Период")
    table.add_column("Сумма, ₽", justify="right")
    table.add_column("Статус")
    table.add_column("Заметка")

    grand_total = 0.0
    for r in results:
        if not r.ok:
            table.add_row(
                r.site, "—", "—", "—",
                "[red]fail[/red]", (r.error or "")[:80],
            )
            continue
        if not r.receipts:
            table.add_row(r.site, "—", "—", "—", "[yellow]пусто[/yellow]", "")
            continue
        for rcpt in r.receipts:
            label = rcpt.meta.get("address") or rcpt.meta.get("account") or "—"
            if rcpt.meta.get("account"):
                label = f"{rcpt.meta['account']}  ({rcpt.meta.get('address') or '—'})"
            amount = rcpt.amount_rub
            if amount is not None:
                grand_total += amount
            table.add_row(
                r.site,
                label,
                rcpt.period or "—",
                f"{amount:.2f}" if amount is not None else "—",
                "[green]ok[/green]",
                "",
            )
    console.print(table)
    console.print(f"[bold]Итого по всем объектам:[/bold] {grand_total:.2f} ₽")


if __name__ == "__main__":
    main()
