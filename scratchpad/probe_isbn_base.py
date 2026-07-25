#!/usr/bin/env python3
"""jisui2epub 用: 末尾ページの ISBN / 発行日 自動検出の実測プローブ（PyMuPDF版）"""
import re
import sys
import unicodedata
from pathlib import Path

import fitz

_ISBN_SEP = r'[-‐‑\s]?'
_EAN13_RE = re.compile(r'(?<!\d)97[89](?:' + _ISBN_SEP + r'\d){10}(?!\d)')
_ISBN10_RE = re.compile(r'(?:\d' + _ISBN_SEP + r'){9}[\dX]')
_ISBN_KW_RE = re.compile(r'ISBN', re.I)


def _isbn13_ok(s):
    return len(s) == 13 and s.isdigit() and sum(
        (1 if i % 2 == 0 else 3) * int(d) for i, d in enumerate(s)) % 10 == 0


def _isbn10_ok(s):
    if len(s) != 10:
        return False
    t = 0
    for i, c in enumerate(s):
        v = 10 if c == "X" else (int(c) if c.isdigit() else -1)
        if v < 0:
            return False
        t += (10 - i) * v
    return t % 11 == 0


def _to13(s10):
    core = "978" + s10[:9]
    chk = (10 - sum((1 if i % 2 == 0 else 3) * int(d)
                    for i, d in enumerate(core)) % 10) % 10
    return core + str(chk)


def normalize_isbn(raw):
    if not raw:
        return None
    s = unicodedata.normalize("NFKC", raw)
    s = re.sub(r"^\s*ISBN\s*[:：]?\s*", "", s, flags=re.I)
    s = re.sub(r"[^0-9Xx]", "", s).upper()
    if _isbn13_ok(s) and s[:3] in ("978", "979"):
        return s
    if _isbn10_ok(s):
        return _to13(s)
    return None


_KANJI = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
          "七": 7, "八": 8, "九": 9, "十": 10, "元": 1}
_ERA = {"昭和": 1925, "平成": 1988, "令和": 2018}
_N = r"[0-9〇零一二三四五六七八九十元]"
_PUBDATE_RE = re.compile(
    r"(?:(昭和|平成|令和)\s*(" + _N + r"{1,2})|((?:19|20)\d{2}))\s*年"
    r"\s*(" + _N + r"{1,3})?\s*月\s*(" + _N + r"{1,3})?\s*日?")
_FIRST_RE = re.compile(r"初版|第\s*1\s*刷|第\s*一\s*刷")


def _knum(s):
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        return (_KANJI.get(a, 1) if a else 1) * 10 + (_KANJI.get(b, 0) if b else 0)
    v = 0
    for c in s:
        if c not in _KANJI:
            return None
        v = v * 10 + _KANJI[c]
    return v


def _iso(m):
    era, ey, sy, mo, da = m.groups()
    year = _ERA[era] + (_knum(ey) or 0) if era else int(sy)
    if not (1945 <= year <= 2035):
        return None
    mm, dd = _knum(mo), _knum(da)
    if mm and 1 <= mm <= 12:
        if dd and 1 <= dd <= 31:
            return f"{year:04d}-{mm:02d}-{dd:02d}"
        return f"{year:04d}-{mm:02d}"
    return f"{year:04d}"


def page_texts(doc, maxp):
    n = doc.page_count
    lo = max(0, n - maxp)
    out = {}
    for pi in range(n - 1, lo - 1, -1):
        try:
            t = doc[pi].get_text() or ""
        except Exception:
            t = ""
        out[pi] = unicodedata.normalize("NFKC", t) if t else ""
    return out, n, lo


def detect_isbn(doc, maxp=10):
    texts, n, lo = page_texts(doc, maxp)
    for pi in range(n - 1, lo - 1, -1):
        for m in _EAN13_RE.finditer(texts[pi]):
            got = normalize_isbn(m.group(0))
            if got:
                return got, pi + 1, "EAN-13"
    for pi in range(n - 1, lo - 1, -1):
        txt = texts[pi]
        for kw in _ISBN_KW_RE.finditer(txt):
            m = _ISBN10_RE.search(txt[kw.end():kw.end() + 25])
            if m:
                got = normalize_isbn(m.group(0))
                if got:
                    return got, pi + 1, "ISBN-10"
    return None, None, None


def detect_date(doc, maxp=12):
    n = doc.page_count
    for pi in range(n - 1, max(-1, n - 1 - maxp), -1):
        try:
            txt = doc[pi].get_text() or ""
        except Exception:
            continue
        if not txt:
            continue
        txt = unicodedata.normalize("NFKC", txt)
        if "発行" not in txt:
            continue
        cands = []
        for m in _PUBDATE_RE.finditer(txt):
            if "発行" not in txt[m.start():m.end() + 20]:
                continue
            iso = _iso(m)
            if not iso:
                continue
            ctx = txt[max(0, m.start() - 8):m.end() + 20]
            cands.append((iso, bool(_FIRST_RE.search(ctx))))
        if cands:
            firsts = [c for c in cands if c[1]]
            pool = firsts or cands
            pool.sort(key=lambda c: c[0])
            return pool[0][0], pi + 1
    return None, None


def main():
    for arg in sys.argv[1:]:
        p = Path(arg)
        try:
            doc = fitz.open(str(p))
        except Exception as e:
            print(f"{p.name}\tOPEN-ERR {e}")
            continue
        isbn, ipg, kind = detect_isbn(doc)
        dt, dpg = detect_date(doc)
        print(f"{p.name}\tpages={doc.page_count}\tisbn={isbn or '-'}"
              f"({kind or '-'} p{ipg or '-'})\tdate={dt or '-'}(p{dpg or '-'})")
        doc.close()


if __name__ == "__main__":
    main()
