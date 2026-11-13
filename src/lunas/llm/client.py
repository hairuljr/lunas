"""Chat client for any OpenAI-compatible endpoint, configured by `LUNAS_LLM_*`."""

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from openai import APIError, OpenAI

from lunas.config import Settings, StructuredOutput, SystemRole

# `StructuredOutput` is how the endpoint is asked for JSON. Proxies differ, so it is config:
#   json_schema  strict structured output (`response_format: json_schema`)
#   json_object  JSON mode, shape described only by the prompt
#   none         plain completion; the JSON is cut out of the reply text
# Whatever the mode, the reply is validated with Pydantic by the caller.

Message = dict[str, str]


@dataclass(frozen=True)
class JsonSchema:
    name: str
    schema: dict[str, Any]


class ChatClient(Protocol):
    model: str

    def complete(self, messages: list[Message], schema: JsonSchema | None = None) -> str: ...


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
        if self._system_role == "user":
            messages = fold_system_messages(messages)
        request: dict[str, Any] = {"model": self.model, "messages": messages}
        if self._temperature is not None:
            request["temperature"] = self._temperature
        if schema is not None and self._structured_output == "json_schema":
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.name, "strict": True, "schema": schema.schema},
            }
        elif schema is not None and self._structured_output == "json_object":
            request["response_format"] = {"type": "json_object"}

        self._wait_for_slot()
        try:
            response = self._client.chat.completions.create(**request)
        except APIError as error:
            raise LLMRequestError(f"{type(error).__name__}: {error}") from error
        with self._throttle:
            self.usage["requests"] += 1
            if response.usage is not None:
                self.usage["prompt_tokens"] += response.usage.prompt_tokens
                self.usage["completion_tokens"] += response.usage.completion_tokens
        return response.choices[0].message.content or ""


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
