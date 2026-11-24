# lunas

Accounts-payable agent for untrusted invoices. A GitHub portfolio project aimed at applied
AI Engineer roles, planned for release in November/December 2026.

**Read [SPEC.md](SPEC.md) before any design or implementation work.** It holds the success
criteria, architecture, tool risk tiers, eval plan, 4-week roadmap, open questions and the
decision log. Update its decision log when a design decision changes.

## Where the project stands

Update this section at the end of each working session. Keep it current, not a diary: the
decision log in SPEC.md holds the reasons, git holds the history.

Last updated 2026-11-24. Weeks 1 and 2 are built except OCR and vision. Work is on branch
`week2-data-classes`, PR hairuljr/lunas#1 (open; the owner commits and pushes).

### What exists

- Data (`data/generate.py`, fixed seed, byte-identical output): 300 digital invoice PDFs
  from 40 vendors (10 hand-written, 30 generated), 3 layouts x 2 languages, ten classes
  with fixed expected outcomes in `EXPECTED` / `EXPECTED_INVESTIGATION` (93 clean, 23 each
  of price_mismatch, quantity_mismatch, duplicate, wrong_tax, no_po, bank_account_change,
  payment_terms, price_amendment, split_delivery). Each class split half dev (155) / test
  (145): iterate on dev only. Plus vendor master, POs, goods receipts, an invoice ledger,
  40 bilingual contracts (18 with a price addendum) and 720 labelled retrieval questions.
- Workflow (`pipeline/graph.py`, LangGraph): read (parse + extract, `extract.v2`) ->
  check (`pipeline/checks.py`: validate incl. tax rate, duplicates, 3-way match incl.
  payment terms) -> contract (`contract.v1`, price mismatches only, citation verified in
  code) -> route (`route.py`: clean / hard_reject / human / investigate) -> investigate
  (`investigate.v2`, at most 6 turns, read-only tools scoped to the vendor, causes and
  evidence checked in code, never changes the decision) -> decide (code table) ->
  approval (interrupt for route human or decision escalate). State is a Pydantic
  `InvoiceState`; checkpoint types are listed in `SERDE`.
- Checkpoints: Postgres for the CLI (`lunas run`, `lunas pending`, `lunas approve`),
  memory for evals, tests and the gate, which record where invoices stop and never wait.
- Retrieval (`lunas.retrieval`): Postgres `contract_clauses`, full-text (contract-language
  config) + exact pgvector cosine, weighted RRF (keyword 0.5), chunk = clause with a
  vendor / number / article header; parents (whole articles, at most 5) go to models.
- LLM client (`lunas.llm`): OpenAI-compatible, `complete` (structured output, Pydantic
  validation, one retry) and `complete_tools`; `RecordingClient` records and replays both.
- Tracing (`lunas.telemetry`): OTel GenAI-semconv spans, one trace per invoice, off unless
  `LUNAS_OTLP_ENDPOINT` or `LUNAS_TRACE_FILE`; content only with `LUNAS_TRACE_CONTENT`.
  Verified in Phoenix 21.0.0.
- Evals: `evals/node/run.py` (extraction, decision, route, contract check, investigation,
  checkpoint), `evals/node/retrieval.py` (Recall@5), `evals/baseline/react.py` (naive
  ReAct, prompt `baseline_react.v3`, same rules and a contract tool). 222 tests.
- PR gate (`pr-gate.yml`): ruff, pytest (with a pgvector service; store and graph tests
  fail instead of skipping under `CI`), then `evals.node.run --gate`, which replays
  `evals/recordings/` (dev split, 353 replies + recorded retrievals) with no model and no
  database. Gated: decision and key-field accuracy (2-point rule), false approvals (no
  rise), cause accuracy and checkpoint accuracy (2-point rule). Stale when the extract,
  contract or investigate prompt, the `Invoice` schema, the PDF text or the contracts
  change: refresh with `--record --split dev --workers 2` (only uncached calls go live).
  Second gate step, `evals.node.scans --gate`: replays `evals/recordings/scans/` (40 dev
  twins: OCR and vision reads recorded as `ParsedDocument`, replies, retrievals), so CI
  needs no scans, model, database or OCR engine. Baseline 38/40, false-approve 0, key
  fields 0.993. Blind to OCR and scan-generator changes (`evals.node.transcription`
  measures those); refresh with `evals.node.scans --record` (Postgres up; reads through
  `evals/cache/scans`, so only uncached calls go live).

### Commands

`uv run python data/generate.py`; `uv run python -m data.generate_scans` (80 scanned twins, image-only PDFs, in
`data/out/scans/`; evaluate them in their own run, never alongside the originals); `podman compose up -d` (Postgres on host port 5433,
Phoenix on 6006, both loopback only); `uv run python -m lunas.retrieval.index`;
`uv run lunas run [--limit N] [--investigate] [--cache DIR]`; `uv run lunas pending`;
`uv run lunas approve <thread> --decision approve|reject|hold --approver NAME`;
`uv run python -m evals.node.run [--split dev|test|all] [--cache] [--no-investigate]
[--gate | --record]`; `uv run python -m evals.node.retrieval [--split ...]`;
`uv run python -m evals.node.scans [--split ...] [--vision] [--gate | --record]`;
`uv run python -m evals.baseline.react [--split ...] [--cache]`.

### Measured (one live run each, Sumopod `deepseek-v4.1-flash-1017`)

