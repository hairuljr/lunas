from decimal import Decimal

import pytest
from pydantic import BaseModel

from lunas.config import Settings
from lunas.llm import (
    JsonSchema,
    LLMNotConfiguredError,
    Message,
    OpenAICompatClient,
    StructuredOutputError,
    complete_structured,
    strict_schema,
)
from lunas.llm.structured import parse_json
from lunas.models import Invoice


class Receipt(BaseModel):
    title: str
    amount: Decimal
    memo: str | None = None


class ScriptedClient:
    """Returns canned replies in order and records what it was sent."""

    model = "scripted"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[Message]] = []
        self.schemas: list[JsonSchema | None] = []

    def complete(self, messages: list[Message], schema: JsonSchema | None = None) -> str:
        self.calls.append(list(messages))
        self.schemas.append(schema)
        return self.replies.pop(0)


PROMPT: list[Message] = [{"role": "user", "content": "extract"}]


def test_valid_reply_is_parsed_without_retry():
    client = ScriptedClient('{"title": "Lunch", "amount": 125000.50, "memo": null}')

    receipt = complete_structured(client, PROMPT, Receipt)

    assert receipt == Receipt(title="Lunch", amount=Decimal("125000.50"))
    assert len(client.calls) == 1
    assert client.schemas[0] == strict_schema(Receipt)


def test_amounts_do_not_pass_through_floats():
    client = ScriptedClient('{"title": "x", "amount": 0.1, "memo": null}')

    assert complete_structured(client, PROMPT, Receipt).amount == Decimal("0.1")


def test_code_fences_and_prose_around_the_json_are_tolerated():
    client = ScriptedClient('Here it is:\n```json\n{"title": "x", "amount": 5, "memo": null}\n```')

    assert complete_structured(client, PROMPT, Receipt).amount == Decimal(5)


def test_invalid_reply_is_retried_once_with_the_validation_error():
    bad = '{"title": "Lunch", "amount": "a lot"}'
    client = ScriptedClient(bad, '{"title": "Lunch", "amount": 5, "memo": null}')

    receipt = complete_structured(client, PROMPT, Receipt)

    assert receipt.amount == Decimal(5)
    retry = client.calls[1]
    assert retry[:1] == PROMPT
    assert retry[1] == {"role": "assistant", "content": bad}
    assert retry[2]["role"] == "user"
    assert "amount" in retry[2]["content"]


def test_reply_that_is_not_json_is_retried():
    client = ScriptedClient("I cannot do that.", '{"title": "x", "amount": 1, "memo": null}')

    assert complete_structured(client, PROMPT, Receipt).title == "x"
    assert len(client.calls) == 2


def test_two_invalid_replies_raise():
    client = ScriptedClient("{}", "{}")

    with pytest.raises(StructuredOutputError, match="title"):
        complete_structured(client, PROMPT, Receipt)
    assert len(client.calls) == 2


def test_parse_json_rejects_text_without_an_object():
    with pytest.raises(ValueError, match="no JSON object"):
        parse_json("no braces here")


