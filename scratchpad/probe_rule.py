# -*- coding: utf-8 -*-
"""罫線（長い直線）の検出。系図・表を本文から分ける信号になるか測る。

文字のグリフは長い連続した直線をほとんど作らない。罫線・括弧の縦棒は作る。
"""
import sys, re, fitz
sys.path.insert(0, "/home/ayati/jisui2epub")
import jisui2epub as J

SCALE = 2.0
_DARK = bytes(1 if i < 200 else 0 for i in range(256))


def rules(page, min_pt, scale=SCALE):
    """(横罫のリスト, 縦罫のリスト)。各要素は (x0,y0,x1,y1) pt。"""
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY)
    w, h = pix.width, pix.height
    d = pix.samples.translate(_DARK)
    n = int(min_pt * scale)
    pat = re.compile(b"\x01{%d,}" % n)
    hor, ver = [], []
    for y in range(h):
        row = d[y * w:(y + 1) * w]
        for m in pat.finditer(row):
            hor.append((m.start() / scale, y / scale, m.end() / scale, y / scale))
    for x in range(w):
        col = d[x::w]
        for m in pat.finditer(col):
            ver.append((x / scale, m.start() / scale, x / scale, m.end() / scale))
    return hor, ver


def merge(runs, horizontal):
    """隣接する走査線をまとめて1本の罫線にする。"""
    out = []
    for r in sorted(runs, key=lambda r: (r[1], r[0]) if horizontal else (r[0], r[1])):
        hit = None
        for o in out:
            if horizontal:
                if abs(o[1] - r[1]) <= 3 and r[0] < o[2] + 5 and o[0] < r[2] + 5:
                    hit = o
                    break
            else:
                if abs(o[0] - r[0]) <= 3 and r[1] < o[3] + 5 and o[1] < r[3] + 5:
                    hit = o
                    break
        if hit is None:
            out.append(list(r))
        else:
            hit[0] = min(hit[0], r[0]); hit[1] = min(hit[1], r[1])
            hit[2] = max(hit[2], r[2]); hit[3] = max(hit[3], r[3])
    return out


MAX_THICK = 2.5   # 罫線とみなす太さの上限(pt)。写真の暗部は厚い塊になる


def thin_rules(page, min_pt, scale=SCALE):
    """細くて長い直線だけを返す。(横罫, 縦罫)

    **紙面の外周は除く。** スキャンの端に出る黒い帯が罫線に見える
    （タイム・リープ上は y=0〜1 の帯で36ページが誤検出になった）。
    """
    hor, ver = rules(page, min_pt, scale)
    r = page.rect
    mx, my = r.width * 0.03, r.height * 0.02
    def inside(t):
        return (mx < t[0] and t[2] < r.width - mx
                and my < t[1] and t[3] < r.height - my)
    hor = [t for t in hor if inside(t)]
    ver = [t for t in ver if inside(t)]
    hl = [r for r in merge(hor, True)
          if r[2] - r[0] >= min_pt and r[3] - r[1] <= MAX_THICK]
    vl = [r for r in merge(ver, False)
          if r[3] - r[1] >= min_pt and r[2] - r[0] <= MAX_THICK]
    return hl, vl


if __name__ == "__main__":
    path = sys.argv[1]
    nums = [int(x) - 1 for x in sys.argv[2].split(",")]
    minpt = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
    doc = fitz.open(path)
    for pn in nums:
        hor = merge(rules(doc[pn], minpt)[0], True)
        ver = merge(rules(doc[pn], minpt)[1], False)
        hl = [r for r in hor if r[2] - r[0] >= minpt]
        vl = [r for r in ver if r[3] - r[1] >= minpt]
        print(f"p{pn+1}: 横罫{len(hl)} 縦罫{len(vl)}")
        for r in (hl + vl)[:6]:
            print(f"    ({r[0]:.0f},{r[1]:.0f})-({r[2]:.0f},{r[3]:.0f}) 長さ{max(r[2]-r[0], r[3]-r[1]):.0f}pt")
