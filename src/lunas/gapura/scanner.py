"""Document scanner (SPEC §4 step 2): looks for content aimed at a model rather than at a
person. Code only, English and Indonesian.

A flagged document goes to a person (SPEC §3). The scanner is a screening signal, not the
defence: lunas' extraction model has no tools, the payment destination comes from the
vendor master, and the gateway checks every call whatever the scanner said. What it adds
is that an attempt is seen and stopped for review instead of passing silently.

Checks:
- hidden_text: text in a PDF's text layer that a reader does not see (white or near-white,
  under 2 pt, or outside the page). Only a model reading the text layer sees it.
- instruction: phrases that address an AI or override the process (ignore instructions,
  already approved, do not hold, pay to another account, send the vendor list...).
- encoded: zero-width, bidi-override or Unicode tag characters, and base64 runs that decode
  to text (the decoded text is scanned too).
- foreign_email: an email address in the document that is not in the vendor master.

No local classifier yet: the only non-gated multilingual option found (2026-11) is untested
on Indonesian and flags ~3% of long benign documents, which alone would use the whole
false-block budget (SPEC §2). See the decision log.
"""

import base64
import binascii
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pdfplumber

from lunas.models import Issue
from lunas.text import EMAIL

DOCUMENT_FLAGGED = "document_flagged"

_PHRASES: dict[str, list[str]] = {
    "addresses_ai": [
        r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|earlier|all|"
        r"any|your)\b.{0,20}\b(instructions?|rules?|prompts?|checks?|polic(y|ies))\b",
        r"\babaikan\b.{0,30}\b(instruksi|perintah|aturan|arahan|pemeriksaan|kebijakan)\b",
        # "a[il1]": OCR reads a capital I as l or 1 ("Al assistant").
        r"\b(note|message|instructions?|notice)s?\s+(to|for)\s+(the\s+)?(a[il1]\s+)?(ai|"
        r"assistant|model|system|agent|bot|automated|llm)\b",
        r"\b(catatan|pesan|instruksi|perintah)\s+(untuk|kepada)\s+(a[il1]|asisten|sistem|"
        r"agen|bot|model)\b",
        r"\b(you are|as an?)\s+(an?\s+)?(ai|assistant|language model|llm|agent)\b",
        r"\b(anda adalah|sebagai)\s+(ai|asisten|model bahasa|agen)\b",
        r"(\[\s*/?\s*(system|inst|assistant)\s*\]|<\s*/?\s*(system|instructions?)\s*>"
        r"|\bsystem\s*(prompt|override|message)\b)",
    ],
    "overrides_approval": [
        r"\b(pre-?approved|already\s+(been\s+)?approved|approved\s+by\s+(the\s+)?(cfo|"
        r"director|finance|management))\b",
        r"\b(approve|pay)\b.{0,20}\b(immediately|without|right away|automatically)\b",
        r"\b(do\s*n[o']t|never|no need to)\s+(hold|flag|escalate|check|verify|review|"
        r"compare|match)\b",
        r"\bskip\b.{0,15}\b(checks?|verification|review|approval|matching)\b",
        r"\b(sudah|telah)\s+disetujui\b",
        r"\bsetujui\b.{0,20}\b(segera|tanpa|langsung|otomatis)\b",
        r"\bjangan\s+(di)?(tahan|eskalasi|periksa|cek|verifikasi|bandingkan|tandai)\b",
        r"\blewati\b.{0,15}\b(pemeriksaan|verifikasi|persetujuan|pencocokan)\b",
    ],
    "redirects_payment": [
        r"\b(pay|transfer|send|remit)\b.{0,40}\bto\s+(account|acct|a/c|the following account)\b",
        r"\b(new|updated|correct|replacement|alternative)\s+(bank\s+)?(account|acct)"
        r"(\s+(number|no\.?))?\s*(is|:)",
        r"\b(transfer|bayar(kan)?|kirim(kan)?)\b.{0,40}\bke\s+rekening\s+(berikut|ini|nomor)\b",
        r"\brekening\s+(baru|pengganti|yang\s+benar)\b.{0,10}(adalah|:)",
    ],
    "requests_data": [
        r"\b(send|email|forward|share|export)\b.{0,50}\b(vendor\s+(list|master|records|data)|"
        r"bank\s+(details|accounts?)\s+of|all\s+(vendors|suppliers)|other\s+vendors)\b",
        r"\b(kirim(kan)?|teruskan|bagikan|ekspor)\b.{0,50}\b(daftar\s+vendor|data\s+"
        r"(vendor|rekening)|semua\s+(vendor|pemasok)|vendor\s+lain)\b",
    ],
    "changes_amount": [
        r"\b(add|include|plus)\b.{0,40}\b(outstanding|previous|last month|arrears|balance)\b",
        r"\b(tambahkan|sertakan|termasuk)\b.{0,40}\b(tunggakan|sisa|bulan lalu|sebelumnya)\b",
        r"\b(pay|bayar)\b.{0,25}\b(twice|double|dua kali|\d+\s*x)\b",
    ],
    "overrides_duplicate": [
        r"\b(not\s+a\s+duplicate|bukan\s+(faktur\s+)?duplikat)\b",
        r"\b(earlier|previous|original)\s+invoice\b.{0,30}\b(cancell?ed|void(ed)?|withdrawn)\b",
        r"\bfaktur\s+(sebelumnya|lama|asli)\b.{0,30}\b(dibatalkan|batal|tidak berlaku)\b",
    ],
}
_COMPILED = {
    name: [re.compile(p, re.IGNORECASE | re.DOTALL) for p in patterns]
    for name, patterns in _PHRASES.items()
}
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
_BASE64 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
HIDDEN_MIN_CHARS = 8


