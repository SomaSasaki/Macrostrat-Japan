# -*- coding: utf-8 -*-
"""
llm_extract.py — 英文Abstractから地層と年代の候補を取り出す

図幅PDF巻末の英文Abstractを Gemini に1回だけ投げて、地層ごとの
  unit_name / b_age_ma（古い側）/ t_age_ma（若い側）/ quote（原文引用）
を取り出し、レビューExcelの REF_age_from_abstract 列に候補として書き込む。

★ 自動確定はしない。SOMAさんが確認して t_age_ma / b_age_ma に入れると
   t_prop / b_prop が自動計算される。

★ ハルシネーション対策（数値の捏造を構造的に防ぐ）:
   1. quote を必須にする
   2. quote が Abstract 原文に逐語で存在するか照合する
   3. 数値が quote の中に逐語で存在するか照合する
   どれか1つでも外れたらその候補を捨てる。

使い方:
  python run.py llm --test          接続とキーの確認だけ
  python run.py llm towada          候補を取り出してExcelに書き込む
  python run.py llm towada --dry    書き込まずに結果を表示するだけ
"""

import argparse
import contextvars
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_secret, parse_age_ma, secret_status  # noqa: E402
from llm_router import (  # noqa: E402
    AllProvidersFailed,
    LLMRequest,
    ValidationReport,
    single_provider_router,
)
from llm_runtime import DEFAULT_DB_PATH, LLMRuntimeStore  # noqa: E402

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
MODEL = "gemini-3.6-flash"

# ---------------------------------------------------------------------------
# 課金しないための安全装置
# ---------------------------------------------------------------------------
# 本当の保証は Google 側にある:
#   Free Tier の条件 = 「請求先アカウントを紐付けていないこと」
#   紐付けていなければ、上限を超えても課金ではなく 429 エラーで止まる。
#   （公式: Free Tier の Qualification は "Active project or free trial"、
#     Tier 1 以降は "Set up and link an active billing account" が条件）
#
# ここではその上に、こちら側でも使いすぎを止める門を置く。
# 無料枠（Flash 250回/日）より低い値を既定にしてある。
ROOT = Path(__file__).resolve().parents[2]
LIMITS_PATH = ROOT / "config" / "llm_limits.json"
USAGE_PATH = ROOT / "config" / "llm_usage.json"
ROUTING_PATH = ROOT / "config" / "llm_routing.json"
RUNTIME_DB_PATH = DEFAULT_DB_PATH
GEMINI_QUOTA_GROUP = "google-ai-project"
_ACCOUNTING_MODEL = contextvars.ContextVar("legacy_gemini_model", default=MODEL)
_ACCOUNTING_ESTIMATE = contextvars.ContextVar("legacy_gemini_estimate", default=1)

DEFAULT_LIMITS = {
    "max_calls_per_day": 20,
    "max_tokens_per_day": 500_000,
    "max_tokens_per_call": 120_000,
    "allow_paid_tier": False,        # true にしない限り課金モデルは使わない
}

# 無料枠で使えるモデルだけを許可する（有料専用モデルを誤って叩かないため）
FREE_TIER_MODELS = ("flash", "flash-lite", "gemma")

# ---------------------------------------------------------------------------
# レート制限（429）への対処
# ---------------------------------------------------------------------------
# 429 の実体は「毎分あたりのトークン数（TPM）」の超過であることが多い。
# TPM のバケットは約60秒で回復するので、待って投げ直せば通る。
# 合計待機が確実に60秒を超えるように回数と初期待機を決めてある。
RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_RETRIES = 4
BASE_DELAY = 15.0
MAX_DELAY = 120.0


_QUOTA_LIMIT_RE = re.compile(r"limit:\s*(\d+)")
_FREE_TIER_REQUEST_METRIC = "generate_content_free_tier_requests"


