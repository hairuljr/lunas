# lunas

Accounts-payable agent that reads untrusted invoices, matches them against purchase orders and
contracts, and pays only what policy allows.

> **Status: first slice.** Digital PDFs flow from parsing to a decision on a 30-invoice
> synthetic set. The design is in [SPEC.md](SPEC.md) (in Indonesian). No benchmark numbers are
> published yet; the tables below are filled in from eval runs, not by hand.

## Why

An invoice is written by an outside party, the agent holds private vendor data, and it can move
money. That is the lethal trifecta, so safety lives in the architecture rather than in a prompt:

- Raw invoice text only reaches an extraction model that has no tools.
- Arithmetic, matching and the final decision are plain code.
- The payment destination always comes from the vendor master, never from the invoice.
- Tools sit behind `gapura`, a gateway that enforces policy and taint tracking.

## What works today

- Synthetic data generator: vendor master, POs, goods receipts and 30 digital invoice PDFs
  (three layouts, Indonesian and English) in two classes, clean and price mismatch, with
  ground truth. Fixed seed, byte-identical output.
- Ingestion router (`auto | pdf_text | ocr | vision`) and the digital-PDF reader.
- LLM extraction to a typed `Invoice` through any OpenAI-compatible endpoint: Pydantic
  validation, one corrective retry, versioned prompt (`prompts/extract.v1.md`).
- Deterministic invoice validation (line amounts, subtotal, total, dates).
- Deterministic 3-way match (PO, goods receipt, vendor master) and decision table.
- Node evals: per-field exact match for extraction, decision accuracy and false-approve rate.

OCR and vision readers are stubs. Contracts, the investigation loop, `gapura`, tracing and the
naive baseline are not built yet.

## Results

| Experiment | Naive ReAct baseline | lunas |
|---|---|---|
| Decision accuracy | TBD | TBD |
| False-approve rate | TBD | TBD |
| Attack success rate | TBD | TBD |
| p95 latency | TBD | TBD |
| Cost per invoice | TBD | TBD |

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

Run the node evals (extraction and decision). Each run is saved to `evals/runs/`:

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

The deterministic part alone, without an LLM:

```bash
uv run python -m evals.node.run --skip-llm
```

Postgres and Phoenix for later milestones:

```bash
podman compose up -d
```
