# -*- coding: utf-8 -*-
"""
build_vocab.py — Macrostrat 公式の語彙表を取得して config/vocab.json を作る

取得先（いずれも CC-BY 4.0）:
  https://macrostrat.org/api/v2/defs/environments?all=1
  https://macrostrat.org/api/v2/defs/lithologies?all=1
  https://macrostrat.org/api/v2/defs/lithology_attributes?all=1

この語彙表は
  ・LLM のプロンプトに載せて、公式の語を使わせる
  ・export の検証で「公式表に無い語」を知らせる
の2つに使う。

★ 公式表に無い語を使ってはいけないわけではない。
  公式仕様の environment の説明は
    "Depositional environment interpretation; free text (e.g., "fluvial",
     "shallow marine") or Macrostrat environment"
  で、自由記述が明示的に許されている。実際、仕様の例に出てくる "shallow marine"
  自体が environments 表に載っていない。あくまで「照合できたかどうか」を知らせるだけ。

使い方:
  python run.py vocab            公式APIから取り直す
  python run.py vocab --show     いま入っている語彙の件数を表示
"""

import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

API = "https://macrostrat.org/api/v2/defs"
UA = {"User-Agent": "Mozilla/5.0"}
VOCAB_PATH = os.path.join("config", "vocab.json")

SOURCES = {
    "environment": ("environments", ("name", "type", "class")),
    "lithology": ("lithologies", ("name", "type", "group", "class")),
    "lith_att": ("lithology_attributes", ("name", "type")),
}


def fetch_csv(endpoint, timeout=60):
    url = f"{API}/{endpoint}?all=1&format=csv"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))


def build(out_path=VOCAB_PATH):
    vocab = {
        "_出典": "Macrostrat 公式API (CC-BY 4.0)",
        "_取得元": {k: f"{API}/{v[0]}?all=1" for k, v in SOURCES.items()},
        "_取得日": datetime.now().strftime("%Y-%m-%d"),
        "_注意": (
            "公式表に無い語を使ってはいけないわけではない。公式仕様の environment は "
            '"free text ... or Macrostrat environment" とされており、仕様の例に出てくる '
            '"shallow marine" 自体がこの表に無い。export では照合できなかった語を'
            "知らせるだけで、エラーにはしない。"),
        "_区切り": ("lithology / minor_lith / environment は ';' で複数指定する。"
                  "strat_name は ',' で階層（Formation, Group）をつなぐ。"),
    }

    for key, (endpoint, fields) in SOURCES.items():
        try:
            rows = fetch_csv(endpoint)
        except Exception as e:
            print(f"[ERROR] {endpoint} を取得できませんでした: {e}")
            return None
        names, detail = [], {}
        for r in rows:
            n = (r.get("name") or "").strip()
            if not n:
                continue
            if n not in names:
                names.append(n)
            detail[n] = {f: (r.get(f) or "").strip() for f in fields if f != "name"}
        vocab[key] = sorted(names)
        vocab[key + "_detail"] = detail
        print(f"  {key:<14}{len(names):>4} 語")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(vocab, f, ensure_ascii=False, indent=1)
    print(f"\n保存: {out_path}")
    return vocab


def show(path=VOCAB_PATH):
    from common import load_json
    v = load_json(path) or load_json(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path))
    if not v:
        print(f"{path} がありません。`python run.py vocab` で作成してください。")
        return
    print(f"出典: {v.get('_出典')} / 取得日: {v.get('_取得日')}")
    for k in ("environment", "lithology", "lith_att"):
        items = v.get(k) or []
        print(f"  {k:<14}{len(items):>4} 語   例: {', '.join(items[:6])}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Macrostrat公式語彙を取得")
    p.add_argument("--show", action="store_true", help="件数を表示するだけ")
    a = p.parse_args()
    if a.show:
        show()
    else:
        sys.exit(0 if build() else 1)
