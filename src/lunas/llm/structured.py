"""Typed LLM output: JSON schema for the request, Pydantic validation of the reply, and
one corrective retry. Strict mode on a proxy is not trusted; the validation here is."""

import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError

from lunas.llm.client import ChatClient, JsonSchema, Message


class StructuredOutputError(Exception):
    """The model did not return valid output, even after the retry."""


def strict_schema(model: type[BaseModel]) -> JsonSchema:
    """Pydantic's schema, tightened to what strict structured output accepts: every
    property required, no extra properties, no defaults, and amounts as JSON numbers."""
    return JsonSchema(name=model.__name__, schema=_tighten(model.model_json_schema()))


def _tighten(node: Any) -> Any:
    if isinstance(node, list):
        return [_tighten(item) for item in node]
    if not isinstance(node, dict):
        return node
    # Pydantic renders Decimal as number-or-string, plus null when the field is optional.
    types = [option.get("type") for option in node.get("anyOf", [])]
    if types == ["number", "string"]:
        return {"type": "number"}
    if types == ["number", "string", "null"]:
        return {"anyOf": [{"type": "number"}, {"type": "null"}]}

    tightened = {
        # Keys of `properties` and `$defs` are names, not schema keywords: keep them all.
        key: {name: _tighten(value) for name, value in child.items()}
        if key in ("properties", "$defs")
        else _tighten(child)
        for key, child in node.items()
        if key not in ("default", "title")
    }
    if "properties" in tightened:
        tightened["required"] = list(tightened["properties"])
        tightened["additionalProperties"] = False
    return tightened


def parse_json(text: str) -> Any:
    """Parse the JSON object in a reply, tolerating code fences and surrounding prose.
    Numbers become Decimal so amounts never pass through binary floats."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("reply contains no JSON object")
    return json.loads(text[start : end + 1], parse_float=Decimal)


def _describe(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "\n".join(
            f"- {'.'.join(str(part) for part in item['loc']) or '(root)'}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
    return f"- not valid JSON: {error}"


def complete_structured[T: BaseModel](
    client: ChatClient, messages: list[Message], model: type[T], retries: int = 1
) -> T:
    schema = strict_schema(model)
    conversation = list(messages)
    for attempt in range(retries + 1):
        reply = client.complete(conversation, schema)
        try:
            return model.model_validate(parse_json(reply))
        except ValueError as error:  # includes JSONDecodeError and pydantic's ValidationError
            if attempt == retries:
                raise StructuredOutputError(
                    f"{model.__name__} still invalid after {retries} retry:\n{_describe(error)}"
                ) from error
            conversation += [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": "That reply failed validation:\n"
                    f"{_describe(error)}\n"
                    "Return the corrected JSON object only, with every field of the schema.",
                },
            ]
    raise AssertionError("unreachable")
