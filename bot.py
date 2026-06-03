from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Iterable
from urllib.parse import urljoin
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from catalog import ImageItem, names_match
from reports import ReportRecord, ReportWriter


BASE_SEARCH_URL = "https://my.satu.kz/cms/product?page=1&per_page=100&search_term="
PRODUCTS_URL = "https://my.satu.kz/cms/product?page=1&per_page=100&search_term="


@dataclass(frozen=True)
class ExistingImageItem:
    code: str
    image_path: Path
    product_url: str
    expected_name: str
    actual_name: str


@dataclass(frozen=True)
class ProductCandidate:
    url: str
    text: str


@dataclass(frozen=True)
class Selectors:
    product_links: tuple[str, ...] = (
        "a[href*='/cms/product/'][href*='edit']",
        "a[href*='/cms/product/']",
        "tr:has-text('{code}') a[href]",
        "[data-qa*='product'] a[href]",
    )
    name_fields: tuple[str, ...] = (
        "input[name*='name']",
        "textarea[name*='name']",
        "[data-qa*='name'] input",
        "h1",
    )
    code_fields: tuple[str, ...] = (
        "input[name*='sku']",
        "input[name*='code']",
        "input[name*='article']",
        "[data-qa*='sku'] input",
        "[data-qa*='code'] input",
    )
    image_blocks: tuple[str, ...] = (
        "[data-qaid='image_item']",
        "[data-qa*='image']",
        ".product-images",
        ".images",
    )
    file_inputs: tuple[str, ...] = (
        "input[type='file'][data-qaid='upload_images']",
        "input[type='file'][data-qaid='upload_file']",
        "input.b-uploader-extend__file-input[type='file']",
        "input[type='file'][accept*='image']",
        "input[type='file']",
    )
    upload_buttons: tuple[str, ...] = (
        "button:has-text('Добавить')",
        "button:has-text('Загрузить')",
        "a:has-text('Добавить')",
        "a:has-text('Загрузить')",
    )
    save_buttons: tuple[str, ...] = (
        "button[type='button'][data-qaid='save_btn']",
        "[data-qaid='save_btn']",
    )
    delete_image_buttons: tuple[str, ...] = (
        "[data-qaid='delete_img_btn']",
        "[data-qa*='image'] button:has-text('Удалить')",
        "[class*='image'] button:has-text('Удалить')",
        "[class*='image'] a:has-text('Удалить')",
        "button[aria-label*='Удалить']",
    )


StatusCallback = Callable[[str, str], None]


class StopRequested(Exception):
    pass


