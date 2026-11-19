# lunas

Accounts-payable agent for untrusted invoices. A GitHub portfolio project aimed at applied
AI Engineer roles, planned for release in November/December 2026.

**Read [SPEC.md](SPEC.md) before any design or implementation work.** It holds the success
criteria, architecture, tool risk tiers, eval plan, 4-week roadmap, open questions and the
decision log. Update its decision log when a design decision changes.

## Where the project stands

Update this section at the end of each working session.

- Week 1 done and pushed (2026-11-16). Week 2 steps 1-3 committed on branch
  `week2-data-classes` (pushed, no PR yet); step 4 (contracts, retrieval, contract check)
  done and committed; PR hairuljr/lunas#1 open, CI green before the Postgres service was
  added. Baseline v3 + re-measurement not committed. Synthetic data generator (`data/generate.py`, default 300 digital invoice
  PDFs from 40 vendors in 3 layouts x 2 languages, eight classes, invoice ledger, dev/test
  split, ground truth, 40 contracts, 720 labelled retrieval questions), ingestion router, digital-PDF reader, LLM extraction node
  (`pipeline/nodes/extract.py`, `prompts/extract.v2.md`), validation (arithmetic, dates,
  tax rate), duplicate check, 3-way match (with payment terms), decision table, pipeline
  runner (`pipeline/run.py`, plain functions, no LangGraph yet), node evals, naive ReAct
  baseline, tracing, contract retrieval (`lunas.retrieval`), contract-check node, router, investigation loop, 212 tests.
- Commands: `uv run python data/generate.py`, `uv run lunas run`,
  `uv run python -m evals.node.run` (dev split by default, `--split test|all`;
  `--skip-llm` for the deterministic part, `--cache`
  to record LLM replies and resume, `--cache --replay-only` to run with no LLM at all,
  `--gate` for the PR gate, `--record` to refresh its recordings with a live run),
  `uv run python -m evals.baseline.react` (live; `--cache` to resume),
  `uv run python -m lunas.retrieval.index` (rebuild the contract index in Postgres),
  `uv run python -m evals.node.retrieval` (Recall@5; dev by default).
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
- Week 2 milestone order: remaining data classes + dev/test split, duplicate
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
- Step 3 done (2026-11-19): baseline prompt `baseline_react.v2` states the same rules as
  the code (11% tax, vendor-master terms, duplicates, bank account), with a read-only
  `find_invoices` ledger tool and `--split`. Measured on the **test** split, one live run
  each, Sumopod `deepseek-v4.1-flash-1017`, 4 workers (`node-20261119-024349.json`,
  `baseline-20261119-024725.json`):

  | | Naive ReAct | lunas |
  |---|---|---|
  | Decision accuracy | 142/147 | 147/147 |
  | False-approve | 5/93 (Wilson 95% CI 2.3-12.0%) | 0/93 (0-4.0%) |
  | Requests / tokens per invoice | 3.4 / 6,366 | 1 / 1,429 |
  | Latency p50 / p95 | 4.8 s / 12.2 s | 1.4 s / 4.8 s |
  | Provider spend for 147 invoices | 0.0880 | 0.0272 |

  The baseline's five errors: 3 duplicates (1 exact, 2 renumbered) and 2 short deliveries,
  all approved **and paid**, each time after calling the tool that held the evidence
  (ledger 13/13 duplicates, goods receipts). Failure in comparison, not retrieval. Paired
  difference 5 vs 0 on the same invoices, exact McNemar two-sided p = 0.06: suggestive, not
  settled. Run pass^3 or more test runs before calling it significant. The README table
  now shows these numbers; balance after both runs 3.2436.
- Known fairness gaps in `baseline_react.v2`, left as measured (found in review after the
  run): it lists the four outcomes without lunas' precedence (escalate > reject > hold; the
  Rp 50M threshold only for an otherwise clean invoice), does not say that invoice numbers
  match ignoring case and separators, and writes 11% and Rp 50M as literals instead of the
  config values. None of these cases occurs in the current test split, so the measured
  numbers stand; fix them in a `v3` with the next re-measurement, not by editing v2.
