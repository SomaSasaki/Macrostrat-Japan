# -*- coding: utf-8 -*-
"""Command-line entry point for the Macrostrat GSJ data pipeline.

Running without arguments prints the command list.  Implementations live in
``scripts/``; this file owns only command routing and map-sheet resolution.
"""

import glob
import json
import os
import re
import subprocess
import sys
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "loop1_engine", "scripts") if os.path.exists(os.path.join(HERE, "loop1_engine", "scripts")) else os.path.join(HERE, "scripts")
sys.path.insert(0, SCRIPTS)          # scripts/ を import できるようにする（1回だけ）

UA = {"User-Agent": "Mozilla/5.0"}
MAP_INDEX_CACHE = os.path.join(HERE, "loop2_governance", "config", "map_index.json") if os.path.exists(os.path.join(HERE, "loop2_governance", "config", "map_index.json")) else os.path.join("config", "map_index.json")
PUBLICATION_CACHE = os.path.join(HERE, "loop2_governance", "data", "50k", "raw", "publication", "g050") if os.path.exists(os.path.join(HERE, "loop2_governance", "data")) else os.path.join(HERE, "data", "50k", "raw", "publication", "g050")

HELP = """
Nationwide GSJ 50k workflow (ZFK -> Shapefile -> PDF/LLM -> map/KML -> Review Excel):
   python run.py towada
   python run.py 1050
   python run.py check towada
   python run.py export towada
   --force  : replace the existing Review Excel in place
              (without it the run still completes; the result is written to
               m<id>_review.candidate-<stamp>.xlsx and your workbook is kept)

Legacy Review-v2 compatibility commands:
   python run.py review towada

===================================================
 MacroStrat GSJ Data Processing Pipeline CLI
===================================================

1. レビュー用Excelの作成（ZFK・PDF・凡例画像・英文Abstract を一括取得）
   python run.py make towada
   python run.py make ichinohe --columns 3 --compiler "Soma Sasaki"
      --columns 3  : west / central / east の3Columnを最初から用意
      --force      : 既存のレビューファイルを直接置き換える
                     （付けなくても実行は完了する。その場合は
                      m<id>_review.candidate-<日時>.xlsx に書き出し、
                      人が編集した本体はそのまま残す）
                     --force を付けたときは上書き前に .before-<日時> を退避し、
                     GOLDが束縛する派生JSONも system/backup-<日時>/ に退避する
   python run.py make 1050 ichinohe          複数まとめて

2. 英文Abstractから年代候補を取り出す（Gemini・無料枠）
   python run.py llm --usage        使用量と上限（課金されていないことの確認）
   python run.py llm --test         キー・接続・モデルの確認
   python run.py llm --models       このキーで使えるモデル一覧
   python run.py llm towada         候補を抽出してExcelに自動入力
      --dry    書き込まずに結果だけ表示
      --keep   REF_ 列だけ更新し、編集列の既存値には触らない
      --debug  応答の中身を表示（不具合調査用）
      --model gemini-2.5-flash      モデルを変える

3. 入力内容の事前チェック（ファイルは書き出さない）
   python run.py check towada

4. 提出用ファイルへの変換（Macrostrat公式形式 v0.1.1）
   python run.py export towada

5. 20万分の1 全国地質凡例インベントリ・スケルトン（地名・英語名・コード対応）
   python run.py make-200k kyoto        京都・大阪図幅（地名で指定可能）
   python run.py make-200k tokyo        東京図幅
   python run.py make-200k 札幌          日本語名でも指定可能
   python run.py make-200k all          全国112図幅を一括作成
   python run.py check-200k kyoto       厳格整合性チェック（単調性・語彙・prop）
   python run.py export-200k kyoto      提出用Excel出力（v0.1.1）

6. 進捗の確認
   python run.py list
   python run.py ui                 全国50k進捗ダッシュボード（ブラウザ・ズーム式）
      --port 8899   ポート指定（既定 8787。埋まっていれば自動で次を探す）
      --no-browser  ブラウザを開かない
      --no-build    索引を作り直さない
   python run.py grid               図幅グリッド config/gsj_50k_grid.json を導出
      --check       ファイルを書かず検証だけ
   python run.py dashboard-data     ダッシュボード索引JSONだけ作り直す
   python run.py audit ichinohe     5大不変条件の監査と未解決ユニットの棚卸し
      --all         02_review 配下すべて
      --json        system/audit/ に監査結果を保存

7. ZFKデータのある図幅を探す
   python run.py index              最初に1回だけ実行（索引を作成／数分）
   python run.py search             地域別の概況
   python run.py search aomori      青森区画を一覧（青森 / 05 でも可）
   python run.py search 十和田       図幅名・図幅コード・著者名でも検索可
      --coords 中心座標も表示

8. 手直し用
   python run.py repair             Excelの表示崩れを修復（値は変更しない）
   python run.py repair --migrate   古いレビューファイルを現行の列構成に引き上げる
                                    （入力済みの値は保持。--dry で確認だけ）
   python run.py abstract towada    Abstractの取り直し（makeに含まれるので通常不要）
   python run.py vocab --show       Macrostrat公式語彙の件数を表示
   python run.py vocab              公式APIから語彙表を取り直す（年1回程度でよい）

===================================================
"""


