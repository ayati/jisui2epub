# -*- coding: utf-8 -*-
"""罫線の本1冊スキャン。系図・表のページを罫線で拾えるかを測る。"""
import sys, re, time, statistics, fitz
sys.path.insert(0, "/home/ayati/jisui2epub")
import jisui2epub as J
sys.path.insert(0, "/tmp/claude-1000/-home-ayati-jisui2epub/fc8d0242-e39b-4cb4-a578-196678643356/scratchpad")
from probe_rule import thin_rules

path = sys.argv[1]
minpt = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
horiz = len(sys.argv) > 3 and sys.argv[3] == "h"
doc = fitz.open(path)
bs = J.detect_body_size(doc, range(len(doc)))
pages = [J.analyze_page(doc[i], i, bs, horizontal=horiz) for i in range(len(doc))]
drop, hd, bt, bb, hk = J.classify_marginals(pages, bs, horizontal=horiz)
nom = J._page_nombres(pages, drop)
img = J.classify_image_pages(doc, pages, drop)
t0 = time.time()
res = {}
for pg in pages:
    hl, vl = thin_rules(doc[pg.num], minpt)
    if hl or vl:
        res[pg.num] = (len(hl), len(vl), hl + vl)
print(f"罫線のあるページ {len(res)}/{len(pages)}  ({time.time()-t0:.0f}s)")
for p in sorted(res):
    nh, nv, rs = res[p]
    x0 = min(r[0] for r in rs); y0 = min(r[1] for r in rs)
    x1 = max(r[2] for r in rs); y1 = max(r[3] for r in rs)
    print(f"  p{p+1}\tnombre={nom.get(p)}\t横{nh} 縦{nv}\t"
          f"囲み[{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}]\t"
          f"{'画像ページ' if p in img else ''}")
