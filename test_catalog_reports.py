from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook

from catalog import image_code_from_path, is_supported_image, load_catalog, names_match
from reports import ReportRecord, ReportWriter


class CatalogReportsTests(unittest.TestCase):
    def test_image_code_from_path(self) -> None:
        self.assertEqual(image_code_from_path(Path("00000001200.jpeg")), "00000001200")

    def test_supported_image_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            image_path = folder / "photo.JPG"
            image_path.write_text("", encoding="utf-8")
            self.assertTrue(is_supported_image(image_path))
            self.assertFalse(is_supported_image(folder / "photo.bmp"))

    def test_names_match_with_minor_difference(self) -> None:
        self.assertTrue(names_match("Котел газовый настенный 24 кВт", "Котел настенный газовый 24 кВт"))

    def test_load_catalog_detects_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "catalog.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Артикул", "Наименование"])
            sheet.append(["00000002416", "Котел"])
            workbook.save(path)

            catalog = load_catalog(path)

            self.assertEqual(catalog["00000002416"].name, "Котел")

    def test_report_writer_saves_csv_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = ReportWriter(temp_dir)
            writer.add(ReportRecord(code="0001", status="uploaded", image_path="0001.jpg"))

            self.assertTrue(writer.csv_path.exists())
            self.assertTrue(writer.json_path.exists())
            self.assertIn("uploaded", writer.csv_path.read_text(encoding="utf-8-sig"))
            self.assertIn("uploaded", writer.json_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
