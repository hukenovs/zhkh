from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from ..config import Config, load_config
from ..log import setup_logging
from ..runner import run_parsers
from ..sites import PARSERS
from .format import build_llm_summary, build_text_summary

REQUEST_RECEIPTS = "request_receipts"
SUMMARIZE = "summarize"

HELP_TEXT = (
    "<b>ЖКХ-бот</b> — сбор квитанций из личных кабинетов.\n\n"
    "Команды:\n"
    "/start — приветствие и кнопка запроса\n"
    "/help — эта справка\n\n"
    "Кнопка <b>«Запросить квитанции»</b> — бот зайдёт во все настроенные "
    "личные кабинеты, скачает свежие квитанции, пришлёт текстовое саммари "
    "по суммам к оплате и сами файлы квитанций.\n\n"
    "Кнопка <b>«Суммаризировать»</b> — разобрать скачанные квитанции через "
    "VLM-модель (OpenRouter): поставщик, период, разбивка по услугам и итог. "
    "Использует последний сбор; если его не было — сперва соберёт.\n\n"
    "Файлы также сохраняются локально в папку <code>./receipts/</code>.\n\n"
    "Сбор занимает несколько минут — наберитесь терпения. "
    "Одновременно выполняется только один запрос."
)


def _keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Запросить квитанции", callback_data=REQUEST_RECEIPTS)]]
    # Кнопка разбора — только если настроен OpenRouter.
    cfg: Config = context.application.bot_data["cfg"]
    if cfg.openrouter is not None:
        rows.append([InlineKeyboardButton("Суммаризировать", callback_data=SUMMARIZE)])
    return InlineKeyboardMarkup(rows)


def _allowed_ids(context: ContextTypes.DEFAULT_TYPE) -> frozenset[int]:
    return context.application.bot_data["allowed_chat_ids"]


def _is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id in _allowed_ids(context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я собираю квитанции ЖКХ из личных кабинетов.\n"
        "Нажмите кнопку ниже или отправьте /help.",
        reply_markup=_keyboard(context),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP_TEXT, parse_mode="HTML", reply_markup=_keyboard(context)
    )


