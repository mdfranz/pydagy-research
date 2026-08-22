# Concrete Bug and Hardening Backlog

Date: 2026-08-22

This backlog converts the failures observed in the Natalie Harp and agent-
framework research runs into implementation-ready issues. It distinguishes
correctness bugs from security hardening and experiment/reporting work.

Evidence traces:

- Harp fixed-plan fan-out: `01a02aaba1a03a5e0292e96ddbd7f94d`
- Agent-framework planner-driven fan-out:
  `01a02abaeaeb65e62cfd554197e68a16`

## Priority definitions

- **P0:** can cause an unsupported or misleading citation to be accepted.
- **P1:** corrupts provenance, telemetry, comparison metrics, or resilience.
- **P2:** reduces report usability or experiment reliability without directly
  accepting unsupported evidence.

## Summary

| ID | Priority | Type | Short title |
| --- | --- | --- | --- |
| BUG-001 | P0 | Correctness | Model fallback prose is citable as page content |
| BUG-002 | P0 | Correctness | Citation validation does not validate URL or snippet |
| BUG-003 | P0 | Correctness | Claims can rely on uncitable search summaries |
| BUG-004 | P0 | Correctness | Validator accepts binary and error-page content |
| BUG-005 | P0 | Provenance | Evidence can be linked to the wrong retrieval attempt |
| BUG-006 | P1 | Telemetry | Planner request IDs are discarded |
| BUG-007 | P1 | Telemetry | One provider call is counted as multiple attempts |
| BUG-008 | P1 | Telemetry | Search summaries are labeled `verified` |
| BUG-009 | P1 | Correctness | Provider success is conflated with usable evidence |
| BUG-010 | P1 | Correctness | PDF retrieval lacks deterministic text extraction |
| BUG-011 | P1 | Metrics | Citation count overstates independent source coverage |
| BUG-012 | P1 | Telemetry | Attempt spans cannot be directly joined to evidence |
| BUG-013 | P1 | Privacy/UX | Raw provider exception bodies leak into reports |
| BUG-014 | P1 | Planning | Broad plans can leave important topics search-only |
| BUG-015 | P1 | Hardening | Sensitive-person conclusions lack typed assertion roles |
| BUG-016 | P2 | Reporting | Markdown hides evidence provenance and usage |
| BUG-017 | P2 | Experiments | End-to-end experiment arms are not resumable |
| BUG-018 | P2 | Metrics | Fan-out value lacks an incremental-contribution metric |

---

## BUG-001 — Model fallback prose is citable as page content

**Priority/type:** P0 correctness
**Code:** `gateway._extract_native_evidence`,
`AntigravitySDKGateway._apply_response_text_fallback`, `EvidenceRecord`

**Observed:** When provider-native tool metadata contains retrieval status but
no page text, `_extract_native_evidence` stores `result.output` in a successful
`page_content` record. The Antigravity gateway similarly replaces a thin tool
extract with the model's final response. The Writer and citation validator then
treat that model-authored paraphrase as directly fetched page content.

**Expected:** Model-authored fallback text may be retained for triage, but must
never be citable as verified source text.

**Acceptance criteria:**

- Evidence records carry extraction provenance and citability independently of
  `source_kind`.
- `result.output` and Antigravity response fallbacks are recorded as
  `response_text_fallback`, `triage_only`, and non-citable.
- `validate_research_answer` rejects a citation to fallback-derived evidence.
- Existing structured native-tool page extracts remain citable.

**Regression tests:** Add native and Antigravity fallback records and assert
that the Writer output validator rejects citations to both.

## BUG-002 — Citation validation does not validate URL or snippet

**Priority/type:** P0 correctness
**Code:** `models.validate_research_answer`

**Observed:** Runtime validation checks that a citation's `evidence_id` exists
and that the record is `page_content`. It does not require
`Citation.source_url` to equal the evidence record URL or require the citation
snippet to occur in the retrieved extract. A model can therefore cite a real
ID while inventing the displayed URL or quotation.

**Expected:** The citation object must faithfully represent the referenced
evidence record.

**Acceptance criteria:**

- Citation URL equality is checked after the same URL normalization used by
  the Validator.
- A normalized citation snippet must be present in the normalized evidence
  extract; empty snippets are rejected.
- Violations identify the evidence ID and failed invariant.
- The Pydantic AI output validator retries these failures.

**Regression tests:** Cover wrong URL, invented snippet, whitespace-normalized
valid snippet, and a fully valid citation.

## BUG-003 — Claims can rely on uncitable search summaries

**Priority/type:** P0 correctness
**Code:** `models.validate_research_answer`, `agents.WRITER_SYSTEM_PROMPT`

