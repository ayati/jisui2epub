#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jisui2epub.py出力とGOALテキストのルビペア一致率を測定する評価用スクリプト。
使い方: python eval_ruby.py <got.txt> <goal.txt> <goal内の開始アンカー文字列>
"""
import re
import sys

RUBY_RE = re.compile(r'([一-鿿々〆〇ヶ]+)《([^《》]+)》')


def main():
    got_path, goal_path, anchor = sys.argv[1:4]
    with open(got_path, encoding="utf-8") as f:
        got = f.read()
    with open(goal_path, encoding="utf-8") as f:
        goal = f.read()

    got_pairs = RUBY_RE.findall(got)

    start = goal.find(anchor)
    if start == -1:
        print(f"アンカー {anchor!r} が見つかりません", file=sys.stderr)
        sys.exit(1)
    section = goal[start:start + 20000]
    goal_pairs = RUBY_RE.findall(section)

    goal_map = {}
    for parent, ruby in goal_pairs:
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
    print(f"一致: {match}/{len(got_pairs)} ({100*match/len(got_pairs):.1f}%)" if got_pairs else "N/A")
    print("不一致例（先頭20件）:")
    for m in mismatches[:20]:
        print(" ", m)


if __name__ == "__main__":
    main()
