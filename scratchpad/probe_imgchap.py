"""章頭画像ページの識別可能性（P4）を測る。

画像ページのうち「章頭」だけを、巻内の出現間隔と本文文脈で選べるか。
各画像ページについて 直前ページの本文行数・直後ページの本文行数・
ページ上の残テキスト・前後の画像ページとの間隔を出す。
"""
import sys, os, re
sys.path.insert(0, '/home/ayati/jisui2epub')
import fitz
import jisui2epub as J

for pdf in sys.argv[1:]:
    doc = fitz.open(pdf)
    nums = list(range(len(doc)))
    body = J.detect_body_size(doc, nums)
    pages = [J.analyze_page(doc[i], i, body) for i in nums]
    drop, headings, btop, bbot, hkeys = J.classify_marginals(pages, body)
    img = sorted(J.classify_image_pages(doc, pages, drop))

    def nbody(p):
        pg = pages[p]
        return sum(1 for v in pg.vlines if v.text.strip()
                   and (pg.num, id(v)) not in drop
                   and not J.is_junk_line(v.text.strip()))

    print(f"\n### {os.path.basename(pdf)} ({len(doc)}p) 画像ページ {len(img)}枚")
    prev = None
    for p in img:
        pg = pages[p]
        txt = []
        for v in pg.vlines + pg.hlines:
            t = v.text.strip()
            if t:
                txt.append(t)
        gap = "-" if prev is None else str(p - prev)
        print(f"  p{p+1:4d} gap={gap:>4}  前頁本文={nbody(p-1) if p else 0:3d} "
              f"次頁本文={nbody(p+1) if p+1 < len(pages) else 0:3d}  "
              f"残text={txt[:6]}")
        prev = p
