# lunas

Accounts-payable agent for untrusted invoices. A GitHub portfolio project aimed at applied
AI Engineer roles, planned for release in November/December 2026.

**Read [SPEC.md](SPEC.md) before any design or implementation work.** It holds the success
criteria, architecture, tool risk tiers, eval plan, 4-week roadmap, open questions and the
decision log. Update its decision log when a design decision changes.

## Where the project stands

Update this section at the end of each working session.

- Week 1 done and pushed (2026-11-16). Week 2 in progress (2026-11-19, not committed):
  synthetic data generator (`data/generate.py`, default 300 digital invoice PDFs from 10
  vendors in 3 layouts x 2 languages, eight classes, invoice ledger, dev/test split, ground
  truth), ingestion router, digital-PDF reader, LLM extraction node
  (`pipeline/nodes/extract.py`, `prompts/extract.v2.md`), validation (arithmetic, dates,
  tax rate), duplicate check, 3-way match (with payment terms), decision table, pipeline
  runner (`pipeline/run.py`, plain functions, no LangGraph yet), node evals, naive ReAct
  baseline, tracing, 149 tests.
- Commands: `uv run python data/generate.py`, `uv run lunas run`,
  `uv run python -m evals.node.run` (dev split by default, `--split test|all`;
  `--skip-llm` for the deterministic part, `--cache`
  to record LLM replies and resume, `--cache --replay-only` to run with no LLM at all,
  `--gate` for the PR gate, `--record` to refresh its recordings with a live run),
  `uv run python -m evals.baseline.react` (live; `--cache` to resume).
- Mock ERP is `lunas.erp.ErpData`, read from the generator's JSON in `data/out/`. Postgres
  comes later.
- Week-1 measurements below are on the old 30-invoice, two-class dataset, which no longer
  exists (the generator changed). One run each, dev data the prompt was written against.
  All three runs:
  key-field exact match 210/210, whole invoice 30/30, 0 failed extractions, end-to-end
  decision 30/30, false-approve 0/10. Match + decide on ground-truth fields: 30/30.

  | Provider | Model | Structured output | System prompt | Workers | Wall time | Run file |
  |---|---|---|---|---|---|---|
  | Custom fallback proxy | `deepseek-v4.1-flash` | `none` | folded into user message | 1, 8 s gap | about 4 min | `node-20261112-091912.json` |
  | Sumopod (baseline) | `deepseek-v4.1-flash-1017` | `json_schema` | `system` | 4 | about 18 s (whole command) | `node-20261112-094121.json` |
  | Sumopod, after review fixes | `deepseek-v4.1-flash-1017` | `json_schema` | `system` | 4 | 10.0 s for the 30 LLM calls | `node-20261113-024253.json` |

  Read these as "the slice works on easy cases", not as a benchmark: two classes, digital
  PDFs only, no held-out split, no pass^3. The dataset no longer separates providers on
  accuracy; only speed, limits and feature support differ.
- Sumopod cost, from provider usage dashboard on 2026-11-12 (cumulative figures): Rp 30.68 at
  11,025 tokens after the smoke test, Rp 119.65 at 50,419 tokens after the full eval. So
  the 30-invoice eval cost about Rp 89 for about 39,400 tokens: roughly Rp 3 per invoice.
- Token usage is now in the report: the 2026-11-13 Sumopod run used 30 requests (no
  retries), 33,120 prompt + 6,320 completion tokens = 39,440, about 1,315 per invoice. That
  matches the dashboard-derived estimate above. Compare Inferhub on these fields.
- Next provider to compare: Inferhub. Run the same full eval when comparing endpoints.
- **Current dev provider (since 2026-11-12): a paid OpenAI-compatible provider**, set in
  `.env` (`deepseek-v4.1-flash-1017`; five models listed, including `glm-5.3-flash`). The
  provider is Sumopod; balance is budget-capped (the 13-request smoke test cost
  about Rp 31), so prefer cached/replayed runs unless testing live. Observed in a 13-request smoke test: system messages honoured, `json_schema`
  (strict), `json_object` and plain text all work, 4 concurrent requests fine, no rate limit
  hit, about 1-2 s per invoice. Six invoices, one per layout/language, all fields correct.
  `.env` now uses `json_schema`, `system`, 4 workers, no request gap.
