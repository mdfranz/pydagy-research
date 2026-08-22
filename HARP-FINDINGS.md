# Natalie Harp Research Run: Findings and Required Fixes

Date: 2026-08-22
Experiment: `provider-e2e-natalie-harp-v1`
Canonical completed fan-out trace: `01a02aaba1a03a5e0292e96ddbd7f94d`

## Research conclusion

The run asked:

> As of August 22, 2026, do publicly documented facts establish Natalie
> Harp as a threat to United States national security? Assess documented
> risk indicators, mitigating facts, and unresolved uncertainties. Do not
> infer malicious intent or wrongdoing beyond the sources.

The evidence supported a narrower conclusion than the question's premise.
Public reporting documented a potential access-control and personnel-vetting
risk associated with the timing of Harp's security-clearance process. It did
not establish that Harp was a national-security threat. The retrieved sources
did not document espionage, intentional wrongdoing, a security breach, or an
official threat determination. Public reporting also said that Harp ultimately
received a clearance, although the precise timeline and scope of access were
not established by the evidence collected in this run.

This distinction is essential for a sensitive-person assessment:

- a procedural or access risk is not evidence that an incident occurred;
- a security incident is not, by itself, evidence of malicious intent; and
- neither is equivalent to an official determination that a person is a
  national-security threat.

## Sources used by the fixed plan

- The White House's 2025 annual staff report, which listed Harp as a Special
  Assistant to the President and Executive Assistant to the President.
- A Guardian report about questions concerning the clearance process and the
  White House's response.
- CNN transcripts discussing the reported concerns, the uncertain timeline,
  and the understanding that Harp had received a clearance.

The reporting about the delayed clearance relied in part on unnamed sources.
The final answer therefore should have represented it as attributed reporting,
not as an independently established fact.

## What Logfire showed

The completed Gemini-plus-Anthropic fan-out trace took 51.9 seconds. It
retrieved six raw records, retained all six, linked all six records to source
attempts, and produced five claims with four citations.

Evidence-to-provider linkage was:

| Evidence | Provider | Source role |
| --- | --- | --- |
| `EVID-001` | Anthropic | White House staff-report read |
| `EVID-002` | Gemini | White House staff-report read |
| `EVID-003` | Anthropic | Guardian read |
| `EVID-004` | Gemini | Guardian read |
| `EVID-005` | Anthropic | CNN read |
| `EVID-006` | Gemini | CNN read |

Every citation selected by the fixed Gemini Writer came from Gemini:

- `EVID-004`, the Guardian report; and
- `EVID-006`, the CNN transcript.

Anthropic doubled the retained evidence pool but contributed no cited claim in
this run. This is a negative data point for enabling fan-out by default. It is
not enough, by itself, to reject fan-out: the experiment needs repeated runs
and tasks where provider diversity can add unique sources or rescue failed
retrievals.

The attempt telemetry also exposed content-quality problems:

| Provider | Target | Duration | Characters | Observed issue |
| --- | --- | ---: | ---: | --- |
| Anthropic | White House PDF | 6.5 s | 184,418 | Large extract, but not proof of usable PDF text |
| Gemini | White House PDF | 29.8 s | 42,367 | Binary/opaque PDF content could be retained |
| Anthropic | Guardian | 2.7 s | 438 | Permission/error-like prose was marked successful |
| Gemini | Guardian | 6.1 s | 6,678 | Usable page content |
| Anthropic | CNN | 7.0 s | 25,356 | Retained but not cited |
| Gemini | CNN | 9.4 s | 24,342 | Usable and cited |

Raw character count and `status="success"` therefore overstated evidence
quality. A nominally successful fetch could contain binary PDF bytes,
permission-denied prose, a provider refusal, or a model-authored fallback.

## Weaknesses exposed by the run

### 1. Model fallback prose can become citable evidence

When a provider-native fetch returns only status metadata, the gateway may use
the retrieval model's response text as `page_content`. That text is useful for
triage, but it is a model paraphrase rather than verified page content. Giving
it the citable `page_content` type creates an evidence-integrity failure.

### 2. Validation is structural, not content-aware

`ValidatorNode` currently excludes failed, drift-flagged, and duplicate
records. It does not reject content that is binary, an access-denied page, a
provider refusal, implausibly thin, dominated by navigation or metadata, or
inconsistent with the reported MIME type. Consequently, all six records in
this run counted as kept even though not all six were usable evidence.

### 3. PDF handling is not trustworthy enough for citation

