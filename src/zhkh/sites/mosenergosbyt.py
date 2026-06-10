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
from .moek import _parse_rub

LOGIN_URL = "https://my.mosenergosbyt.ru/"
ROOT_URL = "https://my.mosenergosbyt.ru/"


class MosenergosbytParser(BaseParser):
    """Парсер ЛК «Мосэнергосбыт».

    У пользователя может быть несколько лицевых счетов (квартиры/дома).
    Слева в сайдбаре «Ваши счета:» — список ЛС. Для каждого:
    1) клик по карточке в сайдбаре → текущим становится этот ЛС
    2) вкладка «История» (под основными), подвкладка «Квитанции»
    3) сверху раскрыт последний месяц с «Итого к оплате» и
       ссылкой «ПЕЧАТЬ КВИТАНЦИИ» — забираем PDF.
    """

    name = "mosenergosbyt"
    url = ROOT_URL

    def run(self) -> ParseResult:
        result = ParseResult(site=self.name)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            ctx = browser.new_context(
                accept_downloads=True,
                locale="ru-RU",
                viewport={"width": 1600, "height": 1000},
            )
            page = ctx.new_page()
            try:
                self._login(page)
                receipts = self._collect_all_accounts(page)
                result.receipts = receipts
            except Exception as e:  # noqa: BLE001
                self._snapshot(page, "error")
                result.error = f"{type(e).__name__}: {e}"
                self.log.exception("МЭС: упало на стадии парсинга")
            finally:
                ctx.close()
                browser.close()
        return result

    # ---------- стадии ----------

    def _login(self, page: Page) -> None:
        self.log.info("[mes] открываю %s", LOGIN_URL)
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass
        self._snapshot(page, "01_login")

        login_input = self._find_login_input(page)
        password_input = page.locator("input[type='password']").first

        self.log.info("[mes] ввожу логин/пароль")
        login_input.fill(self.creds.login)
        password_input.fill(self.creds.password)
        self._snapshot(page, "02_filled")

        submit = self._find_submit(page)
        self.log.info("[mes] жму вход")
        submit.click()

        # Признак — появление сайдбара «Ваши счета».
        marker = page.get_by_text(re.compile(r"Ваши\s+счета", re.IGNORECASE)).first
        try:
            marker.wait_for(state="visible", timeout=30_000)
        except PWTimeout:
            self._snapshot(page, "03_after_login")
            err = self._read_login_error(page)
            raise RuntimeError(
                "После клика «Войти» сайдбар «Ваши счета» так и не появился"
                + (f", сообщение: {err}" if err else "")
            )
        self._snapshot(page, "03_after_login")
        self.log.info("[mes] логин ок, url=%s", page.url)

    def _collect_all_accounts(self, page: Page) -> list[Receipt]:
        # Прибиваем все вылезшие промо-модалки (ЦИФРОВОЙ СТАРТ и т.п.) —
        # они перехватывают клики по сайдбару. Модалка появляется
        # асинхронно после загрузки кабинета — даём ей секунду
        # подняться, потом закрываем.
        try:
            page.locator("[role='dialog']:visible").first.wait_for(
                state="visible", timeout=4_000
            )
        except PWTimeout:
            pass
        self._dismiss_modals(page)

        # Собираем список ЛС из левого сайдбара. Каждая карточка — это
        # ссылка вида "36953-203-96" (номер ЛС).
        accounts = self._list_accounts(page)
        if not accounts:
            self.log.warning("[mes] не нашёл ни одного лицевого счёта в сайдбаре")
            return []
        self.log.info(
            "[mes] найдено ЛС: %d (%s)",
            len(accounts),
            ", ".join(a["number"] for a in accounts),
        )

        receipts: list[Receipt] = []
        for idx, acc in enumerate(accounts, start=1):
            try:
                rcpt = self._collect_one_account(page, acc, idx)
                if rcpt:
                    receipts.append(rcpt)
            except Exception as e:  # noqa: BLE001
                self.log.warning(
                    "[mes] ЛС %s (%s): не удалось — %s", acc["number"], acc["address"], e
                )
                self._snapshot(page, f"error_lc_{acc['number']}")
        return receipts

    def _list_accounts(self, page: Page) -> list[dict]:
        """Парсим список лицевых счетов из левого сайдбара."""
        # Ссылки/кнопки вида "XXXXX-XXX-XX" — это номера ЛС.
        ls_locator = page.locator("text=/\\b\\d{5}-\\d{3}-\\d{2}\\b/")
        n = ls_locator.count()
        accounts: list[dict] = []
        seen: set[str] = set()
        for i in range(n):
            el = ls_locator.nth(i)
            try:
                text = el.inner_text(timeout=1000).strip()
            except Exception:  # noqa: BLE001
                continue
            m = re.search(r"\b(\d{5}-\d{3}-\d{2})\b", text)
            if not m:
                continue
            ls_num = m.group(1)
            if ls_num in seen:
                continue
            seen.add(ls_num)
            # Адрес — соседний/предшествующий текст в той же карточке.
            address = self._read_account_address(el)
            accounts.append({"number": ls_num, "address": address, "locator": el})
        return accounts

    @staticmethod
    def _read_account_address(ls_el: Locator) -> str:
        """Поднимаемся к карточке ЛС и берём адрес-строку (рядом)."""
        try:
            card = ls_el.locator(
                "xpath=ancestor::*[.//text()[contains(., 'ул') "
                "or contains(., 'пр') or contains(., 'д.')]][1]"
            ).first
            text = card.inner_text(timeout=1000)
        except Exception:  # noqa: BLE001
            return ""
        # Адрес — строка, в которой есть «ул.»/«пр.»/«д.» и нет цифр-ЛС.
        for line in (s.strip() for s in text.splitlines()):
            if not line:
                continue
            if re.search(r"\d{5}-\d{3}-\d{2}", line):
                continue
            if any(tok in line.lower() for tok in ("ул", "пр", "д.")):
                return line
        return ""

    def _collect_one_account(
        self, page: Page, acc: dict, idx: int
    ) -> Receipt | None:
        ls = acc["number"]
        self.log.info("[mes] ЛС %s (%s)", ls, acc["address"] or "—")
        # На случай, если за время прошлой итерации снова вылезла модалка.
        self._dismiss_modals(page)
        # Клик по карточке ЛС в сайдбаре. force=True — на случай если
        # маленький промо-баннер всё-таки висит.
        try:
            acc["locator"].click(timeout=10_000)
        except PWTimeout:
            acc["locator"].click(force=True)
        # Ждём пока в верхней части появится «Лицевой счет: <тот же номер>».
        page.locator(f"text=/Лицевой счет:\\s*{re.escape(ls)}/").first.wait_for(
            state="visible", timeout=20_000
        )
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PWTimeout:
            pass

        # Вкладка «История» в основных табах.
        self._click_tab(page, "История")
        # Подвкладка «Квитанции» в подменю «История».
        self._click_tab(page, "Квитанции")

        # Данные подгружаются async. У МЭС панели месяцев лежат в
        # [testid^='mesReceiptsMain-Panel-Header-'] (-date, -smTotal,
        # -printReceipts). Ждём появления первой панели.
        try:
            page.locator(
                "[testid='mesReceiptsMain-Panel-Header-0-printReceipts']"
            ).wait_for(state="visible", timeout=20_000)
        except PWTimeout:
            self.log.warning("[mes] ЛС %s: панели Квитанций не загрузились", ls)
        self._snapshot(page, f"04_lc_{ls}")

        # Берём период + сумму прямо из шапки последнего месяца.
        period, total = self._read_first_month(page)
        self.log.info(
            "[mes] ЛС %s: период=%s, итого к оплате=%s",
            ls,
            period,
            total,
        )

        # «ПЕЧАТЬ КВИТАНЦИИ» в шапке самого верхнего месяца (Header-0).
        print_link = page.locator(
            "[testid='mesReceiptsMain-Panel-Header-0-printReceipts']"
        )
        try:
            with page.expect_download(timeout=25_000) as dl_info:
                print_link.click()
            dl: Download = dl_info.value
        except PWTimeout:
            self.log.warning("[mes] ЛС %s: download не сработал", ls)
            self._snapshot(page, f"05_no_download_{ls}")
            return None

        suggested = dl.suggested_filename or f"mes_{ls}.pdf"
        out = self.output_dir / self._stamp_filename(ls, suggested)
        dl.save_as(out)
        self.log.info("[mes] ЛС %s: скачано %s", ls, out)
        return Receipt(
            path=out,
            period=period,
            amount_rub=total,
            meta={
                "account": ls,
                "address": acc["address"],
                "suggested_name": suggested,
            },
        )

    # ---------- helpers ----------

    def _dismiss_modals(self, page: Page) -> None:
        """Закрывает все модалки/попапы, перехватывающие клики.

        У МЭС бывает «ЦИФРОВОЙ СТАРТ во вселенной комфорта» и подобные
        промо-баннеры в `<div role="dialog">`.
        """
        for _ in range(5):
            dlg = page.locator("[role='dialog']:visible")
            if not dlg.count():
                return
            # Пробуем закрыть кнопкой/иконкой. У МЭС close — svg[testid=formClose].
            for sel in (
                "[role='dialog'] [testid='formClose']",
                "[role='dialog'] [aria-label*='Закрыть' i]",
                "[role='dialog'] [aria-label*='close' i]",
                "[role='dialog'] button:has-text('Закрыть')",
                "[role='dialog'] button:has-text('Не сейчас')",
                "[role='dialog'] button:has-text('Понятно')",
                "[role='dialog'] button:has-text('×')",
                "[role='dialog'] [class*='close' i]",
            ):
                loc = page.locator(sel)
                if loc.count():
                    try:
                        loc.first.click(timeout=2000)
                        page.wait_for_timeout(300)
                        break
                    except Exception:  # noqa: BLE001
                        continue
            else:
                # Никакая кнопка не сработала — пробуем Escape.
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            if not page.locator("[role='dialog']:visible").count():
                self.log.info("[mes] модалка закрыта")
                return
        self.log.warning("[mes] не смог закрыть модалку — продолжаю с force-click")

    def _click_tab(self, page: Page, name: str) -> None:
        """Клик по табу/подтабу с заданным текстом (видимый)."""
        loc = page.get_by_text(re.compile(rf"^{re.escape(name)}$")).first
        loc.wait_for(state="visible", timeout=10_000)
        loc.click()
        page.wait_for_timeout(300)

    def _read_first_month(self, page: Page) -> tuple[str | None, float | None]:
        """Период и сумма из самой верхней панели месяца (Header-0)."""
        period: str | None = None
        amount: float | None = None
        try:
            date_text = page.locator(
                "[testid='mesReceiptsMain-Panel-Header-0-date']"
            ).inner_text(timeout=2000).strip()
            # В DOM это «апрель 2026» (lowercase) — нормализуем регистр.
            if m := re.match(r"(\S+)\s+(\d{4})", date_text):
                period = f"{m.group(1).capitalize()} {m.group(2)}"
        except Exception:  # noqa: BLE001
            pass
        try:
            total_text = page.locator(
                "[testid='mesReceiptsMain-Panel-Header-0-smTotal']"
            ).inner_text(timeout=2000)
            # «-1 604,83 руб.» — знак отражает «к оплате», берём |x|.
            if m := re.search(r"([+\-−]?\s*[\d\s .,]+)\s*руб", total_text):
                amount = _parse_rub(m.group(1).replace("−", "-"))
                if amount is not None:
                    amount = abs(amount)
        except Exception:  # noqa: BLE001
            pass
        return period, amount

    def _find_login_input(self, page: Page) -> Locator:
        for selector in (
            "input[type='tel']",
            "input[type='email']",
            "input[name*='login' i]",
            "input[name*='user' i]",
            "input[autocomplete='username']",
        ):
            loc = page.locator(selector)
            if loc.count() and loc.first.is_visible():
                return loc.first
        # MUI/ant-design — обычный text-input без атрибутов.
        text_inputs = page.locator("input[type='text']")
        for i in range(text_inputs.count()):
            el = text_inputs.nth(i)
            if el.is_visible():
                return el
        raise RuntimeError("не нашёл поле логина")

    def _find_submit(self, page: Page) -> Locator:
        for selector in (
            "button[type='submit']",
            "button:has-text('Войти')",
            "button:has-text('Вход')",
            "input[type='submit']",
        ):
            loc = page.locator(selector)
            if loc.count() and loc.first.is_visible():
                return loc.first
        raise RuntimeError("не нашёл кнопку входа")

    def _read_login_error(self, page: Page) -> str | None:
        for selector in ("[class*='error']", "[role='alert']"):
            loc = page.locator(selector)
            if loc.count():
                try:
                    text = loc.first.inner_text(timeout=1000).strip()
                    if text:
                        return text
                except Exception:  # noqa: BLE001
                    continue
        return None

    # ---------- утилиты ----------

    def _snapshot(self, page: Page, label: str) -> None:
        try:
            png = self.debug_dir / f"{label}.png"
            html = self.debug_dir / f"{label}.html"
            page.screenshot(path=str(png), full_page=True)
            html.write_text(page.content(), encoding="utf-8")
            self.log.debug("[mes] snapshot %s -> %s", label, png)
        except Exception as e:  # noqa: BLE001
            self.log.debug("[mes] snapshot %s упал: %s", label, e)

    @staticmethod
    def _stamp_filename(account: str, name: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if "." not in name:
            name = name + ".pdf"
        stem, _, ext = name.rpartition(".")
        return f"{stamp}_{account}_{stem}.{ext}"