- The measured run above was on the custom fallback proxy, not this provider, with the system
  prompt folded into the user message.
- A custom fallback proxy (`https://proxy.example.internal`), tested earlier, was customised for agentic
  coding: `/v1/chat/completions` injects its own system prompt and ignores ours. Its
  playground route (`<host>/v1/playground` as base URL) honours system messages and is for
  development only; no proxy-specific path belongs in the repo. With the key used on
  2026-11-12 it rejected `response_format` (needs `LUNAS_LLM_STRUCTURED_OUTPUT=none`),
  allowed one concurrent request and about 44 requests per 3.5 minutes
  (`LUNAS_LLM_WORKERS=1`, `LUNAS_LLM_MIN_INTERVAL_SECONDS=8`, `LUNAS_LLM_MAX_RETRIES=6`),
  and only `deepseek-v4.1-flash` answered; the other two models returned 500.
- External code review of the week-1 slice (2026-11-13) applied, all without LLM calls:
  PO lines for the same item are summed (same item at two PO prices -> `po_price_ambiguous`);
  currency compares case-insensitively; a blank invoice bank account counts as absent and
  one without digits is `bank_account_unreadable` (hold), not `bank_account_mismatch`
  (escalate); `strict_schema` handles `Decimal | None`; `<page>`/`<document>` tags inside
  invoice text are neutralised. `PdfTextReader` still extracts tables that the extraction
  prompt does not use: kept on purpose, they are part of the `ParsedDocument` contract.
- A failed LLM request no longer aborts a batch: that invoice becomes `hold` with issue
  `llm_unavailable`, and the eval reports it as `unanswered`, apart from model errors.
- `lunas.llm.RecordingClient` records replies under `evals/cache/` (gitignored) keyed by
  provider host, model and messages. Verified live on 2026-11-13: the Sumopod run above
  recorded 30 replies, and `--cache --replay-only` with no API key then reproduced 30/30 in
  under a second (`node-20261113-024259.json`, 0 live, 30 replayed). The `llm_unavailable`
  path has only been exercised with stand-in clients; no real request failed.
- PR gate on recorded responses (SPEC §9) is built: `evals/recordings/` (to be committed)
  holds the 30 Sumopod replies of run `node-20261113-024253.json` plus `recording.json`
  (provider, model, baseline metrics, and hashes of the prompt, the `Invoice` schema and the
  PDF text). `--gate` calls no model, needs no `.env`, and exits 1 if decision or key-field
  accuracy falls more than 2 points below the recorded baseline, if false approvals rise at
  all, or if the recordings are stale. `pr-gate.yml` runs it after `data/generate.py`.
- **Changing `prompts/extract.v1.md`, the `Invoice` schema, the generator or the PDF reader
  makes the recordings stale and the gate fail on purpose.** Fix with `--record`, a live run
  (about Rp 89 on Sumopod for 30 invoices; run only when refreshing fixtures), and commit the result.
- The gate also passes in GitHub Actions on Linux (run of 2026-11-16 on `main`), so the PDFs
  render to the same text on macOS and Linux.
- Stubs: OCR and vision readers. Empty packages: `retrieval`, `gapura`, `telemetry`, `api`.
- `policies/default.yaml` is a design draft that no code loads yet. The approval threshold
  lives in `Settings.approval_threshold` and duplicates the value in that draft.
- Naive ReAct baseline built and measured (2026-11-16): `evals/baseline/react.py`, prompt
  `prompts/baseline_react.v1.md`, one loop (max 8 turns) with raw invoice text and five
  tools (`get_po`, `get_vendor`, `get_goods_receipts`, a mock `schedule_payment` that
  accepts any account, `submit_decision`). The prompt states the same decision policy lunas
  has in code, on purpose: the baseline must be fair or the comparison is worthless.