A successful HTTP/tool interaction with a PDF does not mean the returned text
is a readable, source-faithful extraction. PDFs need deterministic host-side
extraction, MIME checks, and minimum text-quality checks before they can be
citable.

### 4. Tool status does not describe evidence usability

Provider/tool completion and evidence success are conflated. Permission-error
prose and unusable payloads can be reported as successful attempts. The system
needs separate transport, extraction, and validation outcomes.

### 5. The evidence model lacks assertion semantics

The Writer receives source text but no structured distinction among an
official record, a confirmed fact, an attributed report, an anonymous-source
report, expert opinion, an official denial, and the pipeline's inference.
That distinction is especially important for allegations about a named person.

### 6. Corroboration can be overstated

Two providers extracting the same URL provide extraction corroboration, not
independent factual corroboration. Provider diversity and source diversity are
different metrics and must not be merged.

### 7. Request and evidence telemetry are only partially correlated

The gateway currently generates retrieval request identifiers instead of
threading the Planner's `SearchOrFetchRequest.request_id` through the complete
path. Evidence is linked in later `research evidence linked` spans, while the
original attempt spans retain a null `evidence_id`. This makes one-query trace
analysis harder than necessary.

### 8. Secret scrubbing can hide answer-level metrics

Logfire appropriately scrubbed some payload text containing security-related
tokens. Scrubbing should remain enabled, but the pipeline needs safe derived
fields—claim count, citation count, cited evidence IDs, cited providers, and
validation totals—so comparisons do not require inspecting scrubbed prose.

### 9. The experiment runner is not operationally resumable

The first sequential end-to-end invocation outlived the shell window. Reruns
then produced duplicate partial Anthropic/fan-out traces. Each arm should be
independently selectable, time-bounded, persisted, and skippable when already
complete.

### 10. The Markdown report repeats citations and hides provenance

The report prints one citation block per citation occurrence, does not group
repeated evidence, and does not show provider, extraction method, evidence
tier, or why uncited records were retained. A reader cannot easily distinguish
retrieved, valid, citable, cited, excluded, and merely unused evidence.

## Required fixes, in priority order

### P0 — evidence integrity

1. Mark response-text fallback records as model-derived and non-citable. Keep
   them for triage only; never represent them as verified `page_content`.
2. Add content-aware validation with explicit reasons such as `binary`,
   `permission_error`, `provider_refusal`, `thin_content`, `model_fallback`,
   `mime_mismatch`, and `non_content`.
3. Add deterministic host-side PDF extraction and validate the extracted text
   before assigning a citable tier.
4. Separate transport success, extraction success, and evidence-validity
   status in both the data model and telemetry.

### P1 — grounding and sensitive-person safeguards

5. Add assertion/source classifications: `official_record`, `confirmed_fact`,
   `attributed_report`, `anonymous_source_report`, `expert_opinion`,
   `official_denial`, and `inference`.
6. For allegations or threat assessments, require the Writer to state
   mitigating/conflicting facts and unresolved uncertainty, and explicitly
   prevent it from equating a risk indicator with an incident or malicious
   intent.
7. Track source-level independence separately from provider-level extraction
   agreement.

### P1 — trace correlation and metrics

8. Thread the Planner request ID into gateway calls and every child attempt.
9. Add a parent retrieval-request span for each logical request and stable
   attempt IDs for provider children.
10. Put the final `evidence_id` on the attempt or provide a stable join key on
    both attempt and linkage spans.
11. Emit scrub-safe answer metrics, including cited evidence IDs/providers and
    incremental fan-out contribution.

### P2 — reports and experiments

12. Group repeated citations by evidence ID and add a provenance table with
    provider, target, source type, evidence tier, extraction method, and use in
    the final answer.
13. Report separate counts for retrieved, transport-successful, extracted,
    valid, citable, cited, excluded, and valid-but-uncited records.
14. Make experiment arms selectable and resumable, with per-arm IDs, explicit
    timeouts, persisted results, and a `skip_completed` option.
15. Evaluate fan-out by incremental supported claims/citations and unique
    source coverage relative to latency and cost—not by retained-record or
    character count alone.

## Decision implication

This run does not support default Gemini-plus-Anthropic fan-out yet. Its most
important result is that evidence-quality classification must be fixed before
provider comparisons can be trusted: `kept_count=6` did not mean six usable,
citable records. After the P0 fixes, repeat the same fixed-plan experiment and
compare each provider's unique valid sources, supported claims, citation use,
latency, and cost across multiple trials.
