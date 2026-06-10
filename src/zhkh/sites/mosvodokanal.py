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
from .moek import _parse_rub  # формат сумм одинаковый

ROOT_URL = "https://onewind.mosvodokanal.ru/"
CABINET_URL = "https://onewind.mosvodokanal.ru/#NaturalPersonView"


class MosvodokanalParser(BaseParser):
    """Парсер ЛК Мосводоканала.

    SPA с хеш-роутингом. Структура: верхние вкладки
    ИНФОРМАЦИЯ / ПЕРЕДАЧА ПОКАЗАНИЙ / КВИТАНЦИИ / ВОПРОСЫ И ОТВЕТЫ.
    На КВИТАНЦИИ — таблица квитанций по выбранному ЛС, кнопка
    «ПЕЧАТЬ КВИТАНЦИИ/СЧЕТА» (триггер скачивания PDF).
    """

    name = "mosvodokanal"
    url = CABINET_URL

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
                receipts = self._collect(page)
                result.receipts = receipts
            except Exception as e:  # noqa: BLE001
                self._snapshot(page, "error")
                result.error = f"{type(e).__name__}: {e}"
                self.log.exception("MVK: упало на стадии парсинга")
            finally:
                ctx.close()
                browser.close()
        return result

    # ---------- стадии ----------

    def _login(self, page: Page) -> None:
        self.log.info("[mvk] открываю %s", ROOT_URL)
        page.goto(ROOT_URL, wait_until="domcontentloaded", timeout=30_000)

        # Лендинг — ExtJS, всё грузится асинхронно. Дожидаемся кнопки
        # «Физическое лицо», иначе клики проваливаются по «ничего нет».
        phys_btn = page.locator(
            ".x-btn:has(.x-btn-inner:has-text('Физическое лицо'))"
        ).first
        phys_btn.wait_for(state="visible", timeout=30_000)
        self._snapshot(page, "01_landing")
        self.log.info("[mvk] кликаю «Физическое лицо»")
        phys_btn.click()

        # Дальше показывается лоадер «Получение информации по пользователю...»
        # и затем либо форма логина, либо уже залогиненный кабинет.
        login_in_locator = page.locator("input[name='login']:visible").first
        cabinet_marker = page.get_by_text(re.compile(r"^КВИТАНЦИИ$")).first
        self.log.info("[mvk] жду форму логина либо кабинет")
        page.wait_for_function(
            """() => {
                const isShown = (el) => el && el.getClientRects().length > 0;
                const loginInputs = [...document.querySelectorAll("input[name='login']")];
                if (loginInputs.some(isShown)) return true;
                return [...document.querySelectorAll('*')].some(n =>
                    /^\\s*КВИТАНЦИИ\\s*$/.test(n.textContent || '') && n.children.length === 0
                );
            }""",
            timeout=30_000,
        )
        self._snapshot(page, "01b_after_phys_pick")

        if cabinet_marker.is_visible():
            self.log.info("[mvk] сессия жива, форма логина не понадобилась")
            return

        captcha = page.locator("input[placeholder='Капча']:visible")
        if captcha.count():
            self._snapshot(page, "captcha")
            raise RuntimeError(
                "Мосводоканал показывает капчу. Авто-логин невозможен. "
                "Запусти с --headed и введи руками — добавлю сохранение сессии "
                "на следующей итерации."
            )

        login_input = login_in_locator
        password_input = page.locator("input[name='pass']:visible").first
        self.log.info("[mvk] ввожу логин/пароль")
        login_input.fill(self.creds.login)
        password_input.fill(self.creds.password)
        self._snapshot(page, "02_filled")

        # «ВОЙТИ В ЛИЧНЫЙ КАБИНЕТ» — ExtJS-кнопка.
        submit = page.locator(
            ".x-btn:has(.x-btn-inner:has-text('ВОЙТИ В ЛИЧНЫЙ КАБИНЕТ'))"
        ).first
        self.log.info("[mvk] жму вход")
        submit.click()

        # Признак входа — появление любой из верхних вкладок.
        marker = page.get_by_text(
            re.compile(r"^(КВИТАНЦИИ|ИНФОРМАЦИЯ|ЛИЦЕВЫЕ\s+СЧЕТА)$", re.IGNORECASE)
        ).first
        try:
            marker.wait_for(state="visible", timeout=30_000)
        except PWTimeout:
            self._snapshot(page, "03_after_login")
            err = self._read_login_error(page)
            raise RuntimeError(
                "После клика «Войти» кабинет так и не появился"
                + (f", сообщение: {err}" if err else "")
            )

        # Признак входа — появление верхней вкладки «КВИТАНЦИИ» либо
        # пункта левого меню «ЛИЦЕВЫЕ СЧЕТА».
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PWTimeout:
            pass  # MVK держит long-poll, networkidle может не наступать
        self._snapshot(page, "03_after_login")
        self.log.info("[mvk] логин ок, url=%s", page.url)

    def _collect(self, page: Page) -> list[Receipt]:
        # Кликаем на вкладку КВИТАНЦИИ (если уже не открыта). ExtJS-вкладка.
        self.log.info("[mvk] переключаюсь на вкладку КВИТАНЦИИ")
        tab = page.locator(
            ".x-tab:has(.x-tab-inner:text-is('КВИТАНЦИИ'))"
        ).first
        try:
            tab.wait_for(state="visible", timeout=10_000)
        except PWTimeout:
            # fallback: иногда роль 'tab' доступна для ARIA-обёртки
            tab = page.get_by_role("tab", name="КВИТАНЦИИ").first
        tab.click()
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PWTimeout:
            pass
        # Ждём пока в таблице появятся строки.
        try:
            page.locator("tr.x-grid-row").first.wait_for(
                state="visible", timeout=15_000
            )
        except PWTimeout:
            self.log.warning("[mvk] таблица квитанций пуста или ещё грузится")
        self._snapshot(page, "04_receipts_tab")

        debt_text = self._read_debt_block(page)
        debt_value = self._extract_debt(debt_text)
        self.log.info("[mvk] задолженность: %r → %s ₽", debt_text, debt_value)

        # Выбираем самую свежую квитанцию = верхняя видимая строка ExtJS-грида.
        # Берём строку с минимальным y из видимых, чтобы не зацепить
        # скрытые гриды соседних подвкладок (Авансы и т.п.).
        first_row = self._find_topmost_visible_row(page)
        if first_row is None:
            self.log.warning("[mvk] не нашёл видимых строк в таблице квитанций")
            return []
        row_meta = self._parse_row(first_row)
        self.log.info(
            "[mvk] последняя квитанция: №=%s, дата=%s, сумма=%s, статус=%s",
            row_meta.get("number"),
            row_meta.get("date"),
            row_meta.get("amount_rub"),
            row_meta.get("status"),
        )
        first_row.click()
        # Ждём пока кнопка «Печать квитанции/счета» перестанет быть disabled.
        print_btn_wrapper = page.locator(
            ".x-btn:has(.x-btn-inner:text-is('Печать квитанции/счета'))"
        ).first
        try:
            page.wait_for_function(
                """(btn) => btn && !btn.classList.contains('x-btn-disabled')
                          && !btn.classList.contains('x-item-disabled')""",
                arg=print_btn_wrapper.element_handle(),
                timeout=5_000,
            )
        except PWTimeout:
            self.log.warning("[mvk] кнопка «Печать» так и не активировалась")
        self._snapshot(page, "05_row_selected")

        try:
            with page.expect_download(timeout=25_000) as dl_info:
                print_btn_wrapper.click()
            dl: Download = dl_info.value
        except PWTimeout:
            self.log.warning(
                "[mvk] клик «Печать» не вызвал download за 25с — снимаю состояние"
            )
            self._snapshot(page, "06_after_print_click")
            raise

        suggested = dl.suggested_filename or f"mvk_{row_meta.get('date','')}.pdf"
        out = self.output_dir / self._stamp_filename(suggested)
        dl.save_as(out)
        self.log.info("[mvk] скачано: %s", out)
        receipt = Receipt(
            path=out,
            period=row_meta.get("date"),
            # На главной — суммарная задолженность; в строке — сумма квитанции.
            amount_rub=row_meta.get("amount_rub"),
            meta={
                **row_meta,
                "suggested_name": suggested,
                "debt_text": debt_text,
                "debt_rub": debt_value,
            },
        )
        return [receipt]

    # ---------- поиск элементов ----------

    def _maybe_click(self, page: Page, text: str) -> bool:
        """Если на странице видна кнопка с такой надписью — кликаем."""
        loc = page.get_by_text(text, exact=False).first
        try:
            if loc.is_visible(timeout=1500):
                self.log.info("[mvk] клик: %r", text)
                loc.click()
                page.wait_for_timeout(300)
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _find_login_input(self, page: Page) -> Locator:
        for label in ("Логин", "Email", "E-mail", "Имя пользователя"):
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
        for selector in (
            "input[type='email']",
            "input[name*='login' i]",
            "input[name*='user' i]",
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
            "button:has-text('Войти')",
            "button:has-text('Вход')",
            "input[type='submit']",
            "a:has-text('Войти')",
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

    def _read_debt_block(self, page: Page) -> str:
        """Возвращает текст блока ЗАДОЛЖЕННОСТЬ или пустую строку."""
        for selector in (
            "text=ЗАДОЛЖЕННОСТЬ",
            ":text-is('ЗАДОЛЖЕННОСТЬ')",
        ):
            loc = page.locator(selector)
            if not loc.count():
                continue
            try:
                # Поднимаемся к ближайшему блоку, у которого внутри есть «руб.».
                block = loc.locator(
                    "xpath=ancestor::*[.//text()[contains(., 'руб')]][1]"
                ).first
                return block.inner_text(timeout=2000).strip()
            except Exception:  # noqa: BLE001
                continue
        return ""

    @staticmethod
    def _extract_debt(block_text: str) -> float | None:
        m = re.search(r"([\-+]?[\d\s .,]+)\s*руб", block_text)
        return _parse_rub(m.group(1)) if m else None

    def _find_topmost_visible_row(self, page: Page) -> Locator | None:
        """Возвращает видимую строку ExtJS-грида с наименьшим y."""
        rows = page.locator("tr.x-grid-row")
        n = rows.count()
        self.log.debug("[mvk] всего x-grid-row в DOM: %d", n)
        best: tuple[float, Locator] | None = None
        for i in range(n):
            r = rows.nth(i)
            try:
                box = r.bounding_box(timeout=500)
            except Exception:  # noqa: BLE001
                continue
            if not box or box["width"] < 5 or box["height"] < 5:
                continue
            if best is None or box["y"] < best[0]:
                best = (box["y"], r)
        return best[1] if best else None

    def _parse_row(self, row: Locator) -> dict:
        """Парсим строку: Квитанция № / Дата / Сумма / НДС / Оплачено /
        Оплачено предопл / Статус."""
        try:
            cells = row.locator("td.x-grid-cell").all_inner_texts()
        except Exception:  # noqa: BLE001
            cells = []
        cells = [c.strip() for c in cells]
        meta: dict = {"row_cells": cells}
        if len(cells) >= 1:
            meta["number"] = cells[0]
        if len(cells) >= 2 and re.match(r"\d{2}\.\d{2}\.\d{4}", cells[1]):
            meta["date"] = cells[1]
        if len(cells) >= 3:
            meta["amount_rub"] = _parse_rub(cells[2])
        if cells:
            meta["status"] = cells[-1]
        return meta

    # ---------- утилиты ----------

    def _snapshot(self, page: Page, label: str) -> None:
        try:
            png = self.debug_dir / f"{label}.png"
            html = self.debug_dir / f"{label}.html"
            page.screenshot(path=str(png), full_page=True)
            html.write_text(page.content(), encoding="utf-8")
            self.log.debug("[mvk] snapshot %s -> %s", label, png)
        except Exception as e:  # noqa: BLE001
            self.log.debug("[mvk] snapshot %s упал: %s", label, e)

    @staticmethod
    def _stamp_filename(name: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if "." not in name:
            name = name + ".pdf"
        stem, _, ext = name.rpartition(".")
        return f"{stamp}_{stem}.{ext}"