async def on_request_receipts(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    # У CallbackQueryHandler нет фильтра по чату — проверяем доступ вручную.
    if not _is_allowed(update, context):
        if query is not None:
            await query.answer("Нет доступа.", show_alert=True)
        return

    await query.answer()
    await _do_run_and_send(update, context)


async def _collect(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> list | None:
    """Прогоняет парсеры под локом, кладёт результат в chat_data['last_results'].

    Возвращает results, либо None если запуск не состоялся (занято/нет сайтов/ошибка).
    """
    cfg: Config = context.application.bot_data["cfg"]
    logger: logging.Logger = context.application.bot_data["logger"]
    lock: asyncio.Lock = context.application.bot_data["run_lock"]
    chat_id = update.effective_chat.id

    if lock.locked():
        await context.bot.send_message(
            chat_id, "Уже собираю квитанции — подождите, пожалуйста."
        )
        return None

    async with lock:
        chosen = [name for name in sorted(PARSERS.keys()) if name in cfg.sites]
        if not chosen:
            await context.bot.send_message(
                chat_id,
                "В конфиге нет ни одного настроенного сайта ЖКХ "
                "(секции с login/password в ~/.zhkh.keys).",
            )
            return None

        await context.bot.send_message(
            chat_id,
            "Собираю квитанции: " + ", ".join(chosen) + ". Это займёт несколько минут…",
        )
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        try:
            results = await asyncio.to_thread(
                run_parsers, cfg, chosen, logger=logger
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("bot: прогон парсеров упал")
            await context.bot.send_message(
                chat_id, f"Не удалось собрать квитанции: {type(e).__name__}: {e}"
            )
            return None

        context.chat_data["last_results"] = results
        return results


async def _do_run_and_send(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger: logging.Logger = context.application.bot_data["logger"]
    chat_id = update.effective_chat.id

    results = await _collect(update, context)
    if results is None:
        return

    for chunk in build_text_summary(results):
        await context.bot.send_message(chat_id, chunk, parse_mode="HTML")

    await _send_files(context, chat_id, results, logger)


async def on_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update, context):
        if query is not None:
            await query.answer("Нет доступа.", show_alert=True)
        return
    await query.answer()
    await _do_summarize(update, context)


async def _do_summarize(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from ..llm import summarize_results

    cfg: Config = context.application.bot_data["cfg"]
    logger: logging.Logger = context.application.bot_data["logger"]
    chat_id = update.effective_chat.id

    if cfg.openrouter is None:
        await context.bot.send_message(
            chat_id,
            "VLM-разбор недоступен: в ~/.zhkh.keys нет секции 'openrouter'.",
        )
        return

    # Используем последний сбор; если его не было — собираем сейчас.
    results = context.chat_data.get("last_results")
    if not results:
        await context.bot.send_message(
            chat_id, "Свежих квитанций нет — сначала соберу их."
        )
        results = await _collect(update, context)
        if results is None:
            return

    has_files = any(r.receipts for r in results)
    if not has_files:
        await context.bot.send_message(chat_id, "Нет скачанных квитанций для разбора.")
        return

    await context.bot.send_message(
        chat_id, f"Разбираю квитанции через {cfg.openrouter.model} …"
    )
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    try:
        summaries = await asyncio.to_thread(
            summarize_results, cfg.openrouter, results, logger=logger
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("bot: VLM-разбор упал")
        await context.bot.send_message(
            chat_id, f"Не удалось разобрать квитанции: {type(e).__name__}: {e}"
        )
        return

    for chunk in build_llm_summary(summaries):
        await context.bot.send_message(chat_id, chunk, parse_mode="HTML")


async def _send_files(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    results: list,
    logger: logging.Logger,
) -> None:
    sent = 0
    for r in results:
        for rcpt in r.receipts:
            path = Path(rcpt.path)
            if not path.exists():
                logger.warning("bot: файл квитанции пропал: %s", path)
                continue
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
            try:
                with path.open("rb") as fh:
                    await context.bot.send_document(
                        chat_id, document=fh, filename=path.name
                    )
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("bot: не отправил %s: %s", path, e)
                await context.bot.send_message(
                    chat_id, f"⚠️ Не удалось отправить файл {path.name}: {e}"
                )
    if sent == 0:
        await context.bot.send_message(chat_id, "Файлов квитанций нет.")


def build_application(cfg: Config, logger: logging.Logger) -> Application:
    assert cfg.telegram is not None
    allowed = cfg.telegram.allowed_chat_ids
    app = Application.builder().token(cfg.telegram.token).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["logger"] = logger
    app.bot_data["allowed_chat_ids"] = allowed
    app.bot_data["run_lock"] = asyncio.Lock()

    chat_filter = filters.Chat(chat_id=list(allowed))
    app.add_handler(CommandHandler("start", cmd_start, filters=chat_filter))
    app.add_handler(CommandHandler("help", cmd_help, filters=chat_filter))
    app.add_handler(
        CallbackQueryHandler(on_request_receipts, pattern=f"^{REQUEST_RECEIPTS}$")
    )
    app.add_handler(CallbackQueryHandler(on_summarize, pattern=f"^{SUMMARIZE}$"))
    return app


def run_bot(keys_path: Path | None = None, debug: bool = False) -> None:
    logger = setup_logging(debug=debug)
    cfg = load_config(keys_path)
    if cfg.telegram is None:
        raise SystemExit(
            "В ~/.zhkh.keys нет секции 'telegram' (token + allowed_chat_ids). "
            "См. .zhkh.keys.example."
        )
    if not cfg.telegram.allowed_chat_ids:
        logger.warning(
            "telegram.allowed_chat_ids пуст — бот не ответит никому. "
            "Добавьте свой chat_id."
        )
    app = build_application(cfg, logger)
    logger.info("бот запущен, жду сообщений (Ctrl+C для остановки)")
    app.run_polling()


@click.command()
@click.option(
    "--keys",
    "keys_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Путь к YAML с кредами (по умолчанию ~/.zhkh.keys).",
)
@click.option("--debug", is_flag=True, help="Подробный лог.")
def main_bot(keys_path: Path | None, debug: bool) -> None:
    """Telegram-бот для сбора квитанций ЖКХ."""
    try:
        run_bot(keys_path=keys_path, debug=debug)
    except FileNotFoundError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main_bot()
