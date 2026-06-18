from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from threading import Event
import time
from typing import Callable, Iterable
from urllib.parse import urljoin
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from catalog import ImageItem, code_lookup_keys, names_match
from reports import ReportRecord, ReportWriter


BASE_SEARCH_URL = "https://my.satu.kz/cms/product?page=1&per_page=100&search_term="
PRODUCTS_URL = "https://my.satu.kz/cms/product?page=1&per_page=100&search_term="


@dataclass(frozen=True)
class ExistingImageItem:
    code: str
    image_path: Path | None
    product_url: str
    expected_name: str
    actual_name: str
    price: str | None = None
    wholesale_price: str | None = None
    sko_price: str | None = None
    name_ru: str | None = None
    name_kz: str | None = None
    availability: str | None = None


@dataclass(frozen=True)
class DeferredUploadItem:
    code: str
    image_path: Path | None
    product_url: str
    expected_name: str
    actual_name: str
    message: str
    price: str | None = None
    wholesale_price: str | None = None
    sko_price: str | None = None
    name_ru: str | None = None
    name_kz: str | None = None
    availability: str | None = None
    image_upload_required: bool = True


@dataclass(frozen=True)
class ProductCandidate:
    url: str
    text: str
    code: str = ""
    price: str = ""
    availability: str = ""
    has_image: bool = False


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
        "input[data-qaid='sku_input']",
        "[data-qaid='sku_input']",
        "input[data-qa*='sku']",
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
        "span[data-qaid='delete_img_btn']",
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
        headless: bool = False,
        update_names: bool = True,
        update_main_price: bool = True,
        update_discount_system: bool = True,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.report_writer = report_writer
        self.status_callback = status_callback or (lambda _status, _url="": None)
        self.selectors = selectors or Selectors()
        self.authorization_event = authorization_event
        self.headless = headless
        self.update_names = update_names
        self.update_main_price = update_main_price
        self.update_discount_system = update_discount_system
        self.stop_requested = False
        self.existing_images: list[ExistingImageItem] = []
        self.deferred_uploads: list[DeferredUploadItem] = []

    def _trace_step(self, page: Page | None, message: str) -> None:
        current_url = ""
        if page is not None:
            try:
                current_url = page.url
            except Exception:
                current_url = ""
        self._set_status(f"[step] {message}", current_url)

    def stop(self) -> None:
        self.stop_requested = True

    def run(self, images: Iterable[ImageItem]) -> list[ExistingImageItem]:
        self.stop_requested = False
        image_list = list(images)
        total_images = len(image_list)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                accept_downloads=True,
                ignore_https_errors=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                self._wait_for_authorization(page)
                for index, image in enumerate(image_list, start=1):
                    self._check_stop()
                    self._process_image(page, image)
                    self._set_status(
                        "__MAIN_PROGRESS__",
                        json.dumps(
                            {
                                "processed": index,
                                "total": total_images,
                                "code": image.code,
                                "path": self._image_path_text(image.path),
                            },
                            ensure_ascii=False,
                        ),
                    )
            finally:
                self.report_writer.save()
                context.close()
        return self.existing_images

    def replace_existing(self, items: Iterable[ExistingImageItem]) -> None:
        self.stop_requested = False
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                ignore_https_errors=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                self._wait_for_authorization(page)
                for item in items:
                    self._check_stop()
                    self._set_status(f"Заменяет изображение: {item.code}", item.product_url)
                    self._safe_goto(page, item.product_url)
                    self._wait_for_product_editor(page)
                    self._remove_existing_images(page)
                    self._upload_image(
                        page,
                        item.image_path,
                        item.price,
                        item.wholesale_price,
                        item.sko_price,
                        item.name_ru,
                        item.name_kz,
                        item.availability,
                    )
                    self.report_writer.add(
                        ReportRecord(
                            code=item.code,
                            status="replaced_existing_image",
                            image_path=self._image_path_text(item.image_path),
                            product_url=item.product_url,
                            expected_name=item.expected_name,
                            actual_name=item.actual_name,
                        )
                    )
            finally:
                self.report_writer.save()
                context.close()

    def replace_direct_links(self, product_urls: Iterable[str], items_by_code: dict[str, ImageItem]) -> None:
        self.stop_requested = False
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        urls = [url.strip() for url in product_urls if url and url.strip()]
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                ignore_https_errors=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                self._wait_for_authorization(page)
                for index, url in enumerate(urls, start=1):
                    self._check_stop()
                    progress_code = url
                    progress_path = ""
                    product_url = urljoin("https://my.satu.kz", url)
                    try:
                        self._set_status(f"Открывает товар по ссылке: {product_url}", product_url)
                        self._safe_goto(page, product_url)
                        self._wait_for_product_editor(page)
                        actual_code = self._normalize_card_code(self._read_first_value(page, self.selectors.code_fields))
                        actual_name = self._read_first_value(page, self.selectors.name_fields)
                        progress_code = actual_code or url
                        if not actual_code:
                            self.report_writer.add(
                                ReportRecord(
                                    code="",
                                    status="direct_link_code_missing",
                                    product_url=page.url,
                                    actual_name=actual_name,
                                    message="Не удалось прочитать артикул в карточке товара.",
                                )
                            )
                            continue
                        image = self._direct_image_item_for_code(actual_code, items_by_code)
                        if image is None:
                            self._set_status(f"Артикул из карточки не найден в Excel: {actual_code}", page.url)
                            self.report_writer.add(
                                ReportRecord(
                                    code=actual_code,
                                    status="direct_link_not_in_catalog",
                                    product_url=page.url,
                                    actual_name=actual_name,
                                    message="Артикул из карточки не найден в Excel-файле.",
                                )
                            )
                            continue
                        progress_path = self._image_path_text(image.path)
                        if image.path is None:
                            self._set_status(f"В папке нет изображения для артикула: {image.code}", page.url)
                            self.report_writer.add(
                                ReportRecord(
                                    code=image.code,
                                    status="direct_link_image_missing",
                                    product_url=page.url,
                                    expected_name=image.catalog_name,
                                    actual_name=actual_name,
                                    message="Для артикула из карточки не найден файл изображения в выбранной папке.",
                                )
                            )
                            continue
                        self._set_status(f"Перезаписывает изображение по ссылке: {image.code}", page.url)
                        try:
                            self._remove_existing_images(page)
                            self._upload_image(
                                page,
                                image.path,
                                image.price,
                                image.wholesale_price,
                                image.sko_price,
                                image.name_ru,
                                image.name_kz,
                                image.availability,
                            )
                            self.report_writer.add(
                                ReportRecord(
                                    code=image.code,
                                    status="direct_link_replaced_image",
                                    image_path=self._image_path_text(image.path),
                                    product_url=page.url,
                                    expected_name=image.catalog_name,
                                    actual_name=actual_name,
                                )
                            )
                        except Exception as exc:
                            message = str(exc)
                            self.deferred_uploads.append(
                                DeferredUploadItem(
                                    code=image.code,
                                    image_path=image.path,
                                    product_url=page.url,
                                    expected_name=image.catalog_name or "",
                                    actual_name=actual_name,
                                    message=message,
                                    price=image.price,
                                    wholesale_price=image.wholesale_price,
                                    sko_price=image.sko_price,
                                    name_ru=image.name_ru,
                                    name_kz=image.name_kz,
                                    availability=image.availability,
                                )
                            )
                            self.report_writer.add(
                                ReportRecord(
                                    code=image.code,
                                    status="direct_link_deferred_upload_error",
                                    image_path=self._image_path_text(image.path),
                                    product_url=page.url,
                                    expected_name=image.catalog_name,
                                    actual_name=actual_name,
                                    message=message,
                                )
                            )
                    except StopRequested:
                        raise
                    except Exception as exc:
                        self.report_writer.add(
                            ReportRecord(
                                code="" if progress_code == url else progress_code,
                                status="direct_link_error",
                                product_url=product_url,
                                message=str(exc),
                            )
                        )
                    finally:
                        self._set_status(
                            "__MAIN_PROGRESS__",
                            json.dumps(
                                {
                                    "processed": index,
                                    "total": len(urls),
                                    "code": progress_code,
                                    "path": progress_path,
                                },
                                ensure_ascii=False,
                            ),
                        )
            finally:
                self.report_writer.save()
                context.close()

    def _direct_image_item_for_code(self, code: str, items_by_code: dict[str, ImageItem]) -> ImageItem | None:
        for key in code_lookup_keys(code):
            image = items_by_code.get(key)
            if image is not None:
                return image
        target = (str(code).strip().lstrip("0") or "0")
        if not target:
            return None
        for item_code, image in items_by_code.items():
            if item_code.isdigit() and (item_code.lstrip("0") or "0") == target:
                return image
        return None

    def retry_deferred_upload(self, item: DeferredUploadItem) -> None:
        self.stop_requested = False
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                ignore_https_errors=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                if item.image_upload_required:
                    self._set_status(f"Повторная загрузка изображения: {item.code}", item.product_url)
                else:
                    self._set_status(f"Повторная синхронизация товара: {item.code}", item.product_url)
                self._safe_goto(page, item.product_url)
                self._wait_for_product_editor(page)
                if item.image_upload_required:
                    if item.image_path is None:
                        raise RuntimeError("Для повторной загрузки изображения не найден файл изображения.")
                    self._upload_image(
                        page,
                        item.image_path,
                        item.price,
                        item.wholesale_price,
                        item.sko_price,
                        item.name_ru,
                        item.name_kz,
                        item.availability,
                    )
                    status = "uploaded_after_manual_fix"
                else:
                    self._sync_product_fields(
                        page,
                        item.price,
                        item.wholesale_price,
                        item.sko_price,
                        item.name_ru,
                        item.name_kz,
                        item.availability,
                    )
                    status = "synced_after_manual_fix"
                self.report_writer.add(
                    ReportRecord(
                        code=item.code,
                        status=status,
                        image_path=self._image_path_text(item.image_path),
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
                    image_path=self._image_path_text(item.image_path),
                    product_url=item.product_url,
                    expected_name=item.expected_name,
                    actual_name=item.actual_name,
                )
            )

    def _process_image(self, page: Page, image: ImageItem) -> None:
        if not image.code:
            self.report_writer.add(ReportRecord(code="", status="invalid_file", image_path=self._image_path_text(image.path)))
            return
        if not image.catalog_name:
            self.report_writer.add(
                ReportRecord(
                    code=image.code,
                    status="invalid_file",
                    image_path=self._image_path_text(image.path),
                    message="В Excel не найдено название для артикула.",
                )
            )
            return

        search_url = f"{BASE_SEARCH_URL}{quote(image.code)}"
        self._set_status(f"Ищет товар: {image.code}", search_url)
        self._safe_goto(page, search_url)
        self._wait_for_search_page(page, image.code)

        candidates = self._find_candidates_across_pages(page, image.code)
        if not candidates:
            self.report_writer.add(
                ReportRecord(
                    code=image.code,
                    status="not_found",
                    image_path=self._image_path_text(image.path),
                    expected_name=image.catalog_name,
                    product_url=search_url,
                )
            )
            return

        expected_name = self._expected_name_for_sync(image)
        matched_any = False
        for candidate in candidates:
            self._check_stop()
            if not self._candidate_name_matches(image, candidate):
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="name_mismatch",
                        image_path=self._image_path_text(image.path),
                        product_url=candidate.url,
                        expected_name=expected_name or image.catalog_name,
                        actual_name=candidate.text,
                    )
                )
                continue

            matched_any = True
            if self._candidate_has_existing_image_for_replacement(image, candidate):
                item = ExistingImageItem(
                    code=image.code,
                    image_path=image.path,
                    product_url=candidate.url,
                    expected_name=image.catalog_name,
                    actual_name=candidate.text,
                    price=image.price,
                    wholesale_price=image.wholesale_price,
                    sko_price=image.sko_price,
                    name_ru=image.name_ru,
                    name_kz=image.name_kz,
                    availability=image.availability,
                )
                self.existing_images.append(item)
                self._set_status(f"Фото уже есть: {image.code}", candidate.url)
                continue

            if self._candidate_is_actual(image, candidate):
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="skipped_actual_list_row",
                        image_path=self._image_path_text(image.path),
                        product_url=candidate.url,
                        expected_name=image.catalog_name,
                        actual_name=candidate.text,
                        message="В строке списка уже есть изображение, актуальная цена и актуальное наличие.",
                    )
                )
                continue

            self._set_status(f"Открывает товар: {image.code}", candidate.url)
            self._safe_goto(page, candidate.url)
            self._wait_for_product_editor(page)
            self._set_status(f"Проверяет карточку товара: {image.code}", page.url)

            actual_code = self._read_first_value(page, self.selectors.code_fields)
            if actual_code and not self._codes_match(self._normalize_card_code(actual_code), image.code):
                continue

            actual_name = self._read_first_value(page, self.selectors.name_fields) or candidate.text
            allow_name_sync = self.update_names and image.name_ru is not None and str(image.name_ru).strip() != ""
            if actual_name and expected_name and not names_match(expected_name, actual_name) and not allow_name_sync:
                self._set_status(f"Название не совпало, пропускает: {image.code}", page.url)
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="name_mismatch",
                        image_path=self._image_path_text(image.path),
                        product_url=page.url,
                        expected_name=image.catalog_name,
                        actual_name=actual_name,
                    )
                )
                continue
            if not actual_name:
                actual_name = "Название не удалось прочитать"

            existing_image_count = self._existing_image_count(page)
            if not self._sync_matched_product_fields(page, image, actual_name, existing_image_count):
                continue

            if image.path is None:
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="synced_without_image",
                        product_url=page.url,
                        expected_name=image.catalog_name,
                        actual_name=actual_name,
                        message="В папке нет изображения для артикула. Цена и наличие обработаны, установка изображения пропущена.",
                    )
                )
                continue

            if existing_image_count > 1:
                self._set_status(f"Несколько фото, пропускает без замены: {image.code}", page.url)
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="skipped_existing_image",
                        image_path=self._image_path_text(image.path),
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
                    price=image.price,
                    wholesale_price=image.wholesale_price,
                    sko_price=image.sko_price,
                    name_ru=image.name_ru,
                    name_kz=image.name_kz,
                    availability=image.availability,
                )
                self.existing_images.append(item)
                self._set_status(f"Фото уже есть: {image.code}", page.url)
                continue

            self._set_status(f"Загружает изображение: {image.code}", page.url)
            try:
                self._upload_image(
                    page,
                    image.path,
                    image.price,
                    image.wholesale_price,
                    image.sko_price,
                    image.name_ru,
                    image.name_kz,
                    image.availability,
                )
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="uploaded",
                        image_path=self._image_path_text(image.path),
                        product_url=page.url,
                        expected_name=image.catalog_name,
                        actual_name=actual_name,
                    )
                )
            except Exception as exc:
                message = str(exc)
                self.deferred_uploads.append(
                    DeferredUploadItem(
                        code=image.code,
                        image_path=image.path,
                        product_url=page.url,
                        expected_name=image.catalog_name,
                        actual_name=actual_name,
                        message=message,
                        price=image.price,
                        wholesale_price=image.wholesale_price,
                        sko_price=image.sko_price,
                        name_ru=image.name_ru,
                        name_kz=image.name_kz,
                        availability=image.availability,
                    )
                )
                self.report_writer.add(
                    ReportRecord(
                        code=image.code,
                        status="deferred_upload_error",
                        image_path=self._image_path_text(image.path),
                        product_url=page.url,
                        expected_name=image.catalog_name,
                        actual_name=actual_name,
                        message=message,
                    )
                )

        if not matched_any:
            self.report_writer.add(
                ReportRecord(
                    code=image.code,
                    status="not_found",
                    image_path=self._image_path_text(image.path),
                    expected_name=image.catalog_name,
                    product_url=search_url,
                    message="Не найдена карточка с точным артикулом и подходящим названием.",
                )
            )

    def _sync_matched_product_fields(
        self,
        page: Page,
        image: ImageItem,
        actual_name: str,
        existing_image_count: int,
    ) -> bool:
        if not self._has_product_fields_to_sync(image):
            return True
        try:
            self._set_status(f"Обновляет цену и наличие товара: {image.code}", page.url)
            self._sync_product_fields(
                page,
                image.price,
                image.wholesale_price,
                image.sko_price,
                image.name_ru,
                image.name_kz,
                image.availability,
                force_save=True,
            )
        except Exception as exc:
            message = str(exc)
            self.deferred_uploads.append(
                DeferredUploadItem(
                    code=image.code,
                    image_path=image.path,
                    product_url=page.url,
                    expected_name=image.catalog_name or "",
                    actual_name=actual_name,
                    message=message,
                    price=image.price,
                    wholesale_price=image.wholesale_price,
                    sko_price=image.sko_price,
                    name_ru=image.name_ru,
                    name_kz=image.name_kz,
                    availability=image.availability,
                    image_upload_required=image.path is not None and existing_image_count == 0,
                )
            )
            self.report_writer.add(
                ReportRecord(
                    code=image.code,
                    status="field_sync_error",
                    image_path=self._image_path_text(image.path),
                    product_url=page.url,
                    expected_name=image.catalog_name,
                    actual_name=actual_name,
                    message=message,
                )
            )
            return False
        self.report_writer.add(
            ReportRecord(
                code=image.code,
                status="fields_synced",
                image_path=self._image_path_text(image.path),
                product_url=page.url,
                expected_name=image.catalog_name,
                actual_name=actual_name,
            )
        )
        return True

    def _image_path_text(self, image_path: Path | None) -> str:
        return str(image_path) if image_path is not None else ""

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

    def _find_candidates_across_pages(self, page: Page, code: str) -> list[ProductCandidate]:
        candidates: dict[str, ProductCandidate] = {}
        for page_number in range(1, 21):
            if page_number > 1:
                search_url = f"https://my.satu.kz/cms/product?page={page_number}&per_page=100&search_term={quote(code)}"
                self._safe_goto(page, search_url)
                self._wait_for_search_page(page, code)

            page_candidates = self._find_candidates(page, code)
            before_count = len(candidates)
            for candidate in page_candidates:
                key = f"{candidate.url}|{candidate.code}|{candidate.text}|{candidate.price}|{candidate.availability}"
                candidates[key] = candidate
            has_next_page = self._has_next_search_page(page, page_number)
            if not has_next_page and len(candidates) == before_count:
                break
            if not has_next_page:
                break
        return list(candidates.values())

    def _find_candidates(self, page: Page, code: str) -> list[ProductCandidate]:
        row_candidates = self._find_row_candidates(page, code)
        if row_candidates:
            return row_candidates

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

    def _find_row_candidates(self, page: Page, code: str) -> list[ProductCandidate]:
        try:
            rows = page.evaluate(
                """(targetCode) => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const normalizeCode = (value) => normalize(value).replace(/\\s+/g, '');
                    const looksLikeImage = (src) => {
                        const lowered = String(src || '').toLowerCase();
                        if (!lowered) return false;
                        const ignored = ['placeholder', 'no-image', 'empty', 'spinner', 'loader', '.svg'];
                        return !ignored.some((fragment) => lowered.includes(fragment));
                    };
                    const result = [];
                    const productRows = [...document.querySelectorAll('[data-qaid="product_row"], .b-common-table__row.b-product')]
                        .filter((row) => row.querySelector('[data-qaid="product_sku"]'));
                    for (const row of productRows) {
                        const rowCode = normalizeCode(row.querySelector('[data-qaid="product_sku"]')?.textContent);
                        if (rowCode !== targetCode) continue;
                        const nameLink = row.querySelector('[data-qaid="product_name"] [data-qaid="value_text_field"], [data-qaid="product_name"] a[href*="/cms/product/"]');
                        const link = row.querySelector('[data-qaid="product_name"] a[href*="/cms/product/"], .b-product__image a[href*="/cms/product/"], a[href*="/cms/product/edit/"]');
                        const href = link ? link.getAttribute('href') : '';
                        const image = row.querySelector('.b-product__image img');
                        result.push({
                            url: href ? new URL(href, 'https://my.satu.kz').href : '',
                            text: normalize(nameLink ? nameLink.textContent : row.textContent),
                            code: rowCode,
                            price: normalize(row.querySelector('[data-qaid="product_price_block"]')?.textContent),
                            availability: normalize(row.querySelector('[data-qaid="product_presence_text"]')?.textContent),
                            has_image: Boolean(image && looksLikeImage(image.getAttribute('src'))),
                        });
                    }
                    return result;
                }""",
                code,
            )
        except Exception:
            return []

        candidates: list[ProductCandidate] = []
        if not isinstance(rows, list):
            return candidates
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "")
            if not url:
                continue
            candidates.append(
                ProductCandidate(
                    url=url,
                    text=str(row.get("text") or ""),
                    code=str(row.get("code") or ""),
                    price=str(row.get("price") or ""),
                    availability=str(row.get("availability") or ""),
                    has_image=bool(row.get("has_image")),
                )
            )
        return candidates

    def _has_next_search_page(self, page: Page, current_page: int) -> bool:
        try:
            return bool(
                page.evaluate(
                    """(currentPage) => {
                        const isDisabled = (item) => {
                            const className = String(item.className || '');
                            return Boolean(
                                item.disabled
                                || item.getAttribute?.('aria-disabled') === 'true'
                                || className.includes('disabled')
                                || className.includes('Disabled')
                            );
                        };
                        const pagination = document.querySelector('[data-qaid="pagination"]') || document;
                        const nextButton = pagination.querySelector('[data-qaid="next_page_btn"]');
                        if (nextButton && !isDisabled(nextButton)) {
                            return true;
                        }
                        const nextPageText = String(currentPage + 1);
                        const pageLinks = [...pagination.querySelectorAll('[data-qaid="page_number"]')];
                        if (pageLinks.some((item) => !isDisabled(item) && String(item.textContent || '').trim() === nextPageText)) {
                            return true;
                        }
                        const links = [...pagination.querySelectorAll('a[href*="page="], button, [role="button"], a')];
                        return links.some((item) => {
                            const text = String(item.textContent || '').replace(/\\s+/g, ' ').trim();
                            const href = item.getAttribute ? String(item.getAttribute('href') || '') : '';
                            if (isDisabled(item)) return false;
                            if (text === nextPageText) return true;
                            if (/следующая/i.test(text)) return true;
                            return href.includes(`page=${currentPage + 1}`);
                        });
                    }""",
                    current_page,
                )
            )
        except Exception:
            return False

    def _candidate_name_matches(self, image: ImageItem, candidate: ProductCandidate) -> bool:
        if self.update_names and image.name_ru is not None and str(image.name_ru).strip() != "":
            return True
        if not image.catalog_name or not candidate.text:
            return True
        return names_match(image.catalog_name, candidate.text)

    def _expected_name_for_sync(self, image: ImageItem) -> str | None:
        if self.update_names and image.name_ru is not None and str(image.name_ru).strip() != "":
            return str(image.name_ru).strip()
        if image.catalog_name:
            return str(image.catalog_name).strip()
        return None

    def _requires_card_open_for_name_sync(self, image: ImageItem, candidate: ProductCandidate | None = None) -> bool:
        if not self.update_names:
            return False
        has_name_updates = any(
            value is not None and str(value).strip() != ""
            for value in (image.name_ru, image.name_kz)
        )
        if not has_name_updates:
            return False
        if (
            candidate is not None
            and image.name_ru is not None
            and str(image.name_ru).strip() != ""
            and candidate.text
            and names_match(str(image.name_ru).strip(), candidate.text)
        ):
            return False
        return True

    def _candidate_is_actual(self, image: ImageItem, candidate: ProductCandidate) -> bool:
        if self.update_discount_system and any(
            value is not None and str(value).strip() != ""
            for value in (image.wholesale_price, image.sko_price)
        ):
            return False
        if self._requires_card_open_for_name_sync(image, candidate):
            return False
        image_ok = candidate.has_image or image.path is None
        price_ok = True if not self.update_main_price else self._candidate_price_matches(image.price, candidate.price)
        availability_ok = self._candidate_availability_matches(image.availability, candidate.availability)
        return image_ok and price_ok and availability_ok

    def _candidate_has_existing_image_for_replacement(self, image: ImageItem, candidate: ProductCandidate) -> bool:
        if self.update_discount_system and any(
            value is not None and str(value).strip() != ""
            for value in (image.wholesale_price, image.sko_price)
        ):
            return False
        if self._requires_card_open_for_name_sync(image, candidate):
            return False
        if image.path is None or not candidate.has_image:
            return False
        price_matches = True if not self.update_main_price else self._candidate_price_matches(image.price, candidate.price)
        return price_matches and self._candidate_availability_matches(
            image.availability,
            candidate.availability,
        )

    def _candidate_price_matches(self, expected: str | None, actual: str) -> bool:
        if expected is None or str(expected).strip() == "":
            return True
        return self._normalize_price_for_compare(str(expected)) == self._normalize_price_for_compare(actual)

    def _candidate_availability_matches(self, expected: str | None, actual: str) -> bool:
        expected_normalized = self._normalize_availability_for_compare(expected)
        if not expected_normalized:
            return True
        return expected_normalized == self._normalize_availability_for_compare(actual)

    def _normalize_price_for_compare(self, value: str) -> str:
        text = str(value).replace("\u00a0", " ").strip()
        text = "".join(char for char in text if char.isdigit() or char in ",.")
        if not text:
            return ""
        text = text.replace(",", ".")
        if text.endswith(".0") and text[:-2].isdigit():
            return text[:-2]
        return text

    def _normalize_availability_for_compare(self, value: str | None) -> str:
        text = "" if value is None else str(value).casefold()
        if not text:
            return ""
        if "нет" in text or "отсут" in text or "not" in text:
            return "Нет в наличии"
        if "заказ" in text or "order" in text:
            return "Под заказ"
        if "налич" in text or "available" in text or "есть" in text:
            return "В наличии"
        return ""

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

    def _is_navigation_context_error(self, exc: Exception) -> bool:
        message = str(exc).casefold()
        return (
            "execution context was destroyed" in message
            or "most likely because of a navigation" in message
            or "cannot find context with specified id" in message
        )

    def _wait_until_dom_ready(self, page: Page, timeout_ms: int = 5000) -> None:
        try:
            page.wait_for_function(
                "() => document.readyState === 'interactive' || document.readyState === 'complete'",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass
        except Exception as exc:
            if self._is_navigation_context_error(exc):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                except Exception:
                    pass
                return
            raise

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
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
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

    def _codes_match(self, left: str, right: str) -> bool:
        return bool(set(code_lookup_keys(left)) & set(code_lookup_keys(right)))

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

    def _upload_image(
        self,
        page: Page,
        image_path: Path,
        price: str | None = None,
        wholesale_price: str | None = None,
        sko_price: str | None = None,
        name_ru: str | None = None,
        name_kz: str | None = None,
        availability: str | None = None,
    ) -> None:
        direct_input = page.locator("input[type='file'][data-qaid='upload_images']").first
        try:
            has_direct_input = direct_input.count() > 0
        except Exception:
            has_direct_input = False
        if has_direct_input:
            self._dismiss_existing_alerts(page)
            direct_input.set_input_files(str(image_path), timeout=10000)
            self._wait_after_file_pick(page)
            self._sync_product_fields(page, price, wholesale_price, sko_price, name_ru, name_kz, availability, save=False)
            self._save_product(page)
            return

        direct_input = page.locator("input[type='file'][data-qaid='upload_file']").first
        try:
            has_direct_input = direct_input.count() > 0
        except Exception:
            has_direct_input = False
        if has_direct_input:
            self._dismiss_existing_alerts(page)
            direct_input.set_input_files(str(image_path), timeout=10000)
            self._wait_after_file_pick(page)
            self._sync_product_fields(page, price, wholesale_price, sko_price, name_ru, name_kz, availability, save=False)
            self._save_product(page)
            return

        for selector in self.selectors.file_inputs:
            locator = page.locator(selector).first
            try:
                has_input = locator.count() > 0
            except Exception:
                continue
            if not has_input:
                continue
            self._dismiss_existing_alerts(page)
            locator.set_input_files(str(image_path), timeout=5000)
            self._wait_after_file_pick(page)
            self._sync_product_fields(page, price, wholesale_price, sko_price, name_ru, name_kz, availability, save=False)
            self._save_product(page)
            return

        for selector in self.selectors.upload_buttons:
            try:
                button = page.locator(selector).first
                if button.count() == 0:
                    continue
                self._dismiss_existing_alerts(page)
                with page.expect_file_chooser(timeout=5000) as chooser_info:
                    button.click()
            except Exception:
                continue
            chooser_info.value.set_files(str(image_path))
            self._wait_after_file_pick(page)
            self._sync_product_fields(page, price, wholesale_price, sko_price, name_ru, name_kz, availability, save=False)
            self._save_product(page)
            return

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

    def _has_product_fields_to_sync(self, image: ImageItem) -> bool:
        return (
            self.update_main_price
            and image.price is not None
            and str(image.price).strip() != ""
        ) or (
            self.update_discount_system
            and image.wholesale_price is not None
            and str(image.wholesale_price).strip() != ""
        ) or (
            self.update_discount_system
            and image.sko_price is not None
            and str(image.sko_price).strip() != ""
        ) or (
            self.update_names
            and image.name_ru is not None
            and str(image.name_ru).strip() != ""
        ) or (
            self.update_names
            and image.name_kz is not None
            and str(image.name_kz).strip() != ""
        ) or (
            image.availability is not None
            and str(image.availability).strip() != ""
        )

    def _sync_product_fields(
        self,
        page: Page,
        price: str | None,
        wholesale_price: str | None,
        sko_price: str | None,
        name_ru: str | None,
        name_kz: str | None,
        availability: str | None,
        save: bool = True,
        force_save: bool = False,
    ) -> bool:
        changed = False
        self._trace_step(page, "sync start")
        changed = self._update_pricing(page, price, wholesale_price, sko_price) or changed
        changed = self._update_product_names(page, name_ru, name_kz) or changed
        changed = self._update_availability(page, availability) or changed
        if save and (changed or force_save):
            self._trace_step(page, "save requested")
            self._save_product(page)
            self._trace_step(page, "save completed")
        return changed or force_save

    def _update_product_names(self, page: Page, name_ru: str | None, name_kz: str | None) -> bool:
        if not self.update_names:
            return False
        changed = False
        if name_ru is not None and str(name_ru).strip() != "":
            self._trace_step(page, "updating RU name")
        changed = self._update_named_input(page, "input[data-qaid='product_name_ru']", name_ru, "русское название") or changed
        if name_kz is not None and str(name_kz).strip() != "":
            self._trace_step(page, "updating KZ name")
        changed = self._update_named_input(page, "input[data-qaid='product_name_kk']", name_kz, "казахское название") or changed
        if name_ru is not None or name_kz is not None:
            self._trace_step(page, f"names updated changed={changed}")
        return changed

    def _update_named_input(self, page: Page, selector: str, value: str | None, field_label: str) -> bool:
        if value is None or str(value).strip() == "":
            return False
        target_value = str(value).strip()
        input_locator = page.locator(selector).first
        try:
            if input_locator.count() == 0:
                raise RuntimeError(f"Не найдено поле, в которое нужно записать {field_label}.")
            current_value = input_locator.input_value(timeout=1000).strip()
            if current_value == target_value:
                return False
            input_locator.fill(target_value, timeout=5000)
            return True
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Не удалось обновить {field_label}: {exc}") from exc

    def _update_price(self, page: Page, price: str | None) -> bool:
        if price is None or str(price).strip() == "":
            return False
        target_price = str(price).strip()
        price_input = page.locator("input[data-qaid='product_price_input']").first
        try:
            if price_input.count() == 0:
                raise RuntimeError("Не найдено поле цены товара: data-qaid='product_price_input'.")
            current_price = price_input.input_value(timeout=1000).strip()
            if current_price == target_price:
                self._trace_step(page, f"retail price unchanged={target_price}")
                return False
            self._trace_step(page, f"filling retail price={target_price}")
            price_input.fill(target_price, timeout=5000)
            self._trace_step(page, "retail price filled")
            return True
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Не удалось заполнить цену товара: {exc}") from exc

    def _read_current_pricing_state(self, page: Page) -> dict[str, object]:
        mode = "retail"
        for candidate_mode in ("universal", "retail", "wholesale", "service"):
            locator = page.locator(f"input[name='presence'][data-qaid='{candidate_mode}']").first
            try:
                if locator.count() > 0 and bool(locator.evaluate("(node) => Boolean(node.checked)")):
                    mode = candidate_mode
                    break
            except Exception:
                continue

        retail_price = ""
        retail_input = page.locator("input[data-qaid='product_price_input']").first
        try:
            if retail_input.count() > 0:
                retail_price = retail_input.input_value(timeout=1000).strip()
        except Exception:
            retail_price = ""

        wholesale_tiers: list[tuple[str, str]] = []
        try:
            price_inputs = page.locator("input[data-qaid='wholesale_price_input']")
            qty_inputs = page.locator("input[data-qaid='wholesale_count_input']")
            tier_count = min(price_inputs.count(), qty_inputs.count())
            for index in range(tier_count):
                price_value = price_inputs.nth(index).input_value(timeout=1000).strip()
                qty_value = qty_inputs.nth(index).input_value(timeout=1000).strip()
                wholesale_tiers.append((price_value, qty_value))
        except Exception:
            wholesale_tiers = []

        return {
            "mode": mode,
            "retail_price": retail_price,
            "wholesale_tiers": wholesale_tiers,
        }

    def _update_pricing(
        self,
        page: Page,
        retail_price: str | None,
        wholesale_price: str | None,
        sko_price: str | None,
    ) -> bool:
        changed = False
        if not self.update_main_price and not self.update_discount_system:
            return False
        current_state = self._read_current_pricing_state(page)
        has_wholesale_tiers = all(
            value is not None and str(value).strip() != ""
            for value in (wholesale_price, sko_price)
        )
        same_as_retail = (
            has_wholesale_tiers
            and retail_price is not None
            and str(retail_price).strip() != ""
            and self._normalize_price_for_compare(str(wholesale_price)) == self._normalize_price_for_compare(str(retail_price))
            and self._normalize_price_for_compare(str(sko_price)) == self._normalize_price_for_compare(str(retail_price))
        )
        use_wholesale_tiers = self.update_discount_system and has_wholesale_tiers and not same_as_retail
        target_tiers = (
            [
                (str(wholesale_price).strip(), "100"),
                (str(sko_price).strip(), "1000"),
            ]
            if use_wholesale_tiers
            else []
        )
        if self.update_discount_system:
            target_mode = "universal" if use_wholesale_tiers else "retail"
            if same_as_retail:
                self._trace_step(page, "wholesale and sko prices equal retail; keeping retail mode")
            current_mode = str(current_state.get("mode") or "retail")
            current_tiers = list(current_state.get("wholesale_tiers") or [])
            tiers_complete = len(current_tiers) >= len(target_tiers)
            tiers_match = current_tiers[: len(target_tiers)] == target_tiers
            if current_mode != target_mode:
                self._trace_step(page, f"switching presence mode={target_mode}")
                changed = self._set_presence_mode(page, target_mode) or changed
                self._wait_for_presence_mode_ready(page, target_mode)
                self._trace_step(page, f"presence mode ready={target_mode}")
            elif use_wholesale_tiers and tiers_complete and tiers_match:
                self._trace_step(page, "discount system already актуальна")
            else:
                self._trace_step(page, f"presence mode unchanged={target_mode}")
        if self.update_main_price:
            changed = self._update_price(page, retail_price) or changed
        if use_wholesale_tiers:
            current_tiers = list(current_state.get("wholesale_tiers") or [])
            tiers_complete = len(current_tiers) >= len(target_tiers)
            tiers_match = current_tiers[: len(target_tiers)] == target_tiers
            if not tiers_complete or not tiers_match:
                self._trace_step(page, "updating wholesale tiers start")
                changed = self._update_wholesale_tiers(page, target_tiers) or changed
                self._trace_step(page, "updating wholesale tiers completed")
        return changed

    def _wait_for_presence_mode_ready(self, page: Page, mode: str) -> None:
        try:
            page.wait_for_function(
                """(targetMode) => {
                    const radio = document.querySelector(`input[name="presence"][data-qaid="${targetMode}"]`);
                    if (!radio || !radio.checked) return false;
                    const retailInput = document.querySelector('input[data-qaid="product_price_input"]');
                    if (!retailInput) return false;
                    if (targetMode !== 'universal') return true;
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const hasAddButton = [...document.querySelectorAll('a, button, div, span')]
                        .some((element) => isVisible(element) && normalize(element.textContent) === '\\u0434\\u043e\\u0431\\u0430\\u0432\\u0438\\u0442\\u044c \\u043e\\u043f\\u0442\\u043e\\u0432\\u0443\\u044e \\u0446\\u0435\\u043d\\u0443');
                    const hasWholesaleLabel = [...document.querySelectorAll('*')]
                        .some((element) => isVisible(element) && normalize(element.textContent).includes('\\u043e\\u043f\\u0442\\u043e\\u0432\\u0430\\u044f \\u0446\\u0435\\u043d\\u0430'));
                    return hasAddButton || hasWholesaleLabel;
                }""",
                arg=mode,
                timeout=8000,
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось дождаться готовности режима товара '{mode}': {exc}") from exc

    def _set_presence_mode(self, page: Page, mode: str) -> bool:
        input_locator = page.locator(f"input[name='presence'][data-qaid='{mode}']").first
        try:
            if input_locator.count() == 0:
                raise RuntimeError(f"Не найден переключатель режима товара: {mode}.")
            is_checked = bool(input_locator.evaluate("(node) => Boolean(node.checked)"))
            if is_checked:
                return False
            label_locator = page.locator(f"label:has(input[name='presence'][data-qaid='{mode}'])").first
            if label_locator.count() > 0:
                label_locator.click(timeout=5000)
            else:
                input_locator.check(force=True, timeout=5000)
            page.wait_for_function(
                """(targetMode) => {
                    const radio = document.querySelector(`input[name="presence"][data-qaid="${targetMode}"]`);
                    return Boolean(radio && radio.checked);
                }""",
                arg=mode,
                timeout=5000,
            )
            page.wait_for_timeout(500)
            return True
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Не удалось переключить режим товара на '{mode}': {exc}") from exc

    def _update_wholesale_tiers(self, page: Page, tiers: list[tuple[str, str]]) -> bool:
        if not tiers:
            return False
        self._ensure_wholesale_tier_count(page, len(tiers))
        changed = False
        for tier_index, (price, quantity) in enumerate(tiers):
            changed = self._set_wholesale_tier_values(page, tier_index, price, quantity) or changed
        return changed

    def _wait_for_wholesale_tier_count(self, page: Page, min_count: int) -> None:
        try:
            page.wait_for_function(
                """(expectedCount) => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const eligibleInputs = (root) => [...root.querySelectorAll('input')]
                        .filter((input) => {
                            if (!isVisible(input)) return false;
                            const type = (input.getAttribute('type') || 'text').toLowerCase();
                            return !['hidden', 'radio', 'checkbox', 'file'].includes(type);
                        });
                    const labels = [...document.querySelectorAll('*')].filter(
                        (element) => isVisible(element) && normalize(element.textContent) === 'оптовая цена'
                    );
                    const containers = [];
                    for (const label of labels) {
                        let current = label;
                        while (current && current !== document.body) {
                            const text = normalize(current.textContent);
                            const inputs = eligibleInputs(current);
                            if (text.includes('оптовая цена') && text.includes('при заказе') && inputs.length >= 2) {
                                if (!containers.includes(current)) containers.push(current);
                                break;
                            }
                            current = current.parentElement;
                        }
                    }
                    return containers.length >= expectedCount;
                }""",
                arg=min_count,
                timeout=5000,
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось дождаться блоков оптовых цен: {exc}") from exc

    def _ensure_wholesale_tier_count(self, page: Page, expected_count: int) -> None:
        while True:
            current_count = int(page.evaluate(
                """() => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const eligibleInputs = (root) => [...root.querySelectorAll('input')]
                        .filter((input) => {
                            if (!isVisible(input)) return false;
                            const type = (input.getAttribute('type') || 'text').toLowerCase();
                            return !['hidden', 'radio', 'checkbox', 'file'].includes(type);
                        });
                    const labels = [...document.querySelectorAll('*')].filter(
                        (element) => isVisible(element) && normalize(element.textContent) === 'оптовая цена'
                    );
                    const containers = [];
                    for (const label of labels) {
                        let current = label;
                        while (current && current !== document.body) {
                            const text = normalize(current.textContent);
                            const inputs = eligibleInputs(current);
                            if (text.includes('оптовая цена') && text.includes('при заказе') && inputs.length >= 2) {
                                if (!containers.includes(current)) containers.push(current);
                                break;
                            }
                            current = current.parentElement;
                        }
                    }
                    return containers.length;
                }"""
            ))
            if current_count >= expected_count:
                return

            clicked = bool(page.evaluate(
                """() => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const candidates = [...document.querySelectorAll('a, button, div, span')]
                        .filter((element) => isVisible(element) && normalize(element.textContent) === 'добавить оптовую цену');
                    const target = candidates[0];
                    if (!target) return false;
                    target.click();
                    return true;
                }"""
            ))
            if not clicked:
                raise RuntimeError("Не найдена кнопка 'Добавить оптовую цену'.")
            self._wait_for_wholesale_tier_count(page, expected_count)

    def _set_wholesale_tier_values(self, page: Page, tier_index: int, price: str, quantity: str) -> bool:
        try:
            result = page.evaluate(
                """({ tierIndex, priceValue, quantityValue }) => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const eligibleInputs = (root) => [...root.querySelectorAll('input')]
                        .filter((input) => {
                            if (!isVisible(input)) return false;
                            const type = (input.getAttribute('type') || 'text').toLowerCase();
                            return !['hidden', 'radio', 'checkbox', 'file'].includes(type);
                        });
                    const labels = [...document.querySelectorAll('*')].filter(
                        (element) => isVisible(element) && normalize(element.textContent) === 'оптовая цена'
                    );
                    const containers = [];
                    for (const label of labels) {
                        let current = label;
                        while (current && current !== document.body) {
                            const text = normalize(current.textContent);
                            const inputs = eligibleInputs(current);
                            if (text.includes('оптовая цена') && text.includes('при заказе') && inputs.length >= 2) {
                                if (!containers.includes(current)) containers.push(current);
                                break;
                            }
                            current = current.parentElement;
                        }
                    }
                    const container = containers[tierIndex];
                    if (!container) {
                        return { ok: false, error: `Не найден блок оптовой цены #${tierIndex + 1}.` };
                    }
                    const inputs = eligibleInputs(container);
                    if (inputs.length < 2) {
                        return { ok: false, error: `В блоке оптовой цены #${tierIndex + 1} не найдены оба поля.` };
                    }
                    let changed = false;
                    const setValue = (input, value) => {
                        const current = String(input.value || '').trim();
                        if (current === value) return false;
                        input.focus();
                        input.value = '';
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.value = value;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.blur();
                        return true;
                    };
                    changed = setValue(inputs[0], priceValue) || changed;
                    changed = setValue(inputs[1], quantityValue) || changed;
                    return { ok: true, changed };
                }""",
                {"tierIndex": tier_index, "priceValue": price, "quantityValue": quantity},
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось заполнить блок оптовой цены #{tier_index + 1}: {exc}") from exc
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or f"Не удалось заполнить блок оптовой цены #{tier_index + 1}."))
        return bool(result.get("changed"))

    def _update_availability(self, page: Page, availability: str | None) -> bool:
        if availability is None or str(availability).strip() == "":
            return False
        target = str(availability).strip()
        allowed = {"В наличии", "Нет в наличии", "Под заказ"}
        if target not in allowed:
            return False

        dropdown_selector = ".b-drop-down[data-qaid='dd_widget'], .b-drop-down"
        dropdown_data = page.evaluate(
            """(target) => {
                const statuses = ['В наличии', 'Нет в наличии', 'Под заказ'];
                const hiddenByStatus = {
                    'В наличии': ['avail', 'available'],
                    'Нет в наличии': ['not_avail', 'not_available', 'unavailable'],
                    'Под заказ': ['order', 'preorder']
                };
                const allHints = Object.values(hiddenByStatus).flat();
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const dropdowns = [...document.querySelectorAll('.b-drop-down[data-qaid="dd_widget"], .b-drop-down')];
                const candidates = [];
                for (let index = 0; index < dropdowns.length; index += 1) {
                    const dropdown = dropdowns[index];
                    if (dropdown.getAttribute('data-qaid') === 'action_dd') {
                        continue;
                    }
                    const hidden = dropdown.querySelector('input[type="hidden"]');
                    const valueText = normalize(dropdown.querySelector('.b-drop-down__value')?.textContent);
                    const itemText = [...dropdown.querySelectorAll('.js-item, .b-drop-down__list-item')]
                        .map((item) => normalize(item.textContent));
                    const hiddenValue = hidden ? String(hidden.value || '') : '';
                    const hasStatusValue = statuses.includes(valueText);
                    const hasStatusOption = itemText.some((text) => statuses.includes(text));
                    const hasTargetOption = itemText.includes(target);
                    const hasKnownHidden = allHints.some((hint) => hiddenValue.includes(hint));
                    if (hasStatusValue || hasStatusOption || hasKnownHidden) {
                        candidates.push({
                            index,
                            current: valueText,
                            hiddenValue,
                            items: itemText,
                            score: (hasTargetOption ? 4 : 0) + (hasStatusValue ? 2 : 0) + (hasKnownHidden ? 1 : 0)
                        });
                    }
                }
                candidates.sort((left, right) => right.score - left.score);
                return candidates[0] || null;
            }""",
            target,
        )
        if dropdown_data is None:
            raise RuntimeError("Не найден выпадающий список наличия товара.")

        current = str(dropdown_data.get("current") or "").strip()
        if current == target:
            return False

        dropdown_index = int(dropdown_data["index"])
        dropdown = page.locator(dropdown_selector).nth(dropdown_index)
        dropdown.click(timeout=5000)
        try:
            clicked = page.evaluate(
                """({ index, target }) => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const dropdowns = [...document.querySelectorAll('.b-drop-down[data-qaid="dd_widget"], .b-drop-down')];
                    const dropdown = dropdowns[index];
                    if (!dropdown) {
                        return false;
                    }
                    const dropdownOptions = [...dropdown.querySelectorAll('.js-item, .b-drop-down__list-item')];
                    const exactDropdownOption = dropdownOptions.find((item) => normalize(item.textContent) === target);
                    if (exactDropdownOption) {
                        exactDropdownOption.click();
                        return true;
                    }
                    const visibleOptions = [...document.querySelectorAll('.js-item, .b-drop-down__list-item')]
                        .filter((item) => {
                            const rect = item.getBoundingClientRect();
                            const style = window.getComputedStyle(item);
                            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                        });
                    const exactVisibleOption = visibleOptions.find((item) => normalize(item.textContent) === target);
                    if (exactVisibleOption) {
                        exactVisibleOption.click();
                        return true;
                    }
                    return false;
                }""",
                {"index": dropdown_index, "target": target},
            )
            if not clicked:
                raise RuntimeError(f"В списке наличия нет варианта '{target}'.")
            page.wait_for_function(
                """({ index, target }) => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const dropdowns = [...document.querySelectorAll('.b-drop-down[data-qaid="dd_widget"], .b-drop-down')];
                    const dropdown = dropdowns[index];
                    if (!dropdown) {
                        return false;
                    }
                    const valueText = normalize(dropdown.querySelector('.b-drop-down__value')?.textContent);
                    return valueText === target;
                }""",
                arg={"index": dropdown_index, "target": target},
                timeout=3000,
            )
            return True
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Не удалось выбрать наличие товара '{target}': {exc}") from exc

    def _remove_existing_images(self, page: Page) -> None:
        initial_count = self._existing_image_count(page)
        if initial_count == 0:
            return

        self._dismiss_existing_alerts(page)
        removed_any = False
        while self._existing_image_count(page) > 0:
            self._check_stop()
            before_count = self._existing_image_count(page)
            self._set_status("Удаляет существующее изображение товара...", page.url)
            if not self._click_first_delete_image_button(page):
                if self._image_disappeared_after_delete(page, before_count, timeout_ms=3000):
                    removed_any = True
                    continue
                raise RuntimeError("В карточке есть изображение, но не удалось нажать кнопку удаления изображения.")
            self._accept_dialog_if_visible(page)
            self._wait_after_image_delete(page, before_count)
            removed_any = True

        if not removed_any:
            raise RuntimeError("Не удалось удалить существующее изображение товара перед загрузкой нового.")

    def _click_first_delete_image_button(self, page: Page) -> bool:
        if self._hover_image_item_and_click_delete(page):
            return True
        for selector in self.selectors.delete_image_buttons:
            buttons = page.locator(selector)
            try:
                count = buttons.count()
            except Exception:
                continue
            for index in range(count):
                try:
                    button = buttons.nth(index)
                    try:
                        button.scroll_into_view_if_needed(timeout=1000)
                    except Exception:
                        pass
                    self._accept_dialog_if_visible(page)
                    button.click(timeout=3000, force=True)
                    return True
                except Exception:
                    if self._existing_image_count(page) == 0:
                        return True
                    continue
        return self._dom_click_first_delete_image_button(page)

    def _hover_image_item_and_click_delete(self, page: Page) -> bool:
        image_items = page.locator("[data-qaid='image_item']")
        try:
            image_count = image_items.count()
        except Exception:
            return False
        for image_index in range(image_count):
            image_item = image_items.nth(image_index)
            try:
                image_item.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            try:
                image_item.hover(timeout=2000, force=True)
                page.wait_for_timeout(150)
            except Exception:
                pass
            delete_button = image_item.locator("[data-qaid='delete_img_btn']").first
            try:
                if delete_button.count() == 0:
                    continue
                self._accept_dialog_if_visible(page)
                delete_button.click(timeout=3000, force=True)
                return True
            except Exception:
                if self._existing_image_count(page) == 0:
                    return True
                try:
                    self._accept_dialog_if_visible(page)
                    clicked = image_item.evaluate(
                        """(item) => {
                            const button = item.querySelector('[data-qaid="delete_img_btn"]');
                            if (!button) {
                                return false;
                            }
                            button.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
                            button.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                            button.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                            button.click();
                            return true;
                        }"""
                    )
                    if clicked:
                        return True
                except Exception:
                    if self._existing_image_count(page) == 0:
                        return True
                    continue
        return False

    def _dom_click_first_delete_image_button(self, page: Page) -> bool:
        self._accept_dialog_if_visible(page)
        try:
            return bool(
                page.evaluate(
                    """() => {
                        const imageItems = [...document.querySelectorAll('[data-qaid="image_item"]')];
                        const roots = imageItems.length ? imageItems : [document];
                        for (const root of roots) {
                            const button = root.querySelector('[data-qaid="delete_img_btn"]');
                            if (!button) {
                                continue;
                            }
                            root.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
                            button.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
                            button.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                            button.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                            button.click();
                            return true;
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            return False

    def _wait_after_image_delete(self, page: Page, before_count: int) -> None:
        if self._image_disappeared_after_delete(page, before_count, timeout_ms=5000):
            return
        raise RuntimeError("После клика удаления изображение не исчезло из карточки товара за 5 секунд.")

    def _image_disappeared_after_delete(self, page: Page, before_count: int, timeout_ms: int) -> bool:
        try:
            page.wait_for_function(
                """(beforeCount) => {
                    const imageItems = [...document.querySelectorAll('[data-qaid="image_item"]')];
                    const deleteButtons = [...document.querySelectorAll('[data-qaid="delete_img_btn"]')];
                    const visibleImageItems = imageItems.filter((item) => {
                        const rect = item.getBoundingClientRect();
                        const style = window.getComputedStyle(item);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    });
                    return imageItems.length < beforeCount
                        || visibleImageItems.length < beforeCount
                        || deleteButtons.length < beforeCount
                        || (beforeCount > 0 && imageItems.length === 0 && deleteButtons.length === 0);
                }""",
                arg=before_count,
                timeout=timeout_ms,
            )
            return True
        except PlaywrightTimeoutError as exc:
            return False

    def _save_product(self, page: Page) -> None:
        self._trace_step(page, "locating save button")
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
        self._trace_step(page, "clicking save")
        button.click(timeout=8000)
        self._wait_after_save_click(page)
        self._trace_step(page, "save alert received")

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
                    const alerts = [...document.querySelectorAll('[data-qaid="alert_text"], .b-alert__content')];
                    return alerts.some((item) => item.textContent && item.textContent.trim());
                }""",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            raise RuntimeError("После сохранения не появилось уведомление о результате.")

        except Exception as exc:
            if self._is_navigation_context_error(exc):
                self._trace_step(page, "save caused navigation; waiting for new page context")
                self._wait_until_dom_ready(page, timeout_ms=10000)
            else:
                raise

        alerts = self._visible_alerts(page)
        if not alerts:
            return

        unexpected_alerts = [
            alert["text"]
            for alert in alerts
            if not self._is_success_alert(alert["text"], alert["kind"])
            and not self._is_ignorable_alert(alert["text"], alert["kind"])
        ]
        if not unexpected_alerts:
            return
        raise RuntimeError("; ".join(unexpected_alerts))

    def _is_success_alert(self, text: str, kind: str) -> bool:
        if kind == "success":
            return True
        success_markers = (
            "Позиция сохранена успешно",
            "Изменения успешно сохранены",
            "Скоро товар начнет отображаться в каталоге",
            "РџРѕР·РёС†РёСЏ СЃРѕС…СЂР°РЅРµРЅР° СѓСЃРїРµС€РЅРѕ",
            "РР·РјРµРЅРµРЅРёСЏ СѓСЃРїРµС€РЅРѕ СЃРѕС…СЂР°РЅРµРЅС‹",
        )
        return any(marker in text for marker in success_markers)

    def _is_ignorable_alert(self, text: str, kind: str) -> bool:
        if kind == "warning":
            return True
        ignored_texts = (
            "Заполните рекомендованные характеристики",
            "Р—Р°РїРѕР»РЅРёС‚Рµ СЂРµРєРѕРјРµРЅРґРѕРІР°РЅРЅС‹Рµ С…Р°СЂР°РєС‚РµСЂРёСЃС‚РёРєРё",
        )
        return any(ignored_text in text for ignored_text in ignored_texts)

    def _visible_alerts(self, page: Page) -> list[dict[str, str]]:
        try:
            return page.evaluate(
                """() => {
                    const nodes = [...document.querySelectorAll(
                        '[data-qaid="alert_text"], .b-alert__content, .alert_action, .alert_content'
                    )];
                    const alerts = [];
                    const seen = new Set();
                    for (const node of nodes) {
                        const item = node.closest('.b-alert__item') || node;
                        const style = window.getComputedStyle(item);
                        if (!style || style.display === 'none' || style.visibility === 'hidden') {
                            continue;
                        }
                        const text = (item.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (!text) {
                            continue;
                        }
                        const icon = item.querySelector('.b-alert__icon');
                        const className = icon ? icon.className.baseVal || icon.className || '' : '';
                        const kind = className.includes('type_warning')
                            ? 'warning'
                            : className.includes('type_error')
                                ? 'error'
                                : className.includes('type_success')
                                    ? 'success'
                                    : '';
                        const key = `${kind}|${text}`;
                        if (seen.has(key)) {
                            continue;
                        }
                        seen.add(key);
                        alerts.push({ text, kind });
                    }
                    return alerts;
                }"""
            )
        except Exception:
            return []

    def _wait_for_wholesale_tier_count(self, page: Page, min_count: int) -> None:
        try:
            page.wait_for_function(
                """(expectedCount) => {
                    const priceLabel = '\\u043e\\u043f\\u0442\\u043e\\u0432\\u0430\\u044f \\u0446\\u0435\\u043d\\u0430';
                    const orderLabel = '\\u043f\\u0440\\u0438 \\u0437\\u0430\\u043a\\u0430\\u0437\\u0435';
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const eligibleInputs = (root) => [...root.querySelectorAll('input')].filter((input) => {
                        if (!isVisible(input)) return false;
                        const type = (input.getAttribute('type') || 'text').toLowerCase();
                        return !['hidden', 'radio', 'checkbox', 'file'].includes(type);
                    });
                    const labels = [...document.querySelectorAll('*')].filter(
                        (element) => isVisible(element) && normalize(element.textContent) === priceLabel
                    );
                    const containers = [];
                    for (const label of labels) {
                        let current = label;
                        while (current && current !== document.body) {
                            const text = normalize(current.textContent);
                            const inputs = eligibleInputs(current);
                            if (text.includes(priceLabel) && text.includes(orderLabel) && inputs.length >= 2) {
                                if (!containers.includes(current)) containers.push(current);
                                break;
                            }
                            current = current.parentElement;
                        }
                    }
                    return containers.length >= expectedCount;
                }""",
                arg=min_count,
                timeout=5000,
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось дождаться блоков оптовых цен: {exc}") from exc

    def _ensure_wholesale_tier_count(self, page: Page, expected_count: int) -> None:
        while True:
            current_count = int(page.evaluate(
                """() => {
                    const priceLabel = '\\u043e\\u043f\\u0442\\u043e\\u0432\\u0430\\u044f \\u0446\\u0435\\u043d\\u0430';
                    const orderLabel = '\\u043f\\u0440\\u0438 \\u0437\\u0430\\u043a\\u0430\\u0437\\u0435';
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const eligibleInputs = (root) => [...root.querySelectorAll('input')].filter((input) => {
                        if (!isVisible(input)) return false;
                        const type = (input.getAttribute('type') || 'text').toLowerCase();
                        return !['hidden', 'radio', 'checkbox', 'file'].includes(type);
                    });
                    const labels = [...document.querySelectorAll('*')].filter(
                        (element) => isVisible(element) && normalize(element.textContent) === priceLabel
                    );
                    const containers = [];
                    for (const label of labels) {
                        let current = label;
                        while (current && current !== document.body) {
                            const text = normalize(current.textContent);
                            const inputs = eligibleInputs(current);
                            if (text.includes(priceLabel) && text.includes(orderLabel) && inputs.length >= 2) {
                                if (!containers.includes(current)) containers.push(current);
                                break;
                            }
                            current = current.parentElement;
                        }
                    }
                    return containers.length;
                }"""
            ))
            if current_count >= expected_count:
                return

            clicked = bool(page.evaluate(
                """() => {
                    const buttonText = '\\u0434\\u043e\\u0431\\u0430\\u0432\\u0438\\u0442\\u044c \\u043e\\u043f\\u0442\\u043e\\u0432\\u0443\\u044e \\u0446\\u0435\\u043d\\u0443';
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const candidates = [...document.querySelectorAll('a, button, div, span')].filter(
                        (element) => isVisible(element) && normalize(element.textContent) === buttonText
                    );
                    const target = candidates[0];
                    if (!target) return false;
                    target.click();
                    return true;
                }"""
            ))
            if not clicked:
                raise RuntimeError("Не найдена кнопка 'Добавить оптовую цену'.")
            self._wait_for_wholesale_tier_count(page, expected_count)

    def _set_wholesale_tier_values(self, page: Page, tier_index: int, price: str, quantity: str) -> bool:
        try:
            result = page.evaluate(
                """({ tierIndex, priceValue, quantityValue }) => {
                    const priceLabel = '\\u043e\\u043f\\u0442\\u043e\\u0432\\u0430\\u044f \\u0446\\u0435\\u043d\\u0430';
                    const orderLabel = '\\u043f\\u0440\\u0438 \\u0437\\u0430\\u043a\\u0430\\u0437\\u0435';
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const eligibleInputs = (root) => [...root.querySelectorAll('input')].filter((input) => {
                        if (!isVisible(input)) return false;
                        const type = (input.getAttribute('type') || 'text').toLowerCase();
                        return !['hidden', 'radio', 'checkbox', 'file'].includes(type);
                    });
                    const labels = [...document.querySelectorAll('*')].filter(
                        (element) => isVisible(element) && normalize(element.textContent) === priceLabel
                    );
                    const containers = [];
                    for (const label of labels) {
                        let current = label;
                        while (current && current !== document.body) {
                            const text = normalize(current.textContent);
                            const inputs = eligibleInputs(current);
                            if (text.includes(priceLabel) && text.includes(orderLabel) && inputs.length >= 2) {
                                if (!containers.includes(current)) containers.push(current);
                                break;
                            }
                            current = current.parentElement;
                        }
                    }
                    const container = containers[tierIndex];
                    if (!container) {
                        return { ok: false, error: `Не найден блок оптовой цены #${tierIndex + 1}.` };
                    }
                    const inputs = eligibleInputs(container);
                    if (inputs.length < 2) {
                        return { ok: false, error: `В блоке оптовой цены #${tierIndex + 1} не найдены оба поля.` };
                    }
                    let changed = false;
                    const setValue = (input, value) => {
                        const current = String(input.value || '').trim();
                        if (current === value) return false;
                        input.focus();
                        input.value = '';
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.value = value;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.blur();
                        return true;
                    };
                    changed = setValue(inputs[0], priceValue) || changed;
                    changed = setValue(inputs[1], quantityValue) || changed;
                    return { ok: true, changed };
                }""",
                {"tierIndex": tier_index, "priceValue": price, "quantityValue": quantity},
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось заполнить блок оптовой цены #{tier_index + 1}: {exc}") from exc
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or f"Не удалось заполнить блок оптовой цены #{tier_index + 1}."))
        return bool(result.get("changed"))

    def _wait_for_wholesale_tier_count(self, page: Page, min_count: int) -> None:
        self._trace_step(page, f"waiting wholesale tier count>={min_count}")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                price_count = page.locator("input[data-qaid='wholesale_price_input']").count()
                qty_count = page.locator("input[data-qaid='wholesale_count_input']").count()
            except Exception:
                price_count = 0
                qty_count = 0
            if price_count >= min_count and qty_count >= min_count:
                self._trace_step(page, f"wholesale tier count ready price={price_count} qty={qty_count}")
                return
            page.wait_for_timeout(200)
        raise RuntimeError("Не удалось дождаться блоков оптовых цен: таймаут ожидания появления полей опта.")

    def _ensure_wholesale_tier_count(self, page: Page, expected_count: int) -> None:
        self._trace_step(page, f"ensuring wholesale tier count={expected_count}")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                current_count = page.locator("input[data-qaid='wholesale_price_input']").count()
            except Exception:
                current_count = 0
            if current_count >= expected_count:
                self._trace_step(page, f"wholesale tier count reached={current_count}")
                return

            add_link = page.locator(".b-add-link__text, .b-add-link").first
            try:
                if add_link.count() == 0:
                    self._wait_for_wholesale_tier_count(page, expected_count)
                    continue
                self._trace_step(page, f"clicking add wholesale tier from count={current_count}")
                add_link.click(timeout=5000)
            except Exception as exc:
                raise RuntimeError(f"Не найдена или не нажалась кнопка 'Добавить оптовую цену': {exc}") from exc
            self._wait_for_wholesale_tier_count(page, min(current_count + 1, expected_count))
        raise RuntimeError("Не удалось подготовить нужное количество блоков оптовых цен.")

    def _set_wholesale_tier_values(self, page: Page, tier_index: int, price: str, quantity: str) -> bool:
        try:
            self._trace_step(page, f"filling wholesale tier #{tier_index + 1} price={price} qty={quantity}")
            price_input = page.locator("input[data-qaid='wholesale_price_input']").nth(tier_index)
            count_input = page.locator("input[data-qaid='wholesale_count_input']").nth(tier_index)
            if price_input.count() == 0 or count_input.count() == 0:
                raise RuntimeError(f"Не найден блок оптовой цены #{tier_index + 1}.")
            changed = False
            current_price = price_input.input_value(timeout=1000).strip()
            if current_price != price:
                price_input.fill(price, timeout=5000)
                changed = True
            current_qty = count_input.input_value(timeout=1000).strip()
            if current_qty != quantity:
                count_input.fill(quantity, timeout=5000)
                changed = True
            self._trace_step(page, f"wholesale tier #{tier_index + 1} filled changed={changed}")
            return changed
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Не удалось заполнить блок оптовой цены #{tier_index + 1}: {exc}") from exc

    def _visible_alert_texts(self, page: Page) -> list[str]:
        return [alert["text"] for alert in self._visible_alerts(page)]

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
