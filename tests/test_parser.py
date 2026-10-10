from pathlib import Path

import fitz

from paper_translator import parse_pdf


def _make_fixture(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((50, 60), "A Structured Paper Title", fontsize=18, fontname="helv")
    page.insert_textbox((50, 100, 270, 160), "This is the left column with enough text to be extracted in reading order.", fontsize=10)
    page.insert_textbox((50, 175, 270, 235), "A second paragraph remains in the left paper column.", fontsize=10)
    page.insert_textbox((325, 100, 545, 160), "This is the right column with more text for the layout detector.", fontsize=10)
    page.insert_textbox((325, 175, 545, 235), "A second paragraph remains in the right paper column.", fontsize=10)
    page.insert_text((50, 270), "E = mc^2", fontsize=14, fontname="helv")
    image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 16, 16))
    image.clear_with(0x336699)
    page.insert_image(fitz.Rect(400, 300, 500, 400), pixmap=image)
    document.save(path)
    document.close()


def test_parse_extracts_layout_blocks_and_images(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    output = tmp_path / "document.json"
    _make_fixture(source)
    result = parse_pdf(source, output)
    assert output.exists()
    assert result["schema_version"] == "paper-translator.document.v1"
    assert result["pages"][0]["layout"] == "two_column"
    assert any(block["type"] == "heading" for block in result["blocks"])
    assert any(block["type"] == "equation" for block in result["blocks"])
    assert len(result["assets"]) == 1
    assert (output.parent / result["assets"][0]["asset"]).exists()


def test_scanned_page_is_marked_for_ocr(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    output = tmp_path / "scan.json"
    document = fitz.open()
    page = document.new_page()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20))
    pixmap.clear_with(0xFFFFFF)
    page.insert_image(fitz.Rect(0, 0, 200, 200), pixmap=pixmap)
    document.save(source)
    document.close()
    result = parse_pdf(source, output)
    assert result["pages"][0]["requires_ocr"] is True
    assert result["warnings"][0]["code"] == "OCR_REQUIRED"