**Observed:** Only `Citation` records are checked for `page_content`. A
`Claim.evidence_ids` entry may point solely to a `search_summary`, and a claim
does not have to have a corresponding `Citation` object. The model constraints
therefore permit an uncitable search synthesis to ground a published claim.

**Expected:** Every published claim must have at least one corresponding,
citable evidence record represented by a valid Citation.

**Acceptance criteria:**

- Every claim has at least one evidence ID that is citable after validation.
- Every evidence ID presented as support for a claim has a corresponding
  Citation, or the schema is redesigned so that this relationship is
  unambiguous.
- A claim supported only by `search_summary` fails validation.

**Regression tests:** Cover search-only claim, missing Citation object, mixed
search/page support, and valid page-only support.

## BUG-004 — Validator accepts binary and error-page content

**Priority/type:** P0 correctness
**Code:** `graph.ValidatorNode`

**Observed:** The Harp run retained PDF/binary-like output and
permission-error prose because filtering only checks `status`, drift, and
duplicate URL/provider pairs. `kept_count=6` overstated usable evidence.

**Expected:** Content that is not credible human-readable source text is
excluded or made explicitly non-citable.

**Acceptance criteria:**

- Content-aware checks detect at least binary payloads, permission/access
  errors, provider refusals, empty/thin content, MIME mismatch, and common
  non-content/navigation responses.
- Each rejection has a stable reason: `binary`, `permission_error`,
  `provider_refusal`, `thin_content`, `mime_mismatch`, or `non_content`.
- Validation telemetry and `ResearchReport.dropped_records` include the new
  reason counts.
- Quality checks are deterministic host code, not another unconstrained model
  judgment.

**Regression tests:** Fixture records for every rejection reason plus a valid
short single-fact page that must not be falsely rejected.

## BUG-005 — Evidence can be linked to the wrong retrieval attempt

**Priority/type:** P0 provenance
**Code:** `graph.ValidatorNode`

**Observed:** The evidence-link loop matches the first unlinked successful
attempt using only provider plus action/source kind. It ignores target URL,
query, Planner request ID, and a stable attempt ID. Multiple reads from the
same provider can therefore be assigned according to incidental list order.

**Expected:** An evidence record links to the exact provider invocation that
created it.

**Acceptance criteria:**

- Every produced record carries a stable attempt ID or equivalent immutable
  join key from the gateway.
- Validator linkage uses that key, never provider/action heuristics.
- A missing or ambiguous link is surfaced as a validation/telemetry error.

**Regression tests:** Return two same-provider reads in reversed completion
order and assert that both retain the correct request, target, and attempt.

## BUG-006 — Planner request IDs are discarded

**Priority/type:** P1 telemetry
**Code:** `gateway.run_plan`, `RetrievalGateway`, gateway telemetry helpers

**Observed:** Plans contain IDs such as `req_1` through `req_5`, but attempt
spans generate unrelated `pn-*` or `ag-*` IDs. Logfire cannot directly group
provider children under the logical Planner request.

**Expected:** `SearchOrFetchRequest.request_id` remains stable through the
gateway, provider fan-out, evidence records, validation, and report.

**Acceptance criteria:**

- Gateway calls receive a request context containing the Planner request ID.
- All child attempts and evidence linkage spans use that ID.
- A `research retrieval request` parent span encloses each provider fan-out.

**Regression tests:** Execute a two-request fixed plan and assert exact request
IDs on request, attempt, and evidence-link telemetry.

## BUG-007 — One provider call is counted as multiple attempts

**Priority/type:** P1 telemetry
**Code:** `PydanticNativeSearchGateway._run`

**Observed:** `_run` emits one `SourceAttempt` per extracted evidence record.
In the agent-framework run, ten provider invocations produced twelve attempt
rows because Anthropic returned multiple records for search calls. Each pair
had effectively identical durations.

**Expected:** An attempt represents one provider invocation; zero or more
evidence records are its outputs.

**Acceptance criteria:**

- Exactly one `SourceAttempt` is emitted for each provider call.
- The attempt reports aggregate output count and aggregate character count.
- Individual records link back through the stable attempt ID from BUG-005.

**Regression tests:** A fake native result with two tool-return records emits
one attempt and two linked evidence records.

## BUG-008 — Search summaries are labeled `verified`

**Priority/type:** P1 telemetry
**Code:** `PydanticNativeSearchGateway._run`, Antigravity attempt telemetry

**Observed:** All successful records longer than 80 characters receive
`tier="verified"`, including `search_summary` records. The data contract says
search summaries are triage-only and never directly citable.

**Expected:** Evidence tier follows provenance/citability, not only length.

