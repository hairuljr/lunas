import re
from decimal import Decimal, InvalidOperation


def normalize(text: str) -> str:
    """Case, punctuation and spacing do not distinguish two names or item descriptions."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


# An email address as written in documents and tool arguments.
EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


def digits(value: object) -> str:
    """Only the digits: how account numbers are compared everywhere (match, gapura, evals)."""
    return re.sub(r"\D", "", str(value or ""))


def parse_amount(value: object) -> Decimal | None:
    """An amount as a tool argument carries it (string or number); None if it is not one."""
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None
