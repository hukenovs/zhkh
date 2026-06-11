from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_KEYS_PATH = Path.home() / ".zhkh.keys"


@dataclass
class SiteCreds:
    login: str
    password: str


@dataclass
class OpenRouterCfg:
    api_key: str
    model: str = "qwen/qwen2.5-vl-72b-instruct:free"
    # Движок разбора PDF в OpenRouter: "pdf-text" (бесплатно, извлечение текста
    # из цифровых PDF — годится для квитанций ЖКХ), "mistral-ocr" (платно, для
    # сканов), "native" (отдать PDF модели как есть, если она это умеет).
    pdf_engine: str = "pdf-text"


@dataclass
class TelegramCfg:
    token: str
    allowed_chat_ids: frozenset[int]


@dataclass
class Config:
    sites: dict[str, SiteCreds]
    openrouter: OpenRouterCfg | None = None
    telegram: TelegramCfg | None = None

    def for_site(self, name: str) -> SiteCreds:
        if name not in self.sites:
            raise KeyError(
                f"В {DEFAULT_KEYS_PATH} нет секции '{name}'. "
                f"См. .zhkh.keys.example."
            )
        return self.sites[name]


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_KEYS_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден {path}. Скопируй .zhkh.keys.example в {path} и заполни."
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    sites: dict[str, SiteCreds] = {}
    openrouter: OpenRouterCfg | None = None
    telegram: TelegramCfg | None = None
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        if key == "openrouter":
            openrouter = OpenRouterCfg(
                api_key=value["api_key"],
                model=value.get("model", OpenRouterCfg.model),
                pdf_engine=value.get("pdf_engine", OpenRouterCfg.pdf_engine),
            )
            continue
        if key == "telegram":
            telegram = TelegramCfg(
                token=value["token"],
                allowed_chat_ids=frozenset(
                    int(x) for x in value.get("allowed_chat_ids", [])
                ),
            )
            continue
        if "login" in value and "password" in value:
            sites[key] = SiteCreds(login=value["login"], password=value["password"])

    return Config(sites=sites, openrouter=openrouter, telegram=telegram)
