from __future__ import annotations
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from PIL import Image
import pymupdf
from .duplicates import SlideImage


def _run_qpdf_optimization(input_path: Path, output_path: Path) -> None:
    qpdf = shutil.which("qpdf")
    if qpdf is None:
        raise RuntimeError("qpdf is required for PDF optimization but was not found in PATH")

    cmd = [
        qpdf,
        "--optimize-images",
        "--recompress-flate",
        "--compression-level=9",
        "--object-streams=generate",
        str(input_path),
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"qpdf optimization failed{suffix}") from exc


def write_slides_pdf(slides: list[SlideImage], output_path: Path) -> None:
    if not slides:
        raise ValueError("cannot create PDF with no slides")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    raw_fd, raw_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".raw.pdf", dir=output_path.parent
    )
    os.close(raw_fd)
    optimized_fd, optimized_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".optimized.pdf", dir=output_path.parent
    )
    os.close(optimized_fd)
    raw_path = Path(raw_name)
    optimized_path = Path(optimized_name)

    try:
        doc = pymupdf.open()
        try:
            for slide in slides:
                with Image.open(slide.path) as im:
                    w, h = im.size
                page = doc.new_page(width=float(w), height=float(h))
                page.insert_image(page.rect, filename=str(slide.path))
            doc.save(raw_path)
        finally:
            doc.close()

        optimized_path.unlink(missing_ok=True)
        _run_qpdf_optimization(raw_path, optimized_path)
        os.replace(optimized_path, output_path)
    except Exception:
        optimized_path.unlink(missing_ok=True)
        raise
    finally:
        raw_path.unlink(missing_ok=True)
        optimized_path.unlink(missing_ok=True)
