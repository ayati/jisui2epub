"""柱テキストの通し変化（＝章境界の候補）を見る。"""
import sys, os, re
sys.path.insert(0, '/home/ayati/jisui2epub')
import fitz
import jisui2epub as J

pdf = sys.argv[1]
doc = fitz.open(pdf)
nums = list(range(len(doc)))
body = J.detect_body_size(doc, nums)
pages = [J.analyze_page(doc[i], i, body) for i in nums]
drop, headings, btop, bbot, hkeys = J.classify_marginals(pages, body)

rows = []
for pg in pages:
    ms = []
    for v in pg.vlines + pg.hlines:
        if (pg.num, id(v)) in drop:
            t = v.text.strip()
            if not t:
                continue
            if J.NOMBRE_RE.match(re.sub(r'\s+', '', t)):
                continue
            ms.append(t)
    body_n = sum(1 for v in pg.vlines if (pg.num, id(v)) not in drop)
    rows.append((pg.num + 1, body_n, ms))

for n, b, ms in rows:
    print(f"p{n:4d} body={b:3d}  {' | '.join(ms)}")
