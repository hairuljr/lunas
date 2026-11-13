from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


@cache
def load_prompt(node: str, version: str) -> str:
    """Prompts are versioned files, one per node: prompts/<node>.<version>.md."""
    return (PROMPTS_DIR / f"{node}.{version}.md").read_text(encoding="utf-8").strip()
