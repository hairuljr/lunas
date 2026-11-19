import re


def normalize(text: str) -> str:
    """Case, punctuation and spacing do not distinguish two names or item descriptions."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())