def daily_request_quota_exhausted(detail):
    """429の本文を見て「1日あたりのリクエスト数」を使い切ったかを判定する。

    Google の 429 本文には、超過したメトリック名と limit の値が入っている:
      Quota exceeded for metric: ...generate_content_free_tier_requests,
      limit: 20, model: gemini-3.6-flash

    ``generate_content_free_tier_requests`` が明記されていれば、ローカル使用量と
    無関係に上流の判定を信頼する。ローカル記録には、別プロセス・別クライアント
    からの呼び出しや失敗したリクエストが含まれないためである。

    メトリック名が曖昧な旧形式の本文だけは、従来どおり本日の記録済み呼び出し
    回数と突き合わせる。これにより一時的な requests-per-minute の429は再試行し、
    明示的な無料枠超過だけを即座に止める。

    使い切っていれば limit の値を、そうでなければ None を返す。
    """
    detail_text = str(detail)
    if "requests" not in detail_text:
        return None
    m = _QUOTA_LIMIT_RE.search(detail_text)
    if not m:
        return None
    limit = int(m.group(1))
    if _FREE_TIER_REQUEST_METRIC in detail_text:
        return limit
    try:
        _, _, _, day = today_usage()
    except Exception:
        return None
    return limit if int(day.get("calls") or 0) >= limit else None


def estimate_prompt_tokens(text):
    """プロンプトのトークン数を見積もる。

    pilot_llm.estimate_tokens と同じ式（UTF-8バイト数 ÷ 3）を使う。
    日本語1文字はUTF-8で3バイトなので、文字数ベースで見積もると
    約3倍の過小評価になり、max_tokens_per_call の門が機能しなくなる。
    """
    return max(1, math.ceil(len(str(text).encode("utf-8")) / 3))


def load_limits():
    """Return the router's Gemini limits as the single budget policy."""

    from common import load_json
    d = dict(DEFAULT_LIMITS)
    routing = load_json(str(ROUTING_PATH)) or {}
    provider = (routing.get("providers") or {}).get("gemini") or {}
    if isinstance(provider.get("limits"), dict):
        d.update(provider["limits"])
    # Preserve the explicit paid-tier kill switch for the compatibility API;
    # numeric limits in this legacy file are no longer authoritative.
    legacy_limits = load_json(str(LIMITS_PATH)) or {}
    if "allow_paid_tier" in legacy_limits:
        d["allow_paid_tier"] = bool(legacy_limits["allow_paid_tier"])
    return d


def _usage_file():
    from common import load_json
    if USAGE_PATH.exists():
        return str(USAGE_PATH), (load_json(str(USAGE_PATH)) or {})
    return str(USAGE_PATH), {}


def _gemini_accounting_config():
    from common import load_json
    routing = load_json(str(ROUTING_PATH)) or {}
    provider = (routing.get("providers") or {}).get("gemini") or {}
    return {
        "quota_group": str(provider.get("quota_group") or GEMINI_QUOTA_GROUP),
        "reset_timezone": str(provider.get("reset_timezone") or "UTC"),
    }


def _runtime_store():
    store = LLMRuntimeStore(RUNTIME_DB_PATH)
    legacy_path, legacy = _usage_file()
    store.import_legacy_usage(
        legacy,
        source_id=str(Path(legacy_path).resolve()),
        quota_group=_gemini_accounting_config()["quota_group"],
    )
    return store


def today_usage():
    accounting = _gemini_accounting_config()
    store = _runtime_store()
    key = store.day_bucket(time.time(), accounting["reset_timezone"])
    data = store.usage_days(quota_group=accounting["quota_group"])
    day = data.get(key) or {"calls": 0, "tokens": 0}
    return str(Path(RUNTIME_DB_PATH).resolve()), data, key, day


def record_usage(tokens):
    accounting = _gemini_accounting_config()
    used = max(1, int(tokens or _ACCOUNTING_ESTIMATE.get() or 1))
    _runtime_store().record_external_attempt(
        provider="gemini",
        model=str(_ACCOUNTING_MODEL.get() or MODEL),
        quota_group=accounting["quota_group"],
        stage="legacy_gemini_direct",
        total_tokens=used,
        reset_timezone=accounting["reset_timezone"],
    )
    return today_usage()[3]


class BudgetExceeded(RuntimeError):
    pass


class GeminiAPIError(OSError):
    """API 呼び出しが最終的に失敗したことを表す。

    OSError を継承しているのは意図的。urllib が投げる URLError / HTTPError は
    どちらも OSError の子であり、pilot.py などの呼び出し側は OSError を捕まえて
    「そのステージだけ諦めてレビュー用の空データを出す」という劣化動作をしている
    （pilot.py の except (BudgetExceeded, ColumnVisionError, OSError, ValueError)）。
    ここで RuntimeError を投げるとその網から漏れ、パイプライン全体が落ちる。
    """
    pass


class QuotaExhaustedError(GeminiAPIError):
    """上流が無料枠のリクエスト上限超過を明示した。"""

    pass


