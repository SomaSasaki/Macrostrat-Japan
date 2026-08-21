# タスク指示書: 全国監視ダッシュボード起動アーキテクチャの恒久化とトラブルシューティング
**担当エージェント: Claude Code / Codex**
**発行者: soma (PI) / Antigravity**
**ステータス: ACTIVE (調査・対応依頼)**

---

## 1. 発生している問題事象 (Problem Statement)

ユーザーがブラウザ（Microsoft Edge）から全国進捗監視システム `http://127.0.0.1:8787/` にアクセスしようとした際、`ERR_CONNECTION_REFUSED`（接続拒否）となり、ダッシュボードが開けない状態が断続的に発生している。

### 背景と経緯
1. **チャット内起動の制約**:
   - Antigravity / Gemini のチャット内で `run.py ui` をバックグラウンド起動（`IsDaemon: true`）するとポート 8787 で正常に `HTTP 200 OK` が返るが、チャット画面上に「1 task running」という通知バーが常駐してしまう。
   - ユーザーは「チャット画面にタスク通知を出さず、目に見えない形で裏で動くか、またはデスクトップ等のショートカットから確実に開ける状態」を求めている。
2. **デスクトップランチャー（`start_dashboard.bat`）の課題**:
   - チャット側のタスクを停止（Kill）した後、デスクトップのショートカット（`start_dashboard.bat`）から起動を試みたが、ユーザー環境で自動的にサーバーが立ち上がらず接続エラーとなる。

---

## 2. 依頼事項 (Required Deliverables for Claude Code)

Claude Code は以下の 3 点について原因を究明し、恒久的な解決策を実装・検証してください：

### ① サーバープロセスの生存性・常駐メカニズムの検証
- `loop1_engine/scripts/dashboard_server.py` の `serve()` 関数および `Handler` クラスにおいて、Windows バックグラウンド起動時にストリーム例外（`sys.stdout`/`sys.stderr`）やシグナル切断でプロセスが終了していないかを精査・堅牢化する。

### ② デスクトップランチャーおよび自動起動の完全動作保証
- ユーザーがデスクトップのショートカット（またはプロジェクト直下の `start_dashboard.bat`）を実行した際、確実に Python プロセスが維持され、ブラウザで `http://127.0.0.1:8787/` が開く仕組みを確定する。
- 実行時にエラーが発生している場合は、即座に終了せず原因が画面に表示されるようデバッグ出力を整備する。

### ③ 代替案（静的 HTML エクスポート形式等の検討）
- ローカル HTTP サーバーを常時起動しなくても、静的 HTML ファイル（例: `dashboard/index.html`）をブラウザで直接開くだけで最新の全国マップが閲覧できる仕組み（事前ビルド JSON 埋め込み等）が可能かどうかも検討する。

---

## 3. 関連ファイル一覧
- `loop1_engine/scripts/dashboard_server.py` : HTTP 配信サーバー
- `loop1_engine/scripts/dashboard_data.py` : 索引データ生成スクリプト
- `run.py` : コマンドエントリポイント (`cmd_ui`)
- `start_dashboard.bat` : デスクトップ用ランチャー
- `loop2_governance/specs/MEMORY.md` : 全AI共通永続記憶
- `loop2_governance/specs/FEEDBACK.md` : 成果報告書
- `loop2_governance/UNIFIED_PORTAL.md` : 統合マスターポータル