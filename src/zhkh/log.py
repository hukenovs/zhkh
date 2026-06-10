from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(debug: bool = False, log_file: Path | None = None) -> logging.Logger:
    level = logging.DEBUG if debug else logging.INFO
    handlers: list[logging.Handler] = [
        RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=True,
        )
    ]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        handlers.append(fh)

    logging.basicConfig(level=level, format="%(message)s", handlers=handlers, force=True)

    # Заглушим шумные либы.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("zhkh")


def stage(logger: logging.Logger, site: str, message: str) -> None:
    logger.info(f"[bold cyan]\\[{site}][/bold cyan] {message}")


def ok(logger: logging.Logger, site: str, message: str) -> None:
    logger.info(f"[bold green]\\[{site}][/bold green] ✓ {message}")


def fail(logger: logging.Logger, site: str, message: str) -> None:
    logger.error(f"[bold red]\\[{site}][/bold red] ✗ {message}")
