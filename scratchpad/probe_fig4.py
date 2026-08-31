# -*- coding: utf-8 -*-
"""残インク方式の部分図検出 probe。

  1. ページをグレースケールで描画してインクマスクを作る
  2. テキスト層の全スパンbboxを引く（＝文字のインクを消す）
  3. 残ったインク＝「絵」。粗いグリッドで連結成分にまとめて外接矩形を出す
  4. 本文縦行と重なる成分は捨て、キャプション（横行）を取り込んで矩形を確定
"""
import sys, statistics, collections, fitz
sys.path.insert(0, "/home/ayati/jisui2epub")
import jisui2epub as J

SCALE = 1.5          # px / pt
CELL  = 4.0          # 連結成分グリッドの1マス(pt)
CELL_MIN = 0.06      # マスを「絵」とみなす暗画素率
MERGE = 10.0         # 成分の統合距離(pt)

def page_figures(doc, pg, drop, body_size, body_top, body_bottom,
                 body_left, body_right, dump=False):
    page = doc[pg.num]
    R = page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(SCALE, SCALE), colorspace=fitz.csGRAY)
    W, H, s = pix.width, pix.height, pix.samples
    # テキストマスク（全スパン）
    spans = J.collect_spans(page)
    boxes = [sp["bbox"] for sp in spans]
    nx = int(R.width / CELL) + 1
    ny = int(R.height / CELL) + 1
    dark = [[0] * nx for _ in range(ny)]
    tot  = [[0] * nx for _ in range(ny)]
    txt  = [[False] * nx for _ in range(ny)]
    for x0, y0, x1, y1 in boxes:
        for gy in range(max(0, int((y0 - 1) / CELL)), min(ny, int((y1 + 1) / CELL) + 1)):
            for gx in range(max(0, int((x0 - 1) / CELL)), min(nx, int((x1 + 1) / CELL) + 1)):
                txt[gy][gx] = True
    for y in range(H):
        row = s[y * W:(y + 1) * W]
        gy = int((y / SCALE) / CELL)
        if gy >= ny: continue
        drow, trow = dark[gy], tot[gy]
        for x in range(W):
            gx = int((x / SCALE) / CELL)
            if gx >= nx: continue
            trow[gx] += 1
            if row[x] < 200:
                drow[gx] += 1
    # 紙面外周（スキャン端の黒縁）は捨てる
    mx, my = R.width * 0.03, R.height * 0.02
    on = [[False] * nx for _ in range(ny)]
    for gy in range(ny):
        for gx in range(nx):
            if txt[gy][gx]: continue
            cx, cy = (gx + .5) * CELL, (gy + .5) * CELL
            if not (mx < cx < R.width - mx and my < cy < R.height - my): continue
            if tot[gy][gx] and dark[gy][gx] / tot[gy][gx] >= CELL_MIN:
                on[gy][gx] = True
    # 連結成分
    seen = [[False] * nx for _ in range(ny)]
    comps = []
    for gy in range(ny):
        for gx in range(nx):
            if not on[gy][gx] or seen[gy][gx]: continue
            q = [(gy, gx)]; seen[gy][gx] = True; cells = []
            while q:
                y, x = q.pop()
                cells.append((y, x))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny_, nx_ = y + dy, x + dx
                        if 0 <= ny_ < ny and 0 <= nx_ < nx and on[ny_][nx_] and not seen[ny_][nx_]:
                            seen[ny_][nx_] = True; q.append((ny_, nx_))
            ys = [c[0] for c in cells]; xs = [c[1] for c in cells]
            comps.append([min(xs)*CELL, min(ys)*CELL, (max(xs)+1)*CELL, (max(ys)+1)*CELL, len(cells)])
    # 近い成分を統合
    changed = True
    while changed:
        changed = False
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                a, b = comps[i], comps[j]
                if (a[0] - MERGE < b[2] and b[0] - MERGE < a[2]
                        and a[1] - MERGE < b[3] and b[1] - MERGE < a[3]):
                    comps[i] = [min(a[0], b[0]), min(a[1], b[1]),
                                max(a[2], b[2]), max(a[3], b[3]), a[4] + b[4]]
                    comps.pop(j); changed = True; break
            if changed: break
    return comps

def main():
    path = sys.argv[1]
    doc = fitz.open(path)
    bs = J.detect_body_size(doc, range(len(doc)))
    pages = [J.analyze_page(doc[i], i, bs) for i in range(len(doc))]
    drop, hd, bt, bb, hk = J.classify_marginals(pages, bs)
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
    only = set(int(x) for x in sys.argv[2:]) if len(sys.argv) > 2 else None
    for pg in pages:
        if only and pg.num + 1 not in only: continue
        comps = page_figures(doc, pg, drop, bs, bt, bb, bl, br)
        for x0, y0, x1, y1, n in comps:
            a = (x1 - x0) * (y1 - y0) / area_body
            if a < 0.03: continue
            print(f"p{pg.num+1}\tnombre={nom.get(pg.num)}\tx[{x0:.0f},{x1:.0f}] y[{y0:.0f},{y1:.0f}]"
                  f"\tarea={a:.2f}\tcells={n}")
main()
