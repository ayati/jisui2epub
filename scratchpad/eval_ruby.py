#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jisui2epub.py出力とGOALテキストのルビペア一致率を測定する評価用スクリプト。
使い方: python eval_ruby.py <got.txt> <goal.txt> <goal内の開始アンカー文字列> [終了アンカー]

GOAL側の正規化:
  - 公式ePub由来のGOALには 中()《なかの》大()《おお》 のような1文字ずつの
    ルビ表記がある（()は青空変換時の名残）。()を除去した上で、隣接する
    親《ルビ》ペアを結合した形（中大《なかのおお》…の全部分列）も正解として
    登録する。jisui2epub.pyの出力は漢字連続にスナップするため、
    蘇我《そが》のようにまとまった単位で出るのに対応する
"""
import re
import sys

RUBY_RE = re.compile(r'([一-鿿々〆〇ヶ]+)《([^《》]+)》')
# 親《ルビ》が隙間なく連続している箇所（1文字ずつルビの本）
ADJ_RE = re.compile(r'(?:[一-鿿々〆〇ヶ]+《[^《》]+》){2,}')


def goal_pairs_with_merges(section):
    """GOAL中のルビペアを抽出し、隣接連続ペアの結合形（全連続部分列）も加える"""
    pairs = RUBY_RE.findall(section)
    merged = []
    for m in ADJ_RE.finditer(section):
        run = RUBY_RE.findall(m.group(0))
        n = len(run)
        for i in range(n):
            for j in range(i + 1, n + 1):
                if j - i == 1:
                    continue  # 単体はpairsに既にある
                parent = ''.join(p for p, _ in run[i:j])
                ruby = ''.join(r for _, r in run[i:j])
                merged.append((parent, ruby))
    return pairs, merged


def main():
    got_path, goal_path, anchor = sys.argv[1:4]
    end_anchor = sys.argv[4] if len(sys.argv) > 4 else None
    with open(got_path, encoding="utf-8") as f:
        got = f.read()
    with open(goal_path, encoding="utf-8") as f:
        goal = f.read()

    goal = goal.replace("()", "")  # 公式ePub由来の1文字ルビの名残を除去

    got_pairs = RUBY_RE.findall(got)

    start = goal.find(anchor)
    if start == -1:
        print(f"アンカー {anchor!r} が見つかりません", file=sys.stderr)
        sys.exit(1)
    if end_anchor:
        end = goal.find(end_anchor, start)
        section = goal[start:end] if end != -1 else goal[start:start + 30000]
    else:
        section = goal[start:start + 30000]

    goal_pairs, goal_merged = goal_pairs_with_merges(section)

    goal_map = {}
    for parent, ruby in goal_pairs + goal_merged:
        goal_map.setdefault(parent, []).append(ruby)

    match = 0
    mismatches = []
    for parent, ruby in got_pairs:
        cand = goal_map.get(parent)
        if cand and ruby in cand:
            match += 1
        else:
            mismatches.append((parent, ruby))

    print(f"got ruby count: {len(got_pairs)}")
    print(f"goal ruby count(該当範囲): {len(goal_pairs)}")
    if got_pairs:
        print(f"一致: {match}/{len(got_pairs)} ({100*match/len(got_pairs):.1f}%)")
    else:
        print("N/A")
    print("不一致例（先頭20件）:")
    for m in mismatches[:20]:
        print(" ", m)


if __name__ == "__main__":
    main()
