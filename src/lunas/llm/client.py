"""Chat client for any OpenAI-compatible endpoint, configured by `LUNAS_LLM_*`."""

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from openai import APIError, OpenAI
from opentelemetry.trace import SpanKind

from lunas.config import Settings, StructuredOutput, SystemRole
from lunas.telemetry import capture_content, set_attributes, span

# `StructuredOutput` is how the endpoint is asked for JSON. Proxies differ, so it is config:
#   json_schema  strict structured output (`response_format: json_schema`)
#   json_object  JSON mode, shape described only by the prompt
#   none         plain completion; the JSON is cut out of the reply text
# Whatever the mode, the reply is validated with Pydantic by the caller.

# A chat message. Tool-calling turns carry lists (`tool_calls`), so values are not all text.
Message = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolTurn:
    """An assistant turn: text, tool calls, or both."""

    content: str
    tool_calls: list[ToolCall]


@dataclass(frozen=True)
class JsonSchema:
    name: str
    schema: dict[str, Any]


class ChatClient(Protocol):
    model: str

    def complete(self, messages: list[Message], schema: JsonSchema | None = None) -> str: ...


class ToolChatClient(Protocol):
    model: str

    def complete_tools(self, messages: list[Message], tools: list[dict[str, Any]]) -> ToolTurn: ...


class LLMNotConfiguredError(RuntimeError):
    pass


class LLMRequestError(RuntimeError):
    """The endpoint did not produce a reply (timeout, 429, 5xx...) after the SDK's retries.
    This says nothing about the model's answer: there was none."""


class OpenAICompatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        structured_output: StructuredOutput = "json_schema",
        system_role: SystemRole = "system",
        temperature: float | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        min_interval: float = 0.0,
    ) -> None:
        self.model = model
        self._structured_output = structured_output
        self._system_role = system_role
        self._temperature = temperature
        # The SDK retries connection errors, 429 and 5xx itself, with backoff.
        self._client = OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        self.host = urlparse(base_url).netloc
        # Running totals over the life of this client, for eval reports.
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._min_interval = min_interval
        self._throttle = threading.Lock()
        self._next_start = 0.0

    def _wait_for_slot(self) -> None:
        """Space request starts at least `min_interval` apart, across threads."""
        if self._min_interval <= 0:
            return
        with self._throttle:
            now = time.monotonic()
            start = max(now, self._next_start)
            self._next_start = start + self._min_interval
        time.sleep(start - now)

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatClient:
        missing = [
            name
            for name, value in (
                ("LUNAS_LLM_BASE_URL", settings.llm_base_url),
                ("LUNAS_LLM_API_KEY", settings.llm_api_key.get_secret_value()),
                ("LUNAS_LLM_MODEL", settings.llm_model),
            )
            if not value
        ]
        if missing:
            raise LLMNotConfiguredError(
                f"{', '.join(missing)} not set. Copy .env.example to .env and fill it in."
            )
        return cls(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            structured_output=settings.llm_structured_output,
            system_role=settings.llm_system_role,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            min_interval=settings.llm_min_interval_seconds,
        )

    def complete(self, messages: list[Message], schema: JsonSchema | None = None) -> str:
        request: dict[str, Any] = {}
        if schema is not None and self._structured_output == "json_schema":
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.name, "strict": True, "schema": schema.schema},
            }
        elif schema is not None and self._structured_output == "json_object":
            request["response_format"] = {"type": "json_object"}
        attributes = {
            "gen_ai.output.type": "json" if schema is not None else "text",
            "app.llm.structured_output": self._structured_output if schema else None,
        }
        return self._send(messages, request, attributes).message.content or ""

    def complete_tools(self, messages: list[Message], tools: list[dict[str, Any]]) -> ToolTurn:
        """One model call that may answer with tool calls instead of text."""
        message = self._send(messages, {"tools": tools}, {"gen_ai.output.type": "text"}).message
        return ToolTurn(
            content=message.content or "",
            tool_calls=[
                ToolCall(call.id, call.function.name, call.function.arguments)
                for call in message.tool_calls or []
            ],
        )

    def _send(
        self, messages: list[Message], request: dict[str, Any], attributes: dict[str, Any]
    ) -> Any:
        """One chat completion inside one `chat` span, with usage counted. Returns the
        first choice."""
        if self._system_role == "user":
            messages = fold_system_messages(messages)
        request = {"model": self.model, "messages": messages} | request
        if self._temperature is not None:
            request["temperature"] = self._temperature
        attributes = {
            "gen_ai.operation.name": "chat",
            # The endpoint that served the call. The same model ID is not the same model
            # behind every OpenAI-compatible host.
            "gen_ai.provider.name": self.host,
            "server.address": self.host,
            "gen_ai.request.model": self.model,
            "gen_ai.request.temperature": self._temperature,
        } | attributes
        with span(f"chat {self.model}", attributes, SpanKind.CLIENT) as current:
            if capture_content():
                current.set_attribute("gen_ai.input.messages", json.dumps(_parts(messages)))
            self._wait_for_slot()
            try:
                response = self._client.chat.completions.create(**request)
            except APIError as error:
                raise LLMRequestError(f"{type(error).__name__}: {error}") from error
            choice = response.choices[0]
            usage = response.usage
            set_attributes(
                current,
                {
                    "gen_ai.response.model": response.model,
                    "gen_ai.response.id": response.id,
                    "gen_ai.response.finish_reasons": [choice.finish_reason],
                    "gen_ai.usage.input_tokens": usage.prompt_tokens if usage else None,
                    "gen_ai.usage.output_tokens": usage.completion_tokens if usage else None,
                },
            )
            if capture_content():
                output = _parts([_as_message(choice.message)])
                output[0]["finish_reason"] = choice.finish_reason
                current.set_attribute("gen_ai.output.messages", json.dumps(output))
        with self._throttle:
            self.usage["requests"] += 1
            if usage is not None:
                self.usage["prompt_tokens"] += usage.prompt_tokens
                self.usage["completion_tokens"] += usage.completion_tokens
        return choice


def _as_message(message: Any) -> Message:
    calls = [
        {"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
        for c in getattr(message, "tool_calls", None) or []
    ]
    return {"role": "assistant", "content": message.content or "", "tool_calls": calls}


def _parts(messages: list[Message]) -> list[dict[str, Any]]:
    """Messages in the shape the GenAI semantic conventions define for span content."""
    parts = []
    for message in messages:
        content = [{"type": "text", "content": message.get("content") or ""}]
        for call in message.get("tool_calls") or []:
            function = call.get("function", call)
            content.append(
                {
                    "type": "tool_call",
                    "id": call.get("id"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments"),
                }
            )
        parts.append({"role": message["role"], "parts": content})
    return parts


def fold_system_messages(messages: list[Message]) -> list[Message]:
    """Move system prompts to the front of the first user message.

    Some proxies silently drop system messages, and the model then never sees its
    instructions. The cost is that instructions and document share one message, so the
    role boundary no longer separates them; only the prompt's own delimiters do.
    """
    system = [message["content"] for message in messages if message["role"] == "system"]
    rest = [dict(message) for message in messages if message["role"] != "system"]
    first_user = next((message for message in rest if message["role"] == "user"), None)
    if not system:
        return rest
    if first_user is None:
        return [{"role": "user", "content": "\n\n".join(system)}, *rest]
    first_user["content"] = "\n\n".join([*system, first_user["content"]])
    return rest
