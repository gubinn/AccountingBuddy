"""Local OCR for payment screenshots.

RapidOCR ships PP-OCR models and runs on CPU through onnxruntime, which keeps
financial screenshots on this machine. The engine is created lazily so the
daemon starts quickly and text-only deployments do not pay the import cost.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_engine = None


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def ocr_image(path: str | Path, min_score: float = 0.35) -> str:
    """Return the text lines of an image, in reading order."""
    image = Path(path)
    if not image.exists():
        raise FileNotFoundError(f"image not found: {image}")

    engine = _get_engine()
    result, _ = engine(str(image))
    if not result:
        return ""

    lines: list[str] = []
    for item in result:
        # RapidOCR returns [box, text, score]
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        text = str(item[1]).strip()
        if not text:
            continue
        try:
            score = float(item[2]) if len(item) > 2 else 1.0
        except (TypeError, ValueError):
            score = 1.0
        if score >= min_score:
            lines.append(text)
    logger.info("OCR read %d lines from %s", len(lines), image.name)
    return "\n".join(lines)