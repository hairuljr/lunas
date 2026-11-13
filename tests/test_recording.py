from pathlib import Path

import pytest

from lunas.config import Settings
from lunas.llm import (
    JsonSchema,
    LLMRequestError,
    Message,
    RecordingClient,
    RecordingMissingError,
    build_client,
)

PROMPT: list[Message] = [{"role": "user", "content": "extract"}]
OTHER: list[Message] = [{"role": "user", "content": "extract something else"}]


class CountingClient:
    def __init__(self, model: str = "some-model", host: str = "provider-a.example") -> None:
        self.model = model
        self.host = host
        self.calls = 0

    def complete(self, messages: list[Message], schema: JsonSchema | None = None) -> str:
        self.calls += 1
        return f"reply {self.calls} to {messages[-1]['content']}"


class FailingClient(CountingClient):
    def complete(self, messages: list[Message], schema: JsonSchema | None = None) -> str:
        self.calls += 1
        raise LLMRequestError("RateLimitError: slow down")


def test_first_call_is_live_and_the_second_is_replayed(tmp_path: Path):
    inner = CountingClient()
    client = RecordingClient(tmp_path, inner)

    first = client.complete(PROMPT)
    second = client.complete(PROMPT)

    assert first == second == "reply 1 to extract"
    assert inner.calls == 1
    assert client.counts == {"live": 1, "replayed": 1}


def test_recordings_survive_a_new_client(tmp_path: Path):
    RecordingClient(tmp_path, CountingClient()).complete(PROMPT)
    inner = CountingClient()

    assert RecordingClient(tmp_path, inner).complete(PROMPT) == "reply 1 to extract"
    assert inner.calls == 0


def test_a_different_request_model_or_provider_is_not_replayed(tmp_path: Path):
    RecordingClient(tmp_path, CountingClient()).complete(PROMPT)

    for inner, messages in (
        (CountingClient(), OTHER),
        (CountingClient(model="other-model"), PROMPT),
        (CountingClient(host="provider-b.example"), PROMPT),
    ):
        RecordingClient(tmp_path, inner).complete(messages)
        assert inner.calls == 1


def test_a_failed_request_records_nothing(tmp_path: Path):
    with pytest.raises(LLMRequestError):
        RecordingClient(tmp_path, FailingClient()).complete(PROMPT)

    assert list(tmp_path.iterdir()) == []
    assert RecordingClient(tmp_path, CountingClient()).complete(PROMPT) == "reply 1 to extract"


def test_replay_only_never_calls_a_model(tmp_path: Path):
    RecordingClient(tmp_path, CountingClient()).complete(PROMPT)
    replay = RecordingClient(tmp_path, None, model="some-model", host="provider-a.example")

    assert replay.complete(PROMPT) == "reply 1 to extract"
    assert replay.usage is None
    with pytest.raises(RecordingMissingError):
        replay.complete(OTHER)


def test_build_client_replay_only_needs_no_api_key(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        llm_base_url="https://provider-a.example/v1",
        llm_api_key="",
        llm_model="some-model",
    )
    RecordingClient(tmp_path, CountingClient()).complete(PROMPT)

    client = build_client(settings, tmp_path, replay_only=True)

    assert client.complete(PROMPT) == "reply 1 to extract"
