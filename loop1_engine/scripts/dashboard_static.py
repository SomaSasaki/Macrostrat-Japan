from __future__ import annotations

# -*- coding: utf-8 -*-
"""ダッシュボードを 1 枚の静的 HTML に焼き込む（サーバ不要版）。

    python run.py ui-static              索引を作り直して dashboard_static.html を書く
    python run.py ui-static --no-build   既存の JSON をそのまま埋め込む
    python run.py ui-static --open       書き出したあとブラウザで開く

出力は既定でリポジトリ直下の ``dashboard_static.html``。
``data/index.json`` と ``data/detail/*.json`` を HTML 内に埋め込み、``fetch()`` を
差し替えて読ませるので、``file://`` で開くだけで全国マップが表示できる。
成果物（PNG・XLSX・PDF）へのリンクは HTML からの相対パスに書き換えるため、
リポジトリ直下に置いたままにすればローカルファイルとして開ける。
"""

import argparse
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = (ROOT / "loop1_engine" / "dashboard") if (ROOT / "loop1_engine" / "dashboard").is_dir() else (ROOT / "dashboard")
DEFAULT_OUT = ROOT / "dashboard_static.html"

# index.html 側で成果物リンクを組み立てている実リテラル。静的版では相対パスへ寄せる。
ASSET_LITERAL = 'files/${encodeURI('
ASSET_REPLACEMENT = '${ASSET_BASE}${encodeURI('


def _json_for_html(obj) -> str:
    """`</script>` で HTML を割らないようにエスケープした JSON リテラル。"""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def collect_payloads(dashboard: Path) -> dict[str, object]:
    """index.html が fetch する JSON をすべて集める。"""
    data_dir = dashboard / "data"
    index_json = data_dir / "index.json"
    if not index_json.is_file():
        raise FileNotFoundError(f"{index_json} がありません。先に索引を作ってください。")
    payloads: dict[str, object] = {
        "data/index.json": json.loads(index_json.read_text(encoding="utf-8"))
    }
    detail_dir = data_dir / "detail"
    if detail_dir.is_dir():
        for path in sorted(detail_dir.glob("*.json")):
            try:
                payloads[f"data/detail/{path.name}"] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[WARN] {path.name} を読めません: {exc}")
    return payloads


def _asset_base(out_path: Path) -> str:
    """出力 HTML から見たリポジトリ直下への相対プレフィックス。"""
    try:
        depth = len(out_path.resolve().parent.relative_to(ROOT.resolve()).parts)
    except ValueError:
        return ""                                    # リポジトリ外に出す場合はリンクを諦める
    return "../" * depth


def build_static(out_path: Path | None = None, build: bool = True) -> Path:
    out_path = out_path or DEFAULT_OUT
    source = DASHBOARD / "index.html"
    if not source.is_file():
        raise FileNotFoundError(f"{source} がありません。")

    if build:
        scripts = (ROOT / "loop1_engine" / "scripts") if (ROOT / "loop1_engine" / "scripts").is_dir() else (ROOT / "scripts")
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        try:
            import dashboard_data
            payload = dashboard_data.build_index()
            dashboard_data.write_outputs(payload)
        except BaseException as exc:                 # 既存 JSON があれば続行する
            print(f"[WARN] 索引の再生成に失敗（既存の JSON を使います）: {exc}")

    payloads = collect_payloads(DASHBOARD)
    html = source.read_text(encoding="utf-8")

    hits = html.count(ASSET_LITERAL)
    html = html.replace(ASSET_LITERAL, ASSET_REPLACEMENT)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    base = _asset_base(out_path)

    embedded = ",\n".join(f'  {json.dumps(key)}: {_json_for_html(value)}'
                          for key, value in payloads.items())
    inject = f"""<script>
/* ---- 静的版（サーバ不要）: {stamp} 生成 ---------------------------- *
 * data/index.json と data/detail/*.json をこのファイルに埋め込み、
 * fetch() を差し替えて読ませている。file:// で直接開ける。
 * ------------------------------------------------------------------ */
const ASSET_BASE = {json.dumps(base)};
const STATIC_BUILT_AT = {json.dumps(stamp)};
const __EMBEDDED__ = {{
{embedded}
}};
(function () {{
  const passthrough = typeof window.fetch === "function" ? window.fetch.bind(window) : null;
  const respond = (body, ok) => Promise.resolve({{
    ok: ok, status: ok ? 200 : 404, headers: {{ get: () => null }},
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  }});
  window.fetch = function (input, init) {{
    const raw = typeof input === "string" ? input : (input && input.url) || "";
    const key = String(raw).replace(/^\\.\\//, "").split(/[?#]/)[0];
    if (Object.prototype.hasOwnProperty.call(__EMBEDDED__, key)) return respond(__EMBEDDED__[key], true);
    if (key.startsWith("data/detail/")) return respond(null, false);
    if (key.startsWith("/api/")) return respond({{ ok: false, error: "静的版では索引を再生成できません。" }}, true);
    return passthrough ? passthrough(input, init) : Promise.reject(new Error("fetch unavailable"));
  }};
  document.addEventListener("DOMContentLoaded", function () {{
    const tag = document.createElement("div");
    tag.textContent = "静的版 " + STATIC_BUILT_AT + " 時点";
    tag.title = "サーバ不要のスナップショット。最新化は python run.py ui-static";
    tag.style.cssText = "position:fixed;right:10px;bottom:10px;z-index:9999;font:12px/1.6 system-ui,sans-serif;"
      + "padding:4px 10px;border-radius:999px;background:rgba(13,54,107,.88);color:#fff;pointer-events:none;";
    document.body.appendChild(tag);
  }});
}})();
</script>
"""

    marker = "</head>"
    if marker not in html:
        raise RuntimeError("index.html に </head> が見つかりません。")
    html = html.replace(marker, inject + marker, 1)
    html = html.replace("<title>", "<title>[静的版] ", 1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"静的ダッシュボード: {out_path}")
    print(f"  埋め込み JSON {len(payloads)} 件 / 成果物リンク書き換え {hits} 箇所 / {size_mb:.2f} MB")
    print("  ブラウザにドラッグ＆ドロップするか、ダブルクリックで開けます（サーバ不要）。")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="サーバ不要の静的ダッシュボードを書き出す")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-build", action="store_true", help="索引を作り直さない")
    parser.add_argument("--open", action="store_true", help="書き出したあとブラウザで開く")
    args = parser.parse_args(argv)
    try:
        out = build_static(args.out, not args.no_build)
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1
    if args.open:
        try:
            webbrowser.open(out.resolve().as_uri())
        except Exception:
            print(f"[WARN] 自動で開けませんでした: {out}")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    raise SystemExit(main())
