"""Record LLM replies to disk and replay them.

Two uses. A batch that died halfway resumes without paying again for the invoices it
already finished. And with no live client behind it, the recordings replay offline, which
is what lets an eval run in CI without calling a model (SPEC §9).

A replayed reply is not a new measurement. Callers that report numbers must say how many
replies were replayed; see `counts`.
"""

import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import urlparse

from lunas.config import Settings
from lunas.llm.client import ChatClient, JsonSchema, Message, OpenAICompatClient


class RecordingMissingError(RuntimeError):
    """Replay-only mode was asked for a request that was never recorded."""


class RecordingClient:
    """Wraps a ChatClient. `inner=None` is replay-only: nothing is ever sent."""

    def __init__(
        self, directory: Path, inner: ChatClient | None, model: str = "", host: str = ""
    ) -> None:
        self._directory = directory
        self._inner = inner
        # Replay-only needs the labels the recording was made under to find its files.
        self.model = inner.model if inner is not None else model
        self.host = getattr(inner, "host", host)
        self.counts = {"live": 0, "replayed": 0}
        # Every recording this client read or wrote, so stale ones can be pruned.
        self.used: set[Path] = set()
        self._lock = threading.Lock()

    @property
    def usage(self) -> dict[str, int] | None:
        """Tokens of the live calls only. Replays cost nothing."""
        return getattr(self._inner, "usage", None)

    def _path(self, messages: list[Message], schema: JsonSchema | None) -> Path:
        # The provider is part of the key: the same model ID is not the same model
        # everywhere, and comparing providers must never replay one as the other.
        key = json.dumps(
            [self.host, self.model, messages, schema.name if schema else None],
            ensure_ascii=False,
            sort_keys=True,
        )
        return self._directory / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def complete(self, messages: list[Message], schema: JsonSchema | None = None) -> str:
        path = self._path(messages, schema)
        with self._lock:
            self.used.add(path)
        if path.exists():
            with self._lock:
                self.counts["replayed"] += 1
            return json.loads(path.read_text(encoding="utf-8"))["reply"]
        if self._inner is None:
            raise RecordingMissingError(
                f"No recording for this request to {self.model} in {self._directory}"
            )

        reply = self._inner.complete(messages, schema)  # a failed request records nothing
        record = {"host": self.host, "model": self.model, "reply": reply}
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)  # atomic: a killed run never leaves half a recording
        with self._lock:
            self.counts["live"] += 1
        return reply


def build_client(
    settings: Settings, cache_dir: Path | None = None, replay_only: bool = False
) -> ChatClient:
    """The configured client, optionally behind a record/replay cache.

    Replay-only needs no API key: only the model and base URL the recordings were made
    under, to find them.
    """
    if replay_only:
        if cache_dir is None:
            raise ValueError("replay-only needs a cache directory")
        host = urlparse(settings.llm_base_url).netloc
        return RecordingClient(cache_dir, None, model=settings.llm_model, host=host)
    live = OpenAICompatClient.from_settings(settings)
    return RecordingClient(cache_dir, live) if cache_dir is not None else live