# ---------------------------------------------------------------------------
# 引数の扱い
# ---------------------------------------------------------------------------

def split_args(args, valued=()):
    """
    引数を (名前のリスト, フラグの集合, オプションのdict) に分ける。

    valued に挙げたフラグは次の引数を値として取る。
      split_args(["towada","--model","x","--dry"], valued=("--model",))
      -> (["towada"], {"--dry"}, {"--model": "x"})
    """
    names, flags, opts = [], set(), {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if a in valued and i + 1 < len(args):
                opts[a] = args[i + 1]
                i += 1
            else:
                flags.add(a)
        else:
            names.append(a)
        i += 1
    return names, flags, opts


def need_names(names, example):
    if not names:
        print(f"エラー: 図幅名かIDを指定してください。例: python run.py {example}")
        return False
    return True


# ---------------------------------------------------------------------------
# 図幅IDの解決
# ---------------------------------------------------------------------------

def _alias_variants(value):
    """Return exact-name aliases from the catalog's several title formats."""
    text = str(value or "").strip()
    if not text:
        return []
    variants = [text]
    without_year = re.sub(r"\s*[\(\uff08]\s*(?:1[89]\d{2}|20\d{2})\s*[\)\uff09]\s*$", "", text).strip()
    if without_year and without_year != text:
        variants.append(without_year)

    # GSJ index labels occur both as ``1:50K GeoMap: Towada`` and
    # ``1:50K GeoMap 'Towada' (2005)``.  Japanese publication titles use
    # corner brackets, e.g. ``5万分の1地質図幅「十和田」 (2005)``.
    for candidate in tuple(variants):
        match = re.search(
            r"GeoMap\s*(?::\s*|\s+)[\"']?(.+?)[\"']?$",
            candidate,
            flags=re.I,
        )
        if match:
            name = match.group(1).strip(" \"'")
            if name:
                variants.append(name)
        match = re.search(r"地質図幅[「『](.+?)[」』]", candidate)
        if match:
            variants.append(match.group(1).strip())

    out, seen = [], set()
    for variant in variants:
        key = variant.casefold()
        if variant and key not in seen:
            seen.add(key)
            out.append(variant)
    return out


def _normalize_entries(raw):
    """
    図幅リストのスキーマ差を吸収して
    [{'id','label','search','aliases'}] に揃える。

    対応するスキーマ:
      {'map_id': '1050', 'name_en': 'Towada'}          ← config/map_index.json（既存）
      {'id': '1050', 'label': '1:50K GeoMap: Towada'}  ← 本スクリプトが書くキャッシュ
      {'@id': '.../map1050', 'label': '...'}           ← GSJ API の生レスポンス

    ここを決め打ちすると地名での図幅指定が全て無言で失敗する（実際に起きた）。
    """
    out = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("map_id") or ""
        if not mid and m.get("@id"):
            mid = str(m["@id"]).split("map")[-1]
        mid = str(mid).strip()
        if not mid.isdigit():
            continue
        label = m.get("label") or m.get("name_en") or m.get("title") or ""
        values = [
            m.get(key)
            for key in (
                "label", "name_en", "title", "title_en", "title_e",
                "name_ja", "title_ja", "title_j",
            )
            if m.get(key)
        ]
        aliases = []
        seen = set()
        for value in values:
            for alias in _alias_variants(value):
                key = alias.casefold()
                if key not in seen:
                    seen.add(key)
                    aliases.append(alias)
        display = str(label).strip() or (aliases[0] if aliases else f"Map {mid}")
        out.append({
            "id": mid,
            "label": display,
            "search": " ".join(aliases),
            "aliases": aliases,
        })
    return out


def _enrich_entries_with_publication(entries, publication_cache=None):
    """Add offline GSJ publication titles as aliases to the compact map index.

    ``config/map_index.json`` intentionally stays small and currently contains
    English names only.  The per-map publication cache contains authoritative
    ``title_j``/``title_e`` values for all published maps, so merge those names
    in memory without rewriting either catalog or requiring network access.
    Missing or malformed cache files are ignored map by map.
    """
    publication_cache = publication_cache or PUBLICATION_CACHE
    enriched = []
    for entry in entries or []:
        item = dict(entry)
        aliases = list(item.get("aliases") or _alias_variants(item.get("label")))
        seen = {str(alias).casefold() for alias in aliases if alias}
        path = os.path.join(publication_cache, f"m{item.get('id')}.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                publication = json.load(handle)
        except (OSError, json.JSONDecodeError, TypeError):
            publication = {}
        if isinstance(publication, dict):
            for key in ("title_j", "title_ja", "title_e", "title_en", "label"):
                for alias in _alias_variants(publication.get(key)):
                    folded = alias.casefold()
                    if folded not in seen:
                        seen.add(folded)
                        aliases.append(alias)
        item["aliases"] = aliases
        item["search"] = " ".join(aliases)
        enriched.append(item)
    return enriched


def _fetch_map_index():
    url = "https://gbank.gsj.jp/ld/resource/publication/map/g050.json"
    try:
        req = urllib.request.Request(url, headers=UA)
        data = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
        return _enrich_entries_with_publication(
            _normalize_entries(data.get("linkData", {}).get("hasPart", []))
        )
    except Exception as e:
        print(f"  [notice] 図幅リストをAPIから取得できませんでした: {e}")
        return []


def _load_map_index():
    """ローカルキャッシュ優先。スキーマが違っても正規化して読む。"""
    if os.path.exists(MAP_INDEX_CACHE):
        try:
            with open(MAP_INDEX_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = _enrich_entries_with_publication(
                _normalize_entries(data if isinstance(data, list)
                                   else data.get("maps", []))
            )
            if entries:
                return entries
            print(f"  [notice] {MAP_INDEX_CACHE} を解釈できませんでした。APIから取得します。")
        except Exception as e:
            print(f"  [notice] {MAP_INDEX_CACHE} の読み込みに失敗 ({e})。APIから取得します。")
    return _fetch_map_index()


def _search_index(maps, arg):
    q = str(arg).strip().casefold()
    exact, partial = [], []
    for m in maps:
        aliases = m.get("aliases") or [m.get("label") or ""]
        folded_aliases = [str(alias).strip().casefold() for alias in aliases if alias]
        item = (m["id"], m["label"])
        if q and any(q == alias for alias in folded_aliases):
            exact.append(item)
        elif q and q in str(m.get("search") or "").casefold():
            partial.append(item)
    return exact, partial


def resolve_map_id(arg):
    """'towada' / '青森' / '1050' から図幅IDを引く。見つからなければ None。"""
    if str(arg).isdigit():
        return str(arg)

    maps = _load_map_index()
    exact, partial = _search_index(maps, arg)

    # キャッシュで見つからなければ、APIの最新リストでもう一度探す
    if not exact and not partial and os.path.exists(MAP_INDEX_CACHE):
        fresh = _fetch_map_index()
        if fresh:
            exact, partial = _search_index(fresh, arg)
            maps = fresh

    found = exact or partial
    if len(found) == 1:
        print(f"'{found[0][1]}' -> ID: {found[0][0]}")
        return found[0][0]
    if len(found) > 1:
        print(f"'{arg}' に複数一致しました:")
        for mid, label in found:
            print(f"  - {label} (ID: {mid})")
        print("より具体的な名前か、IDを直接指定してください。")
        return None

    print(f"'{arg}' に一致する図幅が見つかりません。")
    if maps:
        import difflib
        names = [m["label"] for m in maps if m["label"]]
        for n in difflib.get_close_matches(arg, names, n=5, cutoff=0.6):
            mid = next((m["id"] for m in maps if m["label"] == n), "?")
            print(f"    もしかして: {n} (ID: {mid})")
        print(f"  （図幅リスト {len(maps)} 件を検索しました）")
    return None


def each_map(names, verb):
    """名前のリストを図幅IDに解決しながら回す。見出しも出す。"""
    for name in names:
        mid = resolve_map_id(name)
        if not mid:
            continue
        print(f"\n--- {verb}: Map ID {mid} ---")
        yield mid


def find_review_file(mid):
    for pat in (f"m{mid}_*_review.xlsx", f"m{mid}*_review.xlsx"):
        hits = [p for p in glob.glob(os.path.join("data", "50k", "02_review", "**", pat),
                                     recursive=True)
                if ".bak_" not in os.path.basename(p)
                and not os.path.basename(p).startswith("~$")]
        if hits:
            return hits[0]
    return None


def find_review_v2_file(mid):
    """Prefer the map workspace Review, then legacy generated-output fallbacks."""
    local_patterns = (
        f"m{mid}_review.xlsx",
        f"m{mid}_pilot_review.xlsx",
    )
    for filename in local_patterns:
        hits = [
            path for path in glob.glob(
                os.path.join("data", "50k", "02_review", "**", f"m{mid}_*", filename),
                recursive=True,
            )
            if os.path.isfile(path)
            and ".bak_" not in os.path.basename(path)
            and not os.path.basename(path).startswith("~$")
        ]
        if hits:
            return max(hits, key=os.path.getmtime)

    pilot = os.path.join(
        "outputs", "pilot", f"m{mid}", f"m{mid}_pilot_review.xlsx"
    )
    if os.path.isfile(pilot):
        return pilot
    preferred = os.path.join(
        "outputs", "review_v2", f"m{mid}_review", f"m{mid}_review_v2.xlsx"
    )
    if os.path.isfile(preferred):
        return preferred

    hits = [
        path for path in glob.glob(
            os.path.join("outputs", "**", f"m{mid}*_review_v2.xlsx"),
            recursive=True,
        )
        if not os.path.basename(path).startswith("~$")
    ]
    return max(hits, key=os.path.getmtime) if hits else None


def find_pdf(mid):
    for pat in ("*_D.pdf", "*.pdf"):
        hits = glob.glob(os.path.join("data", "50k", "02_review", "**", f"m{mid}_*",
                                      "references", pat), recursive=True)
        if hits:
            return hits[0]
    return None


def _sub(script, script_args):
    return subprocess.run([sys.executable, "-X", "utf8", os.path.join(SCRIPTS, script)]
                          + script_args).returncode


# ---------------------------------------------------------------------------
# 各コマンド
# ---------------------------------------------------------------------------

def cmd_make(args):
    names, flags, opts = split_args(args, valued=("--columns", "--compiler"))
    if not need_names(names, "make towada"):
        return
    passthru = sorted(flags) + [x for kv in opts.items() for x in kv]
    for mid in each_map(names, "make"):
        if _sub("make_review_sheet.py", [mid] + passthru) != 0:
            print(f"[ERROR] map {mid} の処理に失敗しました")


def cmd_pilot(args):
    """Run the one-command GSJ 1:50,000 pipeline for one selected map."""
    names, flags, opts = split_args(args, valued=("--output-dir", "--model"))
    if not need_names(names, "towada"):
        return 2
    if len(names) != 1:
        print("[ERROR] Process exactly one map sheet per command.")
        return 2
    mid = resolve_map_id(names[0])
    if not mid:
        return 2
    script_args = [mid]
    for option in ("--output-dir", "--model"):
        if option in opts:
            script_args.extend([option, opts[option]])
    for flag in ("--force", "--no-llm"):
        if flag in flags:
            script_args.append(flag)
    return_code = _sub("pilot.py", script_args)
    if return_code != 0:
        print(f"[ERROR] Workflow failed for map {mid}.")
    return return_code


def cmd_review_v2(args):
    """Build the non-destructive Review-v2 artifact set for one or more maps."""
    names, flags, opts = split_args(args, valued=("--output-dir", "--shape"))
    if not need_names(names, "review-v2 towada"):
        return
    if "--shape" in opts and len(names) != 1:
        print("[ERROR] --shape can only be used when one map is selected.")
        return

    many = len(names) > 1
    for mid in each_map(names, "review-v2"):
        review = find_review_file(mid)
        if not review:
            print(f"[ERROR] Review workbook for map {mid} was not found under data/50k/02_review/.")
            print(f"        Run `python run.py make {mid}` first.")
            continue

        script_args = [review, "--map-id", mid]
        if "--output-dir" in opts:
            destination = opts["--output-dir"]
            if many:
                destination = os.path.join(destination, f"m{mid}")
            script_args.extend(["--output-dir", destination])
        if "--shape" in opts:
            script_args.extend(["--shape", opts["--shape"]])
        if "--skip-map" in flags:
            script_args.append("--skip-map")
        if "--force" in flags:
            script_args.append("--force")

        if _sub("review_v2.py", script_args) != 0:
            print(f"[ERROR] Review-v2 generation failed for map {mid}.")


def cmd_export(args, check_only=False):
    verb = "check" if check_only else "export"
    names, flags, _ = split_args(args)
    if not need_names(names, f"{verb} towada"):
        return
    for mid in each_map(names, verb):
        f = None if "--legacy" in flags else find_review_v2_file(mid)
        source_kind = "Review-v2"
        if not f:
            f = find_review_file(mid)
            source_kind = "legacy review"
        if not f:
            print(f"[ERROR] map {mid} のレビューファイルがありません。")
            print(f"        先に `python run.py {mid}` を実行してください。")
            continue
        print(f"  Using: {f} ({source_kind})")
        _sub("export_submission.py", [f] + (["--check-only"] if check_only else []))


def cmd_check(args):
    cmd_export(args, check_only=True)


def cmd_abstract(args):
    from extract_abstract import extract, summarize
    names, _, _ = split_args(args)
    if not need_names(names, "abstract towada"):
        return
    for mid in each_map(names, "abstract"):
        pdf = find_pdf(mid)
        if not pdf:
            print(f"[ERROR] map {mid} の説明書PDFがありません。"
                  f"先に `python run.py make {mid}` を実行してください。")
            continue
        text, rng = extract(pdf)
        if not text.strip():
            print(f"[ERROR] {os.path.basename(pdf)} から英文Abstractを検出できませんでした。")
            continue
        out = os.path.join(os.path.dirname(pdf), f"m{mid}_abstract.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"{os.path.basename(pdf)}  p.{rng[0]}–{rng[1]}")
        for k, v in summarize(text).items():
            print(f"  {k}: {v}")
        print(f"  保存: {out}")


def cmd_llm(args):
    from apply_llm_candidates import apply
    from common import load_secret, secret_status
    from llm_extract import (MODEL, BudgetExceeded, list_models, run as extract_ages,
                             test_connection, usage_summary)

    names, flags, opts = split_args(args, valued=("--model",))
    model = opts.get("--model", MODEL)

    if "--usage" in flags:
        print("\nGemini API の使用状況")
        for k, v in usage_summary().items():
            print(f"  {k}: {v}")
        print("\n  ※ 無料枠の条件は「請求先アカウントを紐付けていないこと」です。")
        print("     https://aistudio.google.com/api-keys で Plan 列が 'Paid' でないことを確認。")
        return

    key = load_secret("gemini_api_key", "GEMINI_API_KEY")
    if not key:
        print("[ERROR] APIキーが見つかりません。")
        for k, v in secret_status().items():
            print(f"  {k}: {v}")
        print('\n  config/secret.json に {"gemini_api_key": "..."} を置いてください。')
        print("  キーの取得: https://aistudio.google.com/apikey")
        return

    if "--models" in flags:
        found = list_models(key)
        print(f"このキーで使えるモデル（{len(found)}件）:")
        for n in found:
            print("  -", n)
        return
    if "--test" in flags:
        test_connection(key, model=model)
        return
    if not need_names(names, "llm towada"):
        return

    dry, debug = "--dry" in flags, "--debug" in flags
    keep = "--keep" in flags        # 編集列の既存値に触らない
    for mid in each_map(names, "llm"):
        review = find_review_file(mid)
        if not review:
            print(f"[ERROR] レビューファイルがありません。先に `python run.py make {mid}` を。")
            continue
        abs_txt = os.path.join(os.path.dirname(review), "references",
                               f"m{mid}_abstract.txt")
        if not os.path.exists(abs_txt):
            print(f"[ERROR] abstract がありません。`python run.py abstract {mid}` を実行してください。")
            continue
        with open(abs_txt, encoding="utf-8") as f:
            text = f.read()

        try:
            found, _ = extract_ages(text, key, model=model, debug=debug)
        except BudgetExceeded as e:
            print(f"\n[STOP] 安全装置が作動しました。呼び出しは行っていません。\n  {e}")
            break
        except Exception as e:
            print(f"[ERROR] Gemini 呼び出しに失敗: {type(e).__name__}: {str(e)[:200]}")
            continue
        if not found:
            print("  候補が得られませんでした。")
            continue

        # ★ 表示より先に保存する。
        #   以前は表示を先にしていて、表示側の不具合で落ちたときに
        #   API呼び出し1回ぶんの結果を丸ごと失った。取得済みの結果を守る。
        written, added, unmatched = apply(review, found, dry=dry, keep=keep)

        print()
        for u in found:
            try:
                print("  " + _llm_line(u))
            except Exception as e:      # 表示だけの失敗で処理を止めない
                print(f"  {u.get('unit_name')}  [表示できません: {type(e).__name__}]")

        if written < 0:
            continue                      # 保存失敗。メッセージは apply 側が出している
        print()
        print(f"  {'(--dry: 書き込みませんでした) ' if dry else ''}"
              f"既存行に記入 {written} 件 / 新規行 {added} 件"
              + (f" / 対応づかず {len(unmatched)} 件" if unmatched else ""))
        for n in unmatched[:6]:
            print(f"    [未対応] {n}（Abstractにはあるが units_review に該当行なし）")
        if not dry and written + added:
            print(f"  → {review} の REF_* 列を確認してください。")


def _fmt_age(b, t):
    """年代を表示用に整える。★ 片側だけの候補があるので両方 None を確認する。"""
    if b is None and t is None:
        return "—"
    if b is None:
        return f"〜{t:g} Ma"          # 上限（若い側）だけ分かっている
    if t is None:
        return f"{b:g} Ma〜"          # 下限（古い側）だけ分かっている
    return f"{b:g} Ma" if b == t else f"{b:g}–{t:g} Ma"


def _fmt_thickness(mn, mx):
    if mn is None and mx is None:
        return None
    if mn is None or mx is None:
        return f"{(mn if mx is None else mx):g}m"
    return f"{mn:g}m" if mn == mx else f"{mn:g}–{mx:g}m"


def _llm_line(u):
    """LLM候補1件を1行に。年代のほか、拾えたフィールドも見えるようにする。"""
    from common import pad
    parts = []
    for label, key in (("層序", "strat_name"), ("岩相", "lithology"),
                       ("副岩相", "minor_lith"), ("環境", "environment"),
                       ("基底", "basal_surface")):
        v = u.get(key)
        if v not in (None, ""):
            parts.append(f"{label}:{str(v)[:24]}")
    th = _fmt_thickness(u.get("min_thickness"), u.get("max_thickness"))
    if th:
        parts.append(f"層厚:{th}")
    age = _fmt_age(u.get("b_age_ma"), u.get("t_age_ma"))
    line = pad(str(u.get("unit_name") or "")[:42], 44) + pad(age, 16)
    return (line + " | ".join(parts)).rstrip()


def cmd_list(args=None):
    """50k および 200k の 02_review 以下を走査して進捗を一覧表示する。"""
    import pandas as pd
    from common import disp_width, pad

    def _show_list_for_base(title, base_dir, sub_base_dir):
        files = sorted(p for p in glob.glob(os.path.join(base_dir, "**",
                                                         "*_review.xlsx"), recursive=True)
                       if ".bak_" not in os.path.basename(p)
                       and not os.path.basename(p).startswith("~$"))
        if not files:
            return 0

        head = (pad("図幅", 26) + pad("地域", 14) + pad("層数", 6, "right")
                + pad("unit_name", 11, "right") + pad("lithology", 11, "right")
                + pad("sort", 7, "right") + "  " + "提出")
        print(f"\n=== {title} ({len(files)} 件) ===")
        print(head)
        print("-" * disp_width(head))
        for f in files:
            parts = f.replace("\\", "/").split("/")
            region = parts[parts.index("02_review") + 1] if "02_review" in parts else "?"
            folder = os.path.basename(os.path.dirname(f))
            try:
                df = pd.read_excel(f, sheet_name="units_review", dtype=object).dropna(how="all")
            except Exception as e:
                print(pad(folder, 26) + pad(region, 14) + f"  読込エラー: {e}")
                continue

            def filled(col):
                if col not in df.columns:
                    col = next((c for c in df.columns
                                if str(c).replace(" ", "_").lower() == col), None)
                    if col is None:
                        return 0
                return int(df[col].notna().sum()
                           - (df[col].astype(str).str.strip() == "").sum())

            sub_dir = os.path.dirname(f).replace("02_review", "03_submission")
            submitted = "✓" if glob.glob(os.path.join(sub_dir, "*.xlsx")) else "-"
            print(pad(folder, 26) + pad(region, 14) + pad(len(df), 6, "right")
                  + pad(filled("unit_name"), 11, "right")
                  + pad(filled("lithology"), 11, "right")
                  + pad(filled("sort_order"), 7, "right") + "  " + submitted)
        print("-" * disp_width(head))
        return len(files)

    c50 = _show_list_for_base("5万分の1地質図幅 (50k)", os.path.join("data", "50k", "02_review"), os.path.join("data", "50k", "03_submission"))
    c200 = _show_list_for_base("20万分の1地質図幅 (200k)", os.path.join("data", "200k", "02_review"), os.path.join("data", "200k", "03_submission"))

    if c50 == 0 and c200 == 0:
        print("レビューファイルがまだありません。`python run.py make <図幅名>` または `python run.py make-200k <図幅名>` から始めてください。")
        return

    print("数値 = 入力済みセル数 / 層数。埋まったら `python run.py export <図幅名>` または `python run.py export-200k <図幅名>`。\n")


def cmd_index(args):
    from build_zfk_index import build
    _, flags, opts = split_args(args, valued=("--workers",))
    build(force="--force" in flags, workers=int(opts.get("--workers", 8)))


def cmd_search(args):
    from search_zfk import run as search
    names, flags, _ = split_args(args)
    search(" ".join(names) if names else None,
           show_coords="--coords" in flags, show_all="--all" in flags)


def resolve_200k_sheet(query, sheets):
    """
    地名（日本語・英語・部分一致）または図幅コードから200k図幅メタデータを解決する。
    例: 'tokyo', '東京', 'kyoto', '京都', 'osaka', '大阪', 'NI-53-14'
    """
    q = str(query or "").strip().lower()
    if not q:
        return None

    # 1. コード完全一致 (ハイフン有無問わず)
    q_norm = q.replace("-", "").replace("_", "")
    for s in sheets:
        sc_norm = s["sheet_code"].lower().replace("-", "").replace("_", "")
        if q_norm == sc_norm:
            return s

    # 2. 英語名 完全一致
    for s in sheets:
        if s.get("name_en", "").lower() == q:
            return s

    # 3. 日本語名 完全一致（「（第2版）」などを除外したものも含む）
    for s in sheets:
        name_ja = s.get("name_ja", "")
        clean_ja = re.sub(r"[（\(].*?[）\)]", "", name_ja).strip()
        if name_ja == query or clean_ja == query:
            return s

    # 4. 英語名 部分一致（'kyoto' -> 'Kyoto-Osaka'）
    for s in sheets:
        name_en = s.get("name_en", "").lower()
        if q in name_en or name_en in q:
            return s

    # 5. 日本語名 部分一致（'京都' -> '京都及大阪'）
    for s in sheets:
        name_ja = s.get("name_ja", "")
        if query in name_ja or name_ja in query:
            return s

    return None


def cmd_make_200k(args):
    """20万分の1図幅のレビュー用Excelを生成する（地名・英語名・コード対応）。"""
    from make_review_200k import make_review_for_sheet, make_all_reviews, CONFIG_PATH, CACHE_DIR, REVIEW_BASE_DIR
    names, _, _ = split_args(args)
    if not names or names[0].lower() == "all":
        make_all_reviews()
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        sheets = json.load(f)

    for target in names:
        s_meta = resolve_200k_sheet(target, sheets)
        if not s_meta:
            print(f"[ERROR] 200k図幅 '{target}' が見つかりません。（例: tokyo, 京都, NI-53-14）")
            continue
        sc = s_meta["sheet_code"]
        name_en = s_meta.get("name_en", sc) or sc
        name_ja = s_meta.get("name_ja", sc)
        region = s_meta.get("region", "00_Other")
        cache_file = os.path.join(CACHE_DIR, f"{sc}.json")
        legends = []
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as cf:
                legends = json.load(cf)
        out_path = os.path.join(REVIEW_BASE_DIR, region, f"m200k_{sc}_{name_en}", f"m200k_{sc}_review.xlsx")
        c_cnt, u_cnt = make_review_for_sheet(s_meta, legends, out_path)
        print(f"[SUCCESS] {sc} {name_ja} ({name_en}): {c_cnt} Columns, {u_cnt} Units -> {out_path}")


def cmd_check_200k(args):
    """20万分の1レビュー用Excelの整合性を検証する（地名・コード対応）。"""
    from export_200k import run_check_cli
    from make_review_200k import CONFIG_PATH
    names, _, _ = split_args(args)
    if not names or names[0].lower() == "all":
        run_check_cli("all")
        return
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        sheets = json.load(f)
    for target in names:
        s_meta = resolve_200k_sheet(target, sheets)
        target_code = s_meta["sheet_code"] if s_meta else target
        run_check_cli(target_code)


def cmd_export_200k(args):
    """20万分の1レビュー用Excelから公式提出用Excelを出力する（地名・コード対応）。"""
    from export_200k import run_export_cli
    from make_review_200k import CONFIG_PATH
    names, _, _ = split_args(args)
    if not names or names[0].lower() == "all":
        run_export_cli("all")
        return
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        sheets = json.load(f)
    for target in names:
        s_meta = resolve_200k_sheet(target, sheets)
        target_code = s_meta["sheet_code"] if s_meta else target
        run_export_cli(target_code)


def cmd_repair(args):
    """表示崩れの修復と、古いレビューファイルの現行スキーマへの移行。"""
    from repair_layout import migrate, migrate_all, repair, repair_all
    names, flags, _ = split_args(args)
    do_migrate, dry = "--migrate" in flags, "--dry" in flags

    if not names:
        (migrate_all(dry=dry) if do_migrate else repair_all())
        return
    for mid in each_map(names, "migrate" if do_migrate else "repair"):
        f = find_review_file(mid)
        if not f:
            print(f"[skip] map {mid} のレビューファイルが見つかりません。")
            continue
        migrate(f, dry=dry) if do_migrate else repair(f)


def cmd_vocab(args):
    """Macrostrat 公式の語彙表（environment / lithology / 属性）を取得・表示する。"""
    from build_vocab import build, show
    if "--show" in (args or []):
        show()
        return
    if build() is None:
        sys.exit(1)


def cmd_inventory(args):
    """全国50k図幅のデータ有無・進捗・次アクションをCSV/JSONに出す。"""
    from national_inventory import build_inventory, print_summary
    _, flags, opts = split_args(args, valued=("--workers", "--limit"))
    result = build_inventory(
        refresh="--refresh" in flags,
        force="--force" in flags,
        workers=int(opts.get("--workers", 8)),
        limit=int(opts["--limit"]) if "--limit" in opts else None,
    )
    print_summary(result)


def cmd_shape(args):
    """取得済み geo_A.dbf の地質ユニットを表示する（読取り専用）。"""
    from shape_source import load_shape_units
    names, _, _ = split_args(args)
    if not need_names(names, "shape towada"):
        return
    for mid in each_map(names, "shape"):
        folders = sorted(p for p in glob.glob(
            os.path.join("data", "50k", "02_review", "**", f"m{mid}_*"), recursive=True)
            if os.path.isdir(p))
        if not folders:
            print(f"[skip] map {mid} の references がありません。先に make を実行してください。")
            continue
        data = load_shape_units(os.path.join(folders[0], "references"))
        if not data["available"]:
            print("  geo_A.dbf はありません。")
            continue
        print(f"  {data['dbf_path']} / {len(data['units'])} units / bbox={data['bbox']}")
        for unit in data["units"]:
            print(f"  {unit['major_code']:>4}  {unit['symbol']:<8} "
                  f"{unit['display_name_ja']} | {unit['display_name_en']}")


def cmd_propose_200k_domains(args):
    """200k 実ポリゴンから地質ドメイン分割と Column Footprint を提案・表示する (WP3)。"""
    from scripts.v2_200k.ingest_vector import ingest_sheet_polygons
    from scripts.v2_200k.unit_registry import build_unit_entities, build_polygon_occurrences
    from scripts.v2_200k.domain_segmentation import segment_sheet_domains
    import scripts.make_review_200k as make_rev

    names, flags, _ = split_args(args)
    if not names:
        print("エラー: 図幅名を指定してください。例: python run.py propose-200k-domains kyoto")
        return

    sheets = make_rev.load_200k_index()

    for name in names:
        nl = name.lower()
        sheet_meta = None
        for s in sheets:
            if nl in s['sheet_code'].lower() or nl in s['name_ja'].lower() or nl in s['name_en'].lower():
                sheet_meta = s
                break

        if not sheet_meta:
            print(f"[ERROR] 図幅 '{name}' が見つかりません。")
            continue

        sheet_code = sheet_meta['sheet_code']
        name_ja = sheet_meta['name_ja']
        name_en = sheet_meta['name_en']
        bbox = sheet_meta.get('bbox', [0, 0, 0, 0])

        legends = make_rev.load_sheet_legends(sheet_code, name_en)
        features = ingest_sheet_polygons(sheet_code, bbox, legends)
        unit_entities = build_unit_entities(sheet_code, legends)
        occurrences = build_polygon_occurrences(sheet_code, features, unit_entities)
        domains = segment_sheet_domains(sheet_code, name_en, unit_entities, occurrences)

        print(f"\n=======================================================")
        print(f"【GSJ 200k v2 実ポリゴン ドメイン分割提案】: {sheet_code} ({name_ja} / {name_en})")
        print(f"  総ポリゴン数: {len(occurrences)} 個 | 地質単元数: {len(unit_entities)} 単元")
        print(f"  提案地質ドメイン (Column) 数: {len(domains)} 本")
        print(f"=======================================================")
        for idx, dom in enumerate(domains, start=1):
            print(f"[{idx}] {dom.domain_name}")
            print(f"    - Column Kind: {dom.column_kind.value}")
            print(f"    - 所属 Unit 数: {len(dom.unit_ids)} 単元")
            print(f"    - 実ポリゴン総面積: {dom.total_area_sq_km} km²")
            print(f"    - 代表座標 (lat, lng): {dom.representative_point}")
            print(f"    - Footprint (WKT 先頭): {dom.footprint_wkt[:60]}...")


def cmd_ui(args):
    """進捗ダッシュボードをローカルサーバで開く（GSJ風ズーム式全国インデックス）。"""
    from dashboard_server import main as ui_main
    return ui_main(args)


def cmd_grid(args):
    """50k図幅の正規グリッド（緯度10分×経度15分）を公式データから導出する。"""
    from sheet_geometry import main as grid_main
    return grid_main(args)


def cmd_ui_static(args):
    """サーバを使わずに開ける静的HTML（dashboard_static.html）を書き出す。"""
    from dashboard_static import main as static_main
    return static_main(args)


def cmd_ui_status(args):
    """ダッシュボードサーバが動いているかだけを確認する。"""
    from dashboard_server import main as ui_main
    return ui_main(list(args) + ["--status"])


def cmd_dashboard_data(args):
    """ダッシュボードが読む索引JSONだけを作り直す（サーバは起動しない）。"""
    from dashboard_data import main as data_main
    return data_main(args)


def cmd_publish(args):
    """loop3_community/publications/ の日英論文から GitHub 公開用ドキュメントを同期生成する。"""
    from sync_github_release import sync_github_release
    return sync_github_release()


def cmd_audit(args):
    """レビュー簿に5大不変条件を当て、未解決ユニットを根拠つきで並べる。"""
    from invariant_audit import main as audit_main
    return audit_main(args)


COMMANDS = {
    "ui": cmd_ui,
    "dashboard": cmd_ui,
    "ui-static": cmd_ui_static,
    "uistatic": cmd_ui_static,
    "dashboard-static": cmd_ui_static,
    "ui-status": cmd_ui_status,
    "dashboard-status": cmd_ui_status,
    "grid": cmd_grid,
    "dashboard-data": cmd_dashboard_data,
    "audit": cmd_audit,
    "publish": cmd_publish,
    "make": cmd_make,
    "make-200k": cmd_make_200k,
    "make200k": cmd_make_200k,
    "propose-200k-domains": cmd_propose_200k_domains,
    "review": cmd_review_v2,
    "review-v2": cmd_review_v2,
    "review2": cmd_review_v2,
    "export": cmd_export,
    "export-200k": cmd_export_200k,
    "export200k": cmd_export_200k,
    "check": cmd_check,
    "check-200k": cmd_check_200k,
    "check200k": cmd_check_200k,
    "list": cmd_list,
    "abstract": cmd_abstract,
    "llm": cmd_llm,
    "index": cmd_index,
    "search": cmd_search,
    "find": cmd_search,
    "repair": cmd_repair,
    "fix": cmd_repair,
    "vocab": cmd_vocab,
    "inventory": cmd_inventory,
    "shape": cmd_shape,
}


def main():
    if len(sys.argv) < 2:
        print(HELP)
        return
    handler = COMMANDS.get(sys.argv[1].lower())
    if handler is None:
        # A map name or ID directly invokes the complete one-map workflow.
        return cmd_pilot(sys.argv[1:])
    return handler(sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main() or 0)