- Baseline vs lunas, one live run each, same 30 invoices, Sumopod
  `deepseek-v4.1-flash-1017`, 4 workers (`baseline-20261116-045546.json`,
  `node-20261116-045614.json`):

  | | Naive ReAct | lunas |
  |---|---|---|
  | Decision accuracy | 30/30 | 30/30 |
  | False-approve | 0/10 | 0/10 |
  | Requests per invoice | 3.7 (110 total) | 1 (30 total) |
  | Tokens per invoice | 5,488 (164,646 total) | 1,313 (39,388 total) |
  | Latency per invoice p50 / p95 | 5.7 s / 25.5 s | 1.9 s / 5.2 s |

  **The baseline is as accurate as lunas on this dataset.** It called all three lookups on
  every invoice, paid only the 20 clean ones, always to the vendor-master account and for the
  invoice total. The measured difference is cost (4.2x tokens) and latency. Do not write or
  imply an accuracy or safety advantage until harder classes (week 2) and the attack set
  (week 3) are measured. Four baseline invoices took 24-28 s and set its p95; cause not
  investigated (single run).
- Tracing built (2026-11-16): `lunas.telemetry`, manual OpenTelemetry spans, no
  instrumentor. One trace per invoice: `process_invoice` > `parse`, `extract` >
  `chat <model>` (one per model call), `validate`, `match`, `decide`. Chat spans use the
  OTel GenAI semconv (operation, provider = endpoint host, request/response model, finish
  reason, input/output tokens); our own attributes are under `app.*`. Off unless
  `LUNAS_OTLP_ENDPOINT` or `LUNAS_TRACE_FILE` is set; prompts and replies go on spans only
  with `LUNAS_TRACE_CONTENT=true`. `lunas run` and the node eval flush on exit;
  `lunas run --limit N` processes the first N invoices.
- Tracing verified on 2026-11-16: span-tree tests (in-memory exporter), a real two-invoice
  run on Sumopod written to a JSONL trace file (14 spans, 2 traces, no orphans, token counts
  present, no content), and real traces in Phoenix 21.0.0. Phoenix maps the GenAI-semconv
  chat span to its own LLM view on ingestion (kind `llm`, model, provider, prompt and
  completion tokens, the node tree, and with `LUNAS_TRACE_CONTENT=true` the system and user
  messages), so no OpenInference attributes are needed. Its cost column shows $0: it has no
  price for this model. Node spans show kind `unknown`, which is expected for plain spans.
- Phoenix in a container turns authentication on (it listens on 0.0.0.0) and answers OTLP
  exports with 401. `compose.yaml` sets `PHOENIX_ENABLE_AUTH=false` and publishes both
  ports on 127.0.0.1 only. A deployment reachable from outside (the VPS demo) must keep
  auth on and needs an API-key header on the exporter, which `lunas.telemetry` does not
  support yet. A rejected export is only reported by the OTel exporter on stderr.
- `podman compose up -d` works since 2026-11-16 (Podman machine `podman-machine-default`,
  libkrun, 2 GiB; `podman-compose` from Homebrew). Both services verified: Postgres 18.7
  with pgvector 0.8.7 (extension created in database `lunas`), and Phoenix accepting OTLP
  exports. Postgres is published on host port **5433**, because port 5432 is commonly
  occupied by an existing local PostgreSQL service; `LUNAS_DATABASE_URL` defaults to 5433. No code
  uses the database yet.
- Not traced: the naive ReAct baseline (it keeps its own counters), metrics, and scores
  linked to traces.
- Week 1 (SPEC §12) is complete.
- Sumopod balance: `GET <host>/v2/user/info` with the API key as bearer token returns
  `max_budget` and `spend`; balance = `max_budget - spend`. On 2026-11-16: budget 3.428,
  spend 0.037 after all runs so far. Check it before and after a large run instead of
  estimating cost from tokens.
- Embeddings (decided 2026-11-16): `text-embedding-3-small` from Sumopod for now, in
  `LUNAS_EMBEDDING_MODEL`. Smoke-tested only: 1,536 dimensions; an Indonesian clause and
  its English translation scored 0.67 cosine against 0.22 for an unrelated item. No
  retrieval code or Recall@5 measurement exists yet.
