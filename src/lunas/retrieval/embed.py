"""Text embeddings through the same OpenAI-compatible endpoint as the chat model, with a
disk cache so a clause or a query is paid for once per model."""

import hashlib
import json
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from openai import APIError, OpenAI

from lunas.config import Settings
from lunas.llm import LLMNotConfiguredError, LLMRequestError

BATCH_SIZE = 64


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatEmbedder:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0) -> None:
        self.model = model
        self.host = urlparse(base_url).netloc
        self.usage = {"requests": 0, "tokens": 0}
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=2)

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatEmbedder:
        key = settings.llm_api_key.get_secret_value()
        if not (settings.llm_base_url and key and settings.embedding_model):
            raise LLMNotConfiguredError(
                "LUNAS_LLM_BASE_URL, LUNAS_LLM_API_KEY and LUNAS_EMBEDDING_MODEL must be set."
            )
        return cls(settings.llm_base_url, key, settings.embedding_model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            try:
                response = self._client.embeddings.create(
                    model=self.model, input=texts[start : start + BATCH_SIZE]
                )
            except APIError as error:
                raise LLMRequestError(f"{type(error).__name__}: {error}") from error
            self.usage["requests"] += 1
            self.usage["tokens"] += response.usage.total_tokens if response.usage else 0
            vectors += [item.embedding for item in sorted(response.data, key=lambda d: d.index)]
        return vectors


class CachedEmbedder:
    """One JSON file per text, keyed by provider host, model and text."""

    def __init__(self, inner: Embedder, directory: Path) -> None:
        self.model = inner.model
        self._inner = inner
        self._directory = directory
        self.counts = {"cached": 0, "embedded": 0}

    def _path(self, text: str) -> Path:
        key = json.dumps([getattr(self._inner, "host", ""), self.model, text])
        return self._directory / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def embed(self, texts: list[str]) -> list[list[float]]:
        found: dict[int, list[float]] = {}
        missing: list[int] = []
        for index, text in enumerate(texts):
            path = self._path(text)
            if path.exists():
                found[index] = json.loads(path.read_text(encoding="utf-8"))
            else:
                missing.append(index)
        if missing:
            fresh = self._inner.embed([texts[index] for index in missing])
            self._directory.mkdir(parents=True, exist_ok=True)
            for index, vector in zip(missing, fresh, strict=True):
                self._path(texts[index]).write_text(json.dumps(vector), encoding="utf-8")
                found[index] = vector
        self.counts["cached"] += len(texts) - len(missing)
        self.counts["embedded"] += len(missing)
        return [found[index] for index in range(len(texts))]
