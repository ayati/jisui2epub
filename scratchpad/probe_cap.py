# -*- coding: utf-8 -*-
"""検出した図の矩形の中にある横組み行（キャプション候補）を並べる。"""
import sys, fitz
sys.path.insert(0, "/home/ayati/jisui2epub")
import jisui2epub as J

path = sys.argv[1]
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
doc = fitz.open(path)
bs = J.detect_body_size(doc, range(len(doc)))
pages = [J.analyze_page(doc[i], i, bs) for i in range(len(doc))]
drop, hd, bt, bb, hk = J.classify_marginals(pages, bs)
body_end = J.detect_body_end(pages, drop, bs, bt)
img = J.classify_image_pages(doc, pages, drop)
figs, full = J.detect_inline_figures(doc, pages, drop, hd, bs, bt, bb,
                                     image_pages=img, body_end=body_end)
by = {p.num: p for p in pages}
n = 0
for pnum in sorted(figs):
    pg = by[pnum]
    for r in figs[pnum]:
        n += 1
        if n > limit:
            sys.exit()
        print(f"p{pnum+1} 図 x[{r[0]:.0f},{r[2]:.0f}] y[{r[1]:.0f},{r[3]:.0f}]")
        inside = [h for h in pg.hlines
                  if h.x0 >= r[0] - 2 and h.x1 <= r[2] + 2
                  and h.y0 >= r[1] - 2 and h.y1 <= r[3] + 2]
        for h in sorted(inside, key=lambda h: h.yc):
            frac = (h.x1 - h.x0) / max(1e-6, r[2] - r[0])
            print(f"    H y={h.yc:.0f} 幅比{frac:.2f} size={h.size:.1f} "
                  f"下端まで{r[3]-h.y1:.0f} {h.text[:50]!r}")
