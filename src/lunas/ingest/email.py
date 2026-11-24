"""Invoices that arrive as an email (.eml): the body and the PDF attachment.

The body is as untrusted as the invoice, and it is where an attacker who cannot touch the
PDF writes instead. It is read as the document's first page, so it reaches the same
places the invoice text reaches (the extraction model, the grounding check, the scanner)
and nothing else.
"""

import hashlib
import tempfile
from email import policy
from email.parser import BytesParser
from functools import cache
from pathlib import Path

from lunas.models import Page, ParsedDocument

_ATTACHMENTS = Path(tempfile.gettempdir()) / "lunas-email-attachments"


def is_email(path: Path) -> bool:
    return path.suffix.lower() == ".eml"


@cache
def _parts(path: Path) -> tuple[str, Path]:
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    header = "\n".join(
        f"{name}: {message[name]}" for name in ("From", "To", "Subject") if message[name]
    )
    body = message.get_body(preferencelist=("plain",))
    text = f"{header}\n\n{body.get_content() if body else ''}".strip()
    for part in message.iter_attachments():
        if part.get_content_type() == "application/pdf":
            data = part.get_content()
            _ATTACHMENTS.mkdir(parents=True, exist_ok=True)
            # Named by content: one file per distinct attachment, shared across runs.
            target = _ATTACHMENTS / f"{hashlib.sha256(data).hexdigest()[:20]}.pdf"
            if not target.exists():
                target.write_bytes(data)
            return text, target
    raise ValueError(f"{path.name} has no PDF attachment")


def email_body(path: Path) -> str:
    return _parts(path)[0]


def email_attachment(path: Path) -> Path:
    return _parts(path)[1]


def with_body(path: Path, attachment: ParsedDocument) -> ParsedDocument:
    """The attachment's document with the email body as page 1."""
    pages = [Page(number=1, text=email_body(path))] + [
        page.model_copy(update={"number": page.number + 1}) for page in attachment.pages
    ]
    return attachment.model_copy(update={"pages": pages})


def document_path(path: Path) -> Path:
    """The file a reader reads: the attachment of an email, else the file itself."""
    return email_attachment(path) if is_email(path) else path


def read_with(reader, path: Path) -> ParsedDocument:
    """`reader.read`, with an email's body prepended to its attachment."""
    if is_email(path):
        return with_body(path, reader.read(email_attachment(path)))
    return reader.read(path)
