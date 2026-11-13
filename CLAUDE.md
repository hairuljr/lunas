# lunas

Accounts-payable agent for untrusted invoices. A GitHub portfolio project aimed at applied
AI Engineer roles, planned for release in November/December 2026.

**Read [SPEC.md](SPEC.md) before any design or implementation work.** It holds the success
criteria, architecture, tool risk tiers, eval plan, 4-week roadmap, open questions and the
decision log. Update its decision log when a design decision changes.

## Where the project stands

Update this section at the end of each working session.

- Week-1 slice built (2026-11-12), not committed. Working: synthetic data generator
  (`data/generate.py`: 10 vendors, 30 POs + goods receipts, 30 digital invoice PDFs in 3
  layouts x 2 languages, classes `clean` and `price_mismatch`, ground truth), ingestion
  router, digital-PDF reader, LLM extraction node (`pipeline/nodes/extract.py`,
  `prompts/extract.v1.md`), validation, 3-way match, decision table, pipeline runner
  (`pipeline/run.py`, plain functions, no LangGraph yet), node evals, 104 tests.
- Commands: `uv run python data/generate.py`, `uv run lunas run`,
  `uv run python -m evals.node.run` (`--skip-llm` for the deterministic part, `--cache`
  to record LLM replies and resume, `--cache --replay-only` to run with no LLM at all,
  `--gate` for the PR gate, `--record` to refresh its recordings with a live run).
- Mock ERP is `lunas.erp.ErpData`, read from the generator's JSON in `data/out/`. Postgres
  comes later.
- Measured, one run each, 30 invoices, dev data the prompt was written against. All three runs:
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
- The gate has passed locally on macOS only. It has never run in GitHub Actions: nothing is
  committed or pushed. If the PDFs render to different text on Linux, the gate will say the
  documents hash differs; that would need recordings made on Linux or a looser key.
- Stubs: OCR and vision readers. Empty packages: `retrieval`, `gapura`, `telemetry`, `api`.
- `policies/default.yaml` is a design draft that no code loads yet. The approval threshold
  lives in `Settings.approval_threshold` and duplicates the value in that draft.
- `compose.yaml` has never been run: no Podman machine existed on the Mac when it was written.
- Left for week 1 (SPEC §12): tracing and the naive ReAct baseline.
- Planned for week 2: validating vision model support (`glm-5.3-flash` is listed on
  Sumopod but untested).

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