**Acceptance criteria:**

- Every search result is `triage_only` regardless of character count.
- Model response fallback is `triage_only`.
- Only validated source-faithful page extraction can become `verified`.
- Length may downgrade evidence to `thin`, but can never upgrade unverified
  provenance to `verified`.

**Regression tests:** Long search result, long fallback, thin native page, and
valid native page all receive the correct tier.

## BUG-009 — Provider success is conflated with usable evidence

**Priority/type:** P1 correctness
**Code:** `EvidenceRecord`, `SourceAttempt`, gateway implementations

**Observed:** A completed provider/model call, successful URL retrieval, text
extraction, and evidence validation all collapse into `status="success"`.
The Harp run consequently marked permission-error prose successful.

**Expected:** Transport, extraction, and validation outcomes are separate.

**Acceptance criteria:**

- Telemetry distinguishes provider-call status, retrieval status, extraction
  status, and evidence disposition.
- A provider call can succeed while its evidence is excluded.
- Reports do not call excluded content a successful evidence record.

**Regression tests:** Cover transport failure, content-filter failure,
successful transport with access-denied body, extraction failure, and fully
valid content.

## BUG-010 — PDF retrieval lacks deterministic text extraction

**Priority/type:** P1 correctness
**Code:** retrieval gateway or a host-side extraction layer

**Observed:** The Harp run treated large PDF-like output as successful without
proving it was readable page text. Character count could not distinguish
binary bytes from a valid extraction.

**Expected:** PDFs become citable only after deterministic host-side parsing
and text-quality validation.

**Acceptance criteria:**

- HTTP content type and PDF signature are detected.
- A pinned host-side PDF parser produces the citable text and page metadata.
- Parse failures and image-only PDFs are explicit and non-citable unless an
  separately identified OCR path succeeds.
- Citation provenance records parser name/version and, when available, page
  number.

**Regression tests:** Text PDF, malformed PDF, binary mislabeled as HTML, and
image-only PDF fixtures.

## BUG-011 — Citation count overstates independent source coverage

**Priority/type:** P1 metrics
**Code:** `report_formatter`, answer/experiment metrics

**Observed:** The agent-framework report showed four citations, but two were
provider extracts of the same Microsoft URL. It therefore had three unique
cited sources, not four independent sources.

**Expected:** Provider agreement on one URL is reported separately from
independent source corroboration.

**Acceptance criteria:**

- Reports expose citation occurrences, unique evidence IDs, normalized unique
  source URLs, and unique source domains separately.
- Same-URL provider agreement is labeled extraction corroboration.
- Experiment comparisons use unique valid source coverage as a primary metric.

**Regression tests:** Two providers citing one normalized URL count as two
evidence records, one source, and one extraction-corroborated source.

## BUG-012 — Attempt spans cannot be directly joined to evidence

**Priority/type:** P1 telemetry
**Code:** `TelemetryRecorder`, `ValidatorNode`

**Observed:** `research retrieval attempt` is emitted before stable evidence
IDs exist and retains `retrieval.evidence_id=null`. A later `research evidence
linked` span is required to reconstruct the relationship.

**Expected:** Logfire queries can join attempt and evidence deterministically
without relying on temporal ordering or provider/action matching.

**Acceptance criteria:**

- Attempt spans and linkage spans share a stable attempt ID and Planner request
  ID.
- Evidence-link spans include normalized target and evidence disposition.
- Derived answer telemetry directly lists cited attempt/provider/source IDs.

**Regression tests:** Assert all telemetry joins using IDs only, with reversed
record order and multiple records from one attempt.

## BUG-013 — Raw provider exception bodies leak into reports

**Priority/type:** P1 privacy/UX
**Code:** gateway exception handling, `SourceAttempt.error`, report formatter

**Observed:** The failed Gemini Unit 42 read printed the complete provider
response body, token accounting, internal run/conversation IDs, and model
metadata under Dropped Records. This is noisy and may expose sensitive provider
diagnostics outside Logfire's scrubber.

**Expected:** User-facing reports contain a bounded, classified error summary;
full diagnostic detail remains in protected telemetry when safe.

**Acceptance criteria:**

- Errors have stable `error_code`, bounded `error_summary`, and optional
  scrubbed diagnostic detail.
- Markdown never prints raw provider response objects.
- Error strings are length-limited and secret-scrubbed before persistence.

**Regression tests:** A large synthetic exception containing a token-like
value produces a short safe report while retaining its error classification.

## BUG-014 — Broad plans can leave important topics search-only

**Priority/type:** P1 planning
**Code:** planner prompt and/or deterministic plan validator

