from __future__ import annotations

import base64
import json
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .base import ParseResult
from .config import OpenRouterCfg

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Заголовки рекомендованы OpenRouter для атрибуции (необязательные).
_REFERER = "https://github.com/hukenovs/zhkh"
_TITLE = "zhkh"

# Системный промпт лежит рядом в prompts.txt — правится без касания кода.
_PROMPT_PATH = Path(__file__).with_name("prompts.txt")


def load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


@dataclass
class ReceiptSummary:
    """Результат VLM-разбора одной квитанции."""

    site: str
    path: Path
    provider: str | None = None
    period: str | None = None
    total: float | None = None
    items: list[dict] = field(default_factory=list)
    note: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _data_url(path: Path) -> tuple[str, str]:
    """Возвращает (mime, data-url base64) для файла."""
    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or "application/octet-stream"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return mime, f"data:{mime};base64,{b64}"


def _build_content(path: Path) -> list[dict]:
    """Строит content-массив сообщения с вложенной квитанцией.

    PDF отдаём модели как есть (тип "file", без file-parser плагина — OpenRouter
    передаёт файл прямо в VLM, которая сама его читает). Картинки — через image_url.
    """
    mime, data_url = _data_url(path)
    text_part = {"type": "text", "text": "Разбери эту квитанцию ЖКХ."}
    if mime == "application/pdf" or path.suffix.lower() == ".pdf":
        return [
            text_part,
            {"type": "file", "file": {"filename": path.name, "file_data": data_url}},
        ]
    return [text_part, {"type": "image_url", "image_url": {"url": data_url}}]


def _extract_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели (на случай обёртки в ```)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _as_float(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None


class OpenRouterClient:
    """Минимальный клиент OpenRouter chat completions для VLM-разбора."""

    def __init__(self, cfg: OpenRouterCfg, *, timeout: float = 120.0) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self.prompt = load_prompt()

    def summarize_receipt(self, site: str, path: Path) -> ReceiptSummary:
        result = ReceiptSummary(site=site, path=path)
        if not path.exists():
            result.error = "файл не найден"
            return result

        content = _build_content(path)
        payload: dict = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": self.prompt},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
        }

        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": _REFERER,
            "X-Title": _TITLE,
        }

        try:
            resp = httpx.post(
                OPENROUTER_URL, headers=headers, json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300] if e.response is not None else ""
            result.error = f"HTTP {e.response.status_code}: {body}"
            return result
        except Exception as e:  # noqa: BLE001
            result.error = f"{type(e).__name__}: {e}"
            return result

        try:
            parsed = _extract_json(text)
        except Exception:  # noqa: BLE001
            result.error = f"не удалось распарсить ответ модели: {text[:200]}"
            return result

        result.provider = parsed.get("provider")
        result.period = parsed.get("period")
        result.total = _as_float(parsed.get("total"))
        result.note = parsed.get("note")
        items = parsed.get("items")
        if isinstance(items, list):
            result.items = [
                {"name": str(it.get("name", "")), "amount": _as_float(it.get("amount"))}
                for it in items
                if isinstance(it, dict)
            ]
        return result


def summarize_results(
    cfg: OpenRouterCfg,
    results: list[ParseResult],
    *,
    logger: logging.Logger,
) -> list[ReceiptSummary]:
    """Прогоняет VLM по всем скачанным квитанциям из результатов парсинга.

    Синхронная (httpx.post) — из async-кода вызывать через asyncio.to_thread.
    """
    client = OpenRouterClient(cfg)
    summaries: list[ReceiptSummary] = []
    for r in results:
        for rcpt in r.receipts:
            logger.info("[llm] разбираю %s: %s", r.site, Path(rcpt.path).name)
            summary = client.summarize_receipt(r.site, Path(rcpt.path))
            if not summary.ok:
                logger.warning("[llm] %s: %s", r.site, summary.error)
            summaries.append(summary)
    return summaries
