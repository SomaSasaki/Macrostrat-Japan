# LLM routing and validated failover

## Scope

`scripts/llm_router.py` is the common transport and availability layer for
multi-provider LLM calls.  A stage supplies its own validator; the router only
accepts a response after that validator returns `accept` or `partial`.

The migrated stages are `towada_pdf_llm` (the nationwide main Abstract queue),
`pdf_unit_alias_mapping`, `pdf_body_field_enrichment`, `pdf_unit_bootstrap` and
`column_geography_vision`, plus `pdf_environment_multimodal`.

## Invariants

- API keys are loaded only immediately before adapter construction.
- Prompts, source text, images, raw responses and API keys are not stored in
  the runtime database.
- Image files are read and base64-encoded only inside the selected HTTP
  adapter.  Cache and operational records retain hashes and provenance, not
  image bytes or data URLs.
- Every routed HTTP attempt reserves its local call/token budget transactionally.
- A candidate with a configured `max_output_tokens` below the stage's reserved
  output is skipped before secret loading, budget reservation or HTTP.  The
  sanitized route history records `output_capacity`, required tokens and the
  model limit, then failover continues normally.
- Failed HTTP attempts are counted because providers can charge quota for them.
- Availability circuits are keyed by provider and model.  Quality circuits add
  the stage, so one poor task does not disable an otherwise healthy model.
- Candidates are tried sequentially.  Parallel hedging and cross-provider
  result merging are intentionally disabled.
- If all candidates fail, `AllProvidersFailed`, an `OSError`, reaches the
  existing stage-level safe-degradation path.

## Files

- `config/llm_routing.json`: public provider, model, route and local-limit data.
- `config/secret.json`: private credentials; never committed.
- `data/00_management/llm_runtime.sqlite`: generated operational state;
  ignored by Git.
- `scripts/llm_runtime.py`: reservations, usage records and circuits.
- `scripts/llm_router.py`: adapters, error classification and failover.

## Single accounting ledger

SQLite is the sole live call/token ledger for all production inference
requests.  `llm_extract.today_usage()`,
`record_usage()` and `load_limits()` retain their old signatures, but now read
or write `LLMRuntimeStore`; therefore `pilot.py` and older stage code see the
same `google-ai-project` usage as the router.

`config/llm_usage.json` is retained as an immutable migration source.  On the
first compatibility-ledger read, its day aggregates are transactionally
expanded into SQLite attempts and marked imported by source identity.  Later
reads are idempotent and never edit or delete the JSON file.  Numeric Gemini
limits now come only from `providers.gemini.limits` in `llm_routing.json`;
`llm_limits.json` keeps the paid-tier kill switch and synchronized values for
operator clarity.  The conservative shared Gemini limit is 20 calls/day until
model-specific upstream quota discovery is represented safely.

All six production stages and the `llm_extract` CLI now enter through
`LLMRouter`.  A caller-supplied `api_key` creates a one-candidate router from a
deep-copied public route; the explicit credential remains only in an in-memory
secret-loader closure and is never written to routing config, caches, results
or the runtime ledger.  This preserves the explicit-key API while centralizing
budget reservations, retries, error classification, circuits and validated
failover.  `llm_extract.call_gemini()` and `request_json()` remain temporarily
only as isolated compatibility-test code and have no production callers.

## Runtime inspection

```powershell
python scripts/llm_router.py --status
```

The secret-free route-policy audit shows the operational chain, standby
candidates, dormant providers, qualification conflicts and promotional-credit
boundaries without loading any credential or making a network request:

```powershell
python scripts/llm_route_audit.py
python scripts/llm_route_audit.py --strict --json
```

Production uses ordered failover with at most three operational candidates
(one primary and two backups).  Later configured candidates are standby, not
additional routine fan-out.  The two vision routes currently have only two
qualified/enabled candidates and are reported as thin chains rather than
silently enabling an unqualified third provider.

The output is grouped by provider and requested model and includes accepted
attempt counts and circuit state.  It does not contain source material.

After rotating a key or correcting a model configuration, an operator can
clear that model's persisted circuit state:

```powershell
python scripts/llm_router.py --reset-circuit "groq:llama-3.3-70b-versatile"
```

An optional third component limits the reset to one stage/quality scope:

```powershell
python scripts/llm_router.py --reset-circuit "groq:llama-3.3-70b-versatile:pdf_unit_alias_mapping"
```

## Provider qualification

