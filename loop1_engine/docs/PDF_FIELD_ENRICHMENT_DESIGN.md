# PDF Field Enrichment Design

Status: Proposed  
Date: 2026-08-09  
Scope: GSJ 1:50,000 review workflow

## 1. Objective

When ZFK or Shapefile data is absent or incomplete, the workflow must use the
GSJ explanatory-report PDF to populate reviewable Macrostrat unit fields. The
result must remain auditable: every PDF-derived value must retain the original
quote, PDF page, printed page, section, extraction method, confidence, and
review state.

This design covers these requested fields:

- `section_id`
- `t_pos`
- `t_int`, `b_int`
- `t_prop`, `b_prop`
- `strat_name` (the requested `strart_name` is treated as a spelling error)
- `environment`
- `unit_description`
- `lithology`, `minor_lith` (the requested `minor_lithology` maps to the
  existing canonical field `minor_lith`)
- `min_thickness`, `max_thickness`
- `basal_surface`
- `lateral_relationship`

The design does not force a value when the PDF does not support one. A field
is considered handled when it has either a reviewable value or an explicit
reason such as `not_stated`, `not_applicable`, `conflict`, or
`insufficient_evidence`.

## 2. Current State and Root Cause

The current Abstract stage already requests most descriptive and physical
fields. However, two architectural gaps prevent reliable workbook population.

### 2.1 English Abstract only

`pilot_llm.py` sends only the English Abstract to the field extractor. Many
thicknesses, contacts, local distributions, and omitted map units occur only
in the Japanese main body. The Talus deposits in m1050 are one confirmed
example.

### 2.2 Evidence loses its target after Column subdivision

The PDF-only bootstrap currently creates evidence with
`column_ids=["unsplit"]`. Column Vision later replaces the provisional unit row
with eastern/western Column rows. Unit-global PDF evidence remains attached to
`unsplit`, so the canonical compiler cannot associate it with the new rows.

This is visible in the current m1286 result:

- 140 PDF evidence records exist.
- They include 22 lithology, 18 description, 11 stratigraphic-name, 10 basal
  surface, and 9 numeric-age candidates.
- The final 29 review rows still have all requested fields blank because the
  evidence scope no longer matches the subdivided rows.

### 2.3 Derived fields are calculated too late for canonical visibility

`position`, `section_id`, `t_pos`, `t_prop`, and `b_prop` are intentionally
recalculated during submission export. The Review workbook can display
previews, but `compiled.json` does not currently contain a durable derived
preview layer. Consequently PDF-only workbooks can appear empty even when
enough inputs exist to calculate some values.

## 3. Design Principles

1. **Human input is authoritative.** Automation never overwrites a reviewed
   nonblank value.
2. **Source priority remains:** Review > ZFK > Shapefile > PDF explicit > PDF
   inferred.
3. **Evidence is field-scoped and unit-scoped.** Column subdivision must not
   detach unit-global evidence.
4. **Extract facts; calculate derivatives.** `section_id`, `t_pos`, `t_prop`,
   and `b_prop` are calculated from verified inputs, not copied from prose.
5. **No whole-report LLM request.** Only the English Abstract and locally
   selected Japanese-body excerpts are sent.
6. **Cache before merge.** Every completed LLM response is saved before
   workbook generation.
7. **Fail closed by field.** A bad candidate is dropped without discarding
   valid candidates for other fields.
8. **English output, original-language evidence.** Final Macrostrat values are
   English; Japanese source quotes remain unchanged in Evidence.

## 4. Target Architecture

```text
PDF + local page-text index
        |
        +-- English Abstract extraction
        |
        +-- unit alias table
        |     ZFK Japanese/English names
        |     Shape labels and map symbols
        |     PDF legend/contents aliases for PDF-only maps
        |
        +-- targeted body-page router
              unit heading pages
              age paragraphs
              distribution/thickness paragraphs
              contacts/relationships paragraphs
              tables and stratigraphic figures
        |
        +-- deterministic extractors first
        |     page/section match, ages, thickness, contact keywords
        |
        +-- one cached LLM body job for unresolved page groups
        |
        +-- quote and numeric verification
        |
        +-- canonical vocabulary/interval normalization
        |
        +-- scope-aware Evidence merge
        |
        +-- derived preview calculation
        |
        +-- Review / Columns / Evidence / Project workbook
```

