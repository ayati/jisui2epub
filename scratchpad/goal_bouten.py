# -*- coding: utf-8 -*-
"""GOAL ePub から傍点の当たっているテキストを取り出す（CSSのクラス解決込み）。"""
import html, re, sys, zipfile

def emphasis_items(epub):
    z = zipfile.ZipFile(epub)
    classes = set()
    for n in z.namelist():
        if not n.lower().endswith(".css"):
            continue
        css = z.read(n).decode("utf-8", "replace")
        for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
            sel, body = m.group(1), m.group(2)
            if re.search(r'text-emphasis(-style)?\s*:\s*(?!none)', body):
                classes |= set(re.findall(r'\.([A-Za-z0-9_-]+)', sel))
    items = []
    for n in z.namelist():
        if not n.lower().endswith((".html", ".xhtml")):
            continue
        t = z.read(n).decode("utf-8", "replace")
        t = re.sub(r'<rt>.*?</rt>', '', t, flags=re.S)
        for m in re.finditer(r'<(span|em|strong)\b([^>]*)>(.*?)</\1>', t, re.S):
            attrs, inner = m.group(2), m.group(3)
            cls = set(re.findall(r'class="([^"]*)"', attrs))
            cls = set(w for c in cls for w in c.split())
            hit = bool(cls & classes) or 'text-emphasis' in attrs
            if m.group(1) in ("em",) and not classes:
                hit = True
            if not hit:
                continue
            s = html.unescape(re.sub(r'<[^>]+>', '', inner)).strip()
            if s:
                items.append(s)
    return items, classes

if __name__ == "__main__":
    for f in sys.argv[1:]:
        it, cl = emphasis_items(f)
        import collections
        c = collections.Counter(it)
        print(f"{f.split('/')[-1][:46]:48s} 傍点 {len(it):4d} 異なり {len(c):3d} "
              f"クラス {sorted(cl)[:4]}")
        print("   ", c.most_common(6))
