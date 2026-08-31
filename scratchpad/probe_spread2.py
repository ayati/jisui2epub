# -*- coding: utf-8 -*-
"""見開き図: 連続する画像ページのインク外接矩形が内側の紙端に接するか。"""
import sys, collections, fitz
sys.path.insert(0, "/home/ayati/jisui2epub")
import jisui2epub as J

def ink_bbox(page):
    small = page.get_pixmap(matrix=fitz.Matrix(0.3, 0.3), colorspace=fitz.csGRAY)
    w, h, s = small.width, small.height, small.samples
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = s[y * w:(y + 1) * w]
        if not any(b < 200 for b in row):
            continue
        xs = [x for x, b in enumerate(row) if b < 200]
        x0 = min(x0, xs[0]); x1 = max(x1, xs[-1])
        y0 = min(y0, y); y1 = max(y1, y)
    if x1 < 0:
        return None
    return (x0 / 0.3, y0 / 0.3, (x1 + 1) / 0.3, (y1 + 1) / 0.3)

path = sys.argv[1]
doc = fitz.open(path)
bs = J.detect_body_size(doc, range(len(doc)))
pages = [J.analyze_page(doc[i], i, bs) for i in range(len(doc))]
drop, hd, bt, bb, hk = J.classify_marginals(pages, bs)
body_end = J.detect_body_end(pages, drop, bs, bt)
img = J.classify_image_pages(doc, pages, drop)
figs, full = J.detect_inline_figures(doc, pages, drop, hd, bs, bt, bb,
                                     image_pages=img, body_end=body_end)
allimg = img | full
raw = J._page_nombres(pages, drop)
good = {p: n for p, n in raw.items()
        if any(raw.get(q) == n + (q - p) for q in (p-2, p-1, p+1, p+2) if q in raw)}
def book(p):
    if p in good: return good[p]
    c = collections.Counter(q - good[q] for q in range(p-6, p+7) if q in good)
    return p - c.most_common(1)[0][0] if c else None

for p in sorted(allimg):
    if p + 1 not in allimg:
        continue
    out = []
    for q in (p, p + 1):
        W = doc[q].rect.width
        r = ink_bbox(doc[q])
        if r is None:
            out.append(f"p{q+1}(書籍{book(q)}) インクなし")
        else:
            out.append(f"p{q+1}(書籍{book(q)}) W={W:.0f} 左余白{r[0]:.0f} 右余白{W-r[2]:.0f}")
    print(" | ".join(out))
