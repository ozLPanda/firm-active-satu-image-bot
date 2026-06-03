from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re
from typing import Iterable

from openpyxl import load_workbook


SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

CODE_HEADERS = {
    "код",
    "код товара",
    "код_товара",
    "артикул",
    "article",
    "sku",
}

NAME_HEADERS = {
    "название",
    "название позиции",
    "название_позиции",
    "наименование",
    "товар",
    "name",
    "product name",
}


@dataclass(frozen=True)
class CatalogItem:
    code: str
    name: str


@dataclass(frozen=True)
class ImageItem:
    code: str
    path: Path
    catalog_name: str | None = None


def normalize_code(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def normalize_text(value: object) -> str:
    text = "" if value is None else str(value).lower()
    text = text.replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def similarity(left: str, right: str) -> float:
    left_normalized = normalize_text(left)
    right_normalized = normalize_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return 1.0
    direct_ratio = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    left_tokens = " ".join(sorted(left_normalized.split()))
    right_tokens = " ".join(sorted(right_normalized.split()))
    token_ratio = SequenceMatcher(None, left_tokens, right_tokens).ratio()
    return max(direct_ratio, token_ratio)


def names_match(expected: str, actual: str, threshold: float = 0.85) -> bool:
    return similarity(expected, actual) >= threshold


def image_code_from_path(path: Path) -> str:
    return path.stem.strip()


def is_supported_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def scan_images(folder: str | Path, catalog: dict[str, CatalogItem] | None = None) -> list[ImageItem]:
    folder_path = Path(folder)
    items: list[ImageItem] = []
    for path in sorted(folder_path.iterdir(), key=lambda item: item.name.lower()):
        if not is_supported_image(path):
            continue
        code = image_code_from_path(path)
        catalog_item = catalog.get(code) if catalog else None
        items.append(ImageItem(code=code, path=path, catalog_name=catalog_item.name if catalog_item else None))
    return items


def _header_key(value: object) -> str:
    return normalize_text(str(value).replace("_", " "))


def _find_column(headers: Iterable[object], candidates: set[str]) -> int | None:
    normalized_candidates = {_header_key(candidate) for candidate in candidates}
    for index, header in enumerate(headers, start=1):
        key = _header_key(header)
        if key in normalized_candidates:
            return index
    return None


def load_catalog(excel_path: str | Path) -> dict[str, CatalogItem]:
    workbook = load_workbook(excel_path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header_row:
            raise ValueError("В Excel-файле не найдена строка заголовков.")

        code_column = _find_column(header_row, CODE_HEADERS)
        name_column = _find_column(header_row, NAME_HEADERS)
        if code_column is None or name_column is None:
            raise ValueError(
                "Не удалось определить колонки артикула и названия. "
                "Нужны заголовки вроде 'артикул'/'код товара' и 'название'/'наименование'."
            )

        catalog: dict[str, CatalogItem] = {}
        for row in sheet.iter_rows(min_row=2, values_only=True):
            code = normalize_code(row[code_column - 1] if len(row) >= code_column else None)
            name = str(row[name_column - 1]).strip() if len(row) >= name_column and row[name_column - 1] else ""
            if code and name and code not in catalog:
                catalog[code] = CatalogItem(code=code, name=name)
        return catalog
    finally:
        workbook.close()
