# Findings from live testing

PLAN.md describes the target architecture and its design rationale. This
document is the empirical companion: what actually happened when the
pipeline in `src/pydagy_research/` was pointed at live models, live search,
and live web pages, across both retrieval backends. Every finding below was
reproduced against a real run (not inferred from reading code), and each bug
fix has a commit hash and a regression test.

---

## 1. Bugs found only by running the pipeline live

The offline test suite (mocked SDK objects, `TestModel`) caught none of
these — each depends on real wire behavior, a real subprocess, or real
provider response shapes that a mock can't reproduce faithfully. This is
itself a finding: for this kind of SDK-integration code, live smoke runs are
not optional polish on top of unit tests, they are a distinct and necessary
verification layer.

### 1.1 `Agent()` construction crashed on a cached module reference

**Symptom:** `TypeError: cannot pickle 'module' object` inside
`google.antigravity.Agent.__init__`, before a single tool call happened.

**Root cause:** `AntigravitySDKGateway.__aenter__` cached
`self._ga_types = types` (the `google.antigravity.types` module) as an
instance attribute. Our hooks are bound methods of that same gateway
instance, and `Agent.__init__` deep-copies the whole `AgentConfig` —
including its `hooks` list — before the SDK's own "keep hooks by identity"
step runs. Deep-copying a bound method deep-copies its `__self__`, so the
whole gateway instance had to be deep-copy-safe, and a live module reference
isn't.

**Fix:** module-level memoized `_antigravity_types()` helper instead of an
instance attribute. Commit `8e4dbce`.

### 1.2 Every successful page read was flagged as drift and discarded

**Symptom:** a live run that successfully read `https://www.python.org/downloads/`
still produced zero citable evidence — the read showed up in the raw
evidence list with `status="success"` but `drift_flagged=True`, and the
Evidence Validator Node correctly excludes drift-flagged records per
PLAN.md §6.3.

**Root cause:** the drift check compares the tool's *actually-executed*
argument against what was requested. `search_web`'s wire argument key is
`"query"` (lowercase); `read_url_content`'s is `"Url"` (capitalized) — not
normalized by the SDK. The drift-check code did `args.get("url", "")`,
which silently returned `""` for every read, so `_normalize("")` never
matched the requested URL and every successful read was misclassified as
drift.

**Fix:** lowercase argument keys when captured in the pre-tool-call hook.
Commit `f5a514c`. Confirmed against real hook payloads from a live session,
not guessed.

### 1.3 `pydantic_native` backend crashed immediately on `--backend pydantic_native`

**Symptom:** `TypeError: PydanticNativeSearchGateway.__init__() missing 1
required positional argument: 'model'`.

**Root cause:** `AntigravitySDKGateway.model` is optional (defaults to
`None`, resolves via `LocalAgentConfig`/ADC/env), but
`PydanticNativeSearchGateway.model` is a required positional argument (it
wraps a `pydantic_ai.Agent`, which always needs a model).
`default_deps()`'s `gateway_factory` was just `make_gateway` unbound, so it
never threaded `model` through — invisible when only exercising the
Antigravity backend, which is exactly what all prior testing had done.

**Fix:** backend-aware `gateway_factory` closure: passes `model` only on the
`pydantic_native` branch (Antigravity's `model` kwarg is a *different*
namespace — its own SDK model identifiers, not `pydantic_ai`'s
provider-prefixed ones — and must not receive the same string). Commit
`10d5a08`.

### 1.4 `pydantic_native` citations quoted retrieval-status metadata, not page text

**Symptom:** a live `--backend pydantic_native` citation's snippet was
literally `"{'retrieved_url': 'https://...', 'url_retrieval_status':
'URL_RETRIEVAL_STATUS_SUCCESS'}"`.

**Root cause:** dumped a live `WebFetch()` call's
`result.new_messages()` directly and found Google's native `web_fetch`
tool's `NativeToolReturnPart.content` is *only* retrieval bookkeeping — no
page text field anywhere in it. The real fetched content only appears in
the model's own subsequent `TextPart` (its prose response), which is
exactly the "ungrounded model paraphrase" PLAN.md §2 says must never become
`raw_extract`. The extraction code built an itemized `EvidenceRecord` from
*any* dict content unconditionally, so it stringified the status dict as if
it were real content instead of falling through to the already-implemented
`result.output` opaque-blob fallback.

**Fix:** gate itemized-record construction on the content dict containing a
recognized text field (`snippet`, `text`, `content`, `extract`, `body`,
`summary`); otherwise fall through to the output-text fallback. Commit
`8311a4d`.

### 1.5 The Planner reliably produced search-only plans

**Symptom:** a live run degraded to a limitations-only non-answer even
though the search step surfaced a correct, uncited answer in its (uncitable)
summary.

