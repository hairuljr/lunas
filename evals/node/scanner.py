"""The document scanner's recall on the attack set and its false flags on the dataset.
Code only, no model.

    uv run python -m evals.node.scanner

Recall: attacks flagged, by channel and goal (image attacks are read with OCR, as the
pipeline reads them). False flags: invoices of the dataset flagged, by class; on the
invoices that should be approved, a flag means a benign invoice blocked (SPEC §2: <= 3%).
"""

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from data.generate import load_ground_truth
from data.generate_attacks import ATTACK_MANIFEST_FILE, HELD_OUT
from lunas.config import Settings
from lunas.erp import ErpData
from lunas.gapura.scanner import scan_document, scan_text
from lunas.ingest import choose_backend, get_reader
from lunas.ingest.email import document_path, read_with
from lunas.models import Decision

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"


def _scan(path: Path, settings: Settings, known: set[str]):
    source = document_path(path)
    backend = choose_backend(source, settings.ingest_mode, settings.min_chars_per_page)
    document = read_with(get_reader(backend), path)
    return scan_document(document.text, source, known)


def main() -> None:
    settings = Settings()
    data_dir = settings.data_dir
    erp = ErpData.load(data_dir)
    known = erp.vendor_emails() | set(settings.own_emails)

    attacks = json.loads((data_dir / ATTACK_MANIFEST_FILE).read_text(encoding="utf-8"))["attacks"]
    attack_rows = []
    for attack in attacks:
        findings = _scan(data_dir / attack["file"], settings, known)
        attack_rows.append(
            {**{k: attack[k] for k in ("id", "goal", "channel", "language")},
             "flagged": bool(findings), "checks": sorted({f.check for f in findings})}
        )  # fmt: skip
    by: dict[str, list[bool]] = defaultdict(list)
    for row in attack_rows:
        for key in ("channel", "goal", "language"):
            by[f"{key}={row[key]}"].append(row["flagged"])

    benign_rows = []
    for record in load_ground_truth(data_dir):
        findings = _scan(data_dir / record.file, settings, known)
        benign_rows.append(
            {"id": record.id, "label": record.label, "split": record.split,
             "approve": record.expected_decision == Decision.APPROVE,
             "flagged": bool(findings), "checks": sorted({f.check for f in findings})}
        )  # fmt: skip
    by_label: dict[str, list[bool]] = defaultdict(list)
    for row in benign_rows:
        by_label[row["label"]].append(row["flagged"])
    approve = [r for r in benign_rows if r["approve"]]

    held_out = [
        {"goal": goal, "language": language, "text": text, "flagged": bool(scan_text(text))}
        for goal, language, text in HELD_OUT
    ]

    flagged = sum(r["flagged"] for r in attack_rows)
    print(f"Attacks flagged: {flagged}/{len(attack_rows)}")
    for name, values in sorted(by.items()):
        print(f"    {name:<28} {sum(values)}/{len(values)}")
    for row in attack_rows:
        if not row["flagged"]:
            print(f"    MISSED {row['id']} {row['goal']} via {row['channel']} ({row['language']})")
    print("Dataset invoices flagged, by class:")
    for label, values in sorted(by_label.items()):
        print(f"    {label:<22} {sum(values)}/{len(values)}")
    blocked = sum(r["flagged"] for r in approve)
    print(f"Benign invoices that should be approved, flagged: {blocked}/{len(approve)}")
    caught = sum(r["flagged"] for r in held_out)
    print(f"Held-out paraphrases flagged (text only): {caught}/{len(held_out)}")
    for row in held_out:
        if not row["flagged"]:
            print(f"    MISSED {row['goal']} ({row['language']}): {row['text'][:70]}")

    report = {
        "attacks": {"flagged": flagged, "total": len(attack_rows), "rows": attack_rows},
        "held_out": {"flagged": caught, "total": len(held_out), "rows": held_out},
        "benign": {"approve_flagged": blocked, "approve_total": len(approve), "rows": benign_rows},
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"scanner-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Written to {out}")


if __name__ == "__main__":
    main()
