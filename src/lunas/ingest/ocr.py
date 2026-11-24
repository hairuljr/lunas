"""Local OCR for scans and images, with RapidOCR (ONNX, runs on CPU).

OCR returns text boxes, not lines. They are put back into lines here: the page skew is
estimated from the boxes themselves, box centres are rotated level, boxes whose centres sit
on the same line are joined left to right, and the horizontal gaps become spaces so columns
stay roughly where they were, like the text layer of a digital PDF.

The output is as untrusted as any other document text. `confidence` is the recognizer's
score for its least certain line (of at least MIN_SCORED_CHARS characters): one garbled
bank line is what matters, and an average over a page of clean lines hides it (a page with
its bank line read as "Ban co c n g . Ac co" averaged 0.99). It says how sure the OCR is
of its reading, not that the reading is right.
"""

import math
import statistics
from functools import cache
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from lunas.models import Page, ParsedDocument

RENDER_DPI = 300
# Boxes shorter than this (a lone "1", a ":") are left out of the confidence.
MIN_SCORED_CHARS = 4
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


@cache
def _engine():
    from rapidocr import RapidOCR  # slow import, and only OCR runs need it

    return RapidOCR(params={"Global.log_level": "warning"})


def _images(path: Path) -> list[Image.Image]:
    if path.suffix.lower() in IMAGE_SUFFIXES:
        with Image.open(path) as image:
            return [image.convert("RGB")]
    pdf = pdfium.PdfDocument(path)
    try:
        return [page.render(scale=RENDER_DPI / 72).to_pil().convert("RGB") for page in pdf]
    finally:
        pdf.close()


def _skew(boxes: list) -> float:
    """Median slope of the boxes' top edges, in radians. Short boxes are too noisy to count."""
    slopes = [
        math.atan2(box[1][1] - box[0][1], box[1][0] - box[0][0])
        for box in boxes
        if box[1][0] - box[0][0] > 3 * abs(box[3][1] - box[0][1])
    ]
    return statistics.median(slopes) if slopes else 0.0


def layout(boxes: list, texts: list[str]) -> str:
    """Join OCR boxes into lines of text, keeping the column positions approximately."""
    if not texts:
        return ""
    angle = _skew(boxes)
    cos, sin = math.cos(-angle), math.sin(-angle)
    items = []
    for box, text in zip(boxes, texts, strict=True):
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        cx, cy = sum(xs) / 4, sum(ys) / 4
        left = min(xs) * cos - cy * sin  # the left edge, levelled
        level_y = cx * sin + cy * cos
        height = max(ys) - min(ys)
        width = max(xs) - min(xs)
        items.append((level_y, left, height, width, text))

    line_height = statistics.median(item[2] for item in items) or 1
    char_width = statistics.median(item[3] / max(len(item[4]), 1) for item in items) or 1
    lines: list[list] = []
    for item in sorted(items):
        if lines and abs(item[0] - lines[-1][0][0]) < line_height / 2:
            lines[-1].append(item)
        else:
            lines.append([item])

    origin = min(item[1] for item in items)
    rendered = []
    for line in lines:
        text = ""
        for _, left, _, _, word in sorted(line, key=lambda item: item[1]):
            column = round((left - origin) / char_width)
            text += " " * max(column - len(text), 1 if text else 0) + word
        rendered.append(text)
    return "\n".join(rendered)


# The recognizer squeezes a long text box to a fixed height and a capped width; past about
# this width-to-height ratio the characters blur together (a 95-character bank line came
# back as "Ban co c n g . Ac co"). Such boxes are cut at a blank column and read in pieces.
MAX_ASPECT = 20
REREAD_BELOW = 0.95
INK = 160  # a pixel darker than this is part of a character


def _cut(gray: np.ndarray) -> list[np.ndarray]:
    """Split a line image at blank columns into pieces no wider than MAX_ASPECT heights.
    Cuts fall only where no column has ink, so no character is split or read twice."""
    height, width = gray.shape
    if width <= MAX_ASPECT * height:
        return [gray]
    # Ink is looked for in the middle rows only: a box border or table rule above or below
    # the text would otherwise make every column look inked.
    inner = gray[height // 6 : height - height // 6]
    blank = np.concatenate(([False], ~(inner < INK).any(axis=0), [False]))
    edges = np.flatnonzero(np.diff(blank.astype(np.int8)))
    # Runs of blank columns as wide as a word space (an eighth of the line height): the
    # narrower gaps between letters are not cut.
    gaps = [
        (a + b) // 2 for a, b in zip(edges[::2], edges[1::2], strict=True) if b - a >= height // 8
    ]
    gaps = [g for g in gaps if height < g < width - height]
    if not gaps:
        return [gray]
    cut = min(gaps, key=lambda g: abs(g - width // 2))
    return _cut(gray[:, :cut]) + _cut(gray[:, cut:])


def _reread(image: np.ndarray, box: list) -> tuple[str, float] | None:
    xs, ys = [int(p[0]) for p in box], [int(p[1]) for p in box]
    pad = max(2, (max(ys) - min(ys)) // 8)
    crop = image[max(min(ys) - pad, 0) : max(ys) + pad, max(min(xs) - pad, 0) : max(xs) + pad]
    pieces = _cut(crop.mean(axis=2).astype(np.uint8))
    if len(pieces) < 2:
        return None
    texts, scores = [], []
    for piece in pieces:
        result = _engine()(np.stack([piece] * 3, axis=2), use_det=False, use_cls=False)
        texts += [t for t in result.txts or () if t]
        scores += list(result.scores or ())
    if not texts:
        return None
    return " ".join(t.strip() for t in texts), min(scores)


class OcrReader:
    """Reads scans and images. Free, a few seconds a page, on the CPU."""

    def read(self, path: Path) -> ParsedDocument:
        pages, scores = [], []
        for number, image in enumerate(_images(path), start=1):
            pixels = np.asarray(image)
            # Flags passed every time: the engine keeps the last call's, and _reread turns
            # detection off.
            result = _engine()(pixels, use_det=True, use_cls=True, use_rec=True)
            texts = list(result.txts or ())
            boxes = [box.tolist() for box in result.boxes] if texts else []
            page_scores = list(result.scores or ())
            for i, box in enumerate(boxes):
                width = max(p[0] for p in box) - min(p[0] for p in box)
                height = max(p[1] for p in box) - min(p[1] for p in box)
                if page_scores[i] < REREAD_BELOW and width > MAX_ASPECT * height:
                    reread = _reread(pixels, box)
                    if reread and reread[1] > page_scores[i]:
                        texts[i], page_scores[i] = reread
            pages.append(Page(number=number, text=layout(boxes, texts)))
            scores += [
                score
                for score, text in zip(page_scores, texts, strict=True)
                if len(text.strip()) >= MIN_SCORED_CHARS
            ]
        confidence = min(scores, default=0.0)
        return ParsedDocument(pages=pages, backend="ocr", confidence=round(confidence, 4))