- Step 4, contracts + retrieval (2026-11-19, not committed):
  - Vendors: 10 hand-written + 30 generated from word pools with a fixed seed
    (`VENDOR_COUNT = 40`). Invoices are still round-robin over vendors.
  - One contract per vendor in its language (`contracts.json`, readable copies in
    `data/out/contracts/*.md`): 10 articles, 21 numbered paragraphs, plus an `Addendum`
    changing one item's unit price from a date for 18 of 40 vendors (`AMENDMENT_SHARE`).
    Paragraph text carries the vendor-master terms and bank account. No invoice uses the
    addendum yet.
  - `retrieval_cases.json`: per vendor, 9 question kinds x 2 languages (price change,
    payment terms, payment account, bank change, tax, partial delivery, duplicate, PO
    required, clause by number), labelled with the paragraphs that answer them; split by
    vendor, 360 dev / 360 test.
  - `lunas.retrieval`: Postgres table `contract_clauses` (one row per paragraph; the
    article is the parent), a `tsvector` with the contract's language config
    (`indonesian`/`english`), exact pgvector cosine (no ANN index), weighted RRF with
    keyword weight 0.5. Chunk text = vendor name + clause number + article title + text.
    Embeddings are cached on disk under `evals/cache/embeddings/` (host + model + text).
  - Recall@5, vendor-scoped hybrid (how the pipeline will search): dev 0.949, **test
    0.934** (`retrieval-20261119-042740.json`; target 0.90). On test, vector alone 0.938,
    keyword alone 0.685: fusion matches vector overall and wins on clause-number
    questions (keyword 0.875 vs vector 0.425 on dev). Unscoped (all 40 contracts) hybrid
    0.860 on test. Weakest kinds on test: clause reference 0.725, duplicate 0.875.
    Cross-language questions 0.912 vs same-language 0.956.
  - The keyword weight was swept on dev (1.0/0.7/0.5/0.3/0.0 -> 0.910/0.939/0.949/0.928/
    0.929) before the single test run.
  - Index cost: 822 clause embeddings for 0.0004 of the balance. The gate was re-recorded
    (dataset changed with the vendors): 153/153 on dev again, 0.031; balance now 3.2091.
  - The Postgres store tests skip without a database; CI has no Postgres service, so
    retrieval is only tested locally until one is added to `pr-gate.yml`.
- Contract check (step 6 of SPEC §4), built 2026-11-19: `pipeline/nodes/contract.py`,
  prompt `contract.v1`. Runs only for `price_mismatch` lines (`Issue.line` names the line).
  The retriever asks the vendor's own contract the same price question the retrieval eval
  asks (`lunas.retrieval.questions`) and returns up to 5 whole parent articles. The model
  gets the PO's item name and typed prices and dates, never invoice text, and returns
  clause id, verbatim quote, price and effective date. Code clears the mismatch only if the
  clause was retrieved, the quote is in it word for word, the price and the date's day and
  year are written in the quote, the price equals the invoiced price, and the date is on or
  before the invoice date. Verdicts: explained / not_found / no_answer / citation_invalid /
  price_differs / not_yet_effective. Without a database the pipeline runs with no contract
  check and the mismatch stays held.
- New class `price_amendment` (25 of 300; vendors with an addendum only): the invoice bills
  the addendum's price for an item ordered at the old price. `in_effect` (invoice after the
  addendum's date) -> approve once the contract check clears it; `too_early` -> hold.
  Ground truth now has `expected_check_codes` (before the contract check) and
  `expected_citation`. 100 clean, 25 per other class; dev 154 / test 146.
- Measured on dev, one live run (`node-20261119-071450.json`): decision 154/154, every
  class 13/13 or 50/50, false-approve 0/98. Contract check cleared 6/6 that should be
  cleared, 0/7 that should not (too_early ones), cited the addendum 13/13; on two
  price_mismatch invoices of amended vendors the model cited the addendum and code
  rejected it (`price_differs`). 189 requests, 247k tokens, 0.035 of the balance (now
  3.1704). Not yet measured on test.
- First extraction errors on this prompt (dev, same run): inv-086 (compact/en) got a due
  date the document does not print, computed from the terms; inv-299 (compact/id) read a
  unit price 13,400 as 13,300. Neither changed a decision: 299 was held anyway, and the
  misread price breaks `quantity x unit_price = amount`, which validation flags. The
  earlier dev run on the same prompt had no misses, so this is run-to-run variation.
- Gate re-recorded on that run: 189 replies plus 32 recorded retrievals under
  `evals/recordings/retrieval/`; the fingerprint now also covers `contracts.json` and the
  contract prompt. Verified offline with no API key and an unreachable database.
- Baseline v3 (2026-11-19): prompt `baseline_react.v3` with ordered rules (escalate >
  reject > hold > threshold > approve), invoice-number matching, tax rate and threshold
  filled from `Settings`, and a read-only `search_contract` tool over the same index
  (`StoreRetriever.search_clauses`); it needs Postgres. CI now starts a pgvector service
  and the store tests fail instead of skipping there (`CI` set).
- Re-measured on the current **test** split (146 invoices, nine classes), one live run each
  (`node-20261119-073816.json`, `baseline-20261119-074801.json`):

  | | Naive ReAct v3 | lunas |
  |---|---|---|
  | Decision accuracy | 141/146 | 146/146 |
  | False-approve | 4/92 (CI 1.7-10.7%) | 0/92 (0-4.0%) |
  | Requests / tokens per invoice | 3.7 / 7,655 | 1.2 / 1,576 |
  | Latency p50 / p95 | 10.7 s / 50.6 s | 2.4 s / 13.9 s |
  | Provider spend | 0.0914 | 0.0321 |

  Baseline errors: 4 exact duplicates approved and paid after `find_invoices` (12/12
  duplicates looked up), 1 no-PO invoice escalated instead of rejected. Both 12/12 on
  `price_amendment` (baseline called `search_contract` on all 12). lunas contract check on
  test: cleared 4/4 that should be, 0/8 that should not, cited the addendum 12/12. McNemar
  p = 0.125 for 4 vs 0: not significant alone; same failure mode as the earlier dataset
  (5 vs 0). README now shows these numbers. Balance after: 3.0463.
