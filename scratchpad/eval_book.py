#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jisui2epub の出力 .txt を GOAL ePub と全文照合して採点する probe。

preproc_lab.py の採点はページ単位（前処理レシピの比較用）だったが、こちらは
**本1冊まるごと**を対象にする。全文を difflib に一度に渡すと現実的な時間で
終わらないので、両側に一意に出現する n-gram をアンカーにして単調な区間対に
分割し、区間ごとに SequenceMatcher をかける。

    python scratchpad/eval_book.py <出力.txt> <GOAL.epub> [--profile] [--diff N]

出力:
  本文再現率  = GOAL の文字のうち出力側で一致した割合（主指標）
  挿入率      = 出力側の余分な文字数 / GOAL 長（柱・ノンブル混入の指標）
  ルビ        = 親《読み》ペアの適合率・再現率
本番コードには含めない。
"""
import argparse
import difflib
import html
import re
import sys
import unicodedata
import zipfile

RUBY_PAIR = re.compile(r'([一-鿿々〆〇ヶA-Za-z]+)《([^《》]+)》')
AOZORA_TAG = re.compile(r'［＃[^］]*］')

_PUNCT_MAP = {"―": "—", "─": "—", "‐": "—", "－": "—", "‥": "…",
              "“": "「", "”": "」", "〝": "「", "〟": "」"}


def norm(s):
    """比較用の正規化。字種の揺れ（ダッシュ・引用符）と空白を吸収する。"""
    s = unicodedata.normalize("NFKC", s)
    s = "".join(_PUNCT_MAP.get(c, c) for c in s)
    return re.sub(r"[\s　]", "", s)


# ------------------------------------------------------------------ GOAL 読込

def epub_docs(path):
    """spine 順に XHTML の中身を返す。"""
    z = zipfile.ZipFile(path)
    opf = [n for n in z.namelist() if n.endswith(".opf")]
    docs = []
    if opf:
        s = z.read(opf[0]).decode("utf-8", "replace")
        order = re.findall(r'idref="([^"]+)"', s)
        # **属性の並び順を仮定しないこと**。href が id より前に来る ePub が
        # 実在し（百億の昼と千億の夜）、`id="…"[^>]*href="…"` だと1件も
        # 取れずファイル名順のフォールバックに落ちて章順が崩れる
        # （序章・第一章・第二章が末尾に回り、再現率が97%→73%に見えた）。
        href = {}
        for item in re.findall(r"<item\b[^>]*>", s):
            i = re.search(r'\bid="([^"]+)"', item)
            h = re.search(r'\bhref="([^"]+)"', item)
            if i and h:
                href[i.group(1)] = h.group(1)
        import os
        base = os.path.dirname(opf[0])
        for i in order:
            h = href.get(i)
            if h:
                docs.append(os.path.normpath(
                    os.path.join(base, h)).replace("\\", "/"))
    if not docs:
        docs = sorted(n for n in z.namelist()
                      if n.lower().endswith((".xhtml", ".html", ".htm")))
    out = []
    for d in docs:
        try:
            out.append(z.read(d).decode("utf-8", "replace"))
        except KeyError:
            pass
    return out


def _ruby_cells(inner):
    """<ruby> の中身を [(親片, 読み片), …] に分解する（文字ごとルビ対応）。"""
    cells, base = [], ""
    for tok in re.split(r"(<rt[^>]*>.*?</rt>)", inner, flags=re.S):
        if tok.startswith("<rt"):
            rt = html.unescape(re.sub(r"<[^>]+>", "", tok))
            cells.append((re.sub(r"\s", "", base), re.sub(r"\s", "", rt)))
            base = ""
        else:
            base += html.unescape(re.sub(r"<[^>]+>", "", tok))
    if base.strip():          # 末尾に rt を伴わない親が残ることがある
        cells.append((re.sub(r"\s", "", base), ""))
    return [c for c in cells if c[0]]


def _ruby_repl(m):
    cells = _ruby_cells(m.group(1))
    base = "".join(c[0] for c in cells)
    rt = "".join(c[1] for c in cells)
    return f"{base}《{rt}》" if base and rt else base


def goal_ruby_groups(path):
    """GOAL のルビを「隣接して連なる並び」ごとに [(親片,読み片), …] で返す。

    **粒度は正解側でも割れている**。棘皮《きょくひ》が
    `<ruby>棘<rt>きょく</rt></ruby><ruby>皮<rt>ひ</rt></ruby>` の
    2要素で入っている ePub が実在する（百億の昼と千億の夜）。要素境界で
    切ると、正しくまとめて出した出力（棘皮《きょくひ》）が全部不一致に
    なるので、**本文中で隙間なく連続するルビは1つの並びに束ねる**。
    eval_ruby.py の ADJ_RE と同じ考え方。
    """
    runs, cur, prev_end = [], [], None
    for t in epub_docs(path):
        t = re.sub(r"<head[^>]*>.*?</head>", "", t, flags=re.S | re.I)
        for tag in ("style", "script"):
            t = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", t, flags=re.S | re.I)
        for m in re.finditer(r"<ruby[^>]*>(.*?)</ruby>|([^<]+)|<[^>]+>",
                             t, flags=re.S | re.I):
            if m.group(1) is not None:
                cells = [(norm(a), norm(b))
                         for a, b in _ruby_cells(m.group(1))]
                cells = [c for c in cells if c[0]]
                if not cells or not any(b for _, b in cells):
                    continue
                if prev_end is not None and m.start() == prev_end:
                    cur.extend(cells)
                else:
                    if cur:
                        runs.append(cur)
                    cur = list(cells)
                prev_end = m.end()
            elif m.group(2) is not None and m.group(2).strip():
                prev_end = None      # 地の文が挟まれたら連結を切る
    if cur:
        runs.append(cur)
    return runs


def load_goal(path):
    """GOAL ePub を (ルビ除去本文, [(親, 読み), …]) にする。

    <ruby>親<rt>読み</rt></ruby> を 親《読み》 に畳んでから抽出するので、
    1文字ずつルビを振る ePub でも隣接分は連結された形で拾える。
    """
    parts = []
    for t in epub_docs(path):
        t = re.sub(r"<head[^>]*>.*?</head>", "", t, flags=re.S | re.I)
        for tag in ("style", "script"):
            t = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", t, flags=re.S | re.I)
        t = re.sub(r"<rp[^>]*>.*?</rp>", "", t, flags=re.S)
        # <ruby>…<rt>読み</rt></ruby> → 親《読み》
        # 1つの <ruby> の中に <rt> が複数ある「文字ごとルビ」（平<rt>ひら</rt>
        # 田<rt>た</rt>…）が実在するので、rt を全部つないで1組にする。
        # 親側に改行が入る（`平\r\n 田`）ため空白の除去は必須。
        t = re.sub(r"<ruby[^>]*>(.*?)</ruby>", _ruby_repl, t, flags=re.S | re.I)
        t = re.sub(r"<(p|div|h[1-6]|br)[^>]*>", "\n", t, flags=re.I)
        t = re.sub(r"<[^>]+>", "", t)
        parts.append(html.unescape(t))
    raw = "\n".join(parts)
    return _split_ruby(raw)


def load_got(path):
    """jisui2epub 出力の .txt を (ルビ除去本文, ペア) にする。"""
    s = open(path, encoding="utf-8", errors="replace").read()
    s = AOZORA_TAG.sub("", s)
    return _split_ruby(s)


def _split_ruby(s):
    pairs = [(norm(a), norm(b)) for a, b in RUBY_PAIR.findall(s)]
    s = RUBY_PAIR.sub(lambda m: m.group(1), s)
    s = re.sub(r"《[^《》]*》|[｜|]", "", s)
    return norm(s), pairs


# ------------------------------------------------------------------ 全文照合

def anchors(gt, hyp, n=12, step=1):
    """両側に一意な n-gram の対応表（単調増加のみ残す）。"""
    from collections import Counter
    cg = Counter(gt[i:i + n] for i in range(0, len(gt) - n, step))
    ch = Counter(hyp[i:i + n] for i in range(0, len(hyp) - n, step))
    uniq = {g for g, c in cg.items() if c == 1 and ch.get(g) == 1}
    posg = {gt[i:i + n]: i for i in range(0, len(gt) - n, step)
            if gt[i:i + n] in uniq}
    pairs = sorted((posg[hyp[j:j + n]], j)
                   for j in range(0, len(hyp) - n, step)
                   if hyp[j:j + n] in posg)
    # 単調増加の最長部分列（貪欲。アンカーは多いので厳密でなくてよい）
    keep, last = [], (-1, -1)
    for i, j in pairs:
        if i > last[0] and j > last[1]:
            keep.append((i, j))
            last = (i, j)
    return keep


def score(gt, hyp, seg=4000, profile=False, show_diff=0):
    """区間ごとに SequenceMatcher をかけて (再現率, 挿入率, 誤り内訳) を返す。"""
    ank = anchors(gt, hyp)
    # seg 文字ごとに間引いて区間境界にする
    bounds, last = [(0, 0)], (0, 0)
    for i, j in ank:
        if i - last[0] >= seg:
            bounds.append((i, j))
            last = (i, j)
    bounds.append((len(gt), len(hyp)))
    match, hyp_len, prof, samples = 0, 0, {}, []
    for (i0, j0), (i1, j1) in zip(bounds, bounds[1:]):
        a, b = gt[i0:i1], hyp[j0:j1]
        hyp_len += len(b)
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        match += sum(x.size for x in sm.get_matching_blocks())
        if profile or show_diff:
            for tag, x1, x2, y1, y2 in sm.get_opcodes():
                if tag == "equal":
                    continue
                _bump(prof, tag, a[x1:x2], b[y1:y2])
                if show_diff and len(samples) < show_diff and \
                        (x2 - x1 <= 30 and y2 - y1 <= 30):
                    samples.append((tag, a[max(0, x1 - 8):x1],
                                    a[x1:x2], b[y1:y2]))
    rec = match / len(gt) if gt else 0.0
    ins = (hyp_len - match) / len(gt) if gt else 0.0
    return rec, ins, prof, samples


_SMALL = set("ぁぃぅぇぉっゃゅょァィゥェォッャュョ")


def _bump(prof, tag, a, b):
    def add(k, n=1):
        prof[k] = prof.get(k, 0) + n
    if tag == "delete":
        add("脱落", len(a))
        return
    if tag == "insert":
        add("余分", len(b))
        return
    if len(a) != len(b):
        add("脱落", max(0, len(a) - len(b)))
        add("余分", max(0, len(b) - len(a)))
        add("置換", min(len(a), len(b)))
        return
    for x, y in zip(a, b):
        dx = unicodedata.normalize("NFD", x)
        dy = unicodedata.normalize("NFD", y)
        if dx[0] == dy[0] and dx != dy:
            add("濁点")
        elif (x in _SMALL) != (y in _SMALL):
            add("小書き")
        else:
            add("置換")


# ------------------------------------------------------------------ ルビ採点

def ruby_score(groups, got):
    """ルビを2つの指標で採点する。

    グルーピングの粒度は正解側と出力側で必ずしも一致しない（GOAL が
    平田秀一《ひらたひでかず》の1組、出力が 梅津《うめづ》＋美喜夫《みきお》の
    2組、のように割れる）。そこで:

      適合率 = 出力ペアのうち、GOAL のどこかの**連続する親片の並び**と
               親・読みとも一致したものの割合（＝読んで嘘にならない割合）
      再現率 = GOAL でルビが振られた親文字のうち、正しい出力ペアに
               覆われた文字数の割合（＝どれだけ拾えたか。粒度に依存しない）
    """
    index = {}          # (親, 読み) → [(group_id, i, j), …]
    total_chars = 0
    for gid, cells in enumerate(groups):
        n = len(cells)
        total_chars += sum(len(c[0]) for c in cells)
        for i in range(n):
            # 連結の上限。総ルビの本では隣接ルビが延々とつながり、
            # 全部分列を張ると組合せ爆発する（三国志で実測）
            for j in range(i + 1, min(n, i + 8) + 1):
                key = ("".join(c[0] for c in cells[i:j]),
                       "".join(c[1] for c in cells[i:j]))
                index.setdefault(key, []).append((gid, i, j))
    covered = {}
    hit = 0
    for pair in got:
        cand = index.get(pair)
        if not cand:
            continue
        hit += 1
        for gid, i, j in cand:      # 未使用の出現を1つ消費する
            used = covered.setdefault(gid, set())
            if not used & set(range(i, j)):
                used |= set(range(i, j))
                break
    cov_chars = sum(len(groups[g][i][0]) for g, s in covered.items() for i in s)
    prec = hit / max(1, len(got))
    rec = cov_chars / max(1, total_chars)
    return prec, rec, total_chars, len(got)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("got")
    ap.add_argument("goal")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--diff", type=int, default=0)
    ap.add_argument("--seg", type=int, default=4000)
    a = ap.parse_args()

    gtext, _ = load_goal(a.goal)
    groups = goal_ruby_groups(a.goal)
    htext, hruby = load_got(a.got)
    rec, ins, prof, samples = score(gtext, htext, a.seg, a.profile, a.diff)
    print(f"GOAL {len(gtext):,}字 / 出力 {len(htext):,}字")
    print(f"本文再現率 {rec*100:.2f}%   挿入率 {ins*100:.2f}%")
    if groups or hruby:
        p, r, nc, nh = ruby_score(groups, hruby)
        print(f"ルビ GOAL {len(groups)}組/{nc}字 出力 {nh}組 → "
              f"適合率 {p*100:.1f}%  文字再現率 {r*100:.1f}%")
    if prof:
        tot = sum(prof.values())
        print("誤り内訳: " + " / ".join(
            f"{k} {v}" for k, v in sorted(prof.items(), key=lambda x: -x[1])))
        print(f"          計 {tot}")
    for tag, ctx, a_, b_ in samples:
        print(f"  [{tag}] …{ctx}| GOAL={a_!r} 出力={b_!r}")


if __name__ == "__main__":
    main()