## 5. Evidence Scope Contract

Evidence scope must be independent of a temporary Column name.

```json
{
  "evidence_id": "ev_...",
  "unit_id": "m1286_p001",
  "field": "lithology",
  "candidate": "sandstone; mudstone",
  "scope": {
    "type": "unit_global",
    "column_ids": []
  },
  "source": {
    "type": "PDF",
    "file": "..._D.pdf",
    "language": "ja",
    "pdf_page": 61,
    "printed_page": 53,
    "section": "4.15",
    "quote": "original source text"
  },
  "assertion": "explicit",
  "selection": "candidate",
  "confidence": {"class": "B", "score": 0.85}
}
```

Scope rules:

- `unit_global`: apply to every canonical row sharing `unit_id`, including rows
  created later by Column subdivision.
- `column_specific`: apply only to the listed Column rows.
- `map_global`: contextual evidence that is not a unit value.
- The string `unsplit` must never be used as the scope of unit-global evidence.
- After any Column assignment or row split, the canonical layer is rebuilt and
  the binding audit must report zero orphaned evidence records.

Most age, stratigraphic name, description, and lithology evidence is
`unit_global`. Thickness, environment, basal surface, and lateral relationship
may be `column_specific` when the source explicitly limits the statement to a
named area.

## 6. PDF Context Routing

### 6.1 Unit alias table

Create `system/pdf_enrichment/unit_aliases.json` before body extraction. Each
unit stores:

- canonical English `unit_name`
- Japanese name(s)
- GSJ map symbol
- ZFK section title and number, when available
- Shape English/Japanese labels, when available
- PDF-only legend or contents aliases

For PDF-only maps, the existing Abstract inventory provides English names.
Japanese aliases are recovered from the legend, contents pages, or a selected
stratigraphic figure. This is a separate cached alias-mapping job; it is not
repeated for each unit.

### 6.2 Page selection

The local PDF page-text index scores pages using:

1. exact numbered section heading plus map symbol;
2. exact Japanese or English unit heading;
3. unit alias plus field keywords;
4. table/figure caption containing the unit alias;
5. generic name matches only as a last resort.

Field keywords include age, thickness, distribution, lithology, environment,
contact, unconformity, interfingering, and their Japanese equivalents.

The router retains the best heading page and, when required, one adjacent page
for paragraph continuation. It must not send the full report to the LLM.

### 6.3 Context units

The LLM receives paragraph- or table-sized context blocks with stable IDs, not
raw page dumps. Each block contains:

- `context_id`
- unit aliases
- PDF/printed page and section
- original text
- requested unresolved fields only

## 7. Field Rules

| Field | Mode | PDF input | Promotion rule |
|---|---|---|---|
| `section_id` | Derived preview | Column order and verified numeric age bounds | Calculate only when sufficient bounds show a defensible age gap. Otherwise blank with `insufficient_evidence`. |
| `t_pos` | Derived preview | Column membership and `sort_order` | Calculate after expanding shared units by Column. The top unit receives `max(position)+1`; overlapping positions receive an explicit upper position. |
| `t_int`, `b_int` | Extract + normalize | Explicit interval words and verified numeric ages | Normalize to the local Macrostrat international-interval table. Numeric age containment may correct a coarse GSJ label and must create separate normalization evidence. |
| `t_prop`, `b_prop` | Derived preview | Verified numeric top/bottom ages plus normalized intervals | Calculate only when the relevant age and interval exist and the result is within `[0,1]`. Display to three decimals. |
| `strat_name` | Extract | Formal unit hierarchy or official map-unit label | Preserve child-to-parent hierarchy with commas. If no hierarchy is stated, the verified official English unit label may be used as a reviewable fallback. |
| `environment` | Extract/infer | Explicit depositional setting; otherwise verified facies context | Explicit values are preferred. Inferred values are yellow `CHECK` candidates. Do not force an environment for intrusive or eruptive units when it is not applicable. |
| `unit_description` | Synthesize from facts | One or more verified unit paragraphs | Produce concise English text from verified facts only. Retain every Japanese/English source quote used. A translation or synthesis is always marked inferred. |
| `lithology` | Extract/normalize | Main lithology wording | Main materials only; semicolon-separated Macrostrat terms. Promote only validated vocabulary or clearly allowed free text. |
| `minor_lith` | Extract/normalize | Subordinate or intercalated materials | Terms introduced by minor, subordinate, intercalated, locally, rare, or equivalent wording. Do not duplicate primary lithology. |
| `min_thickness` | Extract | Explicit range, minimum, or lower-bound statement | Metres only. A range supplies min/max; `more than X` supplies min only. Local values remain Column-specific. |
| `max_thickness` | Extract | Explicit range, maximum, or upper-bound statement | Metres only. `up to X` supplies max only; never invent a minimum of zero. |
| `basal_surface` | Extract/normalize | Explicit basal contact with the underlying unit | `conformable`, `disconformable`, `unconformable`, `fault`, `gradational`, `sharp`, `erosional`, or `intrusive`. Plain `overlies` is insufficient. |
| `lateral_relationship` | Extract only | Explicit lateral relation | Accept only explicit interfingering, onlap/transgressive, erosional, or gradational lateral statements. Ordinary superposition is not lateral evidence. |