def check_budget(model, est_tokens=0, *, usage=None):
    """呼び出す前に上限を確認する。超えていれば例外を投げて止める。"""
    lim = load_limits()

    if not lim.get("allow_paid_tier", False):
        if not any(m in str(model).lower() for m in FREE_TIER_MODELS):
            raise BudgetExceeded(
                f"モデル '{model}' は無料枠の対象外の可能性があります。\n"
                f"  無料枠で使えるのは {', '.join(FREE_TIER_MODELS)} を含むモデルです。\n"
                f"  意図的に使う場合のみ config/llm_limits.json で "
                f'"allow_paid_tier": true にしてください。')

    if est_tokens and est_tokens > lim["max_tokens_per_call"]:
        raise BudgetExceeded(
            f"1回のトークン数が多すぎます（推定 {est_tokens:,} > "
            f"上限 {lim['max_tokens_per_call']:,}）。")

    _ACCOUNTING_MODEL.set(str(model or MODEL))
    _ACCOUNTING_ESTIMATE.set(max(1, int(est_tokens or 1)))
    if usage is None:
        accounting = _gemini_accounting_config()
        store = _runtime_store()
        day_key = store.day_bucket(time.time(), accounting["reset_timezone"])
        day = store.usage_totals(
            quota_group=accounting["quota_group"],
            day_bucket=day_key,
            include_reservations=True,
        )
    else:
        day = {
            "calls": int(usage.get("calls") or 0),
            "tokens": int(usage.get("tokens") or 0),
        }
    if int(lim.get("max_calls_per_day") or 0) and day["calls"] + 1 > lim["max_calls_per_day"]:
        raise BudgetExceeded(
            f"本日の呼び出し回数が上限に達しました（{day['calls']} / "
            f"{lim['max_calls_per_day']} 回）。\n"
            f"  日付が変われば自動で戻ります。上限は config/llm_limits.json で変更できます。")
    if (
        int(lim.get("max_tokens_per_day") or 0)
        and day["tokens"] + max(0, int(est_tokens or 0)) > lim["max_tokens_per_day"]
    ):
        raise BudgetExceeded(
            f"本日のトークン予約が上限を超えます（{day['tokens']:,} + "
            f"{int(est_tokens or 0):,} / {lim['max_tokens_per_day']:,}）。")
    return lim, day


def usage_summary():
    lim = load_limits()
    _, data, key, day = today_usage()
    total_calls = sum(int(v.get("calls", 0)) for v in data.values())
    total_tokens = sum(int(v.get("tokens", 0)) for v in data.values())
    return {
        "本日": f"{day['calls']} 回 / {day['tokens']:,} トークン",
        "本日の上限": f"{lim['max_calls_per_day']} 回 / {lim['max_tokens_per_day']:,} トークン",
        "記録期間の合計": f"{total_calls} 回 / {total_tokens:,} トークン",
        "概算費用": "¥0（無料枠。請求先アカウントを紐付けていない限り課金されません）",
    }

