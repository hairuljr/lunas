"""Transcription eval for the scanned twins: what the OCR reads against what was printed.

    uv run python -m data.generate_scans      # once
    uv run python -m evals.node.transcription [--split dev|test|all] [--backend ocr]

Calls no model. The reference is the text layer of each twin's digital original, read by
pdf_text. Two measures:
- CER: character error rate (edit distance / reference length), whitespace collapsed, so
  column spacing does not count.
- Critical fields: whether each value the decision depends on appears in the transcript
  (invoice number, PO number, tax, total, bank account). A field the OCR lost or misread
  cannot be extracted right, whatever the extraction model does.

Broken down by degradation tier and by stamp, since those are what the scans vary.
"""

import argparse
import json
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from data.generate import GROUND_TRUTH_FILE
from data.generate_scans import SCAN_MANIFEST_FILE
from lunas.config import Settings
from lunas.ingest import get_reader
from lunas.ingest.vision import VisionReader
from lunas.llm import build_client

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"
CRITICAL = ("invoice_number", "po_number", "tax", "total", "bank_account")
AMOUNTS = {"tax", "total", "bank_account"}


def _words(text: str) -> str:
    """Whitespace collapsed, and rules made of dashes or underscores dropped: a printed rule
    is not text, and OCR rightly reads none."""
    return " ".join(w for w in text.split() if not re.fullmatch(r"[-_=.]{3,}", w))


def cer(reference: str, hypothesis: str) -> float:
    reference, hypothesis = _words(reference), _words(hypothesis)
    if not reference:
        return 0.0 if not hypothesis else 1.0
    previous = list(range(len(hypothesis) + 1))
    for i, r in enumerate(reference, start=1):
        current = [i]
        for j, h in enumerate(hypothesis, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h)))
        previous = current
    return previous[-1] / len(reference)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _numbers(text: str) -> set[str]:
    """Every number in the text with its separators removed, and without cents."""
    found = set()
    for token in re.findall(r"\d[\d.,]*\d|\d", text):
        found.add(re.sub(r"\D", "", token))
        if re.search(r"[.,]\d{2}$", token):
            found.add(re.sub(r"\D", "", token[:-3]))
    return found


def fields_found(invoice: dict, text: str) -> dict[str, bool | None]:
    """None for a field the invoice does not print."""
    numbers, compact = _numbers(text), _compact(text)
    found: dict[str, bool | None] = {}
    for field in CRITICAL:
        value = invoice.get(field)
        if value in (None, ""):
            found[field] = None
        elif field in AMOUNTS:
            found[field] = re.sub(r"\D", "", str(value).split(".")[0]) in numbers
        else:
            found[field] = _compact(str(value)) in compact
    return found


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize(rows: list[dict]) -> dict:
    fields = {
        field: _rate([r["fields"][field] for r in rows if r["fields"][field] is not None])
        for field in CRITICAL
    }
    return {
        "documents": len(rows),
        "mean_cer": round(sum(r["cer"] for r in rows) / len(rows), 4) if rows else None,
        "all_critical_found": _rate(
            [all(v is not False for v in r["fields"].values()) for r in rows]
        ),
        "fields": fields,
        # None for vision, which reports no confidence.
        "min_confidence": min(
            (r["confidence"] for r in rows if r["confidence"] is not None), default=None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--backend", choices=["ocr", "vision"], default="ocr")
    parser.add_argument("--ids", help="only these twins, comma-separated")
    parser.add_argument(
        "--cache", type=Path, default=Path(__file__).resolve().parents[1] / "cache" / "scans"
    )
    parser.add_argument("--data", type=Path)
    args = parser.parse_args()

    data_dir = args.data or Settings().data_dir
    manifest = json.loads((data_dir / SCAN_MANIFEST_FILE).read_text(encoding="utf-8"))
    truth = {
        case["id"]: case
        for case in json.loads((data_dir / GROUND_TRUTH_FILE).read_text(encoding="utf-8"))
    }
    wanted = set(args.ids.split(",")) if args.ids else None
    scans = [
        s
        for s in manifest["scans"]
        if (args.split == "all" or s["split"] == args.split) and (not wanted or s["id"] in wanted)
    ]
    reference_reader = get_reader("pdf_text")
    if args.backend == "vision":  # paid per page; replies cached, so a rerun is free
        settings = Settings()
        vision = settings.model_copy(update={"llm_model": settings.vision_model})
        reader = VisionReader(build_client(vision, args.cache))
    else:
        reader = get_reader(args.backend)

    rows = []
    for scan in scans:
        reference = reference_reader.read(data_dir / scan["source"]).text
        started = time.monotonic()
        document = reader.read(data_dir / scan["file"])
        rows.append(
            {
                **scan,
                "seconds": round(time.monotonic() - started, 2),
                "confidence": document.confidence,
                "cer": round(cer(reference, document.text), 4),
                "fields": fields_found(truth[scan["id"]]["invoice"], document.text),
            }
        )
        print(
            f"{scan['id']}  {scan['tier']:<8}{'stamp' if scan['stamp'] else '':<6}"
            f"cer {rows[-1]['cer']:.3f}  conf {rows[-1]['confidence']}  missing "
            + ",".join(f for f, ok in rows[-1]["fields"].items() if ok is False)
        )

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[f"tier={row['tier']}"].append(row)
        groups[f"stamp={row['stamp']}"].append(row)
    report = {
        "backend": args.backend,
        "split": args.split,
        "overall": summarize(rows),
        "by": {name: summarize(group) for name, group in sorted(groups.items())},
        "rows": rows,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"transcription-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"overall": report["overall"], "by": report["by"]}, indent=2))
    print(f"Written to {out}")


if __name__ == "__main__":
    main()
