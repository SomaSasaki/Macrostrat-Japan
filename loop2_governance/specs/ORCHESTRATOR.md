# Multi-AI Orchestration Protocol: Antigravity, Codex, & Claude Code

本ドキュメントは、Macrostrat Japan プロジェクトにおいて Antigravity, Codex, Claude Code が協調して自律開発およびデータ処理を遂行するための運用プロトコルを定義する。

---

## 1. Division of Labor & Roles

```mermaid
flowchart TD
    Researcher["研究者 (soma / PI)"]
    
    subgraph GovernanceLayer ["第2ループ: ガバナンス・報告レイヤー"]
        AGY["Antigravity (Gemini 3.7 Flash)\n- 研究者との対話・意図の要約\n- タスク仕様書 (specs/TASK.md) の策定\n- 成果物の学術的要約報告"]
    end
    
    subgraph ExecutionLayer ["第1ループ: 自律実行・計算レイヤー"]
        Codex["Codex\n- パイプライン実装\n- 高速バッチ処理\n- テストスイート保守"]
        Claude["Claude Code (Opus/Sonnet)\n- 長文PDF深層推論\n- 柱状図Vision解析\n- 不変条件の自律修復"]
    end

    Researcher <--> AGY
    AGY -->|"タスク仕様書 (specs/TASK.md)"| Codex
    AGY -->|"タスク仕様書 (specs/TASK.md)"| Claude
    Codex -->|"検証結果 & specs/FEEDBACK.md"| AGY
    Claude -->|"抽出結果 & specs/FEEDBACK.md"| AGY
```

---

## 2. Context & Memory Synchronization Protocol
各エージェントは以下のプロトコルに従い、コンテキストの同期とトークン最適化を行う。

1. **起動時の必須参照**:
   - `specs/MEMORY.md`（永続記憶）
   - `specs/CONTEXT_HANDOFF.json`（機械可読状態）
2. **タスク完了時の記録義務**:
   - 変更内容および課題を `specs/FEEDBACK.md` に明記すること。
3. **コンテキストルーティング**:
   - LLM推論時はPDF全体ではなく `pdf_context_router.py` により切り出された該当章節テキストのみを入力とすること。