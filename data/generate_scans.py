"""Generate scanned twins of generated invoices: image-only PDFs with no text layer.

A twin is the same invoice as its digital original (same id, same ground truth), so the
ocr and vision backends are measured on exactly the invoices pdf_text already reads.
A twin shares its original's invoice number: evaluate twins as another way of reading the
same invoice (one run per backend against the same ledger), never in the same run as the
originals, or the duplicate check flags them.

    uv run python data/generate.py
    uv run python -m data.generate_scans

Selection: per class, the first PER_SPLIT dev and PER_SPLIT test invoices by id. Each gets
one degradation tier in turn (TIERS), and every other one a stamp and an initial over the
table. Output is byte-identical for a given seed and dataset.
"""

import argparse
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from data.generate import DEFAULT_SEED, GROUND_TRUTH_FILE

SCAN_DIR = "scans"
SCAN_MANIFEST_FILE = "scans.json"
PER_SPLIT = 4


@dataclass(frozen=True)
class Tier:
    name: str
    dpi: int
    max_skew: float  # degrees, either way
    noise: int  # strongest darkening of a speckle, 0-255
    blur: float  # Gaussian radius in pixels, 0 for none
    shading: int  # darkest point of an uneven-lighting gradient, 0 for none
    jpeg_quality: int


TIERS = (
    Tier("clean", 300, 0.3, 20, 0.0, 0, 90),
    Tier("low_res", 150, 1.5, 45, 0.0, 0, 70),
    Tier("phone", 200, 2.5, 35, 1.0, 60, 55),
)
STAMP_WORDS = {"id": "LUNAS", "en": "PAID"}


@dataclass(frozen=True)
class Scan:
    id: str
    file: str
    source: str
    label: str
    variant: str | None
    split: str
    tier: str
    dpi: int
    stamp: bool


def select(ground_truth: list[dict], per_split: int = PER_SPLIT) -> list[dict]:
    """The first `per_split` dev and test invoices of every class, by id."""
    chosen = []
    for label in sorted({case["label"] for case in ground_truth}):
        for split in ("dev", "test"):
            cases = sorted(
                (c for c in ground_truth if c["label"] == label and c["split"] == split),
                key=lambda c: c["id"],
            )
            chosen.extend(cases[:per_split])
    return sorted(chosen, key=lambda c: c["id"])


def _speckle(rng: random.Random, size: tuple[int, int], strongest: int) -> Image.Image:
    noise = Image.frombytes("L", size, rng.randbytes(size[0] * size[1]))
    # Mostly faint, with an occasional dark speck: square the uniform value.
    return noise.point(lambda v: (v * v // 255) * strongest // 255)


def _stamp(page: Image.Image, rng: random.Random, word: str) -> Image.Image:
    """A rubber stamp and a pen initial across the middle of the page, over the table."""
    width, height = page.size
    font = ImageFont.load_default(size=height // 18)
    layer = Image.new("L", (width, height // 5), 0)
    draw = ImageDraw.Draw(layer)
    left, top, right, bottom = draw.textbbox((0, 0), word, font=font)
    x, y = (width - (right - left)) // 2, (layer.height - (bottom - top)) // 2
    pad = height // 120
    draw.rectangle(
        (x - pad * 2, y - pad, x + right - left + pad * 2, y + bottom - top + pad * 2),
        outline=150,
        width=max(2, pad // 2),
    )
    draw.text((x - left, y - top), word, fill=150, font=font)
    layer = layer.rotate(rng.uniform(-18, 18), resample=Image.Resampling.BICUBIC)
    mask = Image.new("L", page.size, 0)
    mask.paste(layer, (0, int(height * rng.uniform(0.24, 0.34))))

    pen = ImageDraw.Draw(mask)
    x0, y0 = int(width * rng.uniform(0.55, 0.65)), int(height * rng.uniform(0.42, 0.5))
    points = [(x0, y0)]
    for _ in range(6):
        x0 += int(width * rng.uniform(0.01, 0.04))
        y0 += int(height * rng.uniform(-0.02, 0.02))
        points.append((x0, y0))
    pen.line(points, fill=230, width=max(2, height // 400), joint="curve")
    return ImageChops.subtract(page, mask)


def degrade(
    page: Image.Image,
    tier: Tier,
    rng: random.Random,
    stamp_word: str | None,
    hidden_text: str | None = None,
) -> Image.Image:
    """Turn a clean grayscale render into a scan. `hidden_text` is the hook for week-3
    image-only injection: text drawn faintly into the image, present in no text layer."""
    if hidden_text:
        ImageDraw.Draw(page).text(
            (page.width // 20, page.height - page.height // 30),
            hidden_text,
            fill=235,
            font=ImageFont.load_default(size=page.height // 120),
        )
    if stamp_word:
        page = _stamp(page, rng, stamp_word)
    if tier.shading:
        gradient = Image.linear_gradient("L").rotate(rng.uniform(0, 360)).resize(page.size)
        page = ImageChops.subtract(page, gradient.point(lambda v: v * tier.shading // 255))
    page = ImageChops.subtract(page, _speckle(rng, page.size, tier.noise))
    page = page.rotate(
        rng.uniform(-tier.max_skew, tier.max_skew),
        resample=Image.Resampling.BICUBIC,
        fillcolor=255,
    )
    if tier.blur:
        page = page.filter(ImageFilter.GaussianBlur(tier.blur))
    return page


def render_scan(source: Path, target: Path, tier: Tier, seed: int, stamp_word: str | None) -> None:
    rng = random.Random(seed)
    pdf = pdfium.PdfDocument(source)
    try:
        pages = [
            degrade(
                page.render(scale=tier.dpi / 72, grayscale=True).to_pil().convert("L"),
                tier,
                rng,
                stamp_word,
            )
            for page in pdf
        ]
    finally:
        pdf.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Pillow stores each page as a JPEG: a scanner's compression artefacts come with it.
    pages[0].save(
        target,
        "PDF",
        resolution=tier.dpi,
        quality=tier.jpeg_quality,
        save_all=True,
        append_images=pages[1:],
        creationDate=None,
        modDate=None,
    )  # no timestamps: byte-identical


def write_scans(data_dir: Path, seed: int = DEFAULT_SEED, per_split: int = PER_SPLIT) -> list[Scan]:
    ground_truth = json.loads((data_dir / GROUND_TRUTH_FILE).read_text(encoding="utf-8"))
    scans = []
    position: dict[str, int] = {}
    for case in select(ground_truth, per_split):
        index = position[case["label"]] = position.get(case["label"], -1) + 1
        tier = TIERS[index % len(TIERS)]
        stamp = index % 2 == 1
        file = f"{SCAN_DIR}/{case['id']}.pdf"
        render_scan(
            data_dir / case["file"],
            data_dir / file,
            tier,
            seed + int(case["id"].removeprefix("inv-")),
            STAMP_WORDS[case["language"]] if stamp else None,
        )
        scans.append(
            Scan(
                case["id"],
                file,
                case["file"],
                case["label"],
                case["variant"],
                case["split"],
                tier.name,
                tier.dpi,
                stamp,
            )
        )
    manifest = {"seed": seed, "per_split": per_split, "scans": [asdict(s) for s in scans]}
    (data_dir / SCAN_MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return scans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "out")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--per-split", type=int, default=PER_SPLIT)
    args = parser.parse_args()
    scans = write_scans(args.data, args.seed, args.per_split)
    print(f"{len(scans)} scanned twins in {args.data / SCAN_DIR}")


if __name__ == "__main__":
    main()
