"""Shared image utilities for OCR — used by pdf_importer.py, photo_importer.py,
and duval_clerk_scraper.py's lis pendens document OCR."""

import logging
import os
import re
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image
import pytesseract

logger = logging.getLogger(__name__)

# Tesseract binary path — check env var, then Windows default, then system PATH
_TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
if not _TESSERACT_CMD:
    _win_default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_win_default):
        _TESSERACT_CMD = _win_default
if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD


def fix_rotation(image: Image.Image) -> Image.Image:
    """Detect and fix page rotation using Tesseract OSD.

    Tesseract's 'Rotate' value is clockwise degrees, but PIL's rotate()
    is counterclockwise — so we use (360 - angle) to convert.
    """
    try:
        osd = pytesseract.image_to_osd(image)
        angle_match = re.search(r"Rotate:\s*(\d+)", osd)
        if angle_match:
            angle = int(angle_match.group(1))
            if angle and angle != 0:
                # Tesseract Rotate is CW; PIL rotate is CCW → invert
                pil_angle = 360 - angle
                logger.debug("  Detected rotation: %d° CW, applying %d° CCW", angle, pil_angle)
                return image.rotate(pil_angle, expand=True)
        return image
    except Exception:
        # OSD failed — try 270° (common for landscape-scanned pages)
        logger.debug("  OSD failed, applying 270° rotation (landscape fix)")
        return image.rotate(270, expand=True)


def ocr_page(image: Image.Image, psm: int = 3) -> str:
    """Run Tesseract OCR on a page image.

    Args:
        image: PIL Image to OCR.
        psm: Tesseract page segmentation mode.
             3 = fully automatic (best for PDF tables).
             6 = single uniform block (best for individual notice photos).
    """
    text = pytesseract.image_to_string(image, config=f"--psm {psm}")
    return text


def render_pdf_pages(pdf_path: Path, dpi: int = 200) -> list[Image.Image]:
    """Render each PDF page to a PIL Image at the specified DPI."""
    doc = pdfium.PdfDocument(str(pdf_path))
    images = []
    for i in range(len(doc)):
        page = doc[i]
        scale = dpi / 72  # PDF default is 72 DPI
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()
        images.append(pil_image)
    doc.close()
    logger.info("Rendered %d pages from %s at %d DPI", len(images), pdf_path.name, dpi)
    return images


def render_pdf_bytes(pdf_bytes: bytes, dpi: int = 300) -> list[Image.Image]:
    """Render each page of an in-memory PDF (bytes) to a PIL Image at the given DPI."""
    doc = pdfium.PdfDocument(pdf_bytes)
    images = []
    for i in range(len(doc)):
        page = doc[i]
        scale = dpi / 72
        bitmap = page.render(scale=scale)
        images.append(bitmap.to_pil())
    doc.close()
    logger.info("Rendered %d pages from in-memory PDF at %d DPI", len(images), dpi)
    return images
