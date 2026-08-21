# Claude Code Project Guidelines for Macrostrat Japan

## 1. System Invariants & Non-Negotiable Rules
- **Monotonicity**: For every stratigraphic unit, `b_age >= t_age >= 0.0` Ma.
- **Evidence Integrity**: Never invent numerical ages. Every extracted age must retain its verbatim GSJ Japanese quote (`Evidence` sheet).
- **Controlled Vocabulary**: Lithology and environment values must strictly match `loop2_governance/config/vocab.json`.
- **Immutable ID**: Unit IDs (e.g. `m1286_p001`) must remain stable across re-runs.
- **Academic Writing Standard**: Strictly follow `loop3_community/docs/ACADEMIC_WRITING_GUIDELINES.md`. Emojis, hype expressions, and decorative icons are strictly forbidden.

## 2. 3-Loop Architecture & Directory Roles
- **Command Center (Loop 2)**:
  - `loop2_governance/UNIFIED_PORTAL.md`: Master portal and researcher directives.
  - `loop2_governance/specs/TASK.md`: Current sprint task assigned by PI.
  - `loop2_governance/specs/FEEDBACK.md`: Agent execution reports and verification logs.
  - `loop2_governance/specs/MEMORY.md`: Shared persistent context and sprint state.
  - `loop2_governance/specs/CONTEXT_HANDOFF.json`: Real-time lock and state tracker.
  - `loop2_governance/research_hub/`: Prior art database and academic gap matrix.
- **Computation Engine (Loop 1)**:
  - `loop1_engine/scripts/`: Deterministic pipeline modules.
  - `loop1_engine/tests/`: Automated test suite (pytest - 73 tests).
  - `loop1_engine/archive/`: Consolidated master knowledge and historical development archives.
  - `loop1_engine/dashboard/`: GSJ-style nationwide progress map server (`http://127.0.0.1:8787/`).
- **Public Dissemination (Loop 3)**:
  - `loop3_community/publications/PUBLICATION_PAPER.md`: English/Japanese publication paper.

## 3. Mutual Collision Prevention Protocol (Claude Code & Codex)
1. **Pre-check**: Inspect `loop2_governance/specs/CONTEXT_HANDOFF.json` to confirm no active conflicting job from Codex.
2. **Lock State**: Update `CONTEXT_HANDOFF.json` to register active worker status (`RUNNING`).
3. **Execution & Verification**:
   - Run tests: `.venv\Scripts\python -m pytest loop1_engine/tests/`
   - Run audit: `.venv\Scripts\python run.py audit <map_id>`
4. **Post-execution**:
   - Log report to `loop2_governance/specs/FEEDBACK.md`.
   - Update `loop2_governance/specs/MEMORY.md`.
   - Release lock in `loop2_governance/specs/CONTEXT_HANDOFF.json` (`COMPLETED` / `IDLE`).

---
## 関連仕様書・先行研究ハブ
- [[docs/LOOP1_PIPELINE_STRUCTURE.md|LOOP1_PIPELINE_STRUCTURE.md]]
- [[../loop2_governance/UNIFIED_PORTAL.md|UNIFIED_PORTAL.md]]
- [[../loop2_governance/research_hub/INDEX.md|research_hub/ (先行研究包括DB)]]