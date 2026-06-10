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


@dataclass
class Config:
    sites: dict[str, SiteCreds]
    openrouter: OpenRouterCfg | None = None

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
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        if key == "openrouter":
            openrouter = OpenRouterCfg(
                api_key=value["api_key"],
                model=value.get("model", OpenRouterCfg.model),
            )
            continue
        if "login" in value and "password" in value:
            sites[key] = SiteCreds(login=value["login"], password=value["password"])

    return Config(sites=sites, openrouter=openrouter)