PROMPT = """You are reading the English Abstract of a Geological Survey of Japan
1:50,000 quadrangle explanatory report.

For every named geological unit (formation, member, lava, pluton, volcanics,
terrace deposits, pyroclastic flow deposits, etc.), extract the fields below
**only when the text actually states them**.

Return JSON only, no markdown fence, in this exact shape:

{"units": [
  {"unit_name": "Shitazaki Formation",
   "b_age_ma": 10.5, "t_age_ma": 8.5,
   "age_quote": "the Shitazaki: 10.5-8.5 Ma",
   "strat_name": "Shitazaki Formation, Sannohe Group",
   "strat_quote": "the Shitazaki Formation of the Sannohe Group",
   "lithology": "siltstone",
   "minor_lith": "sandstone; tuff",
   "lith_quote": "mainly composed of siltstone with sandstone and tuff",
   "environment": "sublittoral",
   "env_quote": "deposited in a sublittoral environment",
   "unit_description": "A shallow marine sequence mainly composed of fine to coarse sand, forming a marine terrace deposit.",
   "desc_quote": "This formation is a shallow marine sequence and mainly composed of fine to coarse sand.",
   "min_thickness": 200, "max_thickness": 200,
   "thickness_quote": "about 200 m thick",
   "basal_surface": "unconformable",
   "basal_quote": "unconformably overlies the Toya Formation"}
]}

Rules — follow exactly:
- **Every value must have its matching *_quote, copied verbatim (character for
  character) from the Abstract.** If you cannot quote it, set the value to null.
- Never infer, estimate, average, or calculate anything not written in the text.
- b_age_ma is the OLDER (bottom) age, t_age_ma the YOUNGER (top). Convert ka to
  Ma (15 ka -> 0.015).
  * A single instantaneous age (an eruption: "The age is ca. 15 ka") goes in
    BOTH fields.
  * A range with only ONE numeric end goes in that end only, and the other
    stays null. "Early Pleistocene to ca. 0.40 Ma" means the unit ENDS at
    0.40 Ma, so t_age_ma = 0.4 and b_age_ma = null — do NOT copy 0.4 into
    b_age_ma, that would claim the unit has no duration.
- lithology = the main rock type(s). minor_lith = subordinate ones.
  Separate multiple values with a semicolon ';' (never a comma).
- Thickness in metres, as a number. If a range is given use min and max.
- Keep each quote under 120 characters.
- Use unit names exactly as spelled in the Abstract.
- unit_description: a self-contained English description of the unit.
  * It MUST begin with the unit name, e.g. "The Noheji Formation is ..."
    Never start with "They", "It", "This formation", or "The age is" — the
    description is read on its own, away from the Abstract, so a pronoun
    leaves the reader unable to tell which unit it describes.
  * Say what the rock IS: lithology, depositional setting, distribution,
    and relationships to neighbouring units. An age alone is NOT a description
    (the age already has its own field).
  * Build it ONLY from sentences the Abstract gives about that unit. You may
    join and lightly condense them, but add no fact that is not in them.
  * desc_quote must be the longest single sentence you used, verbatim.
  Good:  "The Noheji Formation is distributed in the northeastern part of the
          district; a shallow marine sequence mainly composed of fine to
          coarse sand, forming a marine terrace deposit."
  Bad:   "The age is ca. 15 ka."   /   "They are mainly composed of gravel."
- Omit any field you cannot support with a quote. Listing a unit with only
  unit_name is fine.
{vocab}
Abstract:
---
{abstract}
---"""

VOCAB_BLOCK = """
Use the official Macrostrat vocabulary below wherever it fits. If none of the
terms describes the unit, use the report's own wording (free text is allowed by
the format spec) — do not force a wrong term.

environment ({n_env} official terms):
{environment}

lithology ({n_lith} official terms):
{lithology}

lithology attributes (combine as "<attribute> <lithology>", e.g. "siliceous mudstone"):
{lith_att}

basal_surface: conformable, disconformable, unconformable, fault, gradational,
sharp, erosional, intrusive
lateral_relationship: interfingering, transgressive, onlaps, erosional, gradational
"""


def vocab_hint():
    """config/vocab.json（Macrostrat公式の語彙表）をプロンプトに載せる。"""
    from common import load_vocab
    v = load_vocab()
    if not v or not v.get("environment"):
        return ""
    return VOCAB_BLOCK.format(
        n_env=len(v.get("environment", [])),
        n_lith=len(v.get("lithology", [])),
        environment=", ".join(v.get("environment", [])),
        lithology=", ".join(v.get("lithology", [])),
        lith_att=", ".join(v.get("lith_att", [])),
    )


# 抽出するフィールドと、その裏づけになる引用のキー
FIELD_QUOTES = {
    "b_age_ma": "age_quote",
    "t_age_ma": "age_quote",
    "strat_name": "strat_quote",
    "lithology": "lith_quote",
    "minor_lith": "lith_quote",
    "environment": "env_quote",
    "min_thickness": "thickness_quote",
    "max_thickness": "thickness_quote",
    "basal_surface": "basal_quote",
    "unit_description": "desc_quote",
}
NUMERIC_FIELDS = ("b_age_ma", "t_age_ma", "min_thickness", "max_thickness")


# ---------------------------------------------------------------------------

