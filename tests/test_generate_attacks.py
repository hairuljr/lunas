import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from data.generate_attacks import (
    ATTACK_MANIFEST_FILE,
    CHANNELS,
    GOALS,
    build_attacks,
)
from lunas.gapura.scanner import hidden_text
from lunas.ingest import get_reader
from lunas.ingest.email import read_with


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("attacks")
    build_attacks(path)
    return path


def manifest(out: Path) -> list[dict]:
    return json.loads((out / ATTACK_MANIFEST_FILE).read_text(encoding="utf-8"))["attacks"]


def test_sixty_attacks_cover_every_goal_channel_and_language(out: Path):
    attacks = manifest(out)
    assert len(attacks) == 60
    assert Counter(a["goal"] for a in attacks) == {goal: 12 for goal in GOALS}
    assert Counter(a["channel"] for a in attacks) == {channel: 10 for channel in CHANNELS}
    assert Counter(a["language"] for a in attacks) == {"en": 30, "id": 30}
    assert len({a["base_id"] for a in attacks}) == 60


def test_each_channel_puts_the_payload_where_it_says(out: Path):
    reader = get_reader("pdf_text")
    for attack in manifest(out):
        path = out / attack["file"]
        phrase = attack["payload"].split()[:4]
        if attack["channel"] == "image":
            assert reader.read(path).text.strip() == ""  # no text layer at all
            continue
        text = " ".join(read_with(reader, path).text.split())
        if attack["channel"] == "encoded":
            assert " ".join(phrase) not in text
        else:
            assert " ".join(phrase) in text, attack["id"]
        concealed = hidden_text(path) if path.suffix == ".pdf" else ""
        assert bool(concealed) == (attack["channel"] == "hidden_text"), attack["id"]


def test_the_email_channel_reads_the_body_then_the_invoice(out: Path):
    attack = next(a for a in manifest(out) if a["channel"] == "email_body")
    document = read_with(get_reader("pdf_text"), out / attack["file"])
    assert document.pages[0].text.startswith("From: ")
    assert " ".join(attack["payload"].split()) in " ".join(document.pages[0].text.split())
    assert attack["payload"] not in document.pages[1].text  # the PDF itself is clean
    assert len(document.pages) >= 2


def test_the_attack_set_is_byte_identical(out: Path, tmp_path: Path):
    build_attacks(tmp_path)
    for attack in manifest(out)[:12]:
        first = hashlib.sha256((out / attack["file"]).read_bytes()).hexdigest()
        again = hashlib.sha256((tmp_path / attack["file"]).read_bytes()).hexdigest()
        assert first == again, attack["id"]


def test_the_subtle_set_puts_each_held_out_phrasing_in_a_visible_note(tmp_path: Path):
    from data.generate_attacks import HELD_OUT, SUBTLE_MANIFEST_FILE, build_subtle_attacks

    build_subtle_attacks(tmp_path)
    attacks = json.loads((tmp_path / SUBTLE_MANIFEST_FILE).read_text(encoding="utf-8"))["attacks"]
    assert [a["payload"] for a in attacks] == [text for _, _, text in HELD_OUT]
    reader = get_reader("pdf_text")
    for attack in attacks:
        text = " ".join(reader.read(tmp_path / attack["file"]).text.split())
        assert " ".join(attack["payload"].split()[:5]) in text, attack["id"]
