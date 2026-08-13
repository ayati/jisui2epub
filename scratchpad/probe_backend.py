"""detect_body_end（巻末広告の切り落とし）が効かない理由を見る。"""
import sys, os
sys.path.insert(0, '/home/ayati/jisui2epub')
import fitz
import jisui2epub as J

pdf = sys.argv[1]
doc = fitz.open(pdf)
nums = list(range(len(doc)))
body = J.detect_body_size(doc, nums)
pages = [J.analyze_page(doc[i], i, body) for i in nums]
drop, headings, btop, bbot, hkeys = J.classify_marginals(pages, body)
n = len(pages)
lo = sorted(pg.num for pg in pages)[int(n * (1 - J.BACKMATTER_TAIL))]
print(f"探索開始 p{lo+1} / body_top={btop:.1f} body={body:.2f}")
print(f"MIN_COLS={J.BACKMATTER_MIN_COLS} DEEP={J.BACKMATTER_DEEP} "
      f"FRAC={J.BACKMATTER_DEEP_FRAC} RUN={J.BACKMATTER_MIN_RUN}")
for pg in pages:
    if pg.num < lo:
        continue
    vs = [v for v in pg.vlines
          if (pg.num, id(v)) not in drop and v.text.strip()
          and not J.is_junk_line(v.text.strip())]
    if len(vs) < J.BACKMATTER_MIN_COLS:
        print(f"p{pg.num+1:4d} 列{len(vs):3d} -> 判定せず(中立)")
        continue
    deep = sum(1 for v in vs if v.y0 > btop + body * J.BACKMATTER_DEEP)
    print(f"p{pg.num+1:4d} 列{len(vs):3d} 深{deep:3d} 比{deep/len(vs):.2f}"
          + ("  ★別組み" if deep / len(vs) >= J.BACKMATTER_DEEP_FRAC else ""))
print("detect_body_end =", J.detect_body_end(pages, drop, body, btop))