def request_json(build_request, *, timeout=180, quiet=False,
                 max_retries=MAX_RETRIES, est_tokens=0, model=None,
                 label="Gemini API"):
    """リクエストを投げてJSONを返す。429 と 5xx は待って投げ直す。

    build_request は毎回あたらしい urllib.request.Request を返す呼び出し可能。
    使い回しではなく作り直すのは、urlopen が Request にヘッダを足すため。

    テキスト用の call_gemini と、画像を送る Vision 系の両方がここを通る。
    リトライの規則を1か所にまとめておかないと、片方だけ落ちる状態に戻る。

    再試行の通知は quiet でも stderr に出す。黙って何十秒も止まると
    ハングとの区別がつかないため。
    """
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(build_request(), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))

        except urllib.error.HTTPError as e:
            # 本文は一度しか読めない。429 の本文には超過した quota 名が
            # 入っていて原因特定に要るので、ここで必ず読んでおく。
            try:
                detail = e.read().decode("utf-8", "replace")[:1000]
            except Exception:
                detail = "(本文を読めませんでした)"

            # 日単位の枠切れは待っても回復しない。再試行は時間の浪費なので、
            # 何が起きたかを明示してすぐ諦める。
            if e.code == 429:
                spent = daily_request_quota_exhausted(detail)
                if spent is not None:
                    try:
                        _, _, _, day = today_usage()
                        local_calls = day.get("calls")
                    except Exception:
                        local_calls = "不明"
                    raise QuotaExhaustedError(
                        f"Gemini無料枠のリクエスト上限（limit: {spent}・モデル {model}）に"
                        f"達しました。ローカル記録は {local_calls} 回です。\n"
                        f"  サーバーが無料枠超過を明示したため、この実行では再試行しません。\n"
                        f"  上限リセット後に再実行するか、利用可能な別モデル/枠を使用してください。\n"
                        f"  {detail}") from e

            if e.code not in RETRY_STATUS or attempt >= max_retries:
                raise GeminiAPIError(
                    f"{label} が HTTP {e.code} を返しました"
                    f"（{attempt + 1}回試行・推定 {est_tokens:,} トークン）。\n"
                    f"  {detail}") from e

            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = BASE_DELAY * (2 ** attempt)
            delay = min(delay, MAX_DELAY)

            print(f"  HTTP {e.code}。{delay:.0f}秒待って再試行します"
                  f"（{attempt + 1}/{max_retries}）。\n"
                  f"  理由: {detail[:300]}", file=sys.stderr)
            time.sleep(delay)

        except (urllib.error.URLError, TimeoutError) as e:
            # 接続エラーは中身を見る。タイムアウトや切断は待てば直るが、
            # 名前解決の失敗・プロキシ拒否・接続拒否は待っても直らない。
            # 後者で待つと、落ちるべき場面で何分も止まってしまう。
            reason = getattr(e, "reason", e)
            transient = isinstance(
                reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError))

            if not transient or attempt >= max_retries:
                raise GeminiAPIError(
                    f"{label} へ接続できませんでした"
                    f"（{attempt + 1}回試行）: {reason}") from e

            delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
            print(f"  接続が切れました（{reason}）。{delay:.0f}秒待って再試行します"
                  f"（{attempt + 1}/{max_retries}）。", file=sys.stderr)
            time.sleep(delay)

    raise GeminiAPIError(f"{label} から応答を得られませんでした。")


def call_gemini(prompt, api_key, model=MODEL, timeout=180, quiet=False,
                max_retries=MAX_RETRIES):
    """
    Deprecated compatibility transport for the legacy retry tests.

    Production stages and this module's CLI use ``LLMRouter``.  Keep new
    inference call sites out of this helper so retry, circuit, and accounting
    policy remain centralized.

    呼び出す前に必ず上限を確認する（check_budget）。ここを通さない経路は作らない。
    リトライの規則は request_json に集約してある。
    """
    est = estimate_prompt_tokens(prompt)
    check_budget(model, est)

    body = json.dumps({"model": model, "input": prompt}).encode("utf-8")

    def build():
        return urllib.request.Request(
            API_URL, data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key})

    data = request_json(build, timeout=timeout, quiet=quiet,
                        max_retries=max_retries, est_tokens=est, model=model)

    used = (data.get("usage") or {}).get("total_tokens") or 0
    day = record_usage(used)
    if not quiet:
        lim = load_limits()
        print(f"  使用: {used:,} トークン / 本日 {day['calls']}回・"
              f"{day['tokens']:,}トークン（上限 {lim['max_calls_per_day']}回）・費用 ¥0")
    return data, extract_text(data)


