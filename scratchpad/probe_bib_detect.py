#!/usr/bin/env python3
"""probe v3: 縦組み小説向けに強化した ISBN / 発行日 検出（設計案の実測）"""
import re
import sys
import unicodedata
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).parent))
from probe_isbn_base import normalize_isbn  # noqa

_SEP = r'[-‐‑－\s]*'
_EAN13 = re.compile(r'(?<!\d)97[89](?:' + _SEP + r'\d){10}(?!\d)')
_ISBN10 = re.compile(r'(?:\d' + _SEP + r'){9}[\dXx]')
_KW = re.compile(r'ISBN', re.I)

# ---- 日付 ----
_K = {"〇": 0, "○": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
      "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "元": 1}
_ERA = {"昭和": 1925, "平成": 1988, "令和": 2018, "大正": 1911}
_D = r"[0-9〇○零一二三四五六七八九十元]"
# 西暦(ASCII) / 西暦(漢数字4桁) / 和暦
_PUBDATE = re.compile(
    r"(?:(昭和|平成|令和|大正)\s*(" + _D + r"{1,3})"
    r"|((?:19|20)\d{2})"
    r"|([〇○零一二三四五六七八九]{4}))\s*年"
    r"\s*(" + _D + r"{1,3})?\s*月"
    r"(?:\s*(" + _D + r"{1,3})\s*(?:日|.{0,2}(?:発行|発売|刷|初版)))?")
_FIRST = re.compile(r"初版|新装版第1刷|新装版第一刷|第\s*1\s*刷|第\s*一\s*刷")
_DATE_KW = re.compile(r"発行|発売|刷|初版|印刷")


def compact(t):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", t or ""))


def knum(s):
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        return (_K.get(a, 1) if a else 1) * 10 + (_K.get(b, 0) if b else 0)
    v = 0
    for c in s:
        if c not in _K:
            return None
        v = v * 10 + _K[c]
    return v


def to_iso(m):
    era, ey, sy, ky, mo, da = m.groups()
    if era:
        y = _ERA[era] + (knum(ey) or 0)
    elif sy:
        y = int(sy)
    else:
        y = knum(ky)
        if y is None:
            return None
    if not (1945 <= y <= 2035):
        return None
    import calendar
    mm, dd = knum(mo), knum(da)
    if mm and 1 <= mm <= 12:
        if dd and 1 <= dd <= calendar.monthrange(y, mm)[1]:
            return f"{y:04d}-{mm:02d}-{dd:02d}"
        return f"{y:04d}-{mm:02d}"
    return f"{y:04d}"


def detect_isbn(doc, maxp=20):
    n = doc.page_count
    lo = max(0, n - maxp)
    texts = {pi: compact(doc[pi].get_text()) for pi in range(lo, n)}
    for pi in range(n - 1, lo - 1, -1):
        for m in _EAN13.finditer(texts[pi]):
            got = normalize_isbn(m.group(0))
            if got:
                return got, pi + 1, "EAN13"
    for pi in range(n - 1, lo - 1, -1):
        txt = texts[pi]
        for kw in _KW.finditer(txt):
            m = _ISBN10.search(txt[kw.end():kw.end() + 25])
            if m:
                got = normalize_isbn(m.group(0))
                if got:
                    return got, pi + 1, "ISBN10"
    return None, None, None


def detect_date(doc, maxp=20, debug=False):
    n = doc.page_count
    for pi in range(n - 1, max(-1, n - 1 - maxp), -1):
        txt = compact(doc[pi].get_text())
        if not _DATE_KW.search(txt):
            continue
        cands = []
        for m in _PUBDATE.finditer(txt):
            after = txt[m.end():m.end() + 6]
            ctx = txt[max(0, m.start() - 10):m.end() + 12]
            if not re.search(r"発行|発売|刷|初版", ctx):
                continue
            if after.startswith("印刷"):
                continue
            iso = to_iso(m)
            if not iso:
                continue
            cands.append((iso, bool(_FIRST.search(ctx)), ctx))
        if cands:
            if debug:
                for c in cands:
                    print("    cand", c[0], c[1], c[2][:40])
            firsts = [c for c in cands if c[1]]
            pool = firsts or cands
            pool.sort(key=lambda c: c[0])
            return pool[0][0], pi + 1, len(cands)
    return None, None, 0


def all_isbn(doc):
    vals = {}
    for pi in range(doc.page_count):
        t = compact(doc[pi].get_text())
        for m in _EAN13.finditer(t):
            g = normalize_isbn(m.group(0))
            if g:
                vals.setdefault(g, []).append(pi + 1)
        for kw in _KW.finditer(t):
            m = _ISBN10.search(t[kw.end():kw.end() + 25])
            if m:
                g = normalize_isbn(m.group(0))
                if g:
                    vals.setdefault(g, []).append(pi + 1)
    return vals


if __name__ == "__main__":
    dbg = "--debug" in sys.argv
    for arg in [a for a in sys.argv[1:] if not a.startswith("--")]:
        p = Path(arg)
        doc = fitz.open(str(p))
        i, ip, k = detect_isbn(doc)
        d, dp, nc = detect_date(doc, debug=dbg)
        av = all_isbn(doc)
        print(f"{p.name[:44]:46s} p={doc.page_count:4d} isbn={i or '-':13s}"
              f"({k or '-':6s}p{ip or '-':>4}) date={d or '-':10s}(p{dp or '-'},n={nc})"
              f" full={ {v: ','.join(map(str, pp[:3])) for v, pp in av.items()} }")
        doc.close()
