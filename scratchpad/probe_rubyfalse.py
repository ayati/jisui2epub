"""本文がルビ誤判定されていないかを jisui2epub 自身の判定で確かめる。

判定は analyze_page の `span["size"] <= body_size*RUBY_SIZE_RATIO`。
本文が丸ごとルビ誤判定されると assemble_text が本文を1文字ずつの改行に
分断し、読める文章が消える（vision_reocr のほんもの P.11 の事故）。
ルビ再現率より上位の受け入れ基準（DESIGN_NDLOCR実装.md フェーズ2）。

**再OCR済みページと旧OCR残存ページを分けて集計する。** 再OCRは挿絵ページ等を
処理しないので混在は通常運用で、分けないと旧OCRのジャンク（挿絵ページの
`ノ0－` 等）が「しきい値ぎりぎりの本文」として数えられ判定が使えなくなる
（霧で「余裕+0%」と誤検知した）。再OCRページはスパンサイズが数種類しか
無いことで見分ける（本文1種＋ルビ1〜数種）。

    python probe_rubyfalse.py <PDF> [ラベル]
"""
import sys
from collections import Counter

sys.path.insert(0, "/home/ayati/jisui2epub")
import fitz  # noqa: E402
from jisui2epub import RUBY_SIZE_RATIO, analyze_page, detect_body_size  # noqa: E402

# 再OCRページと判定するスパンサイズの種類数の上限
REOCR_MAX_SIZE_KINDS = 6


def page_sizes(page):
    c = Counter()
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                t = sp["text"].strip()
                if t:
                    c[round(sp["size"], 2)] += len(t)
    return c


def main():
    path = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else path
    doc = fitz.open(path)
    body = detect_body_size(doc, range(len(doc)))
    thr = body * RUBY_SIZE_RATIO
    print(f"\n=== {label} ===")
    print(f"本文サイズ {body:.2f}pt / ルビしきい値 {thr:.2f}pt")

    reocr, legacy = [], []
    tot_v = tot_r = 0
    for i in range(len(doc)):
        c = page_sizes(page := doc[i])
        if not c:
            continue
        (reocr if len(c) <= REOCR_MAX_SIZE_KINDS else legacy).append((i + 1, c))
        pg = analyze_page(page, i + 1, body)
        tot_v += len(pg.vlines)
        tot_r += len(pg.rubies)

    print(f"ページ内訳: 再OCR済み {len(reocr)} / 旧OCR残存 {len(legacy)}"
          + (f"（p{[p for p, _ in legacy][:6]}）" if legacy else ""))
    print(f"縦行 {tot_v} 本 / ルビ判定 {tot_r} 件（全ページ）")

    for name, group in (("再OCR済みページ", reocr), ("旧OCR残存ページ", legacy)):
        sizes = Counter()
        for _, c in group:
            sizes.update(c)
        if not sizes:
            continue
        above = [s for s in sizes if s > thr]
        m = min(above) if above else None
        print(f"  {name}: サイズ{len(sizes)}種 "
              f"最頻 {sizes.most_common(1)[0][0]}pt×{sizes.most_common(1)[0][1]}字")
        if m is not None:
            mark = "  ← 危険" if m < thr * 1.1 else ""
            print(f"    本文側の最小 {m:.2f}pt = 本文比 {m/body:.3f} "
                  f"（限界 {RUBY_SIZE_RATIO}／余裕 {(m-thr)/thr*100:+.0f}%）{mark}")


if __name__ == "__main__":
    main()