def _walk_text(node, out, skip_keys=("thought", "thinking", "reasoning")):
    """JSONを再帰的に歩いて "text" に入っている文字列を全部集める。

    APIのレスポンス形式が変わっても本文を取りこぼさないための保険。
    思考過程（thought / thinking）は本文ではないので除く。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if any(s in kl for s in skip_keys):
                continue
            if kl == "text" and isinstance(v, str) and v.strip():
                out.append(v)
            else:
                _walk_text(v, out, skip_keys)
    elif isinstance(node, list):
        for v in node:
            _walk_text(v, out, skip_keys)


def extract_text(data):
    """レスポンスの形が変わっても本文を拾えるように、既知の経路 → 全探索 の順で試す。"""
    # 1. Interactions API の素直な形
    for key in ("output_text", "outputText", "text"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v

    # 2. steps -> model_output -> content[] -> text
    chunks = []
    for step in data.get("steps", []) or []:
        mo = step.get("model_output") or step.get("modelOutput") or {}
        for c in mo.get("content", []) or []:
            t = c.get("text")
            if isinstance(t, dict):
                t = t.get("text")
            if isinstance(t, str) and t.strip():
                chunks.append(t)
    if chunks:
        return "\n".join(chunks)

    # 3. 旧 generateContent 形式
    for cand in data.get("candidates", []) or []:
        for p in (cand.get("content") or {}).get("parts", []) or []:
            if isinstance(p.get("text"), str) and p["text"].strip():
                chunks.append(p["text"])
    if chunks:
        return "\n".join(chunks)

    # 4. 最後の保険: 全体を歩いて "text" を集める
    _walk_text(data, chunks)
    return "\n".join(chunks)


def describe(data, limit=1800):
    """本文が取れなかったときに、レスポンスの形を人が読める形で見せる。"""
    def shape(node, depth=0):
        pad = "  " * depth
        if isinstance(node, dict):
            lines = []
            for k, v in list(node.items())[:12]:
                if isinstance(v, (dict, list)):
                    lines.append(f"{pad}{k}:")
                    lines.append(shape(v, depth + 1))
                else:
                    s = str(v)
                    lines.append(f"{pad}{k}: {s[:90]}{'…' if len(s) > 90 else ''}")
            return "\n".join(lines)
        if isinstance(node, list):
            if not node:
                return f"{pad}[]（空）"
            return f"{pad}[{len(node)}件]\n" + shape(node[0], depth + 1)
        s = str(node)
        return f"{pad}{s[:90]}"
    try:
        return shape(data)[:limit]
    except Exception:
        return json.dumps(data, ensure_ascii=False)[:limit]


def parse_json_block(text):
    """```json ... ``` に包まれていても取り出す。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------

def _norm(s):
    """照合用に空白とダッシュのゆれを吸収する。"""
    s = str(s or "")
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    s = s.replace("’", "'").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


_NUMBER_IN_TEXT = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")

# 「現在まで」を意味する語。年代 0 Ma はこれで裏づけられる。
_PRESENT_WORDS = ("present", "today", "recent", "now")


def numbers_in(text):
    """文字列に出てくる数値を全部 float で拾う。'0.40' も '10,400' も拾える。"""
    out = []
    for m in _NUMBER_IN_TEXT.finditer(str(text or "")):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return out


def number_supported(value, quote):
    """
    値が引用に裏づけられているかを「数値として」照合する。

    文字列一致だと '0.40 Ma' と 0.4 が別物になってしまう（実際に取りこぼした）。
    引用中の数値を全部拾って、Ma そのまま / ka 換算 のどちらかで一致すればよしとする。
    0 Ma は 'present' 等の語があれば認める。
    """
    if value is None:
        return True
    nums = numbers_in(quote)
    if value == 0:
        return any(w in _norm(quote) for w in _PRESENT_WORDS) or 0 in nums
    tol = max(abs(value) * 1e-6, 1e-9)
    for n in nums:
        # 原文の単位が Ma / ka / 年BP のどれでも拾えるようにする
        for scale in (1.0, 1e3, 1e6):
            if abs(n / scale - value) <= tol:
                return True
    return False


