# Cold-start generation and blind GOLD evaluation

## Boundary

Cold-start generation accepts primary source material only:

- GSJ publication metadata
- the original explanatory PDF
- official GSJ map-bundle assets when supplied (F1 GeoTIFF/world file, KMZ,
  and legend images)
- an optional GSJ Shapefile dataset
- optional ZFK source records

The staging command does not copy Review workbooks, GOLD fixtures, derived
JSON, extracted Abstract text, PDF page indexes, LLM caches, prior images,
coordinates, or reviewed Column configuration.  A pre-run audit fails closed
if any of those files appear before execution.

GOLD is stored under `claude_work/gold_snapshots/` and is marked
`evaluation_only`.  Production and cold-start modules do not import the GOLD
resolver.  Only the post-generation evaluator may open the snapshot.

## Prepare without external calls

```powershell
python -X utf8 claude_work/tools/run_cold_start.py 1286 `
  --source-workspace "data/02_review/05_青森/m1286_一戸 2018" `
  --run-root ".codex_tmp/cold-start-m1286"
```

This copies the admitted primary sources into a new run root, writes an input
manifest, and performs the pre-run audit.  The run root must be absent or
empty; an existing non-empty directory is rejected.

## Execute

Add `--execute` to invoke the production pipeline with the isolated
`PilotSources`.  Legacy cache migration is forcibly disabled.

```powershell
python -X utf8 claude_work/tools/run_cold_start.py 1286 `
  --source-workspace "data/02_review/05_青森/m1286_一戸 2018" `
  --run-root ".codex_tmp/cold-start-m1286-run" `
  --execute
```

This operation can make external LLM calls.  The generated Review workbook,
canonical JSON, evidence, maps, usage, and QA remain inside the run root.

If an in-run stage failed after preparation, resume from that same isolated
root instead of importing outside caches or derived files:

```powershell
python -X utf8 claude_work/tools/run_cold_start.py 1286 `
  --source-workspace "data/02_review/05_青森/m1286_一戸 2018" `
  --run-root ".codex_tmp/cold-start-m1286-run" `
  --execute --resume
```

Resume re-hashes every originally staged primary input and forcibly disables
legacy-cache migration.  Only caches and artifacts produced inside that run
root are reused.

Runs prepared before official raster staging was supported can explicitly add
those primary assets from the same source workspace. The source PDF hash must
match, and no prior workbook, GOLD file, derived JSON, or cache is imported:

```powershell
python -X utf8 claude_work/tools/run_cold_start.py 1286 `
  --source-workspace "data/02_review/05_青森/m1286_一戸 2018" `
  --run-root ".codex_tmp/cold-start-m1286-run" `
  --execute --resume --refresh-map-assets
```

## Evaluate after generation

The evaluator is a separate command.  It rejects a candidate located inside
the GOLD snapshot and never writes into the snapshot.

```powershell
python -X utf8 claude_work/tools/evaluate_cold_start.py `
  --candidate ".codex_tmp/cold-start-m1286-run/m1286_cold_start/system/raw/raw_bundle.json" `
  --output ".codex_tmp/cold-start-m1286-run/inventory_evaluation.json"
```

The first evaluation layer compares semantic unit identities and reports
precision, recall, F1, missing units, extra units, and cosmetic spelling
variants.

Column evaluation is a second, independent post-generation layer:

```powershell
python -X utf8 claude_work/tools/evaluate_cold_start_columns.py `
  --candidate ".codex_tmp/cold-start-m1286-run/m1286_cold_start/system/column_vision/column_proposal.json" `
  --output ".codex_tmp/cold-start-m1286-run/column_evaluation.json"
```

It maps generated Column names semantically (the candidate does not need GOLD
IDs), then reports Column detection, membership precision/recall/F1, per-Column
membership results, and pairwise stratigraphic-order accuracy separately.
Field, evidence, and map scoring remain future independent layers; none may
weaken the generation/evaluation boundary.