- Router + exception taxonomy (2026-11-19, not committed):
  - `pipeline/nodes/route.py`: four lanes, code. `clean`; `hard_reject` (po_missing,
    po_not_found, duplicate_invoice: no investigation, no tokens); `human` (bank account or
    vendor mismatch, plus what the tools cannot investigate: unreadable account, unknown
    vendor, currency, failed extraction, and any code the router does not know);
    `investigate` (the remaining discrepancies). Precedence human > hard_reject >
    investigate > clean. The route never changes the decision. It runs after the contract
    check (span `route`) and is on `PipelineResult.route`; the eval reports route accuracy.
    Dev, replayed: 154/154.
  - `pipeline/nodes/investigate.py`: schema only, the loop is next. `ExceptionCause`
    (13, incl. `unclear`), `POSSIBLE_CAUSES` per issue code (code rejects others),
    `RECOMMENDED_ACTION` per cause (code, not the model), `Evidence` (kind + record id,
    which code will check the agent actually fetched), `Diagnosis`, `InvestigationReport`.
  - **The investigator never changes a decision**: investigated invoices stay `hold`. It
    gets typed fields and issues only, never invoice text (SPEC §6).
  - Ground truth gained `expected_route` and `expected_causes` (`EXPECTED_INVESTIGATION`).
    Honest limit: in the current data only `price_mismatch` has two causes in play
    (overcharge vs contract_price_not_yet_effective); every other investigated class maps
    to one cause, so cause accuracy would be close to trivial. Add a class where a cause
    must be told apart, e.g. a legitimate second delivery with the same total on the same
    PO (`possible_duplicate` -> `separate_delivery`, with two goods receipts).
  - LangGraph approval (next): interrupt + Postgres checkpointer; in eval and the gate the
    run records that the invoice stopped at the right checkpoint with the right state and
    moves on, never waiting for input.
- Investigation loop built (2026-11-19, not committed):
  - New class `split_delivery` (23 of 300): the PO orders twice the billed quantity, two
    goods receipts, the first half already invoiced and paid with the same total. The
    duplicate check flags `possible_duplicate`; the expected cause is
    `separate_delivery`, against `resubmission` for `duplicate/renumbered`. 93 clean, 23
    per other class; dev 155 / test 145.
  - `pipeline/nodes/investigate.py`, prompt `investigate.v1`, max 6 turns. Tools, all
    read-only and scoped to the invoice's vendor: `get_purchase_order`,
    `get_goods_receipts`, `get_vendor_invoices` (ledger), `get_vendor_terms` (no bank
    details), `search_contract` (`ContractRetriever.search_clauses`), and `submit_report`.
    The brief is typed fields, issues with their allowed causes, and the contract check's
    verdicts; no notes, no invoice bank account, descriptions labelled vendor-written.
  - Code finalises the report: one diagnosis per issue (issues without a line are matched
    by code), causes outside `POSSIBLE_CAUSES` become `unclear`, evidence is valid only if
    every ref was returned by a tool or is the vendor id / this invoice's number from the
    brief. An unfinished loop gives `unclear` diagnoses. The decision is never touched.
  - LLM client: `complete_tools` on `OpenAICompatClient` (same traced `_send` path as
    `complete`) and on `RecordingClient`, so the gate replays tool turns too.
  - Measured on dev, one live run (`node-20261119-085055.json`): 72/72 routed invoices
    investigated and finished; cause 71/72 (98.6%; the miss: one split delivery called a
    resubmission); evidence valid 70/72 (two short deliveries cited a record the tools
    had not returned); 0 causes rejected by code; turns mean 2.4, max 4; investigation
    seconds p50 7.7, p95 51.5. Decisions unchanged, 155/155; route 155/155. Whole dev run:
    358 requests, 647k tokens, 0.077 of the balance (now 2.9618). Not measured on test.
  - Gate re-recorded with the investigation on (`recording.json` has `investigate: true`;
    cause accuracy is a gated metric with the 2-point rule; the fingerprint covers
    `investigate.v1`): 358 replies + 83 retrievals, 1.7 MB. Verified offline.
  - `--no-investigate` skips the loop in the node eval; the eval tests use it because
    their stand-in model has no tool calls.
- Remaining week 2: LangGraph approval (interrupt + Postgres checkpointer, non-blocking in
  eval), investigation on the test split, OCR and vision with scanned versions.

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