Connectivity and geological quality are separate gates.  A text probe sends
only a fixed `OK` request and can write a timestamped, secret-safe report:

```powershell
python scripts/probe_llm_providers.py --provider nvidia --json `
  --output data/00_management/nvidia_probe.json
```

The same secret-safe probe path is registered for the new providers:

```powershell
python scripts/probe_llm_providers.py --provider bedrock --json `
  --output data/00_management/bedrock_text_probe.json
python scripts/probe_llm_providers.py --provider azure --json `
  --output data/00_management/azure_text_probe.json
```

Vision uses the production adapter with a fixed synthetic 32x32 checkerboard,
never a project image.  Use two images when proving the environment route's multi-image
payload:

```powershell
python scripts/probe_llm_providers.py --provider cohere --vision --images 2 `
  --output data/00_management/cohere_vision_probe.json
```

Bedrock synthetic-image qualification uses the same command with
`--provider bedrock`.  Running any command in this section is an external API
call and requires the applicable data-send approval.

`scripts/llm_qualification.py` then combines that probe with a normalized GOLD
summary.  The summary contains opaque ASCII expected/actual item IDs (hash a
value rather than embedding it) and the current stage-validator decision, not
prompts, source text, images or raw responses.
See `config/llm_gold_result.example.json` for the input shape.

```powershell
python scripts/llm_qualification.py path/to/gold_result.json `
  --probe-results data/00_management/provider_probe.json --record
