from __future__ import annotations

import re
from datetime import datetime

from playwright.sync_api import (
    Download,
    Locator,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from ..base import BaseParser, ParseResult, Receipt


def _parse_rub(raw: str) -> float | None:
    s = raw.strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "," in s and "." in s:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


LOGIN_URL = "https://elk.moek.ru/fl/login"
LANDING_URL = "https://elk.moek.ru/fl/"
PAYMENTS_URL = "https://elk.moek.ru/fl/payments"


class MoekParser(BaseParser):
    """Парсер личного кабинета МОЭК (теплоснабжение).

    elk.moek.ru/fl/ — обычный SPA на Angular/React, форма логина
    принимает телефон/email + пароль. Точные селекторы определяем
    эвристически (label/placeholder/type), скриншоты на каждой стадии
    лежат в debug_dir.
    """

    name = "moek"
    url = LANDING_URL

    def run(self) -> ParseResult:
        result = ParseResult(site=self.name)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            ctx = browser.new_context(
                accept_downloads=True,
                locale="ru-RU",
                viewport={"width": 1400, "height": 900},
            )
            page = ctx.new_page()
            try:
                self._login(page)
                receipts = self._collect(page)
                result.receipts = receipts
            except Exception as e:  # noqa: BLE001
                self._snapshot(page, "error")
                result.error = f"{type(e).__name__}: {e}"
                self.log.exception("MOEK: упало на стадии парсинга")
            finally:
                ctx.close()
                browser.close()
        return result

    # ---------- стадии ----------

    def _login(self, page: Page) -> None:
        self.log.info("[moek] открываю %s", LOGIN_URL)
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        self._snapshot(page, "01_landing")

        login_input = self._find_login_input(page)
        password_input = self._find_password_input(page)

        self.log.info("[moek] ввожу логин/пароль")
        login_input.fill(self.creds.login)
        password_input.fill(self.creds.password)
        self._snapshot(page, "02_filled")

        submit = self._find_submit(page)
        self.log.info("[moek] жму вход")
        submit.click()

        # MOEK не делает full-navigation: после успешного логина страница
        # остаётся на /fl/login и SPA дорисовывает кабинет. Поэтому ждём
        # появления любого «кабинетного» признака, параллельно отслеживая
        # сообщение об ошибке.
        cabinet_marker = page.get_by_role(
            "link", name=re.compile(r"^(Лицевые счета|Оплата|Главная|Показания)$")
        ).first
        try:
            cabinet_marker.wait_for(state="visible", timeout=20_000)
        except PWTimeout:
            self._snapshot(page, "03_after_login")
            err = self._read_login_error(page)
            raise RuntimeError(
                f"Логин не прошёл, url={page.url}"
                + (f", сообщение: {err}" if err else "")
            )
        # Дадим SPA дорисоваться.
        page.wait_for_load_state("networkidle", timeout=10_000)
        self._snapshot(page, "03_after_login")
        self.log.info("[moek] логин ок, url=%s", page.url)

    def _collect(self, page: Page) -> list[Receipt]:
        """Идём на /fl/payments, по каждой карточке (ЛС) дёргаем «Скачать»."""
        self.log.info("[moek] открываю %s", PAYMENTS_URL)
        page.goto(PAYMENTS_URL, wait_until="networkidle", timeout=30_000)
        if "/login" in page.url:
            raise RuntimeError(
                f"после перехода на {PAYMENTS_URL} нас выкинуло на {page.url} — сессия не сохранилась"
            )
        # «Скачать» в MOEK — это <span class="payments__account-block-details-link">,
        # не <a>/<button>, поэтому идём по классу/тексту.
        download_links = page.locator(
            ".payments__account-block-details-link",
            has_text=re.compile(r"^\s*Скачать\s*$"),
        )
        try:
            download_links.first.wait_for(state="visible", timeout=10_000)
        except PWTimeout:
            pass
        self._snapshot(page, "04_payments_page")

        n = download_links.count()
        self.log.info("[moek] найдено ссылок «Скачать»: %d", n)
        if n == 0:
            raise RuntimeError(
                "на /fl/payments не найдено ни одной кнопки «Скачать» — "
                "возможно квитанций пока нет или вёрстка изменилась "
                "(см. 04_payments_page.png/html в debug-папке)"
            )

        downloads: list[Receipt] = []
        for i in range(n):
            link = download_links.nth(i)
            meta = self._read_card_meta(link)
            label = meta.get("account") or f"#{i + 1}"
            self.log.info(
                "[moek] карточка %s: к оплате=%s ₽, адрес=%s",
                label,
                meta.get("amount_rub"),
                meta.get("address") or "—",
            )
            rcpt = self._try_download(page, link, index=i + 1)
            if rcpt is None:
                self.log.warning("[moek] карточка %s: скачать не удалось", label)
                continue
            rcpt.amount_rub = meta.get("amount_rub")
            rcpt.meta.update(meta)
            downloads.append(rcpt)
        return downloads

    def _read_card_meta(self, download_link: Locator) -> dict:
        """Поднимаемся к ближайшему контейнеру карточки и парсим её текст."""
        try:
            card = download_link.locator(
                "xpath=ancestor::*[.//text()[contains(., 'ЛС №')]][1]"
            ).first
            text = card.inner_text(timeout=2000)
        except Exception:  # noqa: BLE001
            return {}
        meta: dict = {"card_text": text}
        if m := re.search(r"ЛС\s*№\s*(\d+)", text):
            meta["account"] = m.group(1)
        if m := re.search(r"К\s*оплате[^\d\-+]*([\-+]?[\d\s .,]+)\s*₽", text):
            meta["amount_rub"] = _parse_rub(m.group(1))
        # Адрес — строка между ЛС и «К оплате», обрезаем по переводу строки.
        if m := re.search(r"ЛС\s*№\s*\d+\s*\n([^\n]+)", text):
            meta["address"] = m.group(1).strip()
        return meta

    # ---------- эвристики поиска ----------

    def _find_login_input(self, page: Page) -> Locator:
        # Сначала пробуем по label/placeholder с типичными словами.
        for label in (
            "Логин",
            "Email",
            "E-mail",
            "Телефон",
            "Номер телефона",
            "Электронная почта",
        ):
            try:
                loc = page.get_by_label(label, exact=False)
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:  # noqa: BLE001
                pass
            try:
                loc = page.get_by_placeholder(label)
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:  # noqa: BLE001
                pass
        # Fallback по типу инпута.
        for selector in (
            "input[type='email']",
            "input[name*='login' i]",
            "input[name*='email' i]",
            "input[name*='phone' i]",
            "input[autocomplete='username']",
            "form input:not([type='password']):not([type='hidden']):not([type='submit'])",
        ):
            loc = page.locator(selector)
            if loc.count() and loc.first.is_visible():
                return loc.first
        raise RuntimeError("не нашёл поле логина")

    def _find_password_input(self, page: Page) -> Locator:
        loc = page.locator("input[type='password']")
        if loc.count() and loc.first.is_visible():
            return loc.first
        raise RuntimeError("не нашёл поле пароля")

    def _find_submit(self, page: Page) -> Locator:
        for selector in (
            "button[type='submit']",
            "form button:has-text('Войти')",
            "button:has-text('Войти')",
            "button:has-text('Вход')",
            "input[type='submit']",
        ):
            loc = page.locator(selector)
            if loc.count() and loc.first.is_visible():
                return loc.first
        raise RuntimeError("не нашёл кнопку входа")

    def _read_login_error(self, page: Page) -> str | None:
        for selector in (
            "[class*='error']",
            "[class*='Error']",
            "[role='alert']",
            ".mat-error",
        ):
            loc = page.locator(selector)
            if loc.count():
                try:
                    text = loc.first.inner_text(timeout=1000).strip()
                    if text:
                        return text
                except Exception:  # noqa: BLE001
                    continue
        return None

    def _find_download_targets(self, page: Page) -> list[Locator]:
        targets: list[Locator] = []
        for text in (
            "Скачать квитанцию",
            "Скачать PDF",
            "Скачать",
            "Распечатать квитанцию",
            "Печать",
            "PDF",
        ):
            loc = page.get_by_role("button", name=re.compile(text, re.IGNORECASE))
            for i in range(loc.count()):
                targets.append(loc.nth(i))
            loc = page.get_by_role("link", name=re.compile(text, re.IGNORECASE))
            for i in range(loc.count()):
                targets.append(loc.nth(i))
        # Иконки/ссылки на PDF.
        pdf_links = page.locator("a[href$='.pdf'], a[href*='.pdf?']")
        for i in range(pdf_links.count()):
            targets.append(pdf_links.nth(i))
        return targets

    def _try_download(self, page: Page, target: Locator, index: int) -> Receipt | None:
        try:
            with page.expect_download(timeout=20_000) as dl_info:
                target.click()
            dl: Download = dl_info.value
        except PWTimeout:
            # Возможно открылась новая вкладка с PDF — попробуем поймать popup.
            self.log.debug("[moek] кандидат #%d: download не сработал", index)
            return None

        suggested = dl.suggested_filename or f"moek_{index}.pdf"
        out = self.output_dir / self._stamp_filename(suggested)
        dl.save_as(out)
        self.log.info("[moek] скачано: %s", out)
        return Receipt(path=out, meta={"suggested_name": suggested})

    # ---------- утилиты ----------

    def _snapshot(self, page: Page, label: str) -> None:
        try:
            png = self.debug_dir / f"{label}.png"
            html = self.debug_dir / f"{label}.html"
            page.screenshot(path=str(png), full_page=True)
            html.write_text(page.content(), encoding="utf-8")
            self.log.debug("[moek] snapshot %s -> %s", label, png)
        except Exception as e:  # noqa: BLE001
            self.log.debug("[moek] snapshot %s упал: %s", label, e)

    @staticmethod
    def _stamp_filename(name: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # стабилизируем расширение
        if "." not in name:
            name = name + ".pdf"
        stem, dot, ext = name.rpartition(".")
        return f"{stamp}_{stem}.{ext}"
