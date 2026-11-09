"""Lossless slide PDF generation and validation before paired publication."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from .config import PROCESS_TIMEOUTS
from .media import MediaError, _run_process
from .tools import executable
from .models import CancelToken, SlideImage


class InvalidSlidePDF(ValueError):
    """Generated or optimized PDF violates its page/identity contract."""


def _run_qpdf_optimization(input_path: Path, output_path: Path,
                           *, cancel_token: CancelToken | None = None) -> bool:
    """Best-effort, non-lossy qpdf recompression; never optimize embedded images."""
    try:qpdf=executable("qpdf")
    except FileNotFoundError:qpdf=None
    if qpdf is None:
        return False
    cmd = [qpdf, "--recompress-flate", "--compression-level=9",
           "--object-streams=generate", str(input_path), str(output_path)]
    try:
        proc = _run_process(cmd, tool="qpdf", phase="qpdf",
                            timeout=PROCESS_TIMEOUTS["qpdf"],
                            cancel_token=cancel_token, accepted_returncodes=(0, 3))
        return proc.returncode in (0, 3) and output_path.is_file() and output_path.stat().st_size > 0
    except MediaError as exc:
        if exc.code == "Cancelled":
            raise
        return False
    except OSError:
        return False


def validate_slides_pdf(path: Path, slides: list[SlideImage] | None = None,
                        *, export_id: str | None = None) -> str:
    """Reopen a candidate, verify exact page geometry and its export identity.

    Returns the normalized UUID stored in /Subject.
    """
    candidate = Path(path)
    try:
        reader = PdfReader(str(candidate), strict=True)
        pages = reader.pages
        if len(pages) == 0:
            raise InvalidSlidePDF("PDF cannot be empty")
        if slides is not None and len(pages) != len(slides):
            raise InvalidSlidePDF("PDF page count mismatch")
        subject = reader.metadata.subject if reader.metadata else None
        prefix = "slide-extractor:export_id="
        if not subject or not subject.startswith(prefix):
            raise InvalidSlidePDF("Missing export UUID in PDF /Subject")
        actual = subject[len(prefix):]
        if str(UUID(actual)) != actual:
            raise InvalidSlidePDF("Noncanonical export UUID")
        if export_id is not None and actual != str(UUID(str(export_id))):
            raise InvalidSlidePDF("PDF export UUID mismatch")
        if slides is not None:
            for index, (page, slide) in enumerate(zip(pages, slides)):
                with Image.open(slide.path) as image:
                    expected = image.size
                width, height = float(page.mediabox.width), float(page.mediabox.height)
                if (width, height) != expected:
                    raise InvalidSlidePDF(f"PDF page {index+1} geometry mismatch")
        return actual
    except InvalidSlidePDF:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, PdfReadError) as exc:
        raise InvalidSlidePDF(f"Could not validate slide PDF: {exc}") from exc


def write_slides_pdf(slides: list[SlideImage], output_path: Path, *,
                     export_id: str | None = None,
                     cancel_token: CancelToken | None = None) -> None:
    """Render one unscaled image per PDF page, validate before publishing the PDF."""
    if not slides:
        raise ValueError("cannot create PDF with no slides")
    export_id = str(UUID(str(export_id))) if export_id is not None else str(uuid4())
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_fd, raw_name = tempfile.mkstemp(prefix=f".{output_path.name}.",
                                         suffix=".raw.pdf", dir=output_path.parent)
    os.close(raw_fd)
    raw_path = Path(raw_name)
    optimized_path = raw_path.with_suffix(".optimized.pdf")
    try:
        doc = canvas.Canvas(str(raw_path), pageCompression=1, invariant=1)
        doc.setSubject(f"slide-extractor:export_id={export_id}")
        for slide in slides:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            with Image.open(slide.path) as image:
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise InvalidSlidePDF(f"Invalid image dimensions: {slide.path}")
                doc.setPageSize((width, height))
                doc.drawImage(ImageReader(image), 0, 0, width=width, height=height,
                              preserveAspectRatio=False, mask="auto")
                doc.showPage()
        doc.save()
        validate_slides_pdf(raw_path, slides, export_id=export_id)
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        selected = raw_path
        if _run_qpdf_optimization(raw_path, optimized_path, cancel_token=cancel_token):
            try:
                validate_slides_pdf(optimized_path, slides, export_id=export_id)
                selected = optimized_path
            except InvalidSlidePDF:
                # The optimizer is optional: never publish a corrupt optimization.
                pass
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        os.replace(selected, output_path)
    finally:
        raw_path.unlink(missing_ok=True)
        optimized_path.unlink(missing_ok=True)
