#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GOAL ePubの文字単位ルビを正解として、jisui2epub出力のルビ親ずれを精密分類する。

分類:
  exact          : 親範囲の文字ごとの読み連結 == gotルビ（境界・読みとも正しい）
  reading_err    : 親範囲は正しいが読みが違う（OCR誤読。親ずれではない）
  under          : 親範囲がgoalのルビ連続範囲より狭い（語頭/語尾の文字が欠落）
  over           : 親範囲がgoalのルビ連続範囲より広い
  shift          : 親範囲がgoalのルビ範囲と部分重複（前後ずれ）
  spurious       : goal側に対応するルビがそもそもない（過剰ルビ）
  unaligned      : 本文の文脈がgoalに見つからず位置合わせ不能
"""
import glob
import html
import re
import sys
import unicodedata

RUBY_TAG_RE = re.compile(r'<ruby>(.*?)</ruby>', re.S)
RT_RE = re.compile(r'<rt>(.*?)</rt>', re.S)
TAG_RE = re.compile(r'<[^>]+>')
AOZORA_NOTE_RE = re.compile(r'［＃[^］]*］')
GOT_RUBY_RE = re.compile(r'(｜?)([一-鿿々〆〇ヶ]+)《([^《》]+)》')


def parse_epub_xhtml(paths):
    """全xhtmlから (plain_text, readings) を作る。
    readings[i] = i番目の文字に付く読み（rt単位。rtが複数文字の親を持つ場合は
    先頭文字にまとめて付け、残りは '' 連結マーカー CONT）"""
    plain = []
    readings = []  # None=ルビなし, (reading, n_chars) を先頭に、続き文字はCONT
    CONT = "\x01"
    for path in paths:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = src.split("<body", 1)[-1]
        pos = 0
        for m in RUBY_TAG_RE.finditer(body):
            seg = TAG_RE.sub("", body[pos:m.start()])
            seg = html.unescape(seg)
            seg = re.sub(r'\s+', '', seg).replace('()', '')
            for ch in seg:
                plain.append(ch)
                readings.append(None)
            # ruby内: base/rt の交互。rtごとに直前のbase文字列に読みを対応
            inner = re.sub(r'<rp>.*?</rp>', '', m.group(1), flags=re.S)
            # rtで分割し、各チャンク末尾のrtがチャンク内のbaseに対応
            parts = re.split(r'(<rt>.*?</rt>)', inner, flags=re.S)
            base_buf = ""
            for part in parts:
                rt_m = RT_RE.fullmatch(part)
                if rt_m:
                    rt = html.unescape(TAG_RE.sub("", rt_m.group(1))).strip()
                    base = re.sub(r'\s+', '', html.unescape(TAG_RE.sub("", base_buf))).replace("()", "")
                    for k, ch in enumerate(base):
                        plain.append(ch)
                        readings.append((rt, len(base)) if k == 0 else CONT)
                    base_buf = ""
                else:
                    base_buf += part
            # rtが無い残りbase
            base = re.sub(r'\s+', '', html.unescape(TAG_RE.sub("", base_buf))).replace("()", "")
            for ch in base:
                plain.append(ch)
                readings.append(None)
            pos = m.end()
        seg = TAG_RE.sub("", body[pos:])
        seg = html.unescape(seg)
        seg = re.sub(r'\s+', '', seg).replace('()', '')
        for ch in seg:
            plain.append(ch)
            readings.append(None)
    return "".join(plain), readings


def reading_of_range(readings, s, e):
    """plain[s:e]の読み連結。範囲がrt境界と食い違えばNoneでなく部分情報を返す"""
    CONT = "\x01"
    parts = []
    clean = True
    i = s
    while i < e:
        r = readings[i]
        if r is None:
            clean = False
            i += 1
        elif r == CONT:
            clean = False  # 範囲の途中からrt親に食い込んでいる
            i += 1
        else:
            rt, n = r
            parts.append(rt)
            if i + n > e:
                clean = False
            i += n
    return "".join(parts), clean


def ruby_run_bounds(readings, s, e):
    """got範囲[s,e)と重なるgoalのルビ連続範囲（rtが付いた文字の連続）を返す"""
    CONT = "\x01"
    def has_ruby(i):
        return 0 <= i < len(readings) and readings[i] is not None
    # 重なり範囲にルビ文字があるか
    anchor = None
    for i in range(s, e):
        if has_ruby(i):
            anchor = i
            break
    if anchor is None:
        # 前後1文字も見る（完全ずれ）
        for i in (s - 1, e):
            if has_ruby(i):
                anchor = i
                break
    if anchor is None:
        return None
    lo = anchor
    while has_ruby(lo - 1):
        lo -= 1
    hi = anchor
    while has_ruby(hi + 1):
        hi += 1
    return lo, hi + 1


def strip_got(text):
    text = AOZORA_NOTE_RE.sub("", text)
    out = []
    rubies = []
    pos = 0
    idx = 0
    for m in GOT_RUBY_RE.finditer(text):
        seg = re.sub(r'[\s｜]+', '', text[pos:m.start()])
        out.append(seg)
        idx += len(seg)
        parent, ruby = m.group(2), m.group(3)
        rubies.append((idx, parent, ruby, m.group(1) == "｜"))
        out.append(parent)
        idx += len(parent)
        pos = m.end()
    seg = re.sub(r'[\s｜]+', '', text[pos:])
    out.append(seg)
    return "".join(out), rubies


def main():
    got_path = sys.argv[1]
    xhtml_glob = sys.argv[2]
    with open(got_path, encoding="utf-8") as f:
        got = f.read()
    paths = sorted(glob.glob(xhtml_glob))
    goal_plain, readings = parse_epub_xhtml(paths)
    got_plain, got_rubies = strip_got(got)

    sys.stderr.write(f"goal plain {len(goal_plain)} chars, "
                     f"got plain {len(got_plain)} chars, "
                     f"got rubies {len(got_rubies)}\n")

    cats = {}
    last = 0
    for start, parent, ruby, had_bar in got_rubies:
        ctx_b = got_plain[max(0, start - 12):start]
        ctx_a = got_plain[start + len(parent):start + len(parent) + 12]
        probe = ctx_b + parent + ctx_a
        pos = goal_plain.find(probe, max(0, last - 3000))
        if pos == -1:
            pos = goal_plain.find(probe)
        if pos == -1:
            p2b = got_plain[max(0, start - 6):start]
            p2a = got_plain[start + len(parent):start + len(parent) + 6]
            probe2 = p2b + parent + p2a
            pos = goal_plain.find(probe2, max(0, last - 3000))
            if pos == -1:
                pos = goal_plain.find(probe2)
            if pos == -1:
                cats.setdefault("unaligned", []).append((parent, ruby))
                continue
            g_s = pos + len(p2b)
        else:
            g_s = pos + len(ctx_b)
        last = g_s
        g_e = g_s + len(parent)

        expected, clean = reading_of_range(readings, g_s, g_e)
        if clean and expected == ruby:
            cats.setdefault("exact", []).append((parent, ruby))
            continue
        bounds = ruby_run_bounds(readings, g_s, g_e)
        if bounds is None:
            cats.setdefault("spurious", []).append((parent, ruby))
            continue
        lo, hi = bounds
        goal_parent = goal_plain[lo:hi]
        goal_reading, _ = reading_of_range(readings, lo, hi)
        if lo == g_s and hi == g_e:
            cats.setdefault("reading_err", []).append(
                (parent, ruby, goal_reading))
        elif lo <= g_s and g_e <= hi:
            cats.setdefault("under", []).append(
                (parent, ruby, goal_parent, goal_reading))
        elif g_s <= lo and hi <= g_e:
            cats.setdefault("over", []).append(
                (parent, ruby, goal_parent, goal_reading))
        else:
            cats.setdefault("shift", []).append(
                (parent, ruby, goal_parent, goal_reading))

    total = len(got_rubies)
    print(f"got ruby total: {total}")
    for cat in ("exact", "reading_err", "under", "over", "shift",
                "spurious", "unaligned"):
        lst = cats.get(cat, [])
        print(f"{cat}: {len(lst)} ({100*len(lst)/total:.1f}%)")
    print()
    import collections
    for cat in ("under", "over", "shift", "spurious"):
        lst = cats.get(cat, [])
        print(f"== {cat} ({len(lst)}件) ==")
        for item in lst[:60]:
            print("  ", item)
        if len(lst) > 60:
            print(f"   ... 他{len(lst)-60}件")
        print()


if __name__ == "__main__":
    main()
