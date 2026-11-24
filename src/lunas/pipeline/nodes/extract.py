"""Step 3: ParsedDocument -> typed Invoice.

The only node that sees raw invoice text, and the model it calls has no tools. Whatever
the text says, the worst outcome is a wrong field, which steps 4, 5 and 9 check in code.
"""

import re

from lunas.llm import ChatClient, Message, complete_structured, load_prompt
from lunas.models import Invoice, ParsedDocument
from lunas.telemetry import span

PROMPT_NAME = "extract"
PROMPT_VERSION = "v2"


# The sender controls the page text, so it must not be able to close or open the wrapper
# tags itself. A backslash after "<" breaks the tag and leaves the text readable.
_WRAPPER_TAG = re.compile(r"<(?=/?(?:document|page)\b)", re.IGNORECASE)


def build_messages(document: ParsedDocument) -> list[Message]:
    pages = "\n\n".join(
        f'<page number="{page.number}">\n{_WRAPPER_TAG.sub("<\\\\", page.text)}\n</page>'
        for page in document.pages
    )
    return [
        {"role": "system", "content": load_prompt(PROMPT_NAME, PROMPT_VERSION)},
        {"role": "user", "content": f"<document>\n{pages}\n</document>"},
    ]


def extract_invoice(document: ParsedDocument, client: ChatClient) -> Invoice:
    """Raises StructuredOutputError if the reply is still invalid after one retry."""
    attributes = {"app.prompt.name": PROMPT_NAME, "app.prompt.version": PROMPT_VERSION}
    with span("extract", attributes):
        return complete_structured(client, build_messages(document), Invoice, retries=1)
