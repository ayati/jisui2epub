"""章頭画像の等間隔検出（DESIGN_柱ラン章立て.md §4）の閾値を実測で決める。

挿絵の多い本で誤発火しないかを見るのが目的。各本について
「直後が本文ページである画像ページ」の並びから、間隔が中央値の±15%で
つながる最長の鎖を求め、長さ・変動係数・本を覆う割合を出す。
"""
import sys, os, statistics
sys.path.insert(0, '/home/ayati/jisui2epub')
import fitz
import jisui2epub as J

TOL = 0.15


def longest_chain(cands, tol=TOL):
    if len(cands) < 3:
        return []
    gaps = [b - a for a, b in zip(cands, cands[1:])]
    g = statistics.median(gaps)
    lo, hi = g * (1 - tol), g * (1 + tol)
    best, cur = [], [cands[0]]
    for a, b in zip(cands, cands[1:]):
        if lo <= b - a <= hi:
            cur.append(b)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [b]
    if len(cur) > len(best):
        best = cur
    return best


for pdf in sys.argv[1:]:
    doc = fitz.open(pdf)
    nums = list(range(len(doc)))
    body = J.detect_body_size(doc, nums)
    pages = [J.analyze_page(doc[i], i, body) for i in nums]
    drop, headings, bt, bb, hk = J.classify_marginals(pages, body)
    img = sorted(J.classify_image_pages(doc, pages, drop))

    def nbody(p):
        if not (0 <= p < len(pages)):
            return 0
        pg = pages[p]
        return sum(1 for v in pg.vlines if v.text.strip()
                   and (pg.num, id(v)) not in drop
                   and not J.is_junk_line(v.text.strip()))

    med_body = statistics.median([nbody(p) for p in range(len(pages))]) or 1
    cands = [p for p in img if nbody(p + 1) >= med_body * 0.6]
    chain = longest_chain(cands)
    name = os.path.basename(pdf)[:40]
    if len(chain) < 3:
        print(f"{name:42s} 画像{len(img):3d} 候補{len(cands):3d} 鎖 <3 → 不発火")
        continue
    gaps = [b - a for a, b in zip(chain, chain[1:])]
    cv = statistics.pstdev(gaps) / statistics.mean(gaps)
    span = (chain[-1] - chain[0] + 1) / len(pages)
    print(f"{name:42s} 画像{len(img):3d} 候補{len(cands):3d} "
          f"鎖{len(chain):3d} 間隔{statistics.median(gaps):5.1f} "
          f"CV={cv:.3f} 覆い={span:.2f}  p{chain[0]+1}〜p{chain[-1]+1}")
