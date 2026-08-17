#!/usr/bin/env python3
"""ndlocr_reocr._expand_dash_runs を実データで単体テストする。

NDLOCR を動かせない環境向けに、再OCR済みPDFから `_expand_dash_runs` の入力
（行ボックス＝画像ピクセル座標 + 認識テキスト、ページ画像）を復元して関数に
かけ、伸ばした結果を GOAL の正解と突き合わせる。

行ボックスの復元: expand_line_to_cells は箱を字数で等分割するので、
書き戻されたPDFの「同じ (x0,x1) を持ち y が等間隔で連続するセル列」が
1つの行ボックスに対応する。
"""
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import fitz
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ndlocr_reocr as N                                   # noqa: E402
from vision_reocr import largest_embedded_image            # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "temp_sample"
DASHY = set("―─－‐‑–—−")


def goal_text(path):
    out = []
    with zipfile.ZipFile(path) as z:
        for n in sorted(x for x in z.namelist()
                        if x.lower().endswith((".xhtml", ".html", ".htm"))):
            s = z.read(n).decode("utf-8", "replace")
            s = re.sub(r"<head[^>]*>.*?</head>", " ", s, flags=re.S)
            s = re.sub(r"<rt[^>]*>.*?</rt>", "", s, flags=re.S)
            out.append(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"[\s　]+", "", "\n".join(out))


def truth_cells(g, page_text, i, runlen):
    """GOAL 側での正解セル数（ダッシュなら連続数、長音・その他は1）。"""
    for w in (6, 5, 4, 3):
        a, b = page_text[max(0, i - w):i], page_text[i + runlen:i + runlen + w]
        if len(a) < w or len(b) < w:
            continue
        ms = re.compile(re.escape(a) + r"(.{0,8}?)" + re.escape(b)).findall(g)
        if len(set(ms)) == 1:
            r = ms[0]
            if r and set(r) <= DASHY:
                return len(r), "dash"
            if r and set(r) <= {"ー"}:
                return 1, "onbiki"
            return 1, "other"
    return None, None


def rebuild_lines(page, sx, sy):
    """PDFのセルから (行ボックス[画像px], テキスト) を復元する。"""
    cols = defaultdict(list)
    for blk in page.get_text("dict")["blocks"]:
        if "lines" not in blk:
            continue
        for ln in blk["lines"]:
            for sp in ln["spans"]:
                if sp["size"] < 6:                    # ルビは対象外
                    continue
                x0, y0, x1, y1 = sp["bbox"]
                n = len([c for c in sp["text"] if not c.isspace()])
                if n == 0:
                    continue
                step = (y1 - y0) / n
                k = 0
                for c in sp["text"]:
                    if c.isspace():
                        continue
                    cols[(round(x0, 1), round(x1, 1))].append(
                        (y0 + k * step, y0 + (k + 1) * step, c))
                    k += 1
    out = []
    for (x0, x1), items in cols.items():
        items.sort()
        i = 0
        while i < len(items):
            j = i
            step = items[i + 1][0] - items[i][0] if i + 1 < len(items) else None
            while (step and j + 1 < len(items)
                   and abs((items[j + 1][0] - items[j][0]) - step)
                   < max(0.06 * step, 0.15)):
                j += 1
            out.append({
                "cls": "line_main",
                "box": (x0 / sx, items[i][0] / sy, x1 / sx, items[j][1] / sy),
                "text": "".join(c for _, _, c in items[i:j + 1]),
            })
            i = j + 1
    return out


def run(pdf, goal):
    doc = fitz.open(pdf)
    g = goal_text(goal)
    tally = Counter()
    bad = []
    for pno in range(doc.page_count):
        page = doc[pno]
        found = largest_embedded_image(doc, page)
        if found is None:
            continue
        img = cv2.imdecode(np.frombuffer(found[0], dtype=np.uint8),
                           cv2.IMREAD_COLOR)
        if img is None:
            continue
        ih, iw = img.shape[:2]
        sx, sy = page.rect.width / iw, page.rect.height / ih
        lines = rebuild_lines(page, sx, sy)
        if not any("-" in ln["text"] for ln in lines):
            continue
        before = [ln["text"] for ln in lines]
        N._expand_dash_runs(lines, img)
        # 読み順（右の列から）に並べてページ文字列を作り、正解を引く
        order = sorted(range(len(lines)),
                       key=lambda k: -(lines[k]["box"][0] + lines[k]["box"][2]))
        raw_before = "".join(before[k] for k in order)
        pos = 0
        for k in order:
            txt = before[k]
            # ハイフン以外は不変なので、run は前後で順番に1対1で対応する
            runs_b = [(m.start(), len(m.group())) for m in re.finditer("-+", txt)]
            runs_a = [len(m.group())
                      for m in re.finditer("-+", lines[k]["text"])]
            assert len(runs_b) == len(runs_a), (txt, lines[k]["text"])
            for (i, nb), got in zip(runs_b, runs_a):
                want, kind = truth_cells(g, raw_before, pos + i, nb)
                if not want:
                    continue
                tally[(kind, want, got)] += 1
                if got != want:
                    bad.append((kind, want, got, pno + 1,
                                raw_before[max(0, pos + i - 8):pos + i + nb + 8]))
            pos += len(txt)
    doc.close()
    name = Path(pdf).name
    dash_ok = sum(v for (k, w, g_), v in tally.items() if k == "dash" and w == g_)
    dash_n = sum(v for (k, w, g_), v in tally.items() if k == "dash")
    keep_ok = sum(v for (k, w, g_), v in tally.items()
                  if k != "dash" and g_ == 1)
    keep_n = sum(v for (k, w, g_), v in tally.items() if k != "dash")
    print(f"{name}\n   ダッシュ {dash_ok}/{dash_n} 一致 / "
          f"長音・その他 {keep_ok}/{keep_n} を1字維持")
    for b in bad[:6]:
        print(f"     不一致 {b[0]} 正解{b[1]} 出力{b[2]} p{b[3]} {b[4]!r}")
    return dash_ok, dash_n, keep_ok, keep_n


PAIRS = [
    ("どこよりも遠い場所にいる君へ_阿部暁子_ndlocr.pdf",
     "どこよりも遠い場所にいる君へ_GOAL.epub"),
    ("RAIL＿WARS！_豊田巧_ndlocr.pdf",
     "RAIL WARS! 1 日本國有鉄道公安隊_GOAL.epub"),
    ("赤毛のアン_モンゴメリ・村岡花子訳_ndlocr.pdf",
     "赤毛のアン_モンゴメリ_GOAL.epub"),
    ("fixed_連帯惑星ピザンの危機_高千穂遥_ndlocr.pdf",
     "連帯惑星ピザンの危機_GOAL.epub"),
    ("赤毛のアン論－八つの扉_松本侑子_ndlocr.pdf",
     "赤毛のアン論八つの扉_GOAL.epub"),
]

if __name__ == "__main__":
    tot = [0, 0, 0, 0]
    for pdf, goal in PAIRS:
        p, gp = SAMPLE / pdf, SAMPLE / goal
        if not p.exists() or not gp.exists():
            print(f"skip {pdf}")
            continue
        for i, v in enumerate(run(p, gp)):
            tot[i] += v
    print(f"\n合計: ダッシュ {tot[0]}/{tot[1]} 一致 / "
          f"長音・その他 {tot[2]}/{tot[3]} を1字維持")