**Observed:** The agent-framework Planner searched for OWASP guidance but did
not schedule an OWASP page read. The final answer therefore omitted several
major vulnerability classes because search summaries were correctly
uncitable.

**Expected:** A plan's important subtopics have citable reads, or the plan
explicitly narrows scope before retrieval.

**Acceptance criteria:**

- Add a deterministic post-plan check that rejects all-search coverage for a
  named required subtopic.
- Broad questions either receive representative authoritative reads across the
  requested taxonomy or an explicit scope limitation.
- The Planner retry explains which subtopic lacks a read.

**Regression tests:** A plan that searches OWASP but reads only unrelated CVE
pages is rejected; a plan with an authoritative taxonomy read is accepted.

## BUG-015 — Sensitive-person conclusions lack typed assertion roles

**Priority/type:** P1 hardening
**Code:** evidence/claim models and Writer instructions

**Observed:** The Harp Writer had to infer whether text represented an official
record, anonymous-source reporting, expert opinion, official denial, or
pipeline inference. The prose happened to remain bounded, but the schema did
not enforce that distinction.

**Expected:** High-risk allegations cannot silently become unqualified facts.

**Acceptance criteria:**

- Claims/evidence support assertion roles such as `official_record`,
  `attributed_report`, `anonymous_source_report`, `expert_opinion`,
  `official_denial`, and `inference`.
- A sensitive-person task requires conflicting/mitigating facts and unresolved
  uncertainties when present in the evidence.
- Writer instructions explicitly distinguish risk indicator, incident,
  malicious intent, and official threat determination.

**Regression tests:** A Writer output that turns an anonymous-source allegation
into an unqualified fact fails validation.

## BUG-016 — Markdown hides evidence provenance and usage

**Priority/type:** P2 reporting
**Code:** `report_formatter._format_markdown`, `ResearchReport`

**Observed:** The report repeats citation blocks and omits provider, extraction
method, evidence tier, request target, and whether retained evidence was cited.

**Expected:** A reader can audit the path from request to provider attempt to
evidence to claim.

**Acceptance criteria:**

- Group citations by normalized source/evidence instead of repeating blocks.
- Add a provenance table containing request ID, attempt ID, evidence ID,
  provider, source URL, source kind, tier, extraction method, and cited status.
- Separate retrieved, extracted, valid, citable, cited, excluded, and
  valid-but-uncited counts.

**Regression tests:** Markdown snapshot with same-URL provider records, one
dropped record, one cited record, and one valid-but-uncited record.

## BUG-017 — End-to-end experiment arms are not resumable

**Priority/type:** P2 experiments
**Code:** `experiments.run_end_to_end_fixed_plan_experiment`

**Observed:** Arms run sequentially in one invocation. The Harp experiment
outlived the shell window, and restarting it created duplicate partial traces.

**Expected:** Each arm is independently executable, time-bounded, persisted,
and safely resumable.

**Acceptance criteria:**

- Accept an explicit arm/mode list.
- Assign stable per-arm IDs beneath the experiment ID.
- Support per-arm timeouts and persisted JSON results.
- Support `skip_completed` without querying model providers again.
- A timed-out arm records its status without discarding completed arms.

**Regression tests:** Simulate one successful arm and one timeout, then resume
and prove only the incomplete arm executes.

## BUG-018 — Fan-out value lacks an incremental-contribution metric

**Priority/type:** P2 metrics
**Code:** experiment result models and comparison reporting

**Observed:** Anthropic added zero cited evidence in the Harp run but rescued a
unique Unit 42 source in the agent-framework run. Raw evidence and character
counts cannot express this difference.

**Expected:** Provider value is measured by unique valid contribution and
failure recovery relative to latency and cost.

**Acceptance criteria:**

- Per provider, calculate unique valid sources, unique cited sources,
  incremental supported claims, rescued failed targets, latency, tokens, and
  available cost.
- Same-URL duplicate extraction does not count as a unique source.
- Comparison output distinguishes resilience value from ordinary duplicate
  coverage.

**Regression tests:** Encode the Harp and agent-framework result shapes as
fixtures: the former reports zero incremental cited sources for Anthropic; the
latter reports one rescued unique cited source.

## Recommended implementation order

1. BUG-001 through BUG-005: close evidence-integrity and provenance holes.
2. BUG-006 through BUG-013: make telemetry and metrics truthful and safe.
3. BUG-014 and BUG-015: strengthen planning and sensitive-task grounding.
4. BUG-016 through BUG-018: make reports and controlled experiments reliable.

Do not use the current `kept_count`, citation count, or character count as a
provider-selection gate until BUG-001, BUG-004, BUG-007, BUG-008, and BUG-011
are fixed.