| What | Split | Result | Run |
|---|---|---|---|
| Extraction, whole invoice | test 145 | 145/145, 0 failures | `node-20261124-032642.json` |
| Decision / false-approve | test 145 | 145/145 / 0/92 | same |
| Route / approval stops | test 145 | 145/145 / 11/11, 0 wrong | same |
| Contract check | test | cleared 6/6, wrongly 0/5, addendum cited 11/11 | same |
| Investigation, `investigate.v1` | test 66 | cause 64/66, evidence 65/66 | same |
| Investigation, `investigate.v2` | dev 72 | cause 72/72, evidence 70/72 | gate recording 2026-11-24 |
| Investigation, `investigate.v2`, affected classes only | test 28 | cause 28/28 (split_delivery 11/11), evidence 28/28 | `investigation-targeted-20261124-041258.json` |
| Retrieval Recall@5, vendor-scoped hybrid | test 360 | 0.934 (target 0.90; vector alone 0.938) | `retrieval-20261119-042740.json` |
| OCR transcription, scanned twins (no model) | all 80 | CER 0.016; total, tax, invoice and PO number 80/80; bank account 76/80 (all 4 misses stamped) | `transcription-20261124-054813.json` |
| Vision transcription pilot, 10 hardest twins (all stamped) | 10 | bank account 8/10 (recovers 2 OCR misses; guessed one digit once); cost 0.004 | `transcription-20261124-061522.json` |
| Scans end to end, OCR + vision second read | all 80 | decisions 77/80, false-approve 0/67, key fields 0.995; unstamped 40/40; 3 second reads, 0 disagreements. Wrong: 2 stamp-hidden accounts -> escalate, 1 stamp word read into a line description -> hold. Cost 0.017 | `scans-20261124-064412.json` |
| Baseline vs lunas, decision / false-approve | test, previous dataset (9 classes, 146) | 141/146, 4/92 vs 146/146, 0/92 | `baseline-20261119-074801.json`, `node-20261119-073816.json` |

- Read all of it as "works on clean digital PDFs with no attacks", one run, no pass^3.
- `investigate.v2` on test was run only for split_delivery, quantity_mismatch and
  duplicate/renumbered (the classes the change targets); price_mismatch, wrong_tax,
  payment_terms and price_amendment were last measured on test with v1 (all correct).
- The baseline comparison is on the dataset before `split_delivery`; README says so.
  Re-measure both once, after OCR/vision change the dataset again.
- Evidence still fails code's check now and then: a short delivery citing a PO number as a
  goods receipt, or this invoice as a ledger invoice. The check is strict on kind on
  purpose.
- With 4 parallel workers the provider sometimes fails investigation calls after retries;
  those are counted as `unanswered`, kept out of cause accuracy, and block `--record`.
  Use `--workers 2` for investigation runs.

### Provider, budget, infrastructure

- Dev provider: Sumopod, set in `.env`: `deepseek-v4.1-flash-1017` (chat, strict
  `json_schema`, tool calls, system messages), `text-embedding-3-small` (1,536 dims),
  `glm-5.3-flash` (vision; smoke-tested on three clean renders only). Budget-capped:
  check `GET <host>/v2/user/info` (`max_budget - spend`) before and after any live run,
  prefer replays, and ask the owner before full re-runs. Balance 2.7832 on 2026-11-24.
  Rough costs: dev split with investigation about 0.08, re-recording the gate about 0.04.
- Model comparison on 2026-11-24 (prices from the provider's catalogue, owner-supplied):
  `hy4-flash` and `glm-5.3-flash` list at 4-5x cheaper than `deepseek-v4.1-flash-1017`
  after discounts, but the balance fell about 2x more than list price predicts for both.
  Full dev run on `hy4-flash` (`node-20261124-045719.json`, 2 workers): decisions
  155/155, false-approve 0/98, cause 72/72, evidence 67/72 (DeepSeek 70/72), contract
  check and checkpoints all right, whole invoice 154/155: on inv-300 it returned no bank
  account although one is printed. Cost 0.048 against about 0.075 for DeepSeek (about
  1.6x cheaper; it writes about 3x the output tokens). Not adopted: the saving is small in
  absolute terms and a dropped bank account would hide a changed account (`None` means
  "not printed" to the match). A code guard for that belongs with the week-3 scanner.
- Fallback: a custom proxy whose default route injects its own system prompt (its
  `/v1/playground` route does not); it rejects `response_format`, allows one concurrent
  request and about 44 per 3.5 minutes. Use `LUNAS_LLM_STRUCTURED_OUTPUT=none`,
  `LUNAS_LLM_WORKERS=1`, `LUNAS_LLM_MIN_INTERVAL_SECONDS=8` there. Next provider to
  compare: Inferhub.
- Podman machine `podman-machine-default`; `podman-compose` from Homebrew. Phoenix runs
  with auth off on loopback; a reachable deployment needs auth and an exporter API key,
  which `lunas.telemetry` does not support yet.

### Not built yet

- OCR/vision plan (`docs/ocr_vision_strategy.md`, option 1): all five steps are built
  (scanned twins, `OcrReader`, grounding check, `VisionReader` second read, scan gate).
  Open: a stamp word read into a line description (inv-104 -> hold); stamp-hidden
  accounts go to escalate (safe); a fully destroyed bank line grounds nothing (inv-158).
- `gapura`, the MCP tool servers, the attack set and ASR (week 3); `policies/default.yaml`
  is a draft no code loads (the approval threshold lives in `Settings`).
- Approval permissions (any `--approver` string is accepted) and an approval UI.
- pass^3, nightly evals, the API package.

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
