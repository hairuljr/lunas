"""Vision transcription: a vision LLM copies each page image into text.

It transcribes and nothing more: it does not fill `Invoice` fields. Extraction stays one
node that is evaluated the same way for every backend (SPEC §4a). The output is untrusted
like any document text, and more so: the transcriber is a model reading the sender's
image, so text in the image can try to steer it. That is why it is used as a second reader
next to OCR, and why the read node holds an invoice when the two disagree.
"""

import base64
import io
from pathlib import Path

from lunas.ingest.ocr import _images
from lunas.llm import ChatClient, Message
from lunas.llm.prompts import load_prompt
from lunas.models import Page, ParsedDocument
from lunas.telemetry import span

PROMPT_NAME = "transcribe"
PROMPT_VERSION = "v1"
# Long edge of the image sent, in pixels: enough for 8-point digits on an A4 page.
MAX_EDGE = 2000
JPEG_QUALITY = 85


def page_message(image) -> list[Message]:
    image = image.convert("L")
    image.thumbnail((MAX_EDGE, MAX_EDGE))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY)
    url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    return [
        {"role": "system", "content": load_prompt(PROMPT_NAME, PROMPT_VERSION)},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Transcribe this page."},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        },
    ]


class VisionReader:
    """Paid per page and the slowest reader: used as a fallback, not by default."""

    def __init__(self, client: ChatClient | None = None) -> None:
        self._client = client

    def read(self, path: Path) -> ParsedDocument:
        if self._client is None:
            raise ValueError("The vision reader needs a client for LUNAS_VISION_MODEL")
        attributes = {"app.prompt.name": PROMPT_NAME, "app.prompt.version": PROMPT_VERSION}
        pages = []
        with span("transcribe", attributes):
            for number, image in enumerate(_images(path), start=1):
                text = self._client.complete(page_message(image)).strip()
                pages.append(Page(number=number, text=text))
        return ParsedDocument(pages=pages, backend="vision", confidence=None)