### 7.1 Age boundary semantics

- `b_age_ma` is older/bottom; `t_age_ma` is younger/top.
- A numeric range populates both bounds.
- `younger than`, `older than`, `before`, and `after` populate only the
  semantically supported side.
- An isolated radiometric date for a long-duration formation is supporting age
  evidence, not automatically both unit boundaries.
- A clearly instantaneous eruption, tephra fall, lava flow, or pyroclastic-flow
  event may use the same age for both boundaries. Its props are widened by the
  smallest interval that rounds to the same three-decimal display value while
  preserving `b_prop < t_prop`.

### 7.2 Prop calculation

For age `A` within an interval with older bound `B` and younger bound `T`:

```text
prop = (B - A) / (B - T)
```

Validation requires:

- `0 <= prop <= 1`;
- interval contains the numeric age;
- when top and bottom use the same interval, `b_prop < t_prop`;
- display format is `0.000`, while a higher-precision internal value is kept
  for validation and export.

## 8. Candidate Verification and Resolution

Every PDF candidate passes these gates independently:

1. exact canonical unit or verified alias match;
2. source quote occurs on the cited PDF page;
3. reported numeric value occurs in the quote after unit conversion;
4. candidate field was requested and is permitted by the schema;
5. vocabulary or interval normalization succeeds where required;
6. evidence scope matches the target unit/Column;
7. no higher-priority reviewed or structured value is overwritten.

Resolution behavior:

- A verified explicit candidate may fill a blank Review value.
- Inferred environment, synthesized description, and primary/minor lithology
  classification may fill a blank cell only as a yellow `AUTO CANDIDATE` and
  keep the row at `CHECK`.
- Conflicting candidates remain in Evidence; the Review cell is not silently
  resolved.
- Missing fields receive a machine-readable reason in the enrichment manifest.

## 9. Derived Preview Layer

Add `system/derived_previews.json` and include the same preview values in
`compiled.json` under a separate `derived` object. They are never confused with
reviewed values.

```json
{
  "row_key": "m1286_p001::eastern",
  "derived": {
    "position": 12,
    "section_id": null,
    "t_pos": null,
    "t_prop": 0.714,
    "b_prop": 0.231
  },
  "dependencies": {
    "sort_order": 9,
    "t_int": "Tortonian",
    "b_int": "Tortonian",
    "t_age_ma": 8.5,
    "b_age_ma": 10.5
  }
}
```

The workbook shows these fields as blue reference cells. Submission export
always recalculates them from the final edited Column, order, interval, and age
values. A dependency hash lets the workbook/QA report that a preview is stale
after manual edits.

## 10. Workbook Interface

The Review sheet remains compact; it will not add one evidence column beside
every value.

- PDF auto-candidates are yellow.
- Derived previews are blue and read-only in normal review work.
- Each candidate cell receives a short note containing evidence ID, source,
  page, confidence, and the first source sentence.
- Full quotes and all competing candidates remain in the Evidence sheet.
- `age_evidence`, `context_evidence`, and `physical_evidence` remain the three
  compact summary columns.
- Missing-reason and conflict indicators are included in `status` and
  `comments` rather than creating many additional columns.

The Evidence sheet must show original Japanese text without translation loss,
plus the English candidate separately.

## 11. Cache and Token Budget

The body stage is resumable and field-selective.

Cache identity includes:

- map ID and PDF SHA-256;
- selected page/context hashes;
- unit IDs and requested fields;
- prompt version and model;
- vocabulary/interval-table version.

Call strategy per map:

1. one Abstract call, reused when already cached;
2. zero or one alias-mapping call for PDF-only maps;
3. one body extraction call for the selected unresolved contexts;
4. split the body call only when the configured per-call token limit would be
   exceeded.

The preflight shows pending calls and estimated tokens before any request.
Completed jobs are saved before canonical merge. Raw API keys and unrestricted
whole-report prompts are never stored.

## 12. Proposed Modules

- `scripts/evidence_scope.py`
  - scope migration, row binding, orphan audit
- `scripts/pdf_context_router.py`
  - alias table, page scoring, paragraph/table contexts
- `scripts/pdf_field_extract.py`
  - cached body jobs, response schema, quote verification
- `scripts/pdf_field_normalize.py`
  - age semantics, vocabulary, thickness, contacts, relationships
- `scripts/derived_previews.py`
  - position, section, t_pos, and prop previews with dependency hashes
- `scripts/pilot.py`
  - orchestration only; no field-specific parsing logic

All source code, identifiers, prompts, schemas, tests, logs, and generated
machine metadata remain English.

## 13. Implementation Order

### Phase 1: Correct evidence binding

1. Add the scope contract.
2. Migrate existing `unsplit` unit-global PDF evidence in memory.
3. Rebind evidence after Column subdivision.
4. Add an orphan-evidence QA gate.

This phase should immediately make already-cached Ichinohe PDF candidates
visible without another LLM call.

### Phase 2: Add targeted Japanese-body contexts

1. Build the bilingual unit alias table.
2. Route unresolved fields to exact pages/sections.
3. Reuse the Talus deterministic body-heading matcher as the first route.
4. Save contexts and routing decisions for inspection.

### Phase 3: Extract and normalize fields

1. Run deterministic age/thickness/contact extraction first.
2. Run one cached LLM body job for remaining fields.
3. Verify quotes and numeric support.
4. Normalize Macrostrat intervals and vocabulary.
5. Merge only blank, nonconflicting candidates.

### Phase 4: Derived previews and workbook display

1. Calculate the derived preview layer after the final Column split.
2. Add dependency hashes and stale-preview QA.
3. Display candidate notes and missing reasons.
4. Keep export-time recalculation authoritative.

### Phase 5: Pilot validation

1. Regenerate m1286 from the existing cache and confirm scope repair.
2. Regenerate m1050 and confirm Japanese-body evidence remains attached.
3. Compare Review values, Evidence, map, and KML visually.
4. Apply user feedback before processing another map.

## 14. Acceptance Criteria

### Data integrity

- Zero orphaned evidence records after Column subdivision.
- Zero lower-priority overwrites of reviewed values.
- Every promoted PDF value has a page, quote, field, unit ID, confidence, and
  extraction method.
- Every missing requested field has a reason code.

### m1286 regression

- Existing cached PDF evidence is reused with zero external calls.
- Every validated unit-global candidate is visible on every applicable split
  row.
- The current 22 lithology, 18 description, 11 stratigraphic-name, 10 basal
  surface, and 9 numeric-age evidence groups are no longer lost because of
  `unsplit` scope.
- Positions and top caps are displayed for every valid Column ordering.
- Interval and prop previews appear wherever verified age inputs support them.

### m1050 regression

- Talus Japanese body evidence remains cited as PDF page 61 / printed page 53.
- ZFK and Shape values remain higher priority than PDF candidates.
- The existing two-Column map and KML remain valid.
- No new LLM call is made when all required jobs are cached.

### Nationwide behavior

- PDF-only, PDF+Shape, PDF+ZFK, and all-three-source maps use the same command.
- Missing Shape skips map/KML but does not stop PDF enrichment.
- Missing PDF produces a review workbook with explicit missing reasons instead
  of an exception.
- No stage assumes map 1050 or map 1286.

## 15. External References

- Macrostrat documentation: <https://dev.macrostrat.org/docs>
- Macrostrat lexicon and controlled terms: <https://dev.macrostrat.org/lex/units>
- Macrostrat map-field descriptions: <https://tiles.macrostrat.org/>
- Local interval definitions: `config/intervals.json`
- Local vocabulary snapshot: `config/vocab.json`