- Vision model smoke-tested (2026-11-16): `glm-5.3-flash` on Sumopod transcribed three
  clean 110-dpi renders of synthetic invoices (classic/id, modern/en, compact/id) with
  every word of the text layer present (only the compact layout's dashed rules differed),
  in 10-16 s and about 2,300 tokens per page. Not tested: noisy or skewed scans, and the
  `VisionReader` itself is still a stub.
- Week 2 order agreed with the owner: remaining data classes + dev/test split, duplicate
  and payment-terms checks + router, re-measure baseline vs lunas, contracts + hybrid
  retrieval, investigation loop + LangGraph approval, OCR and vision.
- Step 1-2 done (2026-11-19): eight classes with fixed expected outcomes (`EXPECTED` in
  `data/generate.py`): clean -> approve; price_mismatch, quantity_mismatch (short goods
  receipt, billed in full), wrong_tax (12% billed and printed), payment_terms (shorter than
  the vendor master), duplicate/renumbered (`possible_duplicate`) -> hold; duplicate/exact,
  no_po/missing, no_po/unknown -> reject; bank_account_change (new account + a note asking
  to pay it) -> escalate. 300 invoices: 109 clean, 27-28 per other class; each class is
  split half dev (153), half test (147). Iterate on dev only; test is for reported numbers.
- New deterministic checks: `tax_mismatch` (tax != `LUNAS_TAX_RATE` x subtotal, rounding up
  to 1 unit allowed) in validate; `payment_terms_mismatch` (printed term or due date
  shorter than the vendor master) in match; `duplicates.py` (same vendor + same invoice
  number ignoring case and separators -> `duplicate_invoice`, reject; same total + same PO
  or same date under a new number -> `possible_duplicate`, hold). The vendor is the PO's,
  else the vendor-master name match. `ErpData` gained an invoice ledger
  (`invoice_history.json`: 3 past paid invoices per vendor + the originals of duplicates).
  `pipeline.run.check_invoice` is the single list of checks used by the pipeline, the eval
  and the generator tests.
- `Invoice` gained `payment_terms_days` (only when printed as a term); hence prompt
  `extract.v2` (v1 kept for history). Measured on dev, one live run on Sumopod
  (`node-20261119-023239.json`): extraction 153/153 invoices fully correct, decision
  153/153, every class 14/14, false-approve 0/98. 218k tokens, cost 0.0286 of the balance
  (3.3905 -> 3.3619). Still easy cases: clean digital PDFs, no attacks.
- The PR gate was re-recorded on that run: `evals/recordings/` holds the 153 dev replies
  (616 KB) and `recording.json` stores the split; `--gate` replays it and passes offline.
- Router (SPEC step 7) deferred to the investigation-loop step: nothing consumes a route yet.
- **The naive baseline is not updated for the new classes.** Its prompt `baseline_react.v1`
  knows nothing of duplicates, the tax rate or payment terms, and it has no ledger tool, so
  running it now would be unfair. Before re-measuring (week-2 step 3): prompt v2 with the
  same policy as the code, a read-only `find_invoices` tool, and `--split`. Then run both on
  the test split; README numbers come from that run.

## Design rules that must not erode

- The main path is a workflow with code gates. The only agent loop is exception
  investigation: read-only tools, max 6 turns.
- Raw invoice text reaches only the extraction LLM, which has no tools. Other nodes get typed,
  validated fields. OCR and vision output is untrusted too.
- Arithmetic, matching, routing and the final decision are code, never an LLM.
- The payment destination always comes from the vendor master, never from the invoice.
- Every ingest backend returns the same `ParsedDocument`. The vision backend transcribes; it
  does not fill `Invoice` fields.
- Benchmark numbers come from eval runs. Never write a number in README or SPEC that was not
  measured; targets are labelled as targets.

## Tooling

- `uv` for everything Python: `uv sync`, `uv add <pkg>`, `uv run pytest -q`,
  `uv run ruff check .`, `uv run ruff format .`. No pip, no manual venv.
- Python 3.14 (`.python-version`). 3.15 is installed on the machine but is too new for the
  ML wheels the OCR and classifier work will need.
- Podman, not Docker: `podman compose up -d`. Images must publish arm64 (Mac Mini M4 locally,
  Oracle VPS for the demo).
- Use the latest stable version of every dependency, action and image, and look the version
  up rather than recalling it. Pin container images to an exact version tag.
- LLM access goes through an OpenAI-compatible client configured by `LUNAS_*` variables
  (`.env.example`). Synthetic data only, because requests pass through third-party proxies.

## Conventions

- Code, comments, README: English. SPEC.md and architectural documentation: Indonesian with
  English technical terms.
- Prompts are versioned files in `prompts/`, one per node.
- Each new node ships with its eval in `evals/node/`.
