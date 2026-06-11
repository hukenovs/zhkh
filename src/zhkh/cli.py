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
@click.option(
    "--summarize",
    is_flag=True,
    help="Разобрать скачанные квитанции через VLM (OpenRouter) и вывести детализацию.",
)
def main(
    sites: tuple[str, ...],
    keys_path: Path | None,
    headed: bool,
    debug: bool,
    summarize: bool,
) -> None:
    """Авто-сбор квитанций ЖКХ."""
    logger = setup_logging(debug=debug)

    try:
        cfg = load_config(keys_path)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(2)

    # Проверяем конфиг VLM ДО сбора, чтобы не гонять браузер минуты впустую.
    if summarize and cfg.openrouter is None:
        logger.error(
            "Для --summarize нужна секция 'openrouter' в ~/.zhkh.keys "
            "(api_key + model). См. .zhkh.keys.example."
        )
        sys.exit(2)

    chosen = list(sites) if sites else sorted(PARSERS.keys())
    logger.info("сайты в работе: %s", ", ".join(chosen))

    results = run_parsers(cfg, chosen, logger=logger, headed=headed)

    _print_summary(results)

    if summarize:
        from .llm import summarize_results

        assert cfg.openrouter is not None
        logger.info("VLM-разбор квитанций через %s …", cfg.openrouter.model)
        summaries = summarize_results(cfg.openrouter, results, logger=logger)
        _print_llm_summary(summaries)


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


def _print_llm_summary(summaries: list) -> None:
    """Печатает детализацию VLM-разбора квитанций."""
    from .llm import ReceiptSummary

    table = Table(title="VLM-разбор квитанций")
    table.add_column("Сайт")
    table.add_column("Поставщик")
    table.add_column("Период")
    table.add_column("Услуги")
    table.add_column("Итого, ₽", justify="right")
    table.add_column("Заметка")

    total = 0.0
    s: ReceiptSummary
    for s in summaries:
        if not s.ok:
            table.add_row(
                s.site, s.path.name, "—", "—", "—", f"[red]{(s.error or '')[:60]}[/red]"
            )
            continue
        if s.total is not None:
            total += s.total
        services = ", ".join(
            f"{it['name']}: {it['amount']:.0f}"
            for it in s.items
            if it.get("amount") is not None
        )
        table.add_row(
            s.site,
            s.provider or "—",
            s.period or "—",
            (services or "—")[:60],
            f"{s.total:.2f}" if s.total is not None else "—",
            (s.note or "")[:40],
        )
    console.print(table)
    console.print(f"[bold]Итого по разобранным квитанциям:[/bold] {total:.2f} ₽")


if __name__ == "__main__":
    main()
