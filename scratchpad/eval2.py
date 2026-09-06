#!/usr/bin/env python3
"""eval_book.py の anchors() を厳密な LIS（patience sorting）に差し替えた版。

貪欲な first-fit は、早い位置に紛れ込んだ1個の逆順アンカーで鎖全体が
汚染される（ネットオーディオで実測: gt 3267 の直後に hyp 79695 が採られ、
以降の本文7万字が一切照合されず再現率 6.54% と出た）。
"""
import bisect, sys
sys.path.insert(0, '/home/ayati/jisui2epub/scratchpad')
import eval_book as E


def anchors_lis(gt, hyp, n=12, step=1):
    from collections import Counter
    cg = Counter(gt[i:i + n] for i in range(0, len(gt) - n, step))
    ch = Counter(hyp[i:i + n] for i in range(0, len(hyp) - n, step))
    uniq = {g for g, c in cg.items() if c == 1 and ch.get(g) == 1}
    posg = {gt[i:i + n]: i for i in range(0, len(gt) - n, step) if gt[i:i + n] in uniq}
    pairs = sorted((posg[hyp[j:j + n]], j)
                   for j in range(0, len(hyp) - n, step) if hyp[j:j + n] in posg)
    # j について厳密増加の最長部分列
    tails, tails_idx, parent = [], [], [-1] * len(pairs)
    for k, (i, j) in enumerate(pairs):
        p = bisect.bisect_left(tails, j)
        if p:
            parent[k] = tails_idx[p - 1]
        if p == len(tails):
            tails.append(j); tails_idx.append(k)
        else:
            tails[p] = j; tails_idx[p] = k
    out, k = [], (tails_idx[-1] if tails_idx else -1)
    while k >= 0:
        out.append(pairs[k]); k = parent[k]
    return out[::-1]


E.anchors = anchors_lis
if __name__ == "__main__":
    E.main()