def verify(units, abstract):
    """
    フィールドごとに「引用が原文にあるか」「数値が引用の中にあるか」を確かめる。

    ★ 落とすのはフィールド単位。1つ怪しくても他の確かな情報は捨てない。
      年代が駄目でも lithology が確かならそれは残す。

    (採用したもの, 落としたもの) を返す。
    """
    hay = _norm(abstract)
    ok, dropped = [], []

    for u in units or []:
        name = str(u.get("unit_name") or "").strip()
        if not name:
            continue

        # 後方互換: 旧形式の "quote" は age_quote として扱う
        if u.get("quote") and not u.get("age_quote"):
            u["age_quote"] = u["quote"]

        clean = {"unit_name": name}
        quotes = {}

        for fld, qkey in FIELD_QUOTES.items():
            v = u.get(fld)
            if v is None or v == "":
                continue
            q = str(u.get(qkey) or "").strip()
            if not q:
                dropped.append((name, f"{fld}: 引用が無い"))
                continue
            if _norm(q) not in hay:
                dropped.append((name, f"{fld}: 引用が原文に無い ({q[:40]!r})"))
                continue

            if fld in NUMERIC_FIELDS:
                num = parse_age_ma(v) if fld.endswith("_ma") else _as_float(v)
                if num is None:
                    dropped.append((name, f"{fld}: 数値でない ({v!r})"))
                    continue
                if not number_supported(num, q):
                    dropped.append((name, f"{fld}={num:g} が引用に見当たらない"))
                    continue
                clean[fld] = num
            else:
                clean[fld] = " ".join(str(v).split())
            quotes[fld] = q

        # 年代の上下が逆なら入れ替える
        b, t = clean.get("b_age_ma"), clean.get("t_age_ma")
        if b is not None and t is not None and b < t:
            clean["b_age_ma"], clean["t_age_ma"] = t, b
        # 層厚も同様
        mn, mx = clean.get("min_thickness"), clean.get("max_thickness")
        if mn is not None and mx is not None and mn > mx:
            clean["min_thickness"], clean["max_thickness"] = mx, mn

        clean["_quotes"] = quotes
        clean.setdefault("b_age_ma", None)
        clean.setdefault("t_age_ma", None)
        clean["quote"] = quotes.get("b_age_ma") or quotes.get("t_age_ma") or ""
        ok.append(clean)
    return ok, dropped


def _as_float(v):
    try:
        f = float(str(v).replace(",", "").strip())
        return None if f != f else f
    except (TypeError, ValueError):
        return None


# セルに入れる文字列の上限。長すぎると Excel で折り返しきれず「値はあるのに見えない」
# 状態になる（実際に起きた）。原文は abstract シートに全文があるので要点だけでよい。
QUOTE_MAX = 70


def _short(quote):
    q = " ".join(str(quote or "").split())
    return q if len(q) <= QUOTE_MAX else q[:QUOTE_MAX - 1] + "…"


def format_candidate(u):
    """年代の候補を1行の文字列にする（REF_age_from_abstract 用）。"""
    b, t = u.get("b_age_ma"), u.get("t_age_ma")
    src = f"（出典: {_short(u.get('quote'))}）" if u.get("quote") else ""
    if b is None and t is None:
        return f"年代の記載なし{src}" if src else ""
    # ★ 片側だけ通ることがある（もう片方が引用照合に落ちた場合など）
    if b is None:
        return f"上限 {t:g} Ma（下限不明）{src}"
    if t is None:
        return f"下限 {b:g} Ma（上限不明）{src}"
    if b == t:
        return f"{b:g} Ma{src}"
    return f"{b:g}–{t:g} Ma{src}"


def format_field(u, field):
    """年代以外のフィールドを「値（出典: 引用）」の形にする。"""
    v = u.get(field)
    if v is None or v == "":
        return ""
    q = (u.get("_quotes") or {}).get(field)
    val = f"{v:g}" if isinstance(v, float) and v == int(v) else str(v)
    return f"{val}（出典: {_short(q)}）" if q else val


def format_thickness(u):
    mn, mx = u.get("min_thickness"), u.get("max_thickness")
    if mn is None and mx is None:
        return ""
    q = (u.get("_quotes") or {}).get("min_thickness") or \
        (u.get("_quotes") or {}).get("max_thickness")
    if mn == mx or mx is None:
        body = f"{mn:g} m"
    elif mn is None:
        body = f"最大 {mx:g} m"
    else:
        body = f"{mn:g}–{mx:g} m"
    return f"{body}（出典: {_short(q)}）" if q else body


# ---------------------------------------------------------------------------

def list_models(api_key, timeout=60):
    """このキーで使えるモデル名を返す。"""
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "").replace("models/", "") for m in d.get("models", [])]
    except Exception:
        return []