class SatuImageBot:
    def __init__(
        self,
        profile_dir: str | Path,
        report_writer: ReportWriter,
        status_callback: StatusCallback | None = None,
        selectors: Selectors | None = None,
        authorization_event: Event | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.report_writer = report_writer
        self.status_callback = status_callback or (lambda _status, _url="": None)
        self.selectors = selectors or Selectors()
        self.authorization_event = authorization_event
        self.stop_requested = False
        self.existing_images: list[ExistingImageItem] = []

    def stop(self) -> None:
        self.stop_requested = True

    def run(self, images: Iterable[ImageItem]) -> list[ExistingImageItem]:
        self.stop_requested = False
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=False,
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                self._wait_for_authorization(page)
                for image in images:
                    self._check_stop()
                    self._process_image(page, image)
            finally:
                self.report_writer.save()
                context.close()
        return self.existing_images

    def replace_existing(self, items: Iterable[ExistingImageItem]) -> None:
        self.stop_requested = False
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(self.profile_dir), headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                self._wait_for_authorization(page)
                for item in items:
                    self._check_stop()
                    self._set_status(f"Заменяет изображение: {item.code}", item.product_url)
                    self._safe_goto(page, item.product_url)
                    self._wait_for_product_editor(page)
                    self._remove_existing_images(page)
                    self._upload_image(page, item.image_path)
                    self.report_writer.add(
                        ReportRecord(
                            code=item.code,
                            status="replaced_existing_image",
                            image_path=str(item.image_path),
                            product_url=item.product_url,
                            expected_name=item.expected_name,
                            actual_name=item.actual_name,
                        )
                    )
            finally:
                self.report_writer.save()
                context.close()

    def mark_skipped_existing(self, items: Iterable[ExistingImageItem], status: str = "skipped_existing_image") -> None:
        for item in items:
            self.report_writer.add(
                ReportRecord(
                    code=item.code,
                    status=status,
                    image_path=str(item.image_path),
                    product_url=item.product_url,
                    expected_name=item.expected_name,
                    actual_name=item.actual_name,
                )
            )

    def _process_image(self, page: Page, image: ImageItem) -> None:
        if not image.code:
            self.report_writer.add(ReportRecord(code="", status="invalid_file", image_path=str(image.path)))
            return
        if not image.catalog_name:
            self.report_writer.add(
                ReportRecord(
                    code=image.code,
                    status="invalid_file",
                    image_path=str(image.path),
                    message="В Excel не найдено название для артикула.",
                )
            )
            return

        search_url = f"{BASE_SEARCH_URL}{quote(image.code)}"
        self._set_status(f"Ищет товар: {image.code}", search_url)
        self._safe_goto(page, search_url)
        self._wait_for_search_page(page, image.code)

        candidates = self._find_candidates(page, image.code)
        if not candidates:
            self.report_writer.add(
                ReportRecord(
                    code=image.code,
                    status="not_found",
                    image_path=str(image.path),
                    expected_name=image.catalog_name,
                    product_url=search_url,
                )
            )
            return

        matched_any = False
        for candidate in candidates:
            self._check_stop()
            self._set_status(f"Открывает товар: {image.code}", candidate.url)
            self._safe_goto(page, candidate.url)
            self._wait_for_product_editor(page)
            self._set_status(f"Проверяет карточку товара: {image.code}", page.url)

            actual_code = self._read_first_value(page, self.selectors.code_fields)
            if actual_code and self._normalize_card_code(actual_code) != image.code:
                continue

            actual_name = self._read_first_value(page, self.selectors.name_fields) or candidate.text
            if actual_name and not names_match(image.catalog_name, actual_name):
                self._set_status(f"Название не совпало, пропускает: {image.code}", page.url)
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="name_mismatch",
                        image_path=str(image.path),
                        product_url=page.url,
                        expected_name=image.catalog_name,
                        actual_name=actual_name,
                    )
                )
                continue
            if not actual_name:
                actual_name = "Название не удалось прочитать"

            matched_any = True
            existing_image_count = self._existing_image_count(page)
            if existing_image_count > 1:
                self._set_status(f"Несколько фото, пропускает без замены: {image.code}", page.url)
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="skipped_existing_image",
                        image_path=str(image.path),
                        product_url=page.url,
                        expected_name=image.catalog_name,
                        actual_name=actual_name,
                        message="В карточке больше одного изображения. Автоматическая замена отключена.",
                    )
                )
                continue

            if existing_image_count == 1:
                item = ExistingImageItem(
                    code=image.code,
                    image_path=image.path,
                    product_url=page.url,
                    expected_name=image.catalog_name,
                    actual_name=actual_name,
                )
                self.existing_images.append(item)
                self._set_status(f"Фото уже есть: {image.code}", page.url)
                continue

            self._set_status(f"Загружает изображение: {image.code}", page.url)
            try:
                self._upload_image(page, image.path)
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="uploaded",
                        image_path=str(image.path),
                        product_url=page.url,
                        expected_name=image.catalog_name,
                        actual_name=actual_name,
                    )
                )
            except Exception as exc:
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="upload_error",
                        image_path=str(image.path),
                        product_url=page.url,
                        expected_name=image.catalog_name,
                        actual_name=actual_name,
                        message=str(exc),
                    )
                )

        if not matched_any:
            self.report_writer.add(
                ReportRecord(
                    code=image.code,
                    status="not_found",
                    image_path=str(image.path),
                    expected_name=image.catalog_name,
                    product_url=search_url,
                    message="Не найдена карточка с точным артикулом и подходящим названием.",
                )
            )

    def _wait_for_authorization(self, page: Page) -> None:
        self._set_status("Ожидает авторизацию. Войдите в кабинет и нажмите 'Я авторизован'.", PRODUCTS_URL)
        self._safe_goto(page, PRODUCTS_URL)
        if self.authorization_event is None:
            self._set_status("Авторизация пропущена: кнопка подтверждения не настроена.", page.url)
            return
        while not self.authorization_event.is_set():
            self._check_stop()
            page.wait_for_timeout(500)
        self._set_status("Авторизация подтверждена. Начинает обработку товаров.", page.url)

    def _find_candidates(self, page: Page, code: str) -> list[ProductCandidate]:
        candidates: dict[str, ProductCandidate] = {}
        for selector_template in self.selectors.product_links:
            selector = selector_template.format(code=code)
            for link in page.locator(selector).all():
                try:
                    text = link.inner_text(timeout=1000)
                    href = link.get_attribute("href", timeout=1000)
                except PlaywrightTimeoutError:
                    continue
                if not href:
                    continue
                url = urljoin("https://my.satu.kz", href)
                if code in text or selector_template.startswith("a[href"):
                    candidates[url] = ProductCandidate(url=url, text=text)
        return list(candidates.values())

    def _safe_goto(self, page: Page, url: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            self._set_status("Страница долго загружается, продолжаю по доступному DOM...", url)
            try:
                page.wait_for_load_state("commit", timeout=3000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(300)
        self._wait_until_dom_ready(page)

    def _wait_until_dom_ready(self, page: Page, timeout_ms: int = 5000) -> None:
        try:
            page.wait_for_function(
                "() => document.readyState === 'interactive' || document.readyState === 'complete'",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass

    def _wait_for_search_page(self, page: Page, code: str) -> None:
        self._wait_until_dom_ready(page, timeout_ms=4000)
        selectors = [template.format(code=code) for template in self.selectors.product_links]
        self._wait_for_any_selector(page, selectors, timeout_ms=2500)

    def _wait_for_product_editor(self, page: Page) -> None:
        self._wait_until_dom_ready(page, timeout_ms=4000)
        self._wait_for_any_selector(
            page,
            (
                "input[type='file'][data-qaid='upload_images']",
                "input[type='file'][data-qaid='upload_file']",
                "[data-qaid='image_item']",
                "button[type='button'][data-qaid='save_btn']",
            ),
            timeout_ms=5000,
        )

    def _wait_for_any_selector(self, page: Page, selectors: Iterable[str], timeout_ms: int) -> bool:
        deadline = page.evaluate("Date.now()") + timeout_ms
        while page.evaluate("Date.now()") < deadline:
            self._check_stop()
            for selector in selectors:
                try:
                    if page.locator(selector).count() > 0:
                        return True
                except Exception:
                    continue
            page.wait_for_timeout(100)
        return False

    def _read_first_value(self, page: Page, selectors: Iterable[str]) -> str:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                value = locator.input_value(timeout=1000)
                if value:
                    return value.strip()
            except Exception:
                try:
                    text = locator.inner_text(timeout=1000)
                    if text:
                        return text.strip()
                except Exception:
                    continue
        return ""

    def _normalize_card_code(self, value: str) -> str:
        return value.strip().replace(" ", "")

    def _existing_image_count(self, page: Page) -> int:
        image_items = page.locator("[data-qaid='image_item']")
        try:
            count = image_items.count()
            if count > 0:
                return count
        except Exception:
            pass

        count = 0
        for selector in self.selectors.image_blocks:
            locator = page.locator(selector)
            try:
                images = locator.locator("img")
                for index in range(images.count()):
                    src = images.nth(index).get_attribute("src", timeout=1000) or ""
                    if self._looks_like_product_image(src):
                        count += 1
            except Exception:
                continue
        return count

    def _looks_like_product_image(self, src: str) -> bool:
        lowered = src.lower()
        if not lowered:
            return False
        ignored_fragments = ("placeholder", "no-image", "empty", "spinner", "loader", ".svg")
        if any(fragment in lowered for fragment in ignored_fragments):
            return False
        return lowered.startswith(("http://", "https://", "blob:", "data:image/"))

    def _upload_image(self, page: Page, image_path: Path) -> None:
        direct_input = page.locator("input[type='file'][data-qaid='upload_images']").first
        try:
            if direct_input.count() > 0:
                self._dismiss_existing_alerts(page)
                direct_input.set_input_files(str(image_path), timeout=10000)
                self._wait_after_file_pick(page)
                self._save_product(page)
                return
        except Exception:
            pass

        direct_input = page.locator("input[type='file'][data-qaid='upload_file']").first
        try:
            if direct_input.count() > 0:
                self._dismiss_existing_alerts(page)
                direct_input.set_input_files(str(image_path), timeout=10000)
                self._wait_after_file_pick(page)
                self._save_product(page)
                return
        except Exception:
            pass

        for selector in self.selectors.file_inputs:
            locator = page.locator(selector).first
            try:
                if locator.count() > 0:
                    locator.set_input_files(str(image_path), timeout=5000)
                    self._wait_after_file_pick(page)
                    self._save_product(page)
                    return
            except Exception:
                continue

        for selector in self.selectors.upload_buttons:
            try:
                button = page.locator(selector).first
                if button.count() == 0:
                    continue
                self._dismiss_existing_alerts(page)
                with page.expect_file_chooser(timeout=5000) as chooser_info:
                    button.click()
                chooser_info.value.set_files(str(image_path))
                self._wait_after_file_pick(page)
                self._save_product(page)
                return
            except Exception:
                continue

        raise RuntimeError("Не найдено поле загрузки изображения на странице товара.")

    def _wait_after_file_pick(self, page: Page) -> None:
        try:
            page.wait_for_function(
                """() => {
                    return [...document.querySelectorAll('[data-qaid="alert_text"]')]
                        .some((item) => item.textContent && item.textContent.includes('изображение успешно добавлено'));
                }""",
                timeout=20000,
            )
        except Exception:
            raise RuntimeError("Изображение не подтвердило загрузку: не появилось уведомление 'изображение успешно добавлено'.")

    def _remove_existing_images(self, page: Page) -> None:
        for selector in self.selectors.delete_image_buttons:
            buttons = page.locator(selector)
            try:
                count = buttons.count()
            except Exception:
                continue
            for index in range(count):
                try:
                    buttons.nth(index).click(timeout=2000)
                    self._accept_dialog_if_visible(page)
                except Exception:
                    continue

    def _save_product(self, page: Page) -> None:
        direct_save = page.locator("button[type='button'][data-qaid='save_btn']").first
        try:
            if direct_save.count() > 0:
                self._click_save_once(page, direct_save)
                return
        except Exception:
            raise

        for selector in self.selectors.save_buttons:
            button = page.locator(selector).first
            try:
                if button.count() == 0:
                    continue
                self._click_save_once(page, button)
                return
            except Exception:
                continue
        raise RuntimeError("Не найдена кнопка сохранения товара: data-qaid='save_btn'.")

    def _click_save_once(self, page: Page, button) -> None:
        self._dismiss_existing_alerts(page)
        button.click(timeout=8000)
        self._wait_after_save_click(page)

    def _dismiss_existing_alerts(self, page: Page) -> None:
        close_buttons = page.locator("#alerts .b-alert__close")
        try:
            count = close_buttons.count()
        except Exception:
            return
        for index in range(count):
            try:
                close_buttons.nth(index).click(timeout=500)
            except Exception:
                continue

    def _wait_after_save_click(self, page: Page) -> None:
        try:
            page.wait_for_function(
                """() => {
                    return [...document.querySelectorAll('[data-qaid="alert_text"]')]
                        .some((item) => item.textContent && item.textContent.includes('Позиция сохранена успешно'));
                }""",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            pass

    def _accept_dialog_if_visible(self, page: Page) -> None:
        try:
            page.on("dialog", lambda dialog: dialog.accept())
        except Exception:
            pass

    def _wait_for_page(self, page: Page) -> None:
        self._wait_until_dom_ready(page, timeout_ms=5000)

    def _check_stop(self) -> None:
        if self.stop_requested:
            self.report_writer.add(ReportRecord(code="", status="stopped", message="Процесс остановлен пользователем."))
            raise StopRequested()

    def _set_status(self, status: str, url: str = "") -> None:
        self.status_callback(status, url)
