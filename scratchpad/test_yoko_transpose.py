#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""転置方式（DESIGN_横書き対応.md §3）のスパイク検証。

jisui2epub.py 本体は無改造のまま、collect_spans をモンキーパッチして
bbox を90度転置し、既存の縦書きパイプラインに横書きPDFを通す。
確認点:
  1. 段落復元・読み順・字下げが正しいか
  2. 横ルビが親文字に付くか（吾輩《わがはい》・掌《てのひら》）
  3. 章見出し（大活字）の is_big 検出
  4. 柱・ノンブルの漏れ方（§3.3 の予測どおり本文に混入するか）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fitz
import jisui2epub as j

_orig_collect = j.collect_spans


def transposed_collect(page):
    spans = _orig_collect(page)
    H = page.rect.height
    for sp in spans:
        x0, y0, x1, y1 = sp["bbox"]
        sp["bbox"] = (H - y1, x0, H - y0, x1)
    return spans


def main(pdf="scratchpad/yoko_sample.pdf"):
    j.collect_spans = transposed_collect
    doc = fitz.open(pdf)
    body_size = j.detect_body_size(doc, range(len(doc)))
    print(f"本文サイズ: {body_size:.1f}pt / {len(doc)}ページ")

    pages = [j.analyze_page(doc[i], i, body_size) for i in range(len(doc))]
    for pg in pages[:2]:
        print(f"--- p{pg.num + 1}: 行(転置後vline) {len(pg.vlines)} / "
              f"hline {len(pg.hlines)} / ルビ {len(pg.rubies)}")
        for r in pg.rubies:
            print(f"    ルビ: {r.text!r}")

    drop, headings, top, bot, keys = j.classify_marginals(pages, body_size)
    print(f"本文バンド(転置y=元x): {top:.0f}〜{bot:.0f} / drop {len(drop)} / "
          f"見出し候補 {len(headings)} / 柱パターン {list(keys)}")

    body = j.assemble_text(pages, drop, headings, body_size, top, bot, keys)
    print("=" * 60)
    print(body)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "scratchpad/yoko_sample.pdf")
