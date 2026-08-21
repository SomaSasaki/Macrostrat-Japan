# 第1ループを毎回自律実行させるための環境設計

本書は、ユーザー（soma）の 2 つの問いに対する技術的回答である。

1. LLM API が毎回使えないと困る。どういう設定にすれば使えるようになるか。
2. pytest を人手で回さずに済むよう、設計をどう変えればよいか。

対象は「Claude がクラウドサンドボックスから、ユーザー PC 上の MacroStrat リポジトリに対して
自律的に実装・検証・報告を行う」構成である。

---

## 1. 現在の実行環境の測定結果

推測ではなく、2026-08-19 のセッションで実測した値を記す。

### 1.1 3 つのファイルシステム

| 場所 | 実体 | できること | できないこと |
| :--- | :--- | :--- | :--- |
| クラウドサンドボックス | Anthropic が用意する Linux コンテナ | ネットワーク接続、`pip install`、`pytest`、Chromium での画面確認 | ユーザー PC のファイルを直接見ることはできない |
| デバイスブリッジ VM | ユーザー PC 上の隔離 Linux VM。接続フォルダが読み書きでマウントされる | ユーザーのファイルの読み書き、`python3`（pandas / openpyxl / numpy / PIL / pdfplumber / requests / lxml あり） | **ネットワーク接続なし**。`pytest` 未導入で追加もできない。ファイル削除も不可 |
| ユーザー PC (Windows) | `C:\Users\somas\projects\MacroStrat` | `.venv\Scripts\python` による本番実行 | Claude から直接コマンドを実行することはできない |

### 1.2 クラウドサンドボックスからの外部到達性

| 到達先 | 用途 | 結果 |
| :--- | :--- | :--- |
| `generativelanguage.googleapis.com` | Gemini | 到達可 |
| `openrouter.ai` | OpenRouter 経由の各種モデル | 到達可 |
| `api.anthropic.com` | Claude API | 到達可 |
| `pypi.org` | Python パッケージ | 到達可 |
| `api.github.com` | Git ホスティング | 到達可 |
| `gbank.gsj.jp` | GSJ 出版物 API | **遮断** |
| `www.gsj.jp` | GSJ PDF 配布 | **遮断** |
| `bedrock-runtime.*.amazonaws.com` | AWS Bedrock | **遮断** |

結論は次の 2 点である。

- **LLM 側の経路は既に通っている。** 足りないのは鍵だけである。
- **GSJ 本体は遮断されているが実害はない。** `data/50k/raw/publication/g050/` に 763 件、
  `data/50k/00_management/gsj_50k_full_census.json` に全図幅の PDF 実査結果が既にキャッシュされている。
  今回のグリッド導出もダッシュボード索引も、外部アクセスゼロで完結した。

---

## 2. 問い 1 への回答: LLM 段を毎回動かせるようにする

### 2.1 最小構成（今日から可能・設定変更なし）

`config/secret.json` はユーザー PC 上にある。Claude はセッション開始時にこれをサンドボックスへ取り込める。
取り込んだ鍵はそのセッション専用の隔離コンテナ内にのみ存在し、セッション終了時に破棄される。

指示の出し方（`specs/TASK.md` に 1 行書いておけばよい）:

```
LLM 段を実行する場合は config/secret.json をサンドボックスへ取り込んでから実行してよい。
利用可能な経路は Gemini / OpenRouter / Anthropic のみ。Bedrock は遮断されている。
```

**推奨する安全策**

1. `config/secret.json` には**無料枠または低上限のキーだけ**を置く。本番課金キーは別ファイルに分ける。
2. `config/llm_routing.json` の優先順位から Bedrock 系エンドポイントを外す。遮断されているため、
   優先順位に残っているとリトライで時間を浪費する。
3. `config/llm_limits.json` の上限を、1 セッションで使い切ってよい額に設定しておく。
   自律実行では人が止められないため、上限はコード側で持たせるのが確実である。

### 2.2 恒久構成（推奨）

キーをファイルではなく**環境変数**で受け渡す形にすると、取り違えと混入の危険が減る。

1. `config/secrets.example.json` の各キーに対応する環境変数名を決める（例 `GEMINI_API_KEY`）。
2. `scripts/llm_runtime.py` の読み込み順を「環境変数 → `config/secret.json`」にする。
3. 自律セッションでは環境変数だけを設定する。リポジトリに鍵が残らない。

---

## 3. 問い 2 への回答: pytest を人手で回さずに済ませる

### 3.1 今回採った方式（設定変更ゼロ・実績あり）

この方式で本タスクの 73 件を通した。追加の準備は何も要らない。

