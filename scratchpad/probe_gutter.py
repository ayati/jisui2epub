# -*- coding: utf-8 -*-
"""列間の溝のインク＝「文字主体の図（系図・表）」の署名を測る。

縦組みの本文ページでは隣り合う列の間（溝）は必ず白い。系図・表は罫線・括弧が
溝を横切るので溝にインクが出る。
"""
import sys, statistics, fitz
sys.path.insert(0, "/home/ayati/jisui2epub")
import jisui2epub as J

path = sys.argv[1]
targets = set(int(x) for x in sys.argv[2:]) if len(sys.argv) > 2 else None
doc = fitz.open(path)
bs = J.detect_body_size(doc, range(len(doc)))
pages = [J.analyze_page(doc[i], i, bs) for i in range(len(doc))]
drop, hd, bt, bb, hk = J.classify_marginals(pages, bs)

def kept(pg):
    return [v for v in pg.vlines if v.text.strip() and (pg.num, id(v)) not in drop]

for pg in pages:
    if targets and pg.num + 1 not in targets: continue
    vs = sorted(kept(pg), key=lambda v: v.x0)
    if len(vs) < 4: continue
    page = doc[pg.num]
    vals, wide = [], 0
    for a, b in zip(vs, vs[1:]):
        g0, g1 = a.x1, b.x0
        if g1 - g0 < 1.5 or g1 - g0 > bs * 1.2: 
            if g1 - g0 > bs * 1.2: wide += 1
            continue
        y0 = max(bt, min(a.y0, b.y0)); y1 = min(bb, max(a.y1, b.y1))
        if y1 - y0 < bs * 4: continue
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY,
                              clip=fitz.Rect(g0 + 0.3, y0, g1 - 0.3, y1))
        s = pix.samples
        if s: vals.append(sum(1 for c in s if c < 200) / len(s))
    if not vals: continue
    print(f"p{pg.num+1}\tnv={len(vs)}\t溝{len(vals)}本\t"
          f"med={statistics.median(vals):.4f}\tmax={max(vals):.4f}\t"
          f"溝>0.02={sum(1 for v in vals if v>0.02)}")
