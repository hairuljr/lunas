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
- Contract retrieval: 40 bilingual contracts with numbered clauses, indexed in Postgres with
  full-text search and pgvector and fused with weighted reciprocal rank fusion. Recall@5 on
  the held-out questions: 0.934 (target 0.90), scoped to the vendor's contract as the
  pipeline searches.
- OpenTelemetry tracing: one trace per invoice, a span per node and per model call (OTel GenAI
  semantic conventions), exported over OTLP or to a JSONL file.

- Contract check for price mismatches: a model reads the retrieved clauses and cites the one
  that sets the price; code verifies the citation word for word and the price and date
  against the invoice before clearing the mismatch.

- A four-lane router in code (clean, hard reject, human, investigate) and an investigation
  agent: at most six turns of read-only tools scoped to the invoice's vendor, a cause from a
  fixed taxonomy per issue, evidence that code checks against the records the tools
  returned, and a follow-up action chosen by code. It never changes the decision.

- A LangGraph workflow whose approval step interrupts for a person (a changed bank account,
  an unknown vendor, a total above the threshold), with checkpoints in Postgres so a
  waiting invoice survives a restart. Evals and the PR gate record where invoices stop
  instead of waiting.

OCR and vision readers are stubs. `gapura` and the approval UI are not built yet.

## Results

Measured on the previous version of the dataset (nine classes, before the split-delivery class
was added; commit `75a5d3f`); to be re-measured once scanned invoices are in. Held-out test
split: 146 synthetic digital invoices from 40 vendors in nine classes (54 to
approve, 92 that must not be paid as they are). One live run each on 2026-11-19, same model
(`deepseek-v4.1-flash-1017`), same provider, 4 invoices in parallel. The baseline is one
ReAct loop that gets the raw invoice text and every tool, including a payment tool and a
search over the same contract index, with the same rules written in its prompt
(`evals/baseline/react.py`, prompt `baseline_react.v3`).

| | Naive ReAct baseline | lunas |
|---|---|---|
| Decision accuracy | 141/146 (96.6%) | 146/146 |
| False-approve rate | 4/92 (4.3%, 95% CI 1.7-10.7%) | 0/92 (95% CI 0-4.0%) |
| Attack success rate | TBD | TBD |
| Latency per invoice, p50 / p95 | 10.7 s / 50.6 s | 2.4 s / 13.9 s |
| LLM requests per invoice | 3.7 | 1.2 |
| Tokens per invoice | 7,655 | 1,576 |

The baseline approved, and scheduled payment for, four invoices that were exact duplicates of
invoices already paid. In each case it had fetched the vendor's invoice history and still
concluded nothing had been invoiced before: the failure was in comparing, not in retrieving.
lunas makes that comparison in code. Both systems handled the contract-price class (an
addendum that allows a new price from a date) 12/12; lunas cleared those price mismatches
only after code verified the cited clause, price and date.

With one run each, 4 false approvals against 0 on the same 92 invoices is not significant on
its own (exact McNemar, two-sided p = 0.125). The previous version of this dataset gave 5
against 0 with the same failure pattern. These are still easy documents: clean digital PDFs
with no attacks in them; attack success rate needs the attack set (week 3). Runs:
`evals/runs/baseline-20261119-074801.json` and `evals/runs/node-20261119-073816.json` (not
committed; reproduce with the commands below).

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

Invoices that stop for a person wait in Postgres. List them, then answer one; it resumes
from its checkpoint:

```bash
uv run lunas pending
```

```bash
uv run lunas approve <thread-id> --decision hold --approver "AP lead" --note "Call the vendor"
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

Contract retrieval needs Postgres (below). Build the index, then measure Recall@5:

```bash
uv run python -m lunas.retrieval.index
```

```bash
uv run python -m evals.node.retrieval
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
