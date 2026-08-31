# -*- coding: utf-8 -*-
"""部分図検出 probe v6: 本文縦行の「構成スパン」単位の被覆マスク＋インクゲート。

v5 は span_is_vertical でスパンを選んだが、1文字1スパンの旧OCR
（タイム・リープ2013）では向きが判定できず全ページが図になった。
本文行（kept vline）の構成スパンだけを被覆に使えばOCR世代に依存しない。
巨大スパン（図版OCRノイズ '蕊' size164）は行から外す。
"""
import sys, statistics, fitz
sys.path.insert(0, "/home/ayati/jisui2epub")
import jisui2epub as J

import os
HORIZ = bool(os.environ.get("HORIZ"))
BIG_SPAN = 2.2
MIN_H_CH = 3.0
MIN_W_CH = 3.0

# VLine に構成スパンのbboxを記録させる
_orig_add = J.VLine.add_span
_SPANBOX = {}
def add_span(self, sp):
    _SPANBOX.setdefault(id(self), []).append((sp["bbox"], sp["size"]))
    return _orig_add(self, sp)
J.VLine.add_span = add_span


def analyse(path):
    doc = fitz.open(path)
    bs = J.detect_body_size(doc, range(len(doc)))
    pages = [J.analyze_page(doc[i], i, bs, horizontal=HORIZ) for i in range(len(doc))]
    drop, hd, bt, bb, hk = J.classify_marginals(pages, bs, horizontal=HORIZ)
    nom = J._page_nombres(pages, drop)

    def kept(pg):
        return [v for v in pg.vlines if v.text.strip()
                and (pg.num, id(v)) not in drop and not J.is_junk_line(v.text.strip())]
    lefts, rights = [], []
    for pg in pages:
        vs = kept(pg)
        if len(vs) >= 8:
            lefts.append(min(v.x0 for v in vs)); rights.append(max(v.x1 for v in vs))
    bl, br = statistics.median(lefts), statistics.median(rights)
    area_body = (br - bl) * (bb - bt)
    STEP = 1.0
    out = []
    for pg in pages:
        page = doc[pg.num]
        cov = []
        for v in kept(pg):
            for bbox, size in _SPANBOX.get(id(v), []):
                if size <= bs * BIG_SPAN:
                    cov.append(bbox)
        cov = [b for b in cov if b[3] > bt and b[1] < bb and b[2] > bl and b[0] < br]
        W = int((br - bl) / STEP) + 1
        top = [bb] * W; bot = [bt] * W
        for x0, y0, x1, y1 in cov:
            a = max(0, int((x0 - bl) / STEP)); b = min(W - 1, int((x1 - bl) / STEP))
            for i in range(a, b + 1):
                top[i] = min(top[i], max(y0, bt)); bot[i] = max(bot[i], min(y1, bb))
        MINH = bs * MIN_H_CH
        def runs(pred):
            r = []; s = None
            for i in range(W):
                if pred(i):
                    if s is None: s = i
                else:
                    if s is not None: r.append((s, i - 1)); s = None
            if s is not None: r.append((s, W - 1))
            return r
        cands = []
        for a, b in runs(lambda i: top[i] - bt >= MINH):
            cands.append(("top", bl + a * STEP, bt, bl + (b + 1) * STEP, min(top[a:b + 1])))
        for a, b in runs(lambda i: bb - bot[i] >= MINH):
            cands.append(("bot", bl + a * STEP, max(bot[a:b + 1]), bl + (b + 1) * STEP, bb))
        for kind, x0, y0, x1, y1 in cands:
            if x1 - x0 < bs * MIN_W_CH or y1 - y0 < MINH: continue
            r = fitz.Rect(x0, y0, x1, y1)
            r2 = fitz.Rect(x0 + 2, y0 + 2, x1 - 2, y1 - 2)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY, clip=r2)
            s = pix.samples
            ink = sum(1 for c in s if c < 200) / len(s) if s else 0.0
            out.append((pg.num, nom.get(pg.num), kind, (x0, y0, x1, y1),
                        (x1 - x0) * (y1 - y0) / area_body, ink, len(kept(pg))))
    return out

if __name__ == "__main__":
    for num, n, kind, r, a, ink, nv in analyse(sys.argv[1]):
        print(f"p{num+1}\tnombre={n}\t{kind}\tink={ink:.4f}\tarea={a:.2f}\tnv={nv}\t"
              f"x[{r[0]:.0f},{r[2]:.0f}] y[{r[1]:.0f},{r[3]:.0f}]")