```
[ユーザー PC] ソース + 必要データを 1 個の tar.gz に固める
        |  device_stage_files
        v
[クラウド] 展開 -> pip install pytest openpyxl pandas httpx -> pytest tests/
        |
        v  検証済みの差分だけを SendUserFile + device_commit_files で書き戻す
[ユーザー PC]
```

固める対象（データ本体を含めないので 1.2 MB 程度に収まる）:

```
run.py pyproject.toml CLAUDE.md README.md
scripts src tests config specs docs
data/50k/gsj_50k_catalog.json
data/50k/raw/publication
data/50k/00_management/gsj_50k_inventory.json
data/50k/00_management/gsj_50k_full_census.json
```

`knowledge/` は 137 MB あるため除外する（`knowledge/*.md` だけ個別に含める）。

**この方式の限界**: 02_review の実 Excel を含めないため、ワークスペース依存のテストは
対象図幅の簿を個別に取り込む必要がある。今回は m1286 / m1050 の 2 件を取り込んで確認した。

### 3.2 恒久策（推奨）: リポジトリを Git 管理下に置く

現在このリポジトリは **Git 管理下ですらない**（`git rev-parse --is-inside-work-tree` が失敗する）。
`.github/` と `.gitignore` はあるので、初期化の意図はあったと見える。

Git 化すると次が全て解決する。

| 現状の問題 | Git 化後 |
| :--- | :--- |
| 複製のたびに tar を作る | `git clone` 一発 |
| どのファイルを変更したか目視で追う | `git diff` で機械的に確定 |
| 変更を戻したいときに手作業 | `git revert` / `git checkout` |
| 同じ図幅に複数エージェントが触れて衝突 | ブランチで分離 |
| バックアップが `system/backup-<日時>/` と `*.candidate-<日時>.xlsx` に無限増殖（m1286 の 1 図幅だけで 25 世代・200 MB、`data/` 全体で 8.5 GB） | コミット履歴に一本化でき、世代バックアップ機構自体を縮小できる |

**手順**

```
cd C:\Users\somas\projects\MacroStrat
git init
git add .gitignore
git commit -m "chore: initialize repository"
git add .
git commit -m "chore: import existing working tree"
# GitHub 上に private リポジトリを作り
git remote add origin <URL>
git push -u origin main
```

`.gitignore` には最低限これらを入れる（現在の 529 バイトの内容に追記する）。

```
.venv/
__pycache__/
.pytest_cache/
config/secret.json
data/50k/cache/
data/50k/raw/
data/**/system/backup-*/
data/**/llm_cache/
dashboard/data/
outputs/_agent_tmp/
```

`dashboard/data/` は `python run.py ui` が毎回生成するため、追跡不要である。

### 3.3 CI（任意・Git 化の後）

`.github/workflows/tests.yml` を置けば、push のたびに GitHub 側で `pytest` が回る。
第 1 ループの成果が第 2 ループへ渡る前に自動で検証される。

```yaml
name: tests
on: [push, pull_request]
jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install pytest openpyxl pandas httpx
      - run: python -m pytest tests/ -q
```

`data/50k/raw/publication/` を追跡対象に含めるかどうかで、グリッド導出のテストが動くか決まる。
6.7 MB なので追跡して問題ない。

---

## 4. 自律ループの推奨運用

```
[soma] 発案
   |
   v
[Antigravity] specs/TASK.md を書く
   |          （書く前に python run.py audit --all の出力を読み、実データと突き合わせる）
   v
[Claude]  1. specs/MEMORY.md と specs/CONTEXT_HANDOFF.json を読む
          2. リポジトリをサンドボックスへ複製（git clone または tar）
          3. 実装
          4. python -m pytest tests/ を全件通す
          5. python run.py audit --all で不変条件を確認
          6. python run.py grid --check で幾何の健全性を確認
          7. 差分をユーザー PC へ書き戻す
          8. specs/FEEDBACK.md / MEMORY.md / CONTEXT_HANDOFF.json を更新
   |
   v
[Antigravity] FEEDBACK.md を読んで soma へ報告
```

### 4.1 Claude が毎回必ず実行する検証コマンド

```
python -m pytest tests/ -q          全件 PASS が必須
python run.py grid --check          セル重複 0 / 順序違反 1% 未満
python run.py audit --all           error 0 件
python run.py dashboard-data        索引が生成できること
```

### 4.2 指示書に書いておくと手戻りが減る項目

1. 参照するファイルの**実在するパス**（`config/vocab.json` であって `official_vocab.json` ではない）
2. 期待する結果ではなく**現状の実測値**（「残り 2 層」ではなく「audit の出力を見よ」）
3. 外部ネットワークを使ってよいか（GSJ は遮断されている前提で書く）
4. 生成物を書き戻す先（`dashboard/data/` のような生成物は書き戻し不要）