@dataclass(frozen=True)
class Finding:
    check: str
    detail: str


def _decoded_base64(text: str) -> list[str]:
    """Base64 runs that decode to text. A long run wraps across lines in a PDF's text layer,
    so runs are also tried with line breaks between base64 characters removed."""
    joined = re.sub(r"(?<=[A-Za-z0-9+/])[ \t]*\n[ \t]*(?=[A-Za-z0-9+/])", "", text)
    decoded = []
    for run in dict.fromkeys(_BASE64.findall(text) + _BASE64.findall(joined)):
        try:
            value = base64.b64decode(run + "=" * (-len(run) % 4), validate=True).decode("utf-8")
        except binascii.Error, UnicodeDecodeError:
            continue
        if sum(ch.isprintable() for ch in value) >= 0.9 * len(value) and " " in value:
            decoded.append(value)
    return decoded


def _phrases(text: str) -> list[Finding]:
    flat = re.sub(r"\s+", " ", text)
    return [
        Finding("instruction", name)
        for name, patterns in _COMPILED.items()
        if any(p.search(flat) for p in patterns)
    ]


def scan_text(text: str, known_emails: set[str] = frozenset()) -> list[Finding]:
    findings = _phrases(text)
    if _INVISIBLE.search(text):
        findings.append(Finding("encoded", "invisible Unicode characters"))
        findings += _phrases(_INVISIBLE.sub("", text))
    for value in _decoded_base64(text):
        findings.append(Finding("encoded", "base64 that decodes to text"))
        findings += _phrases(value)
    known = {email.lower() for email in known_emails}
    for email in {m.lower() for m in EMAIL.findall(text)}:
        if email not in known:
            findings.append(Finding("foreign_email", email))
    return list(dict.fromkeys(findings))


def _whiteish(color: object) -> bool:
    if not isinstance(color, list | tuple) or not color:
        return False
    values = [float(v) for v in color if isinstance(v, int | float)]
    if len(values) == 1:  # gray
        return values[0] >= 0.9
    if len(values) == 3:  # rgb
        return min(values) >= 0.9
    if len(values) == 4:  # cmyk
        return max(values) <= 0.1
    return False


def hidden_text(path: Path) -> str:
    """Text-layer characters a reader does not see. Empty for scans and images."""
    if path.suffix.lower() != ".pdf":
        return ""
    return _hidden_text(path, path.stat().st_mtime_ns)


@lru_cache(maxsize=1024)
def _hidden_text(path: Path, mtime: int) -> str:
    """Cached by file and modification time: a second read of the same scan does not walk
    the PDF again."""
    hidden = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for char in page.chars:
                off_page = (
                    char["x1"] < 0 or char["x0"] > page.width or char["bottom"] < 0
                    or char["top"] > page.height
                )  # fmt: skip
                if _whiteish(char.get("non_stroking_color")) or char["size"] < 2 or off_page:
                    hidden.append(char["text"])
    return "".join(hidden).strip()


def scan_document(
    text: str, path: Path | None = None, known_emails: set[str] = frozenset()
) -> list[Finding]:
    findings = scan_text(text, known_emails)
    concealed = hidden_text(path) if path is not None else ""
    if len(concealed.replace(" ", "")) >= HIDDEN_MIN_CHARS:
        findings.append(Finding("hidden_text", concealed[:120]))
    return findings


def screening_issues(findings: list[Finding]) -> list[Issue]:
    if not findings:
        return []
    summary = "; ".join(sorted({f"{f.check}: {f.detail}" for f in findings}))
    return [Issue(code=DOCUMENT_FLAGGED, message=f"The scanner flagged this document ({summary})")]
