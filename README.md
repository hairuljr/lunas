# lunas

Accounts-payable agent that reads untrusted invoices, matches them against purchase orders and
contracts, and pays only what policy allows.

> **Status: first slice.** Digital PDFs flow from parsing to a decision on a 30-invoice
> synthetic set. The design is in [SPEC.md](SPEC.md) (in Indonesian). No benchmark numbers are
> exist yet for anything beyond that slice; the table below is filled in from eval runs, not
> by hand.

## Why

An invoice is written by an outside party, the agent holds private vendor data, and it can move
money. That is the lethal trifecta, so safety lives in the architecture rather than in a prompt:

- Raw invoice text only reaches an extraction model that has no tools.
- Arithmetic, matching and the final decision are plain code.
- The payment destination always comes from the vendor master, never from the invoice.
- Tools sit behind `gapura`, a gateway that enforces policy and taint tracking.

## What works today

- Synthetic data generator: vendor master, POs, goods receipts, an invoice ledger and 300
  digital invoice PDFs (three layouts, Indonesian and English) in eight classes: clean,
  price mismatch, short delivery, duplicate, wrong tax, no PO, changed bank account and
  short payment terms. Ground truth per invoice, each class split half dev, half test.
  Fixed seed, byte-identical output.
- Ingestion router (`auto | pdf_text | ocr | vision`) and the digital-PDF reader.
- LLM extraction to a typed `Invoice` through any OpenAI-compatible endpoint: Pydantic
  validation, one corrective retry, versioned prompt (`prompts/extract.v1.md`).
- Deterministic checks: invoice arithmetic, dates and tax rate; duplicates against the ERP
  ledger; 3-way match against PO, goods receipt and vendor master (including bank account
  and payment terms); a fixed decision table.
- Node evals: per-field exact match for extraction, decision accuracy and false-approve rate.

- Naive ReAct baseline for comparison (`evals/baseline/react.py`).
- OpenTelemetry tracing: one trace per invoice, a span per node and per model call (OTel GenAI
  semantic conventions), exported over OTLP or to a JSONL file.

OCR and vision readers are stubs. Contracts, the investigation loop and `gapura` are not built
yet.

## Results

Held-out test split: 147 synthetic digital invoices in eight classes (54 clean, 93 that must
not be paid as they are). One live run each on 2026-11-19, same model
(`deepseek-v4.1-flash-1017`), same provider, 4 invoices in parallel. The baseline is one
ReAct loop that gets the raw invoice text and every tool, including a payment tool, with the
same rules written in its prompt (`evals/baseline/react.py`, prompt `baseline_react.v2`).

| | Naive ReAct baseline | lunas |
|---|---|---|
| Decision accuracy | 142/147 (96.6%) | 147/147 |
| False-approve rate | 5/93 (5.4%, 95% CI 2.3-12.0%) | 0/93 (95% CI 0-4.0%) |
| Attack success rate | TBD | TBD |
| Latency per invoice, p50 / p95 | 4.8 s / 12.2 s | 1.4 s / 4.8 s |
| LLM requests per invoice | 3.4 | 1 |
| Tokens per invoice | 6,366 | 1,429 |

The baseline approved, and scheduled payment for, three duplicates and two short deliveries.
In every one of those five it had fetched the evidence (the vendor's invoice history or the
goods receipt) and still concluded that everything matched: the failure was in comparing,
not in retrieving. lunas makes those comparisons in code. On the same 93 invoices that is 5
false approvals against 0; with one run each that difference is suggestive rather than
settled (exact McNemar, two-sided p = 0.06). The baseline also used 4.5 times the tokens.

These are still easy documents: clean digital PDFs with no attacks in them. Attack success
rate needs the attack set (week 3). Runs: `evals/runs/baseline-20261119-024725.json` and
`evals/runs/node-20261119-024349.json` (not committed; reproduce with the commands below).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
uv sync
```

```bash
uv run pytest -q
```

```bash
uv run ruff check . && uv run ruff format --check .
```

Generate the dataset into `data/out/`:

```bash
uv run python data/generate.py
```

Copy `.env.example` to `.env` and point `LUNAS_LLM_*` at an OpenAI-compatible endpoint, then run
every invoice from PDF to decision:

```bash
uv run lunas run
```

Run the node evals (extraction and decision, per class) on the dev split; add `--split test`
only for numbers you report. Each run is saved to `evals/runs/`:

```bash
uv run python -m evals.node.run
```

Add `--cache` to record the model's replies and replay them on the next run, so an
interrupted run resumes without paying twice. Replayed replies are not re-measured; the
report says how many were replayed.

```bash
uv run python -m evals.node.run --cache
```

The PR gate replays the committed recordings in `evals/recordings/` without calling a model,
and fails on a regression or when the prompt, schema or dataset changed since they were
recorded:

```bash
uv run python -m evals.node.run --gate
```

After such a change, refresh the recordings with a live run and commit them:

```bash
uv run python -m evals.node.run --record
```

The naive ReAct baseline on the same invoices (live; `--cache` resumes an interrupted run,
`--split test` for reported numbers):

```bash
uv run python -m evals.baseline.react
```

The deterministic part alone, without an LLM:

```bash
uv run python -m evals.node.run --skip-llm
```

Tracing is off by default. Write spans to a file, one JSON object per line:

```bash
LUNAS_TRACE_FILE=traces.jsonl uv run lunas run --limit 2
```

Or set `LUNAS_OTLP_ENDPOINT` in `.env` to any OTLP/HTTP endpoint, such as Phoenix below
(`http://localhost:6006`). Prompts and replies are only put on spans with
`LUNAS_TRACE_CONTENT=true`.

Postgres and Phoenix (needs a compose provider such as `podman-compose`). Phoenix runs
without authentication here and both ports are published on loopback only:

```bash
podman compose up -d
```
