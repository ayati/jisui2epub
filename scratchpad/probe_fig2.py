# -*- coding: utf-8 -*-
"""ページ内の「本文縦行に覆われていない矩形」＝図領域の候補を探す probe。"""
import sys, statistics, fitz
sys.path.insert(0, "/home/ayati/jisui2epub")
import jisui2epub as J

path = sys.argv[1]
only = set(int(x) for x in sys.argv[2:]) if len(sys.argv) > 2 else None
doc = fitz.open(path)
body_size = J.detect_body_size(doc, range(len(doc)))
pages = [J.analyze_page(doc[i], i, body_size) for i in range(len(doc))]
drop, headings, body_top, body_bottom, hk = J.classify_marginals(pages, body_size)

def kept(pg):
    return [v for v in pg.vlines if v.text.strip()
            and (pg.num, id(v)) not in drop
            and not J.is_junk_line(v.text.strip())]

# 書籍レベルの本文左右端
lefts, rights, nlines = [], [], []
for pg in pages:
    vs = kept(pg)
    nlines.append(len(vs))
    if len(vs) >= 8:
        lefts.append(min(v.x0 for v in vs)); rights.append(max(v.x1 for v in vs))
body_left = statistics.median(lefts); body_right = statistics.median(rights)
med_lines = statistics.median(nlines)
print(f"body box x[{body_left:.1f},{body_right:.1f}] y[{body_top:.1f},{body_bottom:.1f}] size={body_size:.2f} med_lines={med_lines}")

STEP = 1.0
def ink_ratio(page, rect):
    pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), colorspace=fitz.csGRAY, clip=rect)
    s = pix.samples
    if not s: return 0.0
    return sum(1 for b in s if b < 200) / len(s)

for pg in pages:
    if only and pg.num + 1 not in only: continue
    vs = kept(pg)
    if not vs: continue
    W = int((body_right - body_left) / STEP) + 1
    top = [body_bottom] * W
    bot = [body_top] * W
    for v in vs:
        a = max(0, int((v.x0 - body_left) / STEP))
        b = min(W - 1, int((v.x1 - body_left) / STEP))
        for i in range(a, b + 1):
            top[i] = min(top[i], v.y0)
            bot[i] = max(bot[i], v.y1)
    MINH = body_size * 3
    # 上帯: top[i] が十分下にある最長の連続 x 区間
    def runs(pred):
        out, s = [], None
        for i in range(W):
            if pred(i):
                if s is None: s = i
            else:
                if s is not None: out.append((s, i - 1)); s = None
        if s is not None: out.append((s, W - 1))
        return out
    cands = []
    for a, b in runs(lambda i: top[i] - body_top >= MINH):
        y1 = min(top[a:b+1])
        cands.append(("top", body_left + a*STEP, body_top, body_left + (b+1)*STEP, y1))
    for a, b in runs(lambda i: body_bottom - bot[i] >= MINH):
        y0 = max(bot[a:b+1])
        cands.append(("bot", body_left + a*STEP, y0, body_left + (b+1)*STEP, body_bottom))
    for kind, x0, y0, x1, y1 in cands:
        w, h = x1 - x0, y1 - y0
        if w < body_size * 3 or h < MINH: continue
        area = w * h / ((body_right - body_left) * (body_bottom - body_top))
        r = fitz.Rect(x0, y0, x1, y1)
        print(f"p{pg.num+1}\t{kind}\tx[{x0:.0f},{x1:.0f}] y[{y0:.0f},{y1:.0f}]\tarea={area:.2f}\tink={ink_ratio(doc[pg.num], r):.4f}\tnv={len(vs)}")
