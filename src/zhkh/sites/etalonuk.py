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
    «Квитанции и начисления». На экране три MUI-Select:
    объект (Паркинг/Квартира/...), услуга (ЖКУ, Л/с) и период.
    У пользователя может быть несколько объектов — переключаемся
    по верхнему селекту и для каждого скачиваем «Квитанцию» текущего месяца.
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
                receipts = self._collect_all(page)
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

    def _open_receipts_view(self, page: Page) -> None:
        """Идём из любой страницы кабинета в «Квитанции и начисления»."""
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
        page.get_by_text(re.compile(r"^Квитанция$")).first.wait_for(
            state="visible", timeout=15_000
        )

    def _collect_all(self, page: Page) -> list[Receipt]:
        self._open_receipts_view(page)
        self._snapshot(page, "04_receipts_view")

        # Верхний MUI-Select — выбор объекта (Паркинг/Квартира/...). Если он
        # один, селект скорее всего тоже отрисован, но без альтернатив.
        objects = self._list_objects(page)
        if not objects:
            self.log.info("[etalon] объектов не нашёл — работаю с текущим (single)")
            objects = [{"title": "current", "address": "", "key": "single"}]

        self.log.info(
            "[etalon] объектов найдено: %d (%s)",
            len(objects),
            ", ".join(o["title"] for o in objects),
        )

        receipts: list[Receipt] = []
        for idx, obj in enumerate(objects, start=1):
            try:
                self.log.info(
                    "[etalon] [%d/%d] %s — %s",
                    idx,
                    len(objects),
                    obj["title"],
                    obj.get("address") or "—",
                )
                if obj["key"] != "single":
                    self._select_object(page, obj)
                rcpt = self._download_current(page, obj, idx)
                if rcpt:
                    receipts.append(rcpt)
            except Exception as e:  # noqa: BLE001
                self.log.warning(
                    "[etalon] объект %s — не удалось: %s", obj["title"], e
                )
                self._snapshot(page, f"error_obj_{idx}")
        return receipts

    def _object_select(self, page: Page) -> Locator:
        """Верхний MUI-Select со списком объектов.

        В DOM их три (объект, услуга, период). Объектный — единственный с
        двухстрочной плашкой «<Заголовок>» / «<адрес ул, дом …>».
        """
        # Берём именно тот combobox, у которого внутри текст с «ул» в адресе.
        combo = page.locator(
            "[role='combobox']:has(p:has-text('ул'))"
        ).first
        combo.wait_for(state="visible", timeout=10_000)
        return combo

    def _list_objects(self, page: Page) -> list[dict]:
        """Открыть селект объектов, прочитать пункты, закрыть селект."""
        try:
            combo = self._object_select(page)
        except PWTimeout:
            return []
        combo.click()
        try:
            page.locator("[role='listbox']").first.wait_for(
                state="visible", timeout=5_000
            )
        except PWTimeout:
            # Селект мог быть «фейковым» (один объект — нет dropdown).
            self.log.info("[etalon] dropdown не открылся, один объект")
            page.keyboard.press("Escape")
            return []

        opts = page.locator("[role='option']")
        n = opts.count()
        objects: list[dict] = []
        for i in range(n):
            el = opts.nth(i)
            try:
                text = el.inner_text(timeout=1000).strip()
            except Exception:  # noqa: BLE001
                continue
            if not text:
                continue
            lines = [s.strip() for s in text.splitlines() if s.strip()]
            title = lines[0] if lines else text
            address = lines[1] if len(lines) > 1 else ""
            # Дроп псевдо-опции «+ Добавить адрес» — её на странице нет, она
            # ведёт в форму добавления нового объекта, не в квитанции.
            if re.search(r"добавить\s+адрес", title, re.IGNORECASE):
                continue
            # value MUI хранит в data-value (id объекта в API).
            value = ""
            try:
                value = el.get_attribute("data-value") or ""
            except Exception:  # noqa: BLE001
                pass
            objects.append(
                {
                    "title": title,
                    "address": address,
                    "key": value or f"opt-{i}",
                    "index": i,
                }
            )
        # Закрываем dropdown, выбор сделает _select_object() явно.
        page.keyboard.press("Escape")
        return objects

    def _select_object(self, page: Page, obj: dict) -> None:
        """Открыть селект объектов и кликнуть нужную опцию."""
        combo = self._object_select(page)
        combo.click()
        page.locator("[role='listbox']").first.wait_for(
            state="visible", timeout=10_000
        )
        # Сначала пробуем по data-value, потом по индексу, потом по заголовку.
        opt: Locator | None = None
        if obj.get("key") and not obj["key"].startswith("opt-"):
            cand = page.locator(f"[role='option'][data-value='{obj['key']}']")
            if cand.count():
                opt = cand.first
        if opt is None and "index" in obj:
            cand = page.locator("[role='option']").nth(obj["index"])
            if cand.count():
                opt = cand
        if opt is None:
            cand = page.locator("[role='option']").filter(
                has_text=re.compile(re.escape(obj["title"]))
            )
            if cand.count():
                opt = cand.first
        if opt is None:
            page.keyboard.press("Escape")
            raise RuntimeError(f"не нашёл опцию объекта {obj['title']!r}")
        opt.click()

        # После выбора шапка карточки в сайдбаре должна обновиться на title.
        try:
            page.locator(
                f"[role='combobox']:has-text({self._js_str(obj['title'])})"
            ).first.wait_for(state="visible", timeout=10_000)
        except PWTimeout:
            self.log.debug("[etalon] не дождался смены заголовка на %s", obj["title"])
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PWTimeout:
            pass

    def _download_current(
        self, page: Page, obj: dict, idx: int
    ) -> Receipt | None:
        # Если страница после переключения сбросилась — заходим заново.
        try:
            page.get_by_text(re.compile(r"^Квитанция$")).first.wait_for(
                state="visible", timeout=5_000
            )
        except PWTimeout:
            self.log.info(
                "[etalon] страница «Квитанции» закрылась после переключения — открываю снова"
            )
            self._open_receipts_view(page)

        # Прочитаем Л/с и сводку.
        ls = self._read_account_number(page)
        summary = self._read_summary(page)
        self.log.info(
            "[etalon] %s [Л/с %s] %s — начислено=%s, долг=%s, поступления=%s, итого=%s",
            obj["title"],
            ls or "?",
            summary.get("period"),
            summary.get("accrued"),
            summary.get("overpayment"),
            summary.get("paid"),
            summary.get("total"),
        )
        self._snapshot(page, f"05_obj_{idx}_{self._slug(obj['title'])}")

        download_btn = page.get_by_text(re.compile(r"^Квитанция$")).first
        try:
            with page.expect_download(timeout=25_000) as dl_info:
                download_btn.click()
            dl: Download = dl_info.value
        except PWTimeout:
            self.log.warning(
                "[etalon] клик по «Квитанция» не вызвал download (объект %s)",
                obj["title"],
            )
            self._snapshot(page, f"06_no_download_{idx}")
            return None

        suggested = dl.suggested_filename or "etalon_receipt.pdf"
        out = self.output_dir / self._stamp_filename(obj, ls, suggested)
        dl.save_as(out)
        self.log.info("[etalon] %s: скачано %s", obj["title"], out)

        # Для сводной таблицы хотим видеть и название объекта, и адрес,
        # иначе непонятно, какая из квартир/паркингов перед тобой.
        pretty_address = obj["title"]
        if obj.get("address"):
            pretty_address = f"{obj['title']} — {obj['address']}"
        return Receipt(
            path=out,
            period=summary.get("period"),
            amount_rub=summary.get("total"),
            meta={
                **summary,
                "object": obj["title"],
                "address": pretty_address,
                "address_only": obj.get("address", ""),
                "account": ls,
                "suggested_name": suggested,
            },
        )

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

    def _read_account_number(self, page: Page) -> str:
        """«Л/с 240008041» — текст под названием услуги."""
        try:
            txt = page.inner_text("body", timeout=2000)
        except Exception:  # noqa: BLE001
            return ""
        if m := re.search(r"Л\s*/\s*с\s*([\d\-]+)", txt):
            return m.group(1)
        return ""

    def _read_summary(self, page: Page) -> dict:
        """Снимаем блок с суммами вокруг кнопки «Квитанция»."""
        summary: dict = {}
        try:
            txt = page.inner_text("body", timeout=2000)
            if m := re.search(
                r"\b(Январь|Февраль|Март|Апрель|Май|Июнь|Июль|Август|Сентябрь|"
                r"Октябрь|Ноябрь|Декабрь)\s+(\d{4})\b",
                txt,
            ):
                summary["period"] = f"{m.group(1)} {m.group(2)}"
            for label, key in (
                ("Начислено", "accrued"),
                # На свежем UI вместо «Переплата» — «Долг». Кладём в тот же ключ
                # overpayment (отрицательный = переплата, положительный = долг).
                ("Переплата", "overpayment"),
                ("Долг", "overpayment"),
                ("Поступления", "paid"),
                ("Итого", "total"),
            ):
                pattern = (
                    rf"{label}\s*[\s\S]{{0,200}}?"
                    rf"([+\-−]?\s*[\d][\d\s .,]*)\s*[₽РрPp]"
                )
                if m := re.search(pattern, txt):
                    raw = m.group(1).replace("−", "-").strip()
                    summary[key] = _parse_rub(raw)
        except Exception:  # noqa: BLE001
            pass
        # В свежем UI «Итого» = «к оплате прямо сейчас» и часто равно 0 (всё
        # уплачено наперёд). Если так, реальную задолженность держит «Долг».
        if (summary.get("total") in (None, 0, 0.0)) and summary.get("overpayment") is not None:
            debt = summary["overpayment"]
            if debt and debt > 0:
                summary["total"] = debt
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
    def _slug(value: str) -> str:
        s = re.sub(r"\s+", "_", value.strip())
        s = re.sub(r"[^\w\-]+", "", s, flags=re.UNICODE)
        return s[:40] or "obj"

    @staticmethod
    def _js_str(value: str) -> str:
        """Безопасное представление строки для playwright text-селектора."""
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'

    def _stamp_filename(self, obj: dict, account: str, name: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if "." not in name:
            name = name + ".pdf"
        stem, _, ext = name.rpartition(".")
        # Имя файла: <stamp>_<slug-объекта>_<л/с>_<исходное имя>.pdf
        parts = [stamp, self._slug(obj.get("title", "obj"))]
        if account:
            parts.append(account)
        parts.append(stem)
        return "_".join(parts) + "." + ext
