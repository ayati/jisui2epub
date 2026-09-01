# -*- coding: utf-8 -*-
"""復元した傍点句を「本文中の位置」で GOAL と突き合わせる（文字単位）。"""
import collections, html, re, statistics, sys, unicodedata, zipfile
sys.path.insert(0, "/home/ayati/jisui2epub")
import jisui2epub as J
import fitz
DOT = re.compile(r'^[、。・ヽゝ\'’`,.]+$')

def norm(s):
    return re.sub(r'[\s　]', '', unicodedata.normalize("NFKC", s))

def goal_flags(epub):
    """GOAL 本文（正規化）と、文字ごとの傍点フラグを返す。"""
    z = zipfile.ZipFile(epub)
    classes = set()
    for n in z.namelist():
        if n.lower().endswith(".css"):
            css = z.read(n).decode("utf-8", "replace")
            for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
                if re.search(r'text-emphasis(-style)?\s*:\s*(?!none)', m.group(2)):
                    classes |= set(re.findall(r'\.([A-Za-z0-9_-]+)', m.group(1)))
    docs = [n for n in z.namelist() if n.lower().endswith((".html", ".xhtml"))]
    docs.sort()
    text, flags = [], []
    for n in docs:
        t = z.read(n).decode("utf-8", "replace")
        t = re.sub(r'<rt>.*?</rt>', '', t, flags=re.S)
        t = re.sub(r'<head.*?</head>', '', t, flags=re.S | re.I)
        depth = 0
        for m in re.finditer(r'<[^>]+>|[^<]+', t):
            tok = m.group(0)
            if tok.startswith("<"):
                if re.match(r'</(span|em|strong)\b', tok) and depth:
                    depth -= 1
                elif re.match(r'<(span|em|strong)\b', tok):
                    cls = set(w for c in re.findall(r'class="([^"]*)"', tok)
                              for w in c.split())
                    on = bool(cls & classes) or 'text-emphasis' in tok
                    depth += 1 if (on or depth) else 0
                    if not on and depth:   # 入れ子の内側でない普通のspan
                        pass
                continue
            s = norm(html.unescape(tok))
            text.append(s); flags.extend([depth > 0] * len(s))
    return "".join(text), flags

def recovered(pdf):
    doc = fitz.open(pdf); n = doc.page_count
    body = J.detect_body_size(doc, list(range(n))[n//4:n*3//4][:80])
    out = []
    for i in range(n):
        pg = J.analyze_page(doc[i], i, body)
        dots = [r for r in pg.rubies if DOT.match(r.text)]
        if not dots: continue
        per = collections.defaultdict(list)
        for r in dots:
            best = None
            for vl in pg.vlines:
                if not vl.cells or vl.xc >= r.xc: continue
                gap = r.x0 - vl.x1
                if gap > body * 1.2 or gap < -(vl.x1 - vl.x0): continue
                if r.y1 < vl.y0 - body or r.y0 > vl.y1 + body: continue
                if best is None or vl.xc > best.xc: best = vl
            if best is None: continue
            cs = [(c[1] + c[2]) / 2 for c in best.cells]
            h = (r.y1 - r.y0) / len(r.text)
            for t in range(len(r.text)):
                yc = r.y0 + h * (t + 0.5)
                per[id(best)].append(min(range(len(cs)), key=lambda m: abs(cs[m] - yc)))
        for vl in pg.vlines:
            idx = sorted(set(per.get(id(vl), ())))
            if not idx: continue
            txt = "".join(c[0] for c in vl.cells)
            cur = [idx[0]]
            for j in idx[1:]:
                if j == cur[-1] + 1: cur.append(j)
                else:
                    out.append((txt, cur)); cur = [j]
            out.append((txt, cur))
    return out

pdf, epub = sys.argv[1], sys.argv[2]
gtext, gflag = goal_flags(epub)
res = collections.Counter(); miss = []
MINRUN = int(sys.argv[3]) if len(sys.argv) > 3 else 1
for txt, run in recovered(pdf):
    if len(run) < MINRUN: continue
    a, b = run[0], run[-1] + 1
    ctx = norm(txt[max(0, a - 7):a]); ph = norm(txt[a:b])
    if not ph:
        res["空"] += 1; continue
    key = ctx + ph
    pos = gtext.find(key) if len(key) >= 5 else -1
    if pos < 0:
        pos = gtext.find(ph) if len(ph) >= 4 else -1
        if pos < 0:
            res["GOALに該当箇所なし(OCR差)"] += 1; continue
        s = pos
    else:
        s = pos + len(ctx)
    e = s + len(ph)
    on = gflag[s:e]
    if all(on):
        # GOAL 側の傍点がさらに伸びているか
        if (s > 0 and gflag[s-1]) or (e < len(gflag) and gflag[e]):
            res["正しいが短い"] += 1
        else:
            res["完全一致"] += 1
    elif any(on):
        res["一部ずれ"] += 1
    else:
        res["まったくの誤り"] += 1
        if len(miss) < 12: miss.append(ph)
tot = sum(res.values())
print(f"== {pdf.split('/')[-1][:40]}  復元 {tot}")
for k, v in res.most_common():
    print(f"   {k:26s} {v:4d}  {v/tot*100:5.1f}%")
print("   誤りの例:", miss)