python scripts/llm_qualification.py --status
```

The policy in `config/llm_qualification.json` requires a route/model match,
fresh successful capability probe, current prompt and validator versions,
minimum case coverage, zero critical failures, perfect precision and a
stage-specific recall floor.  A passing verdict does not edit routing config or
enable a disabled candidate.  Records expire automatically when either their
probe or GOLD evidence becomes stale; activation remains an explicit reviewed
configuration change.

### AWS Bedrock and Azure OpenAI preparation

AWS Bedrock and Azure OpenAI are implemented in the same `HTTPAdapter`, so
they inherit the router's transactional usage reservation, retry policy,
error classification, circuit breakers and validator-gated failover.  No
provider-specific `urlopen` or nested retry loop was added; this prevents one
logical attempt from making or recording an unknown number of HTTP calls.

Bedrock uses the Converse endpoint with a bearer token and constructs text and
image ContentBlocks only at invocation time.  Azure uses its configured
`/openai/v1` endpoint, the `api-key` header and the deployed model name loaded
from private configuration.  The GPT-5 candidate sends
`max_completion_tokens`, omits `temperature`, and avoids a sacrificial invalid
request to negotiate parameters.  Neither the Azure endpoint nor deployment
name is copied into operational results.

Azure qualification distinguishes the private deployment name from the model
snapshot returned by the service.  The configured deployment `gpt-5-mini`
currently resolves to the exact reviewed snapshot `gpt-5-mini-2025-08-07`.
That snapshot is pinned in the public route: a future silent deployment change
causes `actual_model_mismatch` and requires fresh review rather than passing by
prefix or wildcard.

Azure `gpt-5-mini` is qualified and enabled for `pdf_unit_alias_mapping`.
Bedrock `mistral.mistral-large-3-675b-instruct` is qualified and enabled for
`pdf_body_field_enrichment`.  Bedrock
`us.anthropic.claude-haiku-4-5-20251001-v1:0` remains registered but disabled
for Column Vision and PDF environment multimodal extraction.

The same Bedrock Mistral Large 3 model is qualified and enabled for
`pdf_unit_bootstrap`.  The bootstrap GOLD runner uses four short
English Abstract excerpts (917 source characters, seven reviewed units), one
provider attempt per case and the production prompt/validator.  Its persisted
result contains only opaque unit hashes, decisions and critical-failure codes;
it does not store the excerpts, prompt, workbook values or raw response.
The final 2026-08-11 run passed 7/7 targets across all four cases with precision
and recall 1.0 and zero critical failures.  It is qualified through 2026-08-25.

The implementation and payload shapes have local network-free tests.  Live
qualification sends are recorded below; the remaining disabled candidates require
their relevant capability probe, Ichinohe GOLD pass and promotional
credit/spend review before a separate configuration change can enable them.
AWS and Azure are marked as credit-backed rather than assumed permanently
free; exhausted or expired promotional credit must be treated as a billing
boundary.

The first Bedrock synthetic-image probe on 2026-08-11 used the base Haiku 4.5
model ID and was rejected before inference because that model does not support
direct on-demand invocation for this account.  The route now uses AWS's exact
US Geo Inference Profile ID,
`us.anthropic.claude-haiku-4-5-20251001-v1:0`.  The failed base-model call is
not evidence for or against image capability; the profile-based results below
supersede it.

After switching to the US Geo Inference Profile, the approved one-image probe
passed with HTTP 200, exact model identity and `OK` in 2.368 seconds.  The
subsequent two-image probe was stopped by Bedrock before model inference with
the Anthropic use-case-details account requirement.  It was not retried.  This
does not establish a multi-image payload failure: Column Vision now has a
connectivity pass, while the environment route remains unqualified until the
account form/propagation issue is cleared and the synthetic two-image probe is
rerun.

### Qualification results

On 2026-08-11 Cohere `command-a-vision-07-2025` passed authenticated one- and
two-image synthetic probes, then failed the reviewed Ichinohe Column Vision
GOLD case.  The production validator accepted 0 of 42 expected memberships;
the provider attempt ended as `json_parse` with 2,048 output tokens.  This is
consistent with a truncated response, but truncation is an inference rather
than a stored raw-response observation.  The candidate remains disabled.  It
must not be enabled unless the production task is safely reduced and the same
GOLD fixture is rerun successfully.

The fixture is bound by SHA-256 to the reviewed Ichinohe workbook, uses the
correct Fig. 2.1 on printed page 6, and evaluates western, central and eastern
memberships as three cases.  Only opaque membership hashes are written to the
qualification result; the temporary rendered page and accepted proposal are
deleted when the run finishes.

On 2026-08-11 NVIDIA `nvidia/nemotron-3-nano-30b-a3b` passed the fixed text
probe, then failed the reviewed Ichinohe main Abstract GOLD set.  The hosted
request used 10,589 input tokens and reached the configured 16,384 output-token
cap after about 162 seconds.  The response was incomplete JSON, so the
production validator accepted 0 of 87 expected fields across four cases and
the qualification verdict was blocked.  The candidate remains disabled.  The
GOLD result and verdict persist only opaque field hashes and aggregate status;
the prompt and raw response are not stored.

## Alias route

The operational order is:

1. Groq `llama-3.3-70b-versatile`
2. Azure `gpt-5-mini` (actual snapshot `gpt-5-mini-2025-08-07`)
3. Cohere `command-a-plus-05-2026`

Mistral `mistral-small-latest` and Gemini `gemini-3.5-flash-lite` remain
configured as standby candidates outside the normal three-candidate chain.

NVIDIA `nvidia/nemotron-3-nano-30b-a3b` is registered after Azure
candidate as a short-JSON candidate, with a 16,384-token output limit and one attempt.
It remains disabled because the 2026-08-11 `pdf_unit_alias_mapping` GOLD run
did not qualify.  If a future reviewed run passes, the route can permit all
eligible candidates subject to the configured failover limit.

Azure's 2,048-token GOLD attempt returned no visible text.  At 8,192 tokens it
produced 17/19 verified mappings with zero false positives.  That run exposed a
validator defect: the valid two-character proper name `関層` was rejected by a
generic minimum-length rule.  The validator now rejects explicit generic terms
(`層`, `地層`, `堆積物`, `火山岩`) instead of rejecting all short proper names.
The rerun then passed 19/19 with all three cases accepted, precision and recall
1.0 and zero critical failures.  Azure is enabled with a candidate-specific
8,192-token minimum output reservation; other alias providers retain the
stage's normal reservation.

The NVIDIA alias GOLD fixture is bound by SHA-256 to the reviewed Ichinohe
workbook, PDF, local PDF-page index and raw canonical bundle.  It contains 19
manually checked English/Japanese mappings in three cases.  The runner sends
only the 19 English unit IDs/names plus locally extracted contents/figure-list
text, forces NVIDIA with one attempt and no failover, and persists only opaque
mapping hashes.  The reviewed mappings, prompt and raw response are never
written to the GOLD result.

The approved live run made one NVIDIA request with no retry or failover.  The
endpoint returned parseable JSON after 4,785 input tokens, exactly 2,048 output
tokens and 29.84 seconds, but production validation accepted 0/19 mappings in
all three cases.  Qualification is therefore `BLOCKED` (validator pass rate
0.0, recall 0.0, three critical failures), and the candidate remains disabled.
Hitting the requested output cap makes truncation plausible, but the raw
response was intentionally not retained, so this is not recorded as a proven
cause.

The router filters candidates by capabilities, output capacity, context
headroom, budget and circuit state.  The existing exact-page and exact-quote
alias verifier then decides whether output is usable.  Zero verified aliases
causes failover; verified incomplete output is accepted as `partial` and
remains reviewable.

Alias cache identity is provider-neutral.  It includes source, prompt, schema
and validator versions, but not provider or model.  Provider provenance and the
actual model are stored in the cache document.  Old exact-model cache entries
are migrated only after the accepted aliases pass the current verifier again.

## Main Abstract extraction route

The operational order for the legacy-named `towada_pdf_llm` stage is Mistral,
Cohere, then Gemini.  Despite the retained artifact name, this is the generic
nationwide main PDF/Abstract enrichment queue.  NVIDIA is registered but
disabled after failing the 2026-08-11 Ichinohe main-extraction GOLD set at the
hosted endpoint's configured 16,384 output-token cap.

Every raw candidate is rerun through the existing field verifier: the unit
must match an exact canonical target, its field must have been requested, its
quote must occur in the Abstract, and numeric ages/thicknesses must occur in
that quote.  Zero verified fields rejects the response and advances to the
next provider.  One or more verified fields is an intentional partial success;
providers are not called again merely to fill optional blanks, and results are
never merged across providers.

The cache identity includes map, source, prompt, target and validator data but
not provider/model.  Provider provenance and sanitized route attempts remain
in the cache and manifest.  Legacy Gemini/model-bound caches are migrated only
after every retained field is reconstructed and rerun through the current
quote, numeric and canonical-target validators.

## Targeted PDF-body route

The operational order is:

1. Mistral `mistral-small-latest`
2. Bedrock `mistral.mistral-large-3-675b-instruct`
3. Cohere `command-a-plus-05-2026`

Gemini `gemini-3.5-flash-lite` remains configured as standby outside the
normal three-candidate chain.

NVIDIA `nvidia/nemotron-3-nano-30b-a3b` is present in the route but disabled
until it passes a long-context GOLD-set quality test.  A connectivity probe is
not sufficient evidence for automatic geological extraction.

Bedrock Mistral Large 3 is registered immediately after Mistral and is enabled
as an independent credit-backed fallback.

The body-field GOLD runner is now implemented in
`scripts/run_body_field_gold.py`.  Its four reviewed Ichinohe cases cover nine
thickness, environment and basal-surface targets from Takayashiki, Seki,
Nanashigure and Oritsumedake.  The fixture is SHA-256-bound to the reviewed
workbook, source PDF, PDF-page index and compiled layer.  Only four short source
spans are placed in the live prompt.  The result persists opaque field hashes,
per-case validator decisions and critical-failure codes; it never persists the
prompt, source spans, workbook values or raw provider response.  The 2026-08-11
local validation passed.  The first live run reached 7/9 with zero false
positives; its only misses came from an Oritsumedake excerpt whose two PDF
columns had been interleaved.  The source-span builder now separates only exact,
page-bound substrings before prompting.  The rerun passed 9/9 with all four
cases accepted, precision and recall 1.0, and zero critical failures.  The
candidate is qualified through 2026-08-25 and enabled in the route.

Candidates whose configured context window cannot hold the conservative input
estimate, reserved output and headroom are skipped before any HTTP request.
The existing field verifier checks unit IDs, requested fields, exact quotes,
numeric support, local-sample versus formation-boundary ages, lithology roles
and controlled vocabulary values.  Zero verified fields triggers failover.  At
least one verified field is accepted as a partial result, preventing duplicate
long-context calls just to fill every optional field.

The body cache is provider-neutral and records the selected provider, requested
model and actual model.  Full routed source contexts are not duplicated into
new cache entries; only the necessary verified quote and context ID are kept.
Old model-keyed entries are migrated only after reconstruction and current
verifier revalidation.

## PDF-only unit bootstrap route

The operational order is:

1. Mistral `mistral-small-latest`
2. Bedrock `mistral.mistral-large-3-675b-instruct`
3. Cohere `command-a-plus-05-2026`

Gemini `gemini-3.5-flash-lite` remains configured as standby outside the
normal three-candidate chain.

The NVIDIA candidate is registered but disabled until it passes a long-context
inventory GOLD test.

Bootstrap is stricter than field enrichment because an incomplete response can
silently remove entire geological units.  Each candidate inventory is checked
for:

- exact or coordinated-list unit-name support in the Abstract;
- existing per-field quote and numeric validation;
- duplicate names;
- invalid-candidate ratio no greater than 50 percent;
- at least 80 percent coverage of conservatively detected, directly named
  formations, members, lavas and deposits;
- deterministic inclusion of explicitly listed minor surficial units.

Generic terrace-rank containers such as `middle terrace deposits` and `lower
terrace deposits` are deterministically removed before invalid-output scoring;
they organize specific named units but are not themselves reviewed units.  The
closed removal set was added after the first Bedrock GOLD run returned the two
container labels alongside both correct Towada pyroclastic-flow units.  That
initial anonymous result is retained for audit, and the corrected rerun is the
qualification record.

Zero accepted units, excessive invalid output or insufficient inventory-hint
coverage triggers the next provider.  Results from different providers are not
merged.  If all candidates fail, the pilot preserves its `NO_DATA` review
placeholder and does not send that placeholder into the later vision stage.

Bootstrap cache identity excludes provider and model.  Prior model-keyed cache
entries are reconstructed and passed through the current field, name and
coverage validators before migration.  Provider, requested model, actual model,
validation metrics and sanitized attempt provenance remain in the cache and
stage manifest.

## Column/geography Vision route

The operational order is Mistral `mistral-small-latest`, then Gemini
`gemini-3.5-flash-lite`.  This moves routine image interpretation away from
Gemini while retaining it as an independent fallback.  Cohere
`command-a-vision-07-2025` is registered but disabled until both its authenticated
image payload and the stage GOLD set pass.  The configured NVIDIA text model is
also disabled because image-input support has not been verified for it.
Bedrock Claude Haiku 4.5 is registered ahead of the enabled candidates but is
disabled: its payload builder is locally tested, while actual image delivery
and Column quality remain unverified.

The common adapter sends Gemini `inline_data` and OpenAI-compatible image data
URLs without writing either representation to disk.  A candidate is accepted
only after the existing Column validator confirms usable Columns, canonical
unit IDs and names, valid memberships, controlled interval names and verbatim
report quotations.  Missing canonical units may remain an explicit partial,
reviewable result; a response with no assignment-ready Column set triggers the
next provider.  Provider outputs are never merged.

Vision cache identity includes the PDF, rendered-image, source-text, prompt and
validator hashes, but excludes provider and model.  Existing Gemini-bound cache
entries are adopted only after their stored proposal passes the current
validator again.  Cache files keep accepted proposal data and provider
provenance, but never the prompt, image data or API key.

## PDF environment multimodal route

The operational order is Mistral `mistral-small-latest`, then Gemini
`gemini-3.5-flash-lite`.  Cohere `command-a-vision-07-2025` is registered but
disabled.  Its authenticated two-image payload passed, but the 2026-08-11
Japanese environment GOLD run produced 150 output tokens and zero of five
reviewed targets passed the production validator.  The sanitized qualification
record has recall 0.0 and no prompt, source text, images or raw response.  The
configured NVIDIA model is disabled because its image-input support has not
been verified.
Bedrock Claude Haiku 4.5 is registered first but disabled until a synthetic
two-image probe and the Japanese environment GOLD both pass.

Up to three figures are attached in the same stable order as their `fig_1`,
`fig_2` and `fig_3` identifiers.  Images are encoded only inside the selected
adapter and are never persisted in caches or operational records.  Every
candidate response is checked by the existing environment verifier for exact
canonical unit and Column membership, controlled environment vocabulary,
applicability state, exact source quotations and valid figure references.  A
response with zero accepted target rows triggers failover.  A verified subset
is retained as an explicit partial result; provider outputs are never merged.

Environment cache identity includes source, targets, figure hashes, prompt and
validator versions, but excludes provider and model.  Compatible legacy
Gemini/model-bound entries are migrated only after their accepted rows are
reconstructed and rerun through the current verifier.  The cache keeps selected
provider provenance and sanitized route attempts, not prompts, image bytes or
credentials.

## Adding a stage

1. Add a route and static capability allowlist to `config/llm_routing.json`.
2. Wrap the existing stage verifier so it returns `ValidationReport`.
3. Define explicit `accept`, `partial` and `reject` conditions.
4. Pass a provider-neutral `logical_job_id` in `LLMRequest`.
5. Preserve the stage's existing all-provider-failure degradation behavior.
6. Add fake-adapter tests for transport failure, malformed JSON, validation
   rejection, quota exhaustion and cache reuse before enabling live traffic.
