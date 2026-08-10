#!/usr/bin/env python3
"""再OCR出力PDFの回帰比較。

PyMuPDF は保存のたびにトレーラの /ID 第2要素（更新識別子）を作り直すため、
内容が完全に同じでもバイト列は必ず29バイト異なる。ここではその部分だけを
除外して比較し、あわせて全ページのテキスト層も突き合わせる。

  .venv/bin/python scratchpad/cmp_pdf.py before.pdf after.pdf
"""
import re
import sys

import fitz


def strip_id(data):
    return re.sub(rb"/ID\s*\[<[0-9A-Fa-f]*><[0-9A-Fa-f]*>\]", b"/ID[]", data)


def main():
    p1, p2 = sys.argv[1], sys.argv[2]
    a, b = open(p1, "rb").read(), open(p2, "rb").read()
    same_bytes = strip_id(a) == strip_id(b)
    print(f"バイト列（/ID除く）: {'一致' if same_bytes else '不一致'} "
          f"({len(a)} / {len(b)} バイト)")

    d1, d2 = fitz.open(p1), fitz.open(p2)
    if len(d1) != len(d2):
        print(f"ページ数が違う: {len(d1)} / {len(d2)}")
        return 1
    diff = []
    for i in range(len(d1)):
        t1, t2 = d1[i].get_text(), d2[i].get_text()
        if t1 != t2:
            diff.append((i + 1, len(t1), len(t2)))
    if diff:
        print(f"テキスト層が違うページ: {len(diff)}件")
        for pno, n1, n2 in diff[:10]:
            print(f"  p{pno}: {n1}字 → {n2}字")
    else:
        print(f"テキスト層: 全{len(d1)}ページ一致")
    d1.close()
    d2.close()
    return 0 if (same_bytes and not diff) else 1


if __name__ == "__main__":
    sys.exit(main())