**Root cause:** the Planner's system prompt only said to "prefer" a
search-then-read sequence — too soft. Since only `page_content` evidence is
ever citable (PLAN.md §2), a plan with no `read` requests produces zero
citable evidence by construction, and the Writer correctly refused to
fabricate a citation rather than emit an ungrounded answer.

**Fix:** explicit prompt requirement plus a worked example. Commit
`f5a514c`.

### 1.6 The Planner named a generic index page instead of the specific page that had the answer

**Symptom:** comparing Firefox vs. Chrome CVEs, Chrome's citation had seven
real CVE numbers; Firefox's citation was the top-level
`mozilla.org/en-US/security/advisories/` page, which only *lists* MFSA
advisory names — no CVE numbers anywhere on it. Both backends made the same
mistake independently.

**Root cause, and why it can't be patched downstream:** `ResearchPlan` is
generated in one shot, before any retrieval happens — PLAN.md's static
graph has no back-edge from the Retrieval Node to the Planner Node. By the
time the Retrieval Node runs, the `read` target URL is already fixed. No
amount of improving the `read()` prompt, or rendering with a real headless
browser, can undo a wrong guess made at plan time — verified directly: the
same Chromium render of the wrong (index) URL just renders the index page
correctly, it doesn't retroactively pick a better URL. The bug has to be
fixed *before* the plan is finalized, or not at all.

**Fix:** gave the Planner its own `WebSearch` capability
(`build_planner_agent(model, enable_search=True)`), so it can look up the
real, current, specific URL before committing to the plan, instead of
guessing from training knowledge. Verified standalone first — a throwaway
script confirmed `WebSearch` combines correctly with forced structured
output (`output_type=ResearchPlan`) on `gemini-3.7-flash`, and successfully
found `https://www.mozilla.org/en-US/security/advisories/mfsa2026-74/`, the
exact page both prior runs missed. Re-running the same Firefox/Chrome query
after the fix produced 12 real, specific Firefox CVEs
(`CVE-2026-74944`, `-74943`, `-74940`, `-74937`, `-74936`, `-74942`,
`-74941`, `-74939`, `-74935`, `-75874`, `-74934`, `-74938`). Commit
`98f9338`. This search is planning-time-only grounding — its results never
become `EvidenceRecord`s; only the Retrieval Node's own `search()`/`read()`
calls produce citable evidence, so the citation trust boundary (PLAN.md §2)
is unchanged.

---

## 2. What "JavaScript rendering" actually required

`read_url_content` (Antigravity) and `WebFetch` (pydantic_native, at least
on Google) are both static HTML fetches — confirmed by observing
`cve.mitre.org` come back with `title=""`, `summary=""` from
`read_url_content` despite a `content_path` being populated (the SDK's own
"page too large, cached to disk" signal — PLAN.md §1.1), and separately by
GitHub/Snyk pages coming back as generic landing-page boilerplate instead of
their real (client-rendered) content.

Four options were weighed (custom Antigravity tool exposing Playwright to
the model; host-controlled Playwright fetch; `pydantic_ai`
`WebFetch(local=...)`; a hosted render-to-markdown service). Went with the
host-controlled option: `BrowserAugmentedGateway` (commit `535ccc7`) wraps
*either* backend, replacing `.read()` with a real headless-Chromium fetch
driven by host Python code — not a model-mediated tool call. That has two
consequences that matter for the trust boundary:

- No drift risk: there's no inner-model tool call to diverge from the
  requested URL.
- No "did the model remember to call `view_file` on the cached content" gap
  (finding 1.6's sibling problem, at the retrieval level rather than the
  planning level) — one `page.goto()` produces the rendered text directly.

Falls back to the wrapped gateway's own `.read()` on any render error or
empty-text render (e.g. GitHub's advisories search page hit a real 20s
Chromium timeout in one run — likely bot-detection-related — and fell back
cleanly rather than failing the whole plan).

---

## 3. Backend comparison: what actually differs, and what doesn't

Several live queries compared `--backend antigravity` against
`--backend pydantic_native` on identical questions (LangChain vs. Agno
CVEs; Firefox vs. Chrome CVEs; nginx vs. Apache CVEs). The first pass of
this comparison drew the wrong conclusion — worth documenting the
correction, not just the final answer, because the reasoning error is
itself informative.

**What looked true after 2-3 runs:** "`pydantic_native` finds better
sources and more complete evidence than Antigravity." It was faster on
every run, and on the LangChain/nginx comparisons it appeared in the final
answer to have captured more (LangChain: 9 GitHub advisories vs. Snyk's
"no known issues"; nginx: 9 CVEs vs. 4 from the same page).