def test_strict_schema_requires_every_property_and_forbids_extras():
    schema = strict_schema(Receipt).schema

    # A field named `title` must survive the removal of the `title` schema keyword.
    assert schema["required"] == ["title", "amount", "memo"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["amount"] == {"type": "number"}
    assert schema["properties"]["memo"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert "title" not in schema


def test_strict_schema_keeps_optional_amounts_nullable():
    class Quote(BaseModel):
        discount: Decimal | None = None
        reference: str | float

    properties = strict_schema(Quote).schema["properties"]

    assert properties["discount"] == {"anyOf": [{"type": "number"}, {"type": "null"}]}
    # Only Decimal's own number-or-string shape is collapsed, not any union with a number.
    assert properties["reference"] == {"anyOf": [{"type": "string"}, {"type": "number"}]}


def test_strict_schema_tightens_nested_models():
    schema = strict_schema(Invoice)

    assert schema.name == "Invoice"
    line_item = schema.schema["$defs"]["LineItem"]
    assert line_item["required"] == ["description", "quantity", "unit_price", "amount"]
    assert line_item["additionalProperties"] is False
    assert set(schema.schema["required"]) == set(Invoice.model_fields)


def test_client_requires_llm_settings():
    settings = Settings(_env_file=None, llm_base_url="", llm_api_key="", llm_model="")

    with pytest.raises(LLMNotConfiguredError, match="LUNAS_LLM_BASE_URL"):
        OpenAICompatClient.from_settings(settings)


@pytest.mark.parametrize(
    ("mode", "expected_format"),
    [
        (
            "json_schema",
            {
                "type": "json_schema",
                "json_schema": {"name": "Receipt", "strict": True, "schema": {"type": "object"}},
            },
        ),
        ("json_object", {"type": "json_object"}),
        ("none", None),
    ],
)
def test_client_asks_for_json_the_way_the_endpoint_supports(mode, expected_format, monkeypatch):
    client = OpenAICompatClient("http://localhost:1/v1", "test-key", "some-model", mode)
    sent = {}

    def create(**request):
        sent.update(request)
        message = type("Message", (), {"content": "{}"})
        choice = type("Choice", (), {"message": message})
        usage = type("Usage", (), {"prompt_tokens": 7, "completion_tokens": 3})
        return type("Response", (), {"choices": [choice], "usage": usage})

    monkeypatch.setattr(client._client.chat.completions, "create", create)

    assert client.complete(PROMPT, JsonSchema("Receipt", {"type": "object"})) == "{}"
    assert sent["model"] == "some-model"
    assert sent["messages"] == PROMPT
    assert sent.get("response_format") == expected_format
    assert "temperature" not in sent
    assert client.usage == {"requests": 1, "prompt_tokens": 7, "completion_tokens": 3}


def test_system_prompt_can_be_folded_into_the_user_message(monkeypatch):
    client = OpenAICompatClient("http://localhost:1/v1", "k", "m", "none", system_role="user")
    sent = {}

    def create(**request):
        sent.update(request)
        message = type("Message", (), {"content": "{}"})
        choice = type("Choice", (), {"message": message})
        usage = type("Usage", (), {"prompt_tokens": 7, "completion_tokens": 3})
        return type("Response", (), {"choices": [choice], "usage": usage})

    monkeypatch.setattr(client._client.chat.completions, "create", create)
    messages = [
        {"role": "system", "content": "Rules."},
        {"role": "user", "content": "Document."},
        {"role": "assistant", "content": "{}"},
        {"role": "user", "content": "Fix it."},
    ]

    client.complete(messages)

    assert sent["messages"] == [
        {"role": "user", "content": "Rules.\n\nDocument."},
        {"role": "assistant", "content": "{}"},
        {"role": "user", "content": "Fix it."},
    ]
    assert messages[0]["role"] == "system"  # the caller's list is not modified


def test_requests_are_spaced_by_the_minimum_interval(monkeypatch):
    client = OpenAICompatClient("http://localhost:1/v1", "k", "m", min_interval=2.0)
    clock, sleeps = [100.0], []
    monkeypatch.setattr("lunas.llm.client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("lunas.llm.client.time.sleep", sleeps.append)

    client._wait_for_slot()
    client._wait_for_slot()
    clock[0] = 110.0
    client._wait_for_slot()

    assert sleeps == [0.0, 2.0, 0.0]


def test_endpoint_errors_become_llm_request_errors(monkeypatch):
    import httpx2
    from openai import APIConnectionError

    from lunas.llm import LLMRequestError

    client = OpenAICompatClient("http://localhost:1/v1", "k", "m")

    def create(**request):
        raise APIConnectionError(request=httpx2.Request("POST", "http://localhost:1/v1"))

    monkeypatch.setattr(client._client.chat.completions, "create", create)

    with pytest.raises(LLMRequestError, match="APIConnectionError"):
        client.complete(PROMPT)
    assert client.usage["requests"] == 0
