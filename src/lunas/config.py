from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

IngestMode = Literal["auto", "pdf_text", "ocr", "vision"]
StructuredOutput = Literal["json_schema", "json_object", "none"]
SystemRole = Literal["system", "user"]


# The VAT rate every invoice in scope is charged at. The generator builds the dataset with
# this same constant, so the data and the check cannot drift apart.
DEFAULT_TAX_RATE = Decimal("0.11")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LUNAS_", env_file=".env", extra="ignore")

    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    vision_model: str = ""
    # Served by the same endpoint as the chat model.
    embedding_model: str = ""
    # How the endpoint is asked for JSON; see lunas.llm.client.
    llm_structured_output: StructuredOutput = "json_schema"
    # "user" folds system prompts into the first user message, for endpoints that drop
    # system messages.
    llm_system_role: SystemRole = "system"
    # Unset = the endpoint's default. Some models reject an explicit temperature.
    llm_temperature: float | None = None
    llm_timeout_seconds: float = 60.0
    # SDK retries (exponential backoff) on connection errors, 429 and 5xx.
    llm_max_retries: int = 2
    # Minimum seconds between the starts of two requests, for rate-limited keys. 0 = off.
    llm_min_interval_seconds: float = 0.0
    # Parallel LLM calls in batch runs. Lower it for keys with a concurrency limit.
    llm_workers: int = 4

    ingest_mode: IngestMode = "auto"
    # A PDF page with fewer extractable characters than this is treated as a scan.
    min_chars_per_page: int = 50
    # An OCR read whose weakest line scores below this is read again by the vision model,
    # as is one whose extraction fails the grounding check. Needs LUNAS_VISION_MODEL.
    ocr_min_confidence: float = 0.9

    # Allowed rounding difference when checking invoice arithmetic and PO prices.
    amount_tolerance: Decimal = Decimal("0.01")
    # VAT rate every invoice in scope is charged at (SPEC §3: configurable, no tax rules).
    tax_rate: Decimal = DEFAULT_TAX_RATE
    # An otherwise clean invoice above this total goes to a human.
    approval_threshold: Decimal = Decimal(50_000_000)
    # Our own addresses: an invoice email addressed to them is not a foreign address.
    own_emails: list[str] = ["ap@lunas-nusantara.example"]

    # Tracing is off unless one of these is set. The endpoint is an OTLP/HTTP base URL
    # (Phoenix: http://localhost:6006); the file gets one JSON span per line.
    otlp_endpoint: str = ""
    trace_file: Path | None = None
    # Put prompts and replies on spans. Synthetic data only.
    trace_content: bool = False

    # Output of data/generate.py: invoice PDFs plus the mock ERP JSON files.
    data_dir: Path = Path("data/out")
    database_url: str = "postgresql://lunas:lunas@localhost:5433/lunas"