**What's actually true, confirmed by an isolation experiment:** with
`--browser` enabled, `BrowserAugmentedGateway.read()` renders a given URL
with Chromium *before* either backend's own retrieval code runs at all, and
only falls back to the backend-specific path on render failure. Since the
pages in question rendered successfully, the retrieval layer was never
actually exercised differently between backends. Verified directly: called
`.read("https://nginx.org/en/security_advisories.html")` through
`BrowserAugmentedGateway` wrapping each backend, back-to-back —

```
Antigravity-wrapped raw_extract length: 9637
Native-wrapped     raw_extract length: 9637
Identical text? True
```

Byte-for-byte identical. The nginx CVE-count difference (4 vs. 9) was
**Writer synthesis variance** — same `writer_agent` code, same model, same
prompt, completely independent of which backend fetched the page; two
separate stochastic calls just enumerated a different subset of the same
20-CVE page. The LangChain source-choice difference (Snyk vs. GitHub) is
the same story one node upstream: `default_deps()` builds the identical
`planner_agent` regardless of `retrieval_backend`, so "which backend's
Planner picked a better URL" was never a meaningful question — it's the
same Planner, run twice, with ordinary sampling variance.

**What genuinely does differ between the two backends, reproducibly:**

- **Latency.** Antigravity's `search_web`/`read_url_content` go through
  `localharness` subprocess IPC and generate multiple internal turns per
  logical call (`invoke_agent → antigravity.step.0 → execute_tool
  search_web → antigravity.step.1 → antigravity.step.2 → invoke_agent →
  antigravity.step.3 → ...`), visible directly in the OTel trace (§4).
  `pydantic_native`'s native tools are a single `chat` call per use. This
  cost is structural, not run-to-run noise: the nginx/Apache comparison was
  44s (`pydantic_native`) vs. 75s (Antigravity); LangChain/Agno was 28s vs.
  75s.
- **Retrieval-layer quality when `--browser` is off, or when the browser
  render fails.** This is where the two SDKs' own extraction genuinely
  differs — Antigravity's `read_url_content` came back completely empty
  (`title=""`, `summary=""`) on `cve.mitre.org` in the very first
  (pre-`--browser`) LangChain run, while `pydantic_native`'s
  fallback-to-`output` path at least captured real synthesized content on
  the same kind of miss. Not yet re-verified in a controlled, `--browser`-off
  head-to-head (see Open Questions).

**Takeaway:** the retrieval backend choice mainly affects latency under
`--browser`, not evidence quality — `BrowserAugmentedGateway` neutralizes
most of the quality difference between the two SDKs' own fetch mechanisms
by short-circuiting them on success. This is a real, if still small-sample
(3 comparisons), data point against PLAN.md's stated default assumption
("Google's Search backend is more robust, so Antigravity is the default" —
PLAN.md §1, §"Assumptions & Boundaries"). Not yet enough evidence to revise
the default; enough to say the assumption needs the actual Benchmark
Comparator PLAN.md already calls for (§Test Plan), not anecdotal runs.

---

## 4. Observability closed a real blind spot

`logfire.instrument_pydantic_ai()` (commit `5e5b71a`) only sees
`pydantic_ai.Agent` internals — it has no visibility into
`AntigravitySDKGateway`, which never touches a `pydantic_ai.Agent`. Before
commit `c7c37b5`, `RetrievalNode` was one opaque span no matter what
happened inside the Antigravity subprocess. The SDK ships
`google.antigravity.utils.otel.get_otel_hooks()` for exactly this — standard
OpenTelemetry spans for the session/turn/step/tool-call lifecycle, which
land in the same Logfire trace automatically once `logfire.configure()` has
registered itself as the global `TracerProvider` (no separate exporter
wiring needed). This is what made finding 3's trace-shape comparison
possible in the first place: without it, the latency difference between the
backends would have been a single opaque number per run, not a visible,
explicable structural difference in turn count.

---

## 5. Open questions / suggested next steps

- **Isolate true backend fetch quality**: re-run the original empty-`mitre.org`
  case with `--browser` *off* on both backends, controlled (same URL, same
  question), to confirm §3's claim that the real retrieval-quality gap only
  shows up without browser augmentation.
- **Replace anecdotal comparison with PLAN.md's actual Benchmark
  Comparator** (§Test Plan): citation accuracy/coverage, unsupported-claim
  rate, latency, and token consumption, across enough runs per backend to
  say something statistically meaningful rather than "it was faster this
  time."
- **Apache HTTP Server's advisory page only covers the latest release**
  (`vulnerabilities_24.html` — both nginx/Apache runs flagged this in their
  own `limitations` field): a fuller historical comparison would need a
  second read of an archived/older advisories page.
- **Writer synthesis completeness itself**: finding 3 surfaced that the same
  20-CVE page can get synthesized down to 4 or up to 9 citations across
  runs. Worth checking whether the Writer prompt should more strongly
  require exhaustively enumerating a page's evidence rather than
  summarizing a subset, independent of the backend question entirely.