def test_connection(api_key, model=MODEL):
    print("接続テスト中 ...")
    prompt = 'Return JSON only, with exactly this shape: {"ok":"OK"}'
    router = single_provider_router(
        stage="towada_pdf_llm",
        provider="gemini",
        model=model,
        secret=str(api_key),
    )

    def validate_connection(response):
        ok = str(response.get("ok") or "").strip().upper() == "OK"
        return ValidationReport(
            decision="accept" if ok else "reject",
            accepted=response if ok else None,
            fatal_errors=() if ok else ("Expected {\"ok\":\"OK\"}.",),
        )

    try:
        routed = router.execute(
            LLMRequest(
                stage="towada_pdf_llm",
                logical_job_id="llm-extract-connection-test",
                prompt=prompt,
                estimated_input_tokens=estimate_prompt_tokens(prompt),
                reserved_output_tokens=32,
                required_capabilities=("text", "json"),
            ),
            validate_connection,
        )
    except AllProvidersFailed as e:
        print(f"[ERROR] 接続できません: {str(e)[:300]}")
        names = list_models(api_key)
        if names:
            print("\n  このキーで使えるモデル:")
            for name in names:
                if "flash" in name or "pro" in name:
                    print("    -", name)
        return False
    except Exception as e:
        print(f"[ERROR] 接続できません: {type(e).__name__}: {str(e)[:200]}")
        return False

    print(f"  応答: {routed.response.get('ok')}")
    print(f"  OK — キー・接続・モデル({model}) すべて問題ありません。")
    return True


def run(abstract_text, api_key, model=MODEL, verbose=True, debug=False):
    """Abstract -> 検証済み候補リスト"""
    prompt = (PROMPT.replace("{vocab}", vocab_hint())
                    .replace("{abstract}", abstract_text))
    estimated_input = estimate_prompt_tokens(prompt)
    if verbose:
        print(f"Gemini に送信中 ... （{model} / 約{estimated_input:,}トークン）")

    router = single_provider_router(
        stage="towada_pdf_llm",
        provider="gemini",
        model=model,
        secret=str(api_key),
    )

    def validate_candidate(response):
        units = response.get("units")
        if not isinstance(units, list):
            return ValidationReport(
                decision="reject",
                fatal_errors=("Response must contain a units array.",),
            )
        accepted, dropped = verify(units, abstract_text)
        if units and not accepted:
            return ValidationReport(
                decision="reject",
                dropped=dropped,
                fatal_errors=("No extracted unit passed deterministic verification.",),
            )
        return ValidationReport(
            decision="partial" if dropped else "accept",
            accepted=accepted,
            dropped=dropped,
        )

    logical_job_id = "llm-extract-" + hashlib.sha256(
        abstract_text.encode("utf-8")
    ).hexdigest()[:20]
    try:
        routed = router.execute(
            LLMRequest(
                stage="towada_pdf_llm",
                logical_job_id=logical_job_id,
                prompt=prompt,
                estimated_input_tokens=estimated_input,
                reserved_output_tokens=32768,
                required_capabilities=("text", "json", "long_context"),
            ),
            validate_candidate,
        )
    except AllProvidersFailed as e:
        print(f"[ERROR] LLM抽出に失敗しました: {str(e)[:400]}")
        return [], []

    if debug:
        print("--- レスポンスの構造 ---")
        print(describe(routed.response))
        print("--- ルーター試行 ---")
        print(describe(list(routed.attempts)))
        print("-" * 60)

    ok = list(routed.validation.accepted or [])
    dropped = list(routed.validation.dropped or [])
    if verbose:
        print(f"  取得 {len(ok) + len(dropped)} 件 → 検証通過 {len(ok)} 件 / 却下 {len(dropped)} 件")
        for name, why in dropped[:8]:
            print(f"    [却下] {name}: {why}")
    return ok, dropped


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="英文Abstractから地層と年代の候補を抽出")
    ap.add_argument("abstract_file", nargs="?", help="abstract のテキストファイル")
    ap.add_argument("--test", action="store_true", help="キーと接続の確認だけ")
    ap.add_argument("--model", default=MODEL)
    a = ap.parse_args()

    key = load_secret("gemini_api_key", "GEMINI_API_KEY")
    if not key:
        print("[ERROR] APIキーが見つかりません。")
        for k, v in secret_status().items():
            print(f"  {k}: {v}")
        print("\n  config/secret.json に {\"gemini_api_key\": \"...\"} を置いてください。")
        sys.exit(1)

    if a.test:
        sys.exit(0 if test_connection(key) else 1)

    if not a.abstract_file:
        print("abstract のファイルを指定するか、--test を付けてください。")
        sys.exit(1)
    with open(a.abstract_file, encoding="utf-8") as f:
        txt = f.read()
    ok, _ = run(txt, key, model=a.model)
    print()
    for u in ok:
        print(f"  {u['unit_name']:<40}{format_candidate(u)}")
