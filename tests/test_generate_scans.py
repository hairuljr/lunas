import json
from collections import Counter
from pathlib import Path

import pytest

from data.generate import build_dataset, write_dataset
from data.generate_scans import SCAN_MANIFEST_FILE, TIERS, select, write_scans
from lunas.ingest import choose_backend


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("dataset")
    write_dataset(build_dataset(7, 16), path, 7)
    write_scans(path, seed=7, per_split=1)
    return path


def test_selection_takes_the_first_of_each_class_and_split():
    cases = [
        {"id": f"inv-{n:03}", "label": label, "split": split}
        for n, (label, split) in enumerate(
            [("clean", "dev"), ("clean", "dev"), ("clean", "test"), ("duplicate", "test")]
        )
    ]
    assert [c["id"] for c in select(cases, per_split=1)] == ["inv-000", "inv-002", "inv-003"]


def test_twins_have_no_text_layer_and_keep_their_ground_truth(scanned: Path):
    scans = json.loads((scanned / SCAN_MANIFEST_FILE).read_text())["scans"]
    truth = {c["id"]: c for c in json.loads((scanned / "ground_truth.json").read_text())}

    assert scans
    assert Counter(choose_backend(scanned / s["file"]) for s in scans) == {"ocr": len(scans)}
    for scan in scans:
        assert truth[scan["id"]]["label"] == scan["label"]
        assert truth[scan["id"]]["file"] == scan["source"]
    assert {s["tier"] for s in scans} <= {t.name for t in TIERS}


def test_scans_are_byte_identical_for_a_seed(scanned: Path):
    before = {p.name: p.read_bytes() for p in (scanned / "scans").iterdir()}
    write_scans(scanned, seed=7, per_split=1)
    assert before == {p.name: p.read_bytes() for p in (scanned / "scans").iterdir()}
