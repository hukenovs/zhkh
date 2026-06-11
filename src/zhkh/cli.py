from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.table import Table

from .base import ParseResult
from .config import load_config
from .log import console, setup_logging
from .runner import run_parsers
from .sites import PARSERS
from .summary import grand_total, iter_summary_rows


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

    results = run_parsers(cfg, chosen, logger=logger, headed=headed)

    _print_summary(results)


_STATUS_MARKUP = {
    "ok": "[green]ok[/green]",
    "fail": "[red]fail[/red]",
    "empty": "[yellow]пусто[/yellow]",
}


def _print_summary(results: list[ParseResult]) -> None:
    table = Table(title="Итог")
    table.add_column("Сайт")
    table.add_column("Объект")
    table.add_column("Период")
    table.add_column("Сумма, ₽", justify="right")
    table.add_column("Статус")
    table.add_column("Заметка")

    for row in iter_summary_rows(results):
        table.add_row(
            row.site,
            row.label,
            row.period,
            f"{row.amount:.2f}" if row.amount is not None else "—",
            _STATUS_MARKUP.get(row.status, row.status),
            row.note[:80],
        )
    console.print(table)
    console.print(
        f"[bold]Итого по всем объектам:[/bold] {grand_total(results):.2f} ₽"
    )


if __name__ == "__main__":
    main()
