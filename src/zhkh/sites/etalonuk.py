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

LOGIN_URL = "https://cab.etalonuk.ru/login/"
ROOT_URL = "https://cab.etalonuk.ru/"


class EtalonParser(BaseParser):
    """Парсер ЛК УК «Эталон» (cab.etalonuk.ru).

    Логин по телефону (маска), 2 галочки согласий, «Войти».
    Дальше: верхнее меню → «Платежи» → правый сайдбар →
    «Квитанции и начисления» → скачать «Квитанцию» текущего месяца.
    """

    name = "etalonuk"
    url = ROOT_URL

    def run(self) -> ParseResult:
        result = ParseResult(site=self.name)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            ctx = browser.new_context(
                accept_downloads=True,
                locale="ru-RU",
                viewport={"width": 1500, "height": 950},
            )
            page = ctx.new_page()
            try:
                self._login(page)
                receipts = self._collect(page)
                result.receipts = receipts
            except Exception as e:  # noqa: BLE001
                self._snapshot(page, "error")
                result.error = f"{type(e).__name__}: {e}"
                self.log.exception("Эталон: упало на стадии парсинга")
            finally:
                ctx.close()
                browser.close()
        return result

    # ---------- стадии ----------

    def _login(self, page: Page) -> None:
        self.log.info("[etalon] открываю %s", LOGIN_URL)
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass
        # Cookie-баннер «Принять» в нижней части — закрываем чтобы не мешал.
        try:
            cookie_btn = page.get_by_role("button", name=re.compile(r"^Принять$")).first
            if cookie_btn.is_visible(timeout=1500):
                cookie_btn.click()
                page.wait_for_timeout(200)
        except Exception:  # noqa: BLE001
            pass
        self._snapshot(page, "01_login")

        phone_input = self._find_phone_input(page)
        digits = re.sub(r"\D", "", self.creds.login)
        # Если уже 11 цифр и начинается с 7 — оставляем как есть, иначе
        # дополняем семёркой (мобильный РФ).
        if len(digits) == 10:
            digits = "7" + digits
        elif len(digits) == 11 and digits[0] == "8":
            digits = "7" + digits[1:]
        # «+7» в маске обычно зафиксировано, остаются последние 10 цифр.
        national10 = digits[-10:]
        self.log.info("[etalon] ввожу телефон (10 цифр), пароль, галочки")
        phone_input.click()
        # На случай если в поле что-то уже есть — чистим.
        phone_input.press("Control+A")
        phone_input.press("Delete")
        phone_input.type(national10, delay=30)

        password_input = page.locator("input[type='password']").first
        password_input.fill(self.creds.password)

        # Две галочки согласий — иногда уже отмечены, иногда нет.
        boxes = page.locator("input[type='checkbox']")
        n = boxes.count()
        for i in range(n):
            cb = boxes.nth(i)
            try:
                if not cb.is_checked():
                    cb.check()
            except Exception:  # noqa: BLE001
                # Чекбокс может быть нестандартный (span поверх input) —
                # тогда клик по родительскому label.
                try:
                    cb.locator("xpath=ancestor::label[1]").click()
                except Exception:  # noqa: BLE001
                    pass
        self._snapshot(page, "02_filled")

        submit = page.get_by_role("button", name=re.compile(r"^\s*Войти\s*$"))
        submit.first.click()

        # Признак входа — появление топ-меню «Платежи».
        marker = page.get_by_text(re.compile(r"^Платежи$")).first
        try:
            marker.wait_for(state="visible", timeout=30_000)
        except PWTimeout:
            self._snapshot(page, "03_after_login")
            err = self._read_login_error(page)
            raise RuntimeError(
                "После клика «Войти» не появилось меню Платежи"
                + (f", сообщение: {err}" if err else "")
            )
        self._snapshot(page, "03_after_login")
        self.log.info("[etalon] логин ок, url=%s", page.url)

    def _collect(self, page: Page) -> list[Receipt]:
        # После логина может выскочить онбординг-модалка («Оплачивайте счета
        # по всем помещениям…»). Если есть «Перейти в платежи» — кликаем её,
        # она же открывает нужный раздел.
        go_btn = page.get_by_role(
            "button", name=re.compile(r"^Перейти в платежи$", re.IGNORECASE)
        )
        if go_btn.count() and go_btn.first.is_visible():
            self.log.info("[etalon] закрываю онбординг через «Перейти в платежи»")
            go_btn.first.click()
        else:
            self.log.info("[etalon] перехожу во вкладку Платежи")
            page.get_by_text(re.compile(r"^Платежи$")).first.click()
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass

        # Правая боковая навигация → «Квитанции и начисления».
        self.log.info("[etalon] открываю «Квитанции и начисления»")
        link = page.get_by_text(
            re.compile(r"Квитанции\s+и\s+начисления", re.IGNORECASE)
        ).first
        link.wait_for(state="visible", timeout=15_000)
        link.click()
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass

        # Ждём кнопку «Квитанция» (download).
        download_btn = page.get_by_text(re.compile(r"^Квитанция$")).first
        download_btn.wait_for(state="visible", timeout=15_000)
        self._snapshot(page, "04_receipts_view")

        # Парсим сводку по месяцу.
        summary = self._read_summary(page)
        self.log.info(
            "[etalon] %s — начислено=%s, переплата=%s, поступления=%s, итого=%s",
            summary.get("period"),
            summary.get("accrued"),
            summary.get("overpayment"),
            summary.get("paid"),
            summary.get("total"),
        )

        try:
            with page.expect_download(timeout=25_000) as dl_info:
                download_btn.click()
            dl: Download = dl_info.value
        except PWTimeout:
            self.log.warning("[etalon] клик по «Квитанция» не вызвал download")
            self._snapshot(page, "05_after_print_click")
            raise

        suggested = dl.suggested_filename or "etalon_receipt.pdf"
        out = self.output_dir / self._stamp_filename(suggested)
        dl.save_as(out)
        self.log.info("[etalon] скачано: %s", out)

        receipt = Receipt(
            path=out,
            period=summary.get("period"),
            # «к оплате» = Итого. Полная начисленная сумма — в meta.
            amount_rub=summary.get("total"),
            meta={**summary, "suggested_name": suggested},
        )
        return [receipt]

    # ---------- эвристики ----------

    def _find_phone_input(self, page: Page) -> Locator:
        # MUI: телефон — обычный <input type='text'> без name/autocomplete.
        # Берём первый видимый non-password текстовый инпут на форме.
        for selector in (
            "input[type='tel']",
            "input[autocomplete='tel']",
            "input[name*='phone' i]",
            "input[type='text']",
        ):
            loc = page.locator(selector)
            for i in range(loc.count()):
                inp = loc.nth(i)
                if inp.is_visible():
                    return inp
        raise RuntimeError("не нашёл поле телефона")

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

    def _read_summary(self, page: Page) -> dict:
        """Снимаем блок с суммами вокруг кнопки «Квитанция»."""
        summary: dict = {}
        # Период — текст селектора месяца (комбобокс «Май 2026»).
        period_loc = page.locator(
            "xpath=//*[normalize-space(text())=normalize-space()][1]"
        )
        try:
            # Простейший путь — найти текст вида «<Месяц> 20XX».
            txt = page.inner_text("body", timeout=2000)
            if m := re.search(
                r"\b(Январь|Февраль|Март|Апрель|Май|Июнь|Июль|Август|Сентябрь|"
                r"Октябрь|Ноябрь|Декабрь)\s+(\d{4})\b",
                txt,
            ):
                summary["period"] = f"{m.group(1)} {m.group(2)}"
            for label, key in (
                ("Начислено", "accrued"),
                ("Переплата", "overpayment"),
                ("Поступления", "paid"),
                ("Итого", "total"),
            ):
                # «Label … <amount> ₽» — value может быть на той же строке
                # или ниже, с произвольным разделителем (флекс-вёрстка).
                pattern = (
                    rf"{label}\s*[\s\S]{{0,200}}?"
                    rf"([+\-−]?\s*[\d][\d\s .,]*)\s*[₽РрPp]"
                )
                if m := re.search(pattern, txt):
                    raw = m.group(1).replace("−", "-").strip()
                    summary[key] = _parse_rub(raw)
        except Exception:  # noqa: BLE001
            pass
        return summary

    # ---------- утилиты ----------

    def _snapshot(self, page: Page, label: str) -> None:
        try:
            png = self.debug_dir / f"{label}.png"
            html = self.debug_dir / f"{label}.html"
            page.screenshot(path=str(png), full_page=True)
            html.write_text(page.content(), encoding="utf-8")
            self.log.debug("[etalon] snapshot %s -> %s", label, png)
        except Exception as e:  # noqa: BLE001
            self.log.debug("[etalon] snapshot %s упал: %s", label, e)

    @staticmethod
    def _stamp_filename(name: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if "." not in name:
            name = name + ".pdf"
        stem, _, ext = name.rpartition(".")
        return f"{stamp}_{stem}.{ext}"
