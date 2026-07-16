#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jisui2epub.py — 自炊PDF（OCRテキスト層付き）→ 青空文庫形式テキスト変換

ScanSnap 等でスキャン+OCR した縦書き小説PDFから本文テキストを抽出し、
  - 天柱・地柱（ページ上下に繰り返し現れる章名・タイトル）の除去
  - ノンブル（ページ番号）の除去
  - ふりがな（ルビ）の検出と 親文字《ルビ》 形式への変換
  - 段落（字下げ）の復元
  - 章見出しの検出（［＃改ページ］+ 見出しマーカー）
を行い、青空文庫形式テキストを出力する。--epub 指定でリフロー型
縦書きePub3も生成する（novel_downloader.py の生成機能を内蔵）。

使い方:
    python3 jisui2epub.py input.pdf
    python3 jisui2epub.py input.pdf -o out.txt --title タイトル --author 著者
    python3 jisui2epub.py input.pdf --pages 10-360 --epub
    python3 jisui2epub.py input.pdf --inspect 12   # ページ構造のデバッグ表示

依存: pip install pymupdf
"""

import argparse
import difflib
import os
import re
import statistics
import sys
import unicodedata
import uuid
import zipfile
from collections import Counter
from datetime import date, datetime, timezone
from html import escape as _esc
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("エラー: PyMuPDF が必要です。 pip install pymupdf", file=sys.stderr)
    sys.exit(1)

# 自動生成表紙のJPEG描画用（任意）。なければ SVG 表紙にフォールバックするが、
# 通常は --cover-page で PDF ページを表紙にするため不要。
try:
    from PIL import Image, ImageDraw, ImageFont
    import io as _io
    _PILLOW_AVAILABLE = True
except ImportError:
    _PILLOW_AVAILABLE = False

# ── 定数 ──────────────────────────────────────────

# ルビが付き得る文字（漢字・繰り返し記号など）
RUBYABLE_RE = re.compile(r'[㐀-鿿豈-﫿々〆ヶ〇]')
# ノンブル（ページ番号）: 半角/全角数字・漢数字のみ
NOMBRE_RE = re.compile(r'^[0-9０-９一二三四五六七八九十百]+$')
# 本文列の端に吸着したノンブル剥がしの対象。漢数字は本文に正当に出るため
# 含めない。半角記号・英字も含めるのは、Visionがノンブルを「1/6」（176）の
# ように誤読することがあり、数字限定だと非数字で剥がしが止まるため。
# 判定は本文領域外の列端に限るので、本文中の正当なASCIIには影響しない
_ARABIC_DIGITS = set("０１２３４５６７８９")


def _nombre_junk_char(ch):
    return ch.isascii() or ch in _ARABIC_DIGITS
# 頻度カウント用に数字を潰す
DIGIT_RE = re.compile(r'[0-9０-９]')

RUBY_SIZE_RATIO = 0.68      # 本文サイズ×この値以下ならルビ
HEADING_SIZE_RATIO = 1.18   # 本文サイズ×この値以上なら見出し候補
HASHIRA_MIN_FREQ = 3        # 同一テキストがこのページ数以上で柱と判定
HEADING_MAX_LEN = 30        # 見出しとして扱う最大文字数
KANA_RE = re.compile(r'[ぁ-ゖァ-ヺー]')


# ── データ構造 ──────────────────────────────────────

class VLine:
    """縦書きの1行（縦の文字列）。cells は (文字, y0, y1) のリスト。"""
    __slots__ = ("cells", "x0", "x1", "y0", "y1", "size")

    def __init__(self):
        self.cells = []
        self.x0 = self.y0 = 1e9
        self.x1 = self.y1 = -1e9
        self.size = 0.0

    def add_span(self, sp):
        x0, y0, x1, y1 = sp["bbox"]
        text = sp["text"]
        n = len(text)
        if n == 0:
            return
        cell_h = (y1 - y0) / n
        for i, ch in enumerate(text):
            self.cells.append((ch, y0 + i * cell_h, y0 + (i + 1) * cell_h))
        self.x0 = min(self.x0, x0)
        self.x1 = max(self.x1, x1)
        self.y0 = min(self.y0, y0)
        self.y1 = max(self.y1, y1)
        self.size = max(self.size, sp["size"])

    def finalize(self):
        self.cells.sort(key=lambda c: c[1])

    @property
    def text(self):
        return "".join(c[0] for c in self.cells)

    @property
    def xc(self):
        return (self.x0 + self.x1) / 2

    def cell_height(self):
        """文字セルの送り（ピッチ）。セルY開始位置の隣接差の中央値を返す。

        インク高さ（c[2]-c[1]）の中央値だと、フォントの字面外マージン分だけ
        実際の文字送りより大きい値になる入力（vision_reocr.py の書き戻しは
        描画メトリクス上インク高さ≈フォントサイズ×1.2）で、attach_rubies の
        Y座標→文字インデックス変換が累積的にずれ、ルビが後方の親文字に
        誤って紐付く（ルビ密度の高い本ではペア一致率が2割以下まで崩壊）。
        1行1スパンの旧OCRではセルは均等分割なので隣接差＝従来値となり
        挙動は変わらない。"""
        if not self.cells:
            return self.size
        if len(self.cells) >= 2:
            diffs = [self.cells[i + 1][1] - self.cells[i][1]
                     for i in range(len(self.cells) - 1)]
            # 濁点分割などで同位置に重なったスパン由来のほぼ0の差は除く
            diffs = [d for d in diffs if d > 0.5]
            if diffs:
                return statistics.median(diffs)
        hs = [c[2] - c[1] for c in self.cells]
        return statistics.median(hs) if hs else self.size


class HLine:
    """横書きの1行（柱・見出し・ノンブルなど）。items は (x0, text, size)。"""
    __slots__ = ("items", "x0", "x1", "y0", "y1", "size")

    def __init__(self):
        self.items = []
        self.x0 = self.y0 = 1e9
        self.x1 = self.y1 = -1e9
        self.size = 0.0

    def add_span(self, sp):
        x0, y0, x1, y1 = sp["bbox"]
        self.items.append((x0, sp["text"], sp["size"]))
        self.x0 = min(self.x0, x0)
        self.x1 = max(self.x1, x1)
        self.y0 = min(self.y0, y0)
        self.y1 = max(self.y1, y1)
        self.size = max(self.size, sp["size"])

    @property
    def text(self):
        return "".join(t for _, t, _ in sorted(self.items, key=lambda i: i[0]))

    @property
    def yc(self):
        return (self.y0 + self.y1) / 2


class RubyRun:
    """ルビの1かたまり。"""
    __slots__ = ("text", "x0", "x1", "y0", "y1")

    def __init__(self, text, x0, y0, x1, y1):
        self.text = text
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    @property
    def xc(self):
        return (self.x0 + self.x1) / 2


class Page:
    __slots__ = ("num", "vlines", "hlines", "rubies", "width", "height")

    def __init__(self, num, width, height):
        self.num = num
        self.width = width
        self.height = height
        self.vlines = []   # 本文候補の縦行（右→左順に後でソート）
        self.hlines = []   # 横書き行（柱・見出し・ノンブル候補）
        self.rubies = []   # RubyRun


# ── PDF 解析 ────────────────────────────────────────

def collect_spans(page):
    """ページから (text, size, bbox) スパンを収集。"""
    spans = []
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                text = s.get("text", "")
                if not text.strip():
                    continue
                spans.append({"text": text.strip("　 "),
                              "size": s["size"], "bbox": s["bbox"]})
    return [s for s in spans if s["text"]]


def detect_body_size(doc, page_range):
    """本文フォントサイズ（文字数重み付き最頻値）を求める。"""
    counter = Counter()
    for i in page_range:
        for sp in collect_spans(doc[i]):
            counter[round(sp["size"] * 2) / 2] += len(sp["text"])
    if not counter:
        return 0.0
    # 最頻サイズの近傍(±15%)をまとめて重み付き平均
    top = counter.most_common(1)[0][0]
    near = {s: c for s, c in counter.items() if abs(s - top) <= top * 0.15}
    total = sum(near.values())
    return sum(s * c for s, c in near.items()) / total


def span_is_vertical(sp):
    x0, y0, x1, y1 = sp["bbox"]
    w, h = x1 - x0, y1 - y0
    n = len(sp["text"])
    if n <= 1:
        return None  # 単独文字は向き不明
    # 縦積みならアスペクト比はほぼ n:1 になる。2文字の場合は句読点や
    # 拗促音で高さが縮むことがあるため閾値を緩める。
    return h > w * (1.0 if n == 2 else 1.5)


def analyze_page(fpage, num, body_size):
    """1ページを解析して Page オブジェクトを返す。"""
    spans = collect_spans(fpage)
    pg = Page(num, fpage.rect.width, fpage.rect.height)
    if not spans:
        return pg

    ruby_max = body_size * RUBY_SIZE_RATIO
    ruby_spans = [s for s in spans if s["size"] <= ruby_max]
    reg_spans = [s for s in spans if s["size"] > ruby_max]

    # ── 縦行クラスタリング（x中心が近いスパンをまとめる）──
    verticals, horizontals, singles = [], [], []
    for sp in reg_spans:
        v = span_is_vertical(sp)
        if v is True:
            verticals.append(sp)
        elif v is False:
            horizontals.append(sp)
        else:
            singles.append(sp)

    # 縦スパンをx中心でクラスタリングして1本の縦行にまとめる。
    # OCRは1列を複数スパンに分割することがある（会話の「！」の後など）。
    # y方向に大きく離れたスパン同士は別の行とみなす。
    cols = []  # [VLine]

    def find_col(xc, tol, y0=None, y1=None):
        best, bd = None, tol
        for vl in cols:
            d = abs(vl.xc - xc)
            if d >= bd:
                continue
            if y0 is not None:
                gap = max(vl.y0 - y1, y0 - vl.y1)
                if gap > body_size * 1.6:
                    continue
            best, bd = vl, d
        return best

    for sp in sorted(verticals, key=lambda s: (round((s["bbox"][0] + s["bbox"][2]) / 2), s["bbox"][1])):
        x0, y0, x1, y1 = sp["bbox"]
        xc = (x0 + x1) / 2
        col = find_col(xc, max(sp["size"] * 0.5, 3.0), y0, y1)
        if col is not None:
            col.add_span(sp)
        else:
            vl = VLine()
            vl.add_span(sp)
            cols.append(vl)

    # 単独文字をx位置でクラスタリング
    singles.sort(key=lambda s: ((s["bbox"][0] + s["bbox"][2]) / 2, s["bbox"][1]))
    pending = []
    for sp in singles:
        xc = (sp["bbox"][0] + sp["bbox"][2]) / 2
        tol = max(sp["size"] * 0.6, 3.0)
        col = find_col(xc, tol, sp["bbox"][1], sp["bbox"][3])
        if col is not None:
            col.add_span(sp)
        else:
            vl = VLine()
            vl.add_span(sp)
            cols.append(vl)
            pending.append(vl)

    # 1文字だけの「縦行」で、周囲の縦行と重ならない横並びのもの（見出しの
    # 章番号など）は横書き行の断片として扱う
    lone = [vl for vl in pending if len(vl.cells) == 1]
    real_singles = []
    for vl in lone:
        cols.remove(vl)
        ch, y0, y1 = vl.cells[0]
        real_singles.append({"text": ch, "size": vl.size,
                             "bbox": (vl.x0, y0, vl.x1, y1)})

    # ── 横書き行クラスタリング（y中心が近いものをまとめる）──
    hl_rows = []
    for sp in horizontals + real_singles:
        yc = (sp["bbox"][1] + sp["bbox"][3]) / 2
        placed = False
        for hl in hl_rows:
            if abs(hl.yc - yc) < max(sp["size"], hl.size) * 0.7:
                hl.add_span(sp)
                placed = True
                break
        if not placed:
            hl = HLine()
            hl.add_span(sp)
            hl_rows.append(hl)

    for vl in cols:
        vl.finalize()
    pg.vlines = sorted(cols, key=lambda v: (-v.xc, v.y0))  # 右→左
    pg.hlines = hl_rows

    # ── ルビのグルーピング ──
    # x中心の近いルビスパンを列にまとめ、y方向のギャップで分割する
    ruby_cols = {}
    for sp in ruby_spans:
        xc = round((sp["bbox"][0] + sp["bbox"][2]) / 2 / 3)  # 3pt格子
        ruby_cols.setdefault(xc, []).append(sp)
    for group in ruby_cols.values():
        group.sort(key=lambda s: s["bbox"][1])
        run = None
        for sp in group:
            x0, y0, x1, y1 = sp["bbox"]
            gap_limit = sp["size"] * 1.7
            if run is not None and y0 - run.y1 <= gap_limit:
                run.text += sp["text"]
                run.x0 = min(run.x0, x0)
                run.x1 = max(run.x1, x1)
                run.y1 = max(run.y1, y1)
            else:
                if run is not None:
                    pg.rubies.append(run)
                run = RubyRun(sp["text"], x0, y0, x1, y1)
        if run is not None:
            pg.rubies.append(run)

    return pg


# ── 挿絵ノイズ判定 ──────────────────────────────────

# 挿絵をOCRすると出てくる記号類（幾何学記号・罫線・半角カナなど）
JUNK_CHAR_RE = re.compile(
    r'[■□◆◇●○◎▲△▼▽★☆＊※÷×↑↓→←〓〒｜|＝=≠≦≧∞∴♂♀♯♭'
    r'§¶〃￣＿∈∋⊆⊇⊂⊃∪∩∧∨￢⇒⇔∀∃∠⊥⌒∂∇≡≒≪≫√∽∝∵∫∬'
    r'ｦ-ﾟ_｀＾~〜ヽヾゝ]')
# 罫線・ダッシュだけの行
DASH_SOUP_RE = re.compile(r'^[一二三＝=ー－\-‐―_｜|’‘、。･・．，"\'\s：；;:]+$')


def valid_heading_item(text):
    """見出しとして意味を成すか（かな・数字を含む、または定型見出し）。
    挿絵ノイズは珍しい漢字の羅列になることが多く、かなを含まない。"""
    t = re.sub(r'[\s　]+', "", text)
    if not t or is_junk_line(t):
        return False
    if KANA_RE.search(t) or re.search(r'[0-9０-９]', t):
        return True
    return bool(re.fullmatch(
        r'第?[0-9０-９一二三四五六七八九十]+(部|章|話|節|編|幕)?|'
        r'(もくじ|目次|序|あとがき|エピローグ|プロローグ)', t))


def dedup_norm_heading(text):
    """見出し重複判定用の正規化（全半角統一・空白記号除去、数字は残す）。"""
    t = unicodedata.normalize("NFKC", text)
    return re.sub(r'[\s()（）.,，。、･・「」『』]', "", t)


def is_junk_line(text):
    """挿絵のOCRノイズ行かどうかを判定する。"""
    t = re.sub(r'[\s　]+', "", text)
    n = len(t)
    if n == 0:
        return True
    if DASH_SOUP_RE.match(t):
        return True
    kana = len(KANA_RE.findall(t))
    digits = len(re.findall(r'[0-9０-９]', t))
    bad = len(JUNK_CHAR_RE.findall(t))
    if bad / n > 0.2:
        return True
    if kana == 0 and digits == 0:
        # かなも数字もない行。見出しの定型（第１部など）以外で
        # 記号を含む、または長い場合は漢字の羅列ノイズとみなす
        if re.fullmatch(r'第?[0-9０-９一二三四五六七八九十]+(部|章|話|節|編|幕)?',
                        t):
            return False
        if bad > 0 or n >= 6:
            return True
    return False


# ── 柱・ノンブル判定 ─────────────────────────────────

_CHAPTER_HEAD_RE = re.compile(
    r'^(第[0-9０-９一二三四五六七八九十百]+[章話部]|序章|終章|プロローグ|エピローグ)')
_CHAPTER_MARK_RE = re.compile(
    r'(第[0-9０-９一二三四五六七八九十百]*[章話部]|序章|終章)')


def _single_chapter_heading(text):
    """章番号で始まり、章マーカーを1つだけ含む（目次ページの複数章題連結を弾く）。"""
    return (bool(_CHAPTER_HEAD_RE.match(text))
            and len(_CHAPTER_MARK_RE.findall(text)) == 1)


def norm_hashira(text):
    """柱の頻度カウント用正規化（数字・空白除去）。
    章番号とタイトルが別スパンに分かれてOCRされることがあるため、
    数字は完全に除去して照合する。"""
    return DIGIT_RE.sub("", re.sub(r'\s+', "", text))


def classify_marginals(pages, body_size):
    """
    全ページを通して柱・ノンブルを特定する。
    戻り値: (drop, headings, body_top, body_bottom, hashira_keys)
      drop     : set of (page_num, id(line)) — 除去すべき行
      headings : {(page_num, id(line)): text} — 見出しとして残す行
      hashira_keys : {正規化テキスト: 出現ページ数} — 柱テキスト一覧
    """
    # 本文領域（縦行の上端・下端の中央値）。
    # Vision再OCR経路ではノンブルの数字が本文サイズで書き戻されるため、
    # ページ端の数字が最寄りの本文列の末尾に吸着していることがある
    # （霧P.24: 本文列が「…うっそうとした木2」になり列下端も約22pt伸びる）。
    # 領域推定に数字セルを含めると本文下端がノンブル位置まで膨らみ、
    # ノンブル行が「本文領域内」と誤判定されて除去に乗らなくなるので、
    # 算用数字以外のセルの範囲で推定する
    tops, bottoms = [], []
    for pg in pages:
        pg_tops, pg_bottoms = [], []
        for v in pg.vlines:
            if len(v.cells) < 3 or v.size < body_size * 0.8:
                continue
            nd = [c for c in v.cells if not _nombre_junk_char(c[0])]
            if nd:
                pg_tops.append(min(c[1] for c in nd))
                pg_bottoms.append(max(c[2] for c in nd))
        if pg_tops:
            tops.append(min(pg_tops))
            bottoms.append(max(pg_bottoms))
    body_top = statistics.median(tops) if tops else 0
    body_bottom = statistics.median(bottoms) if bottoms else 1e9
    cell = body_size

    # 本文列の端に吸着したノンブル数字を剥がす。対象は「数字・ASCII のセル」
    # かつ「y中心が本文領域の外」のものだけ（章見出し先頭の章番号は
    # 本文領域内なので残る）。剥がした結果空になった縦行はページから除く
    for pg in pages:
        kept = []
        for vl in pg.vlines:
            while vl.cells and _nombre_junk_char(vl.cells[-1][0]) and \
                    (vl.cells[-1][1] + vl.cells[-1][2]) / 2 > \
                    body_bottom + cell * 0.3:
                vl.cells.pop()
            while vl.cells and _nombre_junk_char(vl.cells[0][0]) and \
                    (vl.cells[0][1] + vl.cells[0][2]) / 2 < \
                    body_top - cell * 0.3:
                vl.cells.pop(0)
            if vl.cells:
                vl.y0 = min(c[1] for c in vl.cells)
                vl.y1 = max(c[2] for c in vl.cells)
                kept.append(vl)
        pg.vlines = kept

    # 柱・ノンブルはフォントが小さく「ルビ」に誤分類されていることがある。
    # 本文領域の外にあるルビはマージン行（柱・ノンブル候補）に移す。
    for pg in pages:
        keep, migrate = [], []
        for run in pg.rubies:
            ryc = (run.y0 + run.y1) / 2
            if ryc < body_top - cell * 0.7 or ryc > body_bottom + cell * 0.7:
                migrate.append(run)
            else:
                keep.append(run)
        pg.rubies = keep
        for run in migrate:
            hl = HLine()
            hl.add_span({"text": run.text, "size": body_size * 0.5,
                         "bbox": (run.x0, run.y0, run.x1, run.y1)})
            pg.hlines.append(hl)

    # 柱候補の頻度: 横書き行 + 本文領域外の縦行
    freq = Counter()
    candidates = []  # (pg, line, is_vline)
    for pg in pages:
        for hl in pg.hlines:
            candidates.append((pg, hl, False))
            freq[norm_hashira(hl.text)] += 1
        for vl in pg.vlines:
            if vl.y1 < body_top - cell * 0.5 or vl.y0 > body_bottom + cell * 0.5:
                candidates.append((pg, vl, True))
                freq[norm_hashira(vl.text)] += 1

    drop = set()
    headings = {}  # (page_num, id) -> text
    for pg, ln, is_v in candidates:
        text = ln.text.strip()
        key = norm_hashira(text)
        # 行のy中心が本文領域の外なら天/地マージンとみなす
        # （端の座標だと柱・ノンブルが数ptの差で本文側に食い込むことがある）
        lyc = (ln.y0 + ln.y1) / 2
        in_margin = (lyc < body_top - cell * 0.3 or
                     lyc > body_bottom + cell * 0.3)
        if NOMBRE_RE.match(re.sub(r'\s+', "", text)):
            drop.add((pg.num, id(ln)))           # ノンブル
        elif is_junk_line(text):
            drop.add((pg.num, id(ln)))           # 挿絵ノイズ
        elif ln.size >= body_size * HEADING_SIZE_RATIO and not in_margin:
            # 大きな文字 → 見出し。章始めの見出しは柱と同じテキストを
            # 持つため、頻度チェックより先にサイズで判定する
            headings[(pg.num, id(ln))] = text
        elif freq[key] >= HASHIRA_MIN_FREQ:
            drop.add((pg.num, id(ln)))           # 柱（頻出）
        elif in_margin and freq[key] >= 2:
            drop.add((pg.num, id(ln)))           # マージン内で複数回 → 柱
        elif in_margin and ln.size < body_size * 0.95:
            drop.add((pg.num, id(ln)))           # マージン内の小さい文字 → 柱
        elif len(re.sub(r'\s+', "", text)) < 3 and \
                ln.size < body_size * HEADING_SIZE_RATIO:
            drop.add((pg.num, id(ln)))           # 短い断片ノイズ
        elif ln.size >= body_size * 1.05 or in_margin:
            headings[(pg.num, id(ln))] = text    # 残り（章始めの見出し等）
        else:
            # 本文領域内・本文サイズ以下の横書き → 挿絵キャプション等
            drop.add((pg.num, id(ln)))

    # 本文中の章見出し検出用に、柱テキストの一覧も返す。
    # 短い章では柱の出現回数が少ないため閾値は2回。ただしOCRノイズを
    # 除くため、3文字以上でかな・漢字を含むものに限る。
    hashira_keys = {k: c for k, c in freq.items()
                    if c >= 2 and len(k) >= 3 and not NOMBRE_RE.match(k)
                    and re.search(r'[ぁ-ゖァ-ヺー㐀-鿿]', k)}
    return drop, headings, body_top, body_bottom, hashira_keys


# ── 画像ページ（挿絵・口絵・画像主体の章頭）の検出と抽出 ──────────────

def _page_ink_ratio(doc_page):
    """縮小グレースケールレンダリングの暗画素率（画像ページ判定用）。"""
    pix = doc_page.get_pixmap(matrix=fitz.Matrix(0.3, 0.3),
                              colorspace=fitz.csGRAY)
    s = pix.samples
    return sum(1 for b in s if b < 200) / max(len(s), 1)


def classify_image_pages(doc, pages, drop):
    """
    挿絵・口絵・画像主体の章頭ページを検出して page_num の集合を返す。

    判定は2軸:
      1. 本文行がほぼ無い（柱・ノンブル・挿絵ノイズ除去後 2 行以下）
      2. インク率が本文ページの中央値の1.6倍超（白ページ・「上巻完」の
         ような余白ページを除外。紙色の影響は本ごとの較正で吸収する）
    """
    def n_body_lines(pg):
        return sum(1 for v in pg.vlines
                   if v.text.strip() and (pg.num, id(v)) not in drop
                   and not is_junk_line(v.text.strip()))

    candidates = [pg for pg in pages if n_body_lines(pg) <= 4]
    if not candidates:
        return set()
    # 本文ページのインク率ベースライン（サンプル30ページで較正）。
    # 本文行が4行以下しかないページのインクはその1割程度のはずなので、
    # ベースラインの0.7倍を超えるインクがあれば画像とみなせる
    body_pages = [pg for pg in pages if n_body_lines(pg) > 10]
    step = max(1, len(body_pages) // 30)
    base_inks = [_page_ink_ratio(doc[pg.num]) for pg in body_pages[::step][:30]]
    base = statistics.median(base_inks) if base_inks else 0.03
    threshold = max(base * 0.7, 0.018)

    result = set()
    for pg in candidates:
        if _page_ink_ratio(doc[pg.num]) > threshold:
            result.add(pg.num)
    return result


def render_image_page(doc, page_num):
    """
    PDFページを画像ページ用JPEGにレンダリングして bytes を返す。
    外周の白余白はインクのバウンディングボックスで自動トリミングする。
    """
    page = doc[page_num]
    # 縮小グレースケールでインク領域のbboxを求める
    small = page.get_pixmap(matrix=fitz.Matrix(0.3, 0.3),
                            colorspace=fitz.csGRAY)
    w, h, s = small.width, small.height, small.samples
    xs, ys = [], []
    for y in range(h):
        row = s[y * w:(y + 1) * w]
        for x, b in enumerate(row):
            if b < 200:
                xs.append(x)
                ys.append(y)
    if xs:
        pad = max(2, int(min(w, h) * 0.02))
        clip = fitz.Rect(max(0, min(xs) - pad) / 0.3,
                         max(0, min(ys) - pad) / 0.3,
                         min(w, max(xs) + pad) / 0.3,
                         min(h, max(ys) + pad) / 0.3)
    else:
        clip = page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip)
    return pix.tobytes("jpeg", jpg_quality=85)


# ── ルビ挿入 ────────────────────────────────────────

def attach_rubies(pg, body_size, verbose=False):
    """
    ページ内の RubyRun を縦行の文字に対応付ける。
    戻り値: {id(vline): {cell_index: (start, end, ruby_text)}} 的な
    実際は vline ごとの [(i0, i1, ruby)] リスト。
    """
    result = {}  # id(vline) -> list[(i0, i1, ruby_text)]
    for run in pg.rubies:
        # ルビとして意味を成すのはかな中心の文字列のみ（濁点や記号の
        # OCRノイズ・欄外の数字などを除外）
        kana = len(KANA_RE.findall(run.text))
        if kana == 0 or kana * 2 < len(run.text):
            continue
        # 親行: ルビの左隣にある縦行のうち最も近いもの
        best = None
        for vl in pg.vlines:
            if not vl.cells:
                continue
            if vl.xc >= run.xc:          # ルビは親行の右側
                continue
            gap = run.x0 - vl.x1
            if gap > body_size * 1.2 or gap < -(vl.x1 - vl.x0):
                continue
            if run.y1 < vl.y0 - body_size or run.y0 > vl.y1 + body_size:
                continue                  # y方向に重なりなし
            if best is None or vl.xc > best.xc:
                best = vl
        if best is None:
            continue

        cells = best.cells
        ch = best.cell_height() or body_size
        # ルビのy範囲 → 文字インデックス範囲
        yc0 = run.y0 + ch * 0.15
        yc1 = run.y1 - ch * 0.15
        i0 = max(0, min(len(cells) - 1,
                        int((yc0 - cells[0][1]) / ch)))
        i1 = max(0, min(len(cells) - 1,
                        int((yc1 - cells[0][1]) / ch)))
        if i1 < i0:
            i0, i1 = i1, i0

        # 漢字連続部分にスナップ
        def rubyable(i):
            return bool(RUBYABLE_RE.match(cells[i][0]))

        # 範囲内に漢字がなければ近傍±2文字を探す
        if not any(rubyable(i) for i in range(i0, i1 + 1)):
            found = None
            for d in (1, 2):
                if i0 - d >= 0 and rubyable(i0 - d):
                    found = i0 - d
                    break
                if i1 + d < len(cells) and rubyable(i1 + d):
                    found = i1 + d
                    break
            if found is None:
                if verbose:
                    print(f"  [p{pg.num}] ルビ非対応: 《{run.text}》 "
                          f"付近に漢字なし", file=sys.stderr)
                continue
            i0 = i1 = found

        # 端の非漢字を落とす（bbox由来の範囲を信頼し、拡張はしない）
        while i0 <= i1 and not rubyable(i0):
            i0 += 1
        while i1 >= i0 and not rubyable(i1):
            i1 -= 1
        if i1 < i0:
            continue

        result.setdefault(id(best), []).append((i0, i1, run.text))

    # 同一行内で親文字範囲が重複、または1文字（漢字）を挟んで隣接する
    # ルビを統合する（OCRが「やし」→「や」「し」のように分割する対策）
    for key, lst in result.items():
        lst.sort()
        # 対応する縦行を探す
        vl = next((v for v in pg.vlines if id(v) == key), None)
        merged = []
        for i0, i1, txt in lst:
            if merged:
                p0, p1, ptxt = merged[-1]
                gap_ok = (i0 - p1 <= 2 and
                          all(RUBYABLE_RE.match(vl.cells[j][0])
                              for j in range(p1 + 1, i0)) if vl else False)
                if i0 <= p1 + 1 or gap_ok:
                    merged[-1] = (p0, max(p1, i1), ptxt + txt)
                    continue
            merged.append((i0, i1, txt))
        result[key] = merged
    return result


def render_vline(vl, ruby_map):
    """縦行1本を、ルビを埋め込んだ文字列にする。"""
    cells = vl.cells
    rubies = sorted(ruby_map.get(id(vl), []))
    out = []
    ri = 0
    i = 0
    while i < len(cells):
        if ri < len(rubies) and rubies[ri][0] == i:
            i0, i1, rtxt = rubies[ri]
            base = "".join(c[0] for c in cells[i0:i1 + 1])
            # 直前が漢字なら｜で親文字の始まりを明示
            need_bar = (i0 > 0 and RUBYABLE_RE.match(cells[i0 - 1][0]))
            out.append(("｜" if need_bar else "") + base + "《" + rtxt + "》")
            i = i1 + 1
            ri += 1
        else:
            # 過ぎてしまったルビはスキップ
            while ri < len(rubies) and rubies[ri][0] < i:
                ri += 1
            if ri < len(rubies) and rubies[ri][0] == i:
                continue
            out.append(cells[i][0])
            i += 1
    return "".join(out)


# ── テキスト組み立て ──────────────────────────────────

def assemble_text(pages, drop, headings, body_size, body_top, body_bottom,
                  hashira_keys=None, indent=True, verbose=False,
                  image_pages=None):
    """全ページから青空文庫形式の本文を組み立てる。
    image_pages: {page_num: 画像ファイル名} — 図タグとして挿入するページ
    """
    hashira_keys = hashira_keys or {}
    image_pages = image_pages or {}
    out = []             # 段落のリスト（文字列）
    cur = ""             # 組み立て中の段落
    prev_line_short = True   # 直前の行が途中で終わった（段落末尾）か
    first_heading_done = False

    last_heading_norm = None
    paras_since_heading = 99
    junk_count = [0]

    def flush():
        nonlocal cur, paras_since_heading
        if cur.strip():
            out.append(("　" if indent else "") + cur)
            paras_since_heading += 1
        cur = ""

    def emit_pagebreak():
        """意図的な改ページを発行する。文書冒頭・改ページ直後は重複させない。"""
        flush()
        last = next((x for x in reversed(out) if x.strip()), None)
        if last is None or last == "［＃改ページ］":
            return
        out.append("［＃改ページ］")

    def emit_heading(title):
        nonlocal first_heading_done, prev_line_short
        nonlocal last_heading_norm, paras_since_heading
        flush()
        title = re.sub(r'\s+', "　", title.strip())
        # 章の扉ページと本文ページの両方に同じ章題が現れることがある
        # → 章番号（数字）が同じで本文がまだ短ければ重複とみなし排除。
        #   数字が違えば「戦い(1)」「戦い(2)」のような連続章なので残す。
        norm = dedup_norm_heading(title)
        digits = "".join(re.findall(r'\d', norm))
        if paras_since_heading <= 15 and last_heading_norm:
            l_norm, l_digits = last_heading_norm
            # OCRの数字誤読（(3)→(31 等）があるため前方一致で比較
            digits_match = (digits == l_digits or
                            (digits and l_digits and
                             (digits.startswith(l_digits) or
                              l_digits.startswith(digits))))
            if digits_match and (
                    norm == l_norm or
                    difflib.SequenceMatcher(
                        None, norm, l_norm).ratio() >= 0.75):
                return
        if out or first_heading_done:
            emit_pagebreak()
        out.append(f"［＃「{title}」は中見出し］")
        out.append(title)
        out.append(f"［＃「{title}」は中見出し終わり］")
        out.append("")
        first_heading_done = True
        prev_line_short = True
        last_heading_norm = (norm, digits)
        paras_since_heading = 0

    body_height = max(body_bottom - body_top, body_size)

    # 意図的な改ページの検出準備: 縦書きは右→左に行が詰まるため、
    # 「ページ最左行が本文左端よりだいぶ手前で終わる」＝章末・章扉・目次などの
    # 意図的な改ページと判定できる。本文左端は全ページの最左行位置の下位25%点
    # （大半のページは左端まで詰まる想定）、行送りは行間隔の中央値で推定する。
    page_min_x = []
    gaps = []
    for pg in pages:
        xs = sorted((v.xc for v in pg.vlines
                     if (pg.num, id(v)) not in drop and v.text.strip()
                     and not is_junk_line(v.text.strip())), reverse=True)
        if xs:
            page_min_x.append(xs[-1])
        gaps.extend(a - b for a, b in zip(xs, xs[1:])
                    if 0 < a - b < body_size * 4)
    line_pitch = statistics.median(gaps) if gaps else body_size * 1.75
    left_edge = sorted(page_min_x)[len(page_min_x) // 4] if page_min_x else 0.0
    prev_page_short = False   # 直前の本文ページが左端まで達せず終わったか

    for pg in pages:
        vlines = [v for v in pg.vlines if (pg.num, id(v)) not in drop]
        hls = [h for h in pg.hlines if (pg.num, id(h)) not in drop]

        # 柱テキストと同文の縦行（章の始まりの見出し）を数える。
        # 見出しと柱でOCR結果が微妙に異なることがあるためあいまい一致。
        def matches_hashira(text):
            key = norm_hashira(text)
            if len(key) < 3:
                return False
            if key in hashira_keys:
                return True
            return any(difflib.SequenceMatcher(None, key, k).ratio() >= 0.66
                       for k in hashira_keys)

        hashira_match = [v for v in vlines
                         if 0 < len(v.cells) <= HEADING_MAX_LEN
                         and (v.y1 - v.y0) < body_height * 0.75
                         and matches_hashira(v.text)]
        # 3行以上が柱テキストに一致するページは目次とみなし見出し化しない
        toc_like = len(hashira_match) >= 3

        # 本文領域内の縦行だけを本文として扱う
        n_textlines = sum(1 for v in vlines if v.text.strip())
        body_lines = []
        heading_lines = []   # (order_key, text) 見出しとして扱う縦行
        for v in vlines:
            text = v.text.strip()
            if not text:
                continue
            if is_junk_line(text):
                junk_count[0] += 1
                continue
            # 見出しは天から下がった位置（字下げ）から始まる。天付きで
            # 始まる行は前ページからの段落続き断片なので見出しにしない。
            indented = v.y0 > body_top + body_size * 0.6
            # 大きな文字の見出し。OCRのサイズ揺れによる誤検出を避けるため、
            # 行が短く（本文領域の7割未満）、段落末尾の断片らしくないこと。
            is_big = (v.size >= body_size * HEADING_SIZE_RATIO and
                      len(v.cells) <= HEADING_MAX_LEN and
                      (v.y1 - v.y0) < body_height * 0.7 and
                      indented and
                      (len(v.cells) >= 4 or n_textlines <= 3) and
                      text[-1] not in "。」！？、）』…")
            # 章見出しは通常の段落字下げ（1字）より深く下がっている
            deep_indented = v.y0 > body_top + body_size * 1.5
            is_hashira_head = (not toc_like and v in hashira_match and
                               deep_indented and
                               v is vlines[0])  # ページ右端の行のみ
            if is_big or is_hashira_head:
                heading_lines.append((-v.xc, v.text))
            else:
                body_lines.append(v)

        # 横書きの見出し（柱でないもの）
        h_headings = []
        long_items = []      # 長すぎて見出しにできないもの → 本文段落
        for h in hls:
            txt = headings.get((pg.num, id(h)))
            if txt:
                h_headings.append((h.yc, txt))

        page_headings = ([t for _, t in sorted(h_headings)] +
                         [t for _, t in sorted(heading_lines)])
        page_headings = [t for t in page_headings if valid_heading_item(t)]
        # 長すぎる「見出し」（表紙・帯・奥付などの誤検出）は本文段落に落とす
        long_items = [t for t in page_headings if len(t) > HEADING_MAX_LEN]
        page_headings = [t for t in page_headings if len(t) <= HEADING_MAX_LEN]

        # ── 画像ページ（挿絵・口絵・章頭）: 図タグを改ページに挟んで挿入 ──
        # 章頭ページの見出しテキストは目次のため先に発行する
        # （改ページ → 見出し → 画像 → 本文 の順）。
        # ただし口絵・目次ページの飾り文字が大活字見出しに誤検出されやすいため、
        # 画像ページ上の見出しは柱一致（＝実際の章題）か単独の章番号パターンに
        # 限定する。どちらも無ければページ内の縦行から柱一致テキストを探す
        # （章扉の飾り書体はOCRが崩れやすく、見出し判定から漏れることがある）
        if pg.num in image_pages:
            emit_pagebreak()
            kept = [t for t in page_headings
                    if matches_hashira(t) or _single_chapter_heading(t)]
            if not kept:
                for v in vlines:
                    t = v.text.strip()
                    if (t and not is_junk_line(t)
                            and len(t) <= HEADING_MAX_LEN
                            and matches_hashira(t)):
                        kept = [t]
                        break
            if kept:
                emit_heading("　".join(kept))
            flush()
            out.append(f"［＃「挿絵」の図（{image_pages[pg.num]}）入る］")
            prev_line_short = True
            prev_page_short = True   # 画像ページの後は必ず改ページ
            continue

        # 直前の本文ページが途中で終わっていたら意図的な改ページを反映する。
        # ただし最終行がページ下端まで達していた場合（prev_line_short=False）は
        # 段落が継続中＝挿絵などでページ左側が空いただけなので改ページしない。
        # （見出しページ自身の改ページは emit_heading 側で重複なく処理される）
        if page_headings or long_items or body_lines:
            if prev_page_short and prev_line_short:
                emit_pagebreak()
            prev_page_short = False

        if page_headings:
            emit_heading("　".join(page_headings))
        for t in long_items:
            flush()
            out.append(("　" if indent else "") + t)

        if not body_lines:
            if page_headings or long_items:
                # 章扉など本文のないページ → 次の本文の前で改ページする
                prev_page_short = True
            # 白ページ・挿絵のみのページは判定を持ち越す（段落継続を壊さない）
            continue

        # ルビ対応付け（このページ全体）
        ruby_map = attach_rubies(pg, body_size, verbose=verbose)

        page_top = min(v.y0 for v in body_lines)
        # 極端に見出しの下から始まるページは自身のtopを使う
        eff_top = max(body_top, page_top) if page_top < body_top + body_size * 3 \
            else page_top

        for v in body_lines:
            cell = v.cell_height() or body_size
            indent_depth = v.y0 - eff_top
            starts_indented = indent_depth > cell * 0.55
            ends_short = v.y1 < body_bottom - cell * 1.3

            if starts_indented or prev_line_short:
                flush()
            cur += render_vline(v, ruby_map)
            prev_line_short = ends_short
        # ページ末尾: 段落継続は次ページに持ち越す。
        # 最左行が本文左端より行送り2.5本分以上手前なら意図的な改ページ
        prev_page_short = (min(v.xc for v in body_lines) - left_edge
                           >= line_pitch * 2.5)

    flush()
    _demote_image_only_headings(out)
    if junk_count[0]:
        print(f"挿絵ノイズ除去: {junk_count[0]} 行")
    return "\n".join(out)


_MIDASHI_LINE_RE = re.compile(r"^［＃「(.*)」は中見出し(終わり)?］$")


def _demote_image_only_headings(out):
    """
    本文が無く画像だけのセクションの見出しを、後方に類似見出しがある場合に
    取り消す（目次・口絵ページの飾り文字が章見出しに誤検出され、実際の
    章頭見出しと二重になるのを防ぐ。章扉→本文の正しい見出しは残る）。
    out をインプレースで書き換える。
    """
    # 見出しブロック開始位置と見出しテキストを収集
    heads = []   # (開始index, テキスト)
    for i, ln in enumerate(out):
        m = _MIDASHI_LINE_RE.match(ln)
        if m and not m.group(2):
            heads.append((i, m.group(1)))

    drop_idx = []
    for n, (i, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(out)
        # セクション内の本文量を数える（見出しタイトル表示行は本文でない）。
        # 目次・口絵ページのOCR断片が数文字混じることがあるため、
        # 合計8字以下なら本文なしとみなす
        body_chars = 0
        for ln in out[i:end]:
            s = ln.strip()
            if (not s or s == "［＃改ページ］" or _MIDASHI_LINE_RE.match(s)
                    or s == title.strip()
                    or _FIG_PLAIN_RE.fullmatch(s) or _FIG_CAP_RE.fullmatch(s)):
                continue
            body_chars += len(s)
        if body_chars > 8:
            continue
        # 複数の章マーカーを含む見出し＝目次ページの章題連結 → 無条件で取り消す
        if len(_CHAPTER_MARK_RE.findall(title)) >= 2:
            drop_idx.append((i, title))
            continue
        # 記号が3割を超える見出し＝口絵・帯の飾りノイズ → 無条件で取り消す
        core = re.sub(r'\s', '', title)
        syms = len(re.findall(
            r'[^ぁ-ゖァ-ヺー㐀-鿿々〆〇0-9０-９a-zA-Zａ-ｚＡ-Ｚ]', core))
        if core and syms / len(core) > 0.3:
            drop_idx.append((i, title))
            continue
        # 後方に類似見出しがあるか（漢数字も章番号として比較する）
        norm = dedup_norm_heading(title)
        digits = "".join(re.findall(r'[0-9０-９一二三四五六七八九十百]', norm))
        for _, later in heads[n + 1:]:
            l_norm = dedup_norm_heading(later)
            l_digits = "".join(
                re.findall(r'[0-9０-９一二三四五六七八九十百]', l_norm))
            contained = len(norm) >= 4 and norm in l_norm
            # 数字（漢数字含む）の厳格比較は取り消し候補側に数字がある場合のみ
            # （(1)(2)(3)のような連続章の誤取り消し防止）。数字が読めていない
            # 目次由来の見出しは類似度・包含だけで判定する
            if digits and digits != l_digits:
                continue
            # 縦書き目次のOCRは読み順が崩れるため、文字集合の重なりでも判定
            _ca, _cb = Counter(norm), Counter(l_norm)
            char_overlap = (sum((_ca & _cb).values())
                            / max(min(len(norm), len(l_norm)), 1)
                            if min(len(norm), len(l_norm)) >= 5 else 0.0)
            if (norm == l_norm or contained or char_overlap >= 0.75
                    or difflib.SequenceMatcher(
                        None, norm, l_norm).ratio() >= 0.70):
                drop_idx.append((i, title))
                break

    # 見出しブロック（開始・タイトル行・終わり・直後の空行）を後ろから除去
    for i, title in reversed(drop_idx):
        j = i
        end = min(i + 4, len(out))
        block = []
        for k in range(i, end):
            s = out[k].strip()
            if (_MIDASHI_LINE_RE.match(s) or s == title.strip()
                    or (not s and k > i)):
                block.append(k)
            else:
                break
        for k in reversed(block):
            del out[k]


# ── 紙の目次ページを章題辞書として使う見出し精錬 ──────────────
#
# 章頭の見出し検出は「位置」は正確だが「表記」が弱い: 縦中横の2桁章番号は
# ほぼ確実に化け（11→ｕ、12→胆、17→Ⅳ。ほんものの魔法使で20章中9章）、
# 章題自体の誤字（ジェイン→ジエガイン）や章の取りこぼし（訳者あとがき）も
# 起きる。一方、前付けの目次ページは章題部分のOCRがきれい（同書で全章正確）
# だが、読み順が崩れて章番号と題の対応が取れず、ページ数の列はジャンク化
# するため、目次ページから直接目次を生成することはできない。
# → 本文の見出し（位置）を主、目次ページ（表記）を辞書として突き合わせる。
#
# 処理: (1)目次セグメント特定（見出しへのあいまい一致3行以上、または
# 目次ラベル＋2行以上） (2)見出し⇔エントリのスコア降順貪欲マッチ。見出し
# 先頭の1〜2文字をスキップした方が良く一致すれば化けた章番号とみなす
# (3)表記が食い違えば柱テキストに近い方を採用（目次側も誤る: マジェイァ）
# (4)章番号の連番再構成（正しく読めた番号をアンカーに、スロット数が合う
# 区間のみ内挿。末尾方向は番号の証拠か目次一致がある見出しに限り外挿し、
# あとがき類で打ち切る） (5)目次にあって見出しに無い章を改ページ直後の
# 本文行から回収 (6)書名と同一の見出し（扉）を目次に無い場合のみ降格
# （書名と同名の表題章がある本を守る） (7)目次ページ本文の除去
# （ePubではnav目次が役割を代替する）

_TOC_LABEL_RE = re.compile(r'^[　\s]*(目\s*次|もくじ|くじ)[　\s]*$')
# 番号を振らない見出し（前後付け）。連番の末尾外挿はここで打ち切る
_UNNUMBERED_RE = re.compile(
    r'^(序[章文]?|プロローグ|エピローグ|まえがき|はじめに)$'
    r'|あとがき|解説|おわりに|訳者|参考文献|初出|謝辞|付録|年表|索引')
_LEADNUM_RE = re.compile(r'^(第?)([0-9０-９]{1,3})([章話部]|[　\s])?')
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")


def _heading_fuzzy_score(a, b):
    """見出しと目次エントリの類似度。縦書き目次は読み順が崩れることがある
    ため、difflib比と文字集合オーバーラップの大きい方を使う。"""
    if not a or not b:
        return 0.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    if min(len(a), len(b)) >= 4:
        ca, cb = Counter(a), Counter(b)
        r = max(r, sum((ca & cb).values()) / min(len(a), len(b)))
    return r


def _toc_entry_of_line(line):
    """目次セグメントの1行を章題エントリに正規化する。エントリでない行は
    None。ルビ（ページ数の巻き込み等ジャンクが多い）と末尾に付いた
    漢数字・数字のページ番号を落とす。"""
    s = re.sub(r'《[^《》]*》|[｜]', '', line).strip("　 \t")
    if (not s or s.startswith("［＃") or _TOC_LABEL_RE.match(s)
            or is_junk_line(s)):
        return None
    # 末尾のページ番号（漢数字・数字の連なり）を除去。
    # 「一にお芝居」のような先頭の漢数字は残る
    s = re.sub(r'[0-9０-９一二三四五六七八九十百千〇=＝・．\s　]+$', '', s)
    # 数字を除いてかな・漢字を2文字以上含まない行はページ数列・ノイズ
    core = re.sub(r'[0-9０-９一二三四五六七八九十百千〇\s　]', '', s)
    if len(core) < 2 or len(s) > HEADING_MAX_LEN:
        return None
    return s


def _norm_t(s):
    return re.sub(r'[\s　]', '', s)


def refine_headings_with_toc(body, book_title, hashira_keys=None,
                             verbose=False):
    """紙の目次ページと本文見出しを突き合わせて見出しを精錬した本文と
    処理統計を返す。目次ページが見つからない本でも章番号の内挿だけは行う。"""
    hashira_keys = hashira_keys or {}
    out = body.split("\n")

    heads = [i for i, ln in enumerate(out)
             if (m := _MIDASHI_LINE_RE.match(ln)) and not m.group(2)]
    if not heads:
        return body, {}

    def head_title(i):
        return _MIDASHI_LINE_RE.match(out[i]).group(1)

    stats = {}

    # ── 1. 目次セグメントの特定（改ページ・見出しブロックで区切る）──
    # 誤検出は本文の大量削除に直結するため、条件は保守的に:
    #   - 前付けにあること（本文の先頭15%以内 かつ 4番目の見出しより前）。
    #     詩集の詩行・巻末広告など「見出しに似た短行の多いページ」が
    #     本文中に現れても目次と誤認しない（書を捨てよ・霧で実測した事故）
    #   - 一致判定は difflib 比0.75以上（文字集合オーバーラップは短い行が
    #     多い総ルビ本の本文に誤ヒットするためセグメント判定には使わない）
    #   - セグメント数の上限3。超えたら判定自体が疑わしいので照合を中止
    titles_norm = [_norm_t(head_title(i)) for i in heads]
    head_set = set(heads)
    segments = []
    seg_start = 0
    for i in range(len(out) + 1):
        ln = out[i] if i < len(out) else "［＃改ページ］"
        if ln.strip() == "［＃改ページ］" or i in head_set:
            if i > seg_start:
                segments.append((seg_start, i))
            seg_start = i if i in head_set else i + 1
    front_limit = min(int(len(out) * 0.15),
                      heads[3] if len(heads) > 3 else len(out))
    toc_spans = []
    toc_entries = []
    for a, b in segments:
        if a > front_limit:
            continue
        entries = []
        has_label = False
        contains_heading = False
        for j in range(a, b):
            if _MIDASHI_LINE_RE.match(out[j]):
                contains_heading = True
                break
            if _TOC_LABEL_RE.match(out[j]):
                has_label = True
                continue
            e = _toc_entry_of_line(out[j])
            if e:
                entries.append(e)
        if contains_heading or not entries:
            continue
        n_match = sum(1 for e in entries
                      if any(difflib.SequenceMatcher(
                          None, _norm_t(e), t).ratio() >= 0.75
                          for t in titles_norm))
        if n_match >= 3 or (has_label and n_match >= 2):
            toc_spans.append((a, b))
            toc_entries.extend(entries)
    if len(toc_spans) > 3:
        toc_spans = []

    if not toc_spans:
        n = _renumber_headings(out, heads, {})
        if n:
            stats["章番号再構成"] = n
        return "\n".join(out), stats
    stats["目次ページ"] = len(toc_spans)

    # ── 2. 見出し⇔エントリの対応付け（スコア降順の貪欲マッチ）──
    cands = []
    for hi, i in enumerate(heads):
        t = _norm_t(head_title(i))
        for ei, e in enumerate(toc_entries):
            en = _norm_t(e)
            for skip in (0, 1, 2):
                if len(t) - skip < 2:
                    break
                sc = _heading_fuzzy_score(t[skip:], en)
                if sc >= 0.70:
                    cands.append((sc, -skip, hi, ei))
    cands.sort(reverse=True)
    matched = {}          # hi -> (エントリ文字列, skip)
    used_ei = set()
    for sc, nskip, hi, ei in cands:
        if hi in matched or ei in used_ei:
            continue
        matched[hi] = (toc_entries[ei], -nskip)
        used_ei.add(ei)

    # ── 3. 表記の修復（柱を審判に、目次エントリと見出しの良い方を採用）──
    def hashira_vote(a, b):
        """柱テキスト群に近いのは a か b か。同点なら a（現状維持）。"""
        best_a = max((difflib.SequenceMatcher(None, norm_hashira(a), k).ratio()
                      for k in hashira_keys), default=0.0)
        best_b = max((difflib.SequenceMatcher(None, norm_hashira(b), k).ratio()
                      for k in hashira_keys), default=0.0)
        return a if best_a >= best_b else b

    info = {}   # 行index -> (番号なし章題, 番号があった証拠, 目次一致)
    n_repair = 0
    for hi, i in enumerate(heads):
        title = head_title(i)
        m = _LEADNUM_RE.match(title)
        lead_num = m is not None
        rest = title[m.end():] if m else title
        evidence = lead_num
        if hi in matched:
            e, skip = matched[hi]
            if skip and not lead_num:
                evidence = True       # 化けた番号をスキップして一致した
                rest = title[skip:]
            # 目次エントリ側にも章番号が付いている本（グリック等）があるため、
            # エントリの先頭番号を剥がしてから比較・採用する（剥がさないと
            # 「第１部第１部」「６　６ドブネズミ…」のような二重番号になる）
            me = _LEADNUM_RE.match(e)
            e_core = e[me.end():].strip("　 ") if me else e
            if e_core and _norm_t(rest) != _norm_t(e_core):
                fixed = hashira_vote(rest, e_core)
                if _norm_t(fixed) != _norm_t(rest):
                    n_repair += 1
                rest = fixed
        info[i] = (rest.strip("　 "), evidence, hi in matched)
    if n_repair:
        stats["章題修復"] = n_repair

    # ── 6. 書名と同一の見出しの降格（目次に載っていない場合のみ）──
    title_norm = _norm_t(book_title or "")
    demote = set()
    if title_norm:
        for hi, i in enumerate(heads):
            if hi in matched:
                continue
            if _heading_fuzzy_score(_norm_t(info[i][0]), title_norm) >= 0.85:
                demote.add(i)
    if demote:
        stats["扉見出し降格"] = len(demote)

    # ── 5. 欠落章の検出（改ページ直後の本文行から。目次ページ内は除く）──
    # 連番の再構成より先に検出だけ行い、あとがき類が回収された場合に
    # 末尾方向の外挿がそこで正しく打ち切られるようにする
    in_toc = set()
    for a, b in toc_spans:
        in_toc.update(range(a, b))
    # 回収は本文行の見出し昇格＝誤ると本文構造を壊すため、判定は
    # difflib比0.85以上（オーバーラップ不使用）・回収数は5件まで
    used_texts = {e for e, _ in matched.values()}
    recovered = []
    for e in toc_entries:
        if len(recovered) >= 5:
            break
        if e in used_texts or len(_norm_t(e)) < 4:
            continue
        en = _norm_t(e)
        for j, ln in enumerate(out):
            if j in in_toc or j in head_set:
                continue
            s = ln.strip("　 ")
            if not s or s.startswith("［＃"):
                continue
            prev = [x for x in out[max(0, j - 3):j] if x.strip()]
            if not prev or prev[-1].strip() != "［＃改ページ］":
                continue
            if difflib.SequenceMatcher(None, _norm_t(s), en).ratio() >= 0.85:
                recovered.append((j, e))
                used_texts.add(e)
                break

    # ── 4. 章番号の再構成＋修復章題の反映 ──
    keep_heads = [i for i in heads if i not in demote]
    n_renum = _renumber_headings(out, keep_heads, info,
                                 breaks=[j for j, _ in recovered])
    if n_renum:
        stats["章番号再構成"] = n_renum

    for j, e in recovered:
        out[j] = (f"［＃「{e}」は中見出し］\n{e}\n"
                  f"［＃「{e}」は中見出し終わり］")
    if recovered:
        stats["欠落章回収"] = len(recovered)

    # ── 6b/7. 扉見出しのマーカー除去・目次ページ本文の除去 ──
    for i in demote:
        for k in range(i, min(i + 4, len(out))):
            if _MIDASHI_LINE_RE.match(out[k]):
                out[k] = None
    for a, b in toc_spans:
        for k in range(a, b):
            if out[k] is None:
                continue
            s = out[k].strip()
            if _FIG_PLAIN_RE.fullmatch(s) or _FIG_CAP_RE.fullmatch(s):
                continue   # 図タグは残す（目次ページが画像化された場合）
            out[k] = None

    out = [ln for ln in out if ln is not None]
    cleaned = []
    for ln in out:
        if (ln.strip() == "［＃改ページ］" and cleaned
                and cleaned[-1].strip() == "［＃改ページ］"):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned), stats


def _renumber_headings(out, head_idx, info, breaks=()):
    """見出しの章番号を連番で再構成し、修復済み章題も反映する
    （outをインプレースで書き換え、変更した見出し数を返す）。
    info が空の場合（目次なし）は見出し行から番号だけ解析して内挿する。
    breaks: これから見出しに昇格する行index（欠落章の回収位置）。連番の
    切れ目として扱い、内挿・外挿はここをまたがない。"""
    chapters = []   # [行index, 章題, 番号orNone, 書式, 証拠, 目次一致, 番号対象外]
    for i in sorted(set(head_idx) | set(breaks)):
        if i in head_idx:
            m = _MIDASHI_LINE_RE.match(out[i])
            if not m:
                continue
            title = m.group(1)
            virtual = False
        else:
            title = out[i].strip("　 ")
            virtual = True
        mnum = _LEADNUM_RE.match(title)
        num = int(mnum.group(2).translate(_ZEN2HAN)) if mnum else None
        fmt = (mnum.group(1) or "", mnum.group(3) or "") if mnum else ("", "")
        if virtual:
            rest, evidence, toc_ok = title, False, False
        elif info:
            rest, evidence, toc_ok = info.get(i, (title, False, False))
        else:
            rest = title[mnum.end():].strip("　 ") if mnum else title
            evidence, toc_ok = num is not None, False
        # 「第一章」等の漢数字番号付き・あとがき類・回収予定行は番号対象外
        no_num = bool(virtual or _UNNUMBERED_RE.search(rest[:10])
                      or _CHAPTER_HEAD_RE.match(rest))
        chapters.append([i, rest, num, fmt, evidence, toc_ok, no_num, virtual])

    anchors = []    # (chapters内位置, 番号)
    for pos, c in enumerate(chapters):
        if c[2] is not None and not c[6]:
            if not anchors or c[2] > anchors[-1][1]:
                anchors.append((pos, c[2]))

    assign = {}
    if len(anchors) >= 3:
        def span_ok(lo, hi):
            return all(not chapters[k][6] for k in range(lo, hi))
        # 先頭方向: スロット数が番号とちょうど合う場合のみ
        p0, n0 = anchors[0]
        if n0 - 1 == p0 and span_ok(0, p0):
            for k in range(p0):
                assign[k] = n0 - (p0 - k)
        # アンカー間: スロット数が一致する場合のみ内挿
        for (pa, na), (pb, nb) in zip(anchors, anchors[1:]):
            if nb - na == pb - pa and span_ok(pa + 1, pb):
                for k in range(pa + 1, pb):
                    assign[k] = na + (k - pa)
        # 末尾方向: 番号の証拠か目次一致がある見出しに限り外挿し、
        # あとがき類・回収章（番号対象外）で打ち切る
        pk, nk = anchors[-1]
        nxt = nk
        for k in range(pk + 1, len(chapters)):
            c = chapters[k]
            if c[6]:
                break
            if c[4] or c[5]:
                nxt += 1
                assign[k] = nxt
            else:
                break
        for pos, c in enumerate(chapters):
            if c[2] is not None and not c[6]:
                assign.setdefault(pos, c[2])

    # 書式: アンカーの書式（第/章の有無）を引き継ぐ。無指定は
    # 1桁=全角数字・2桁=半角（縦中横の印刷慣行に合わせる）
    fmt_default = chapters[anchors[-1][0]][3] if anchors else ("", "")

    n_changed = 0
    for pos, c in enumerate(chapters):
        if c[7]:
            continue    # 回収予定行は切れ目マーカーとしてのみ使う
        i, rest, num0, fmt = c[0], c[1], c[2], c[3]
        num = assign.get(pos)
        if num is not None:
            # 数字は1桁=全角・2桁以上=半角（縦中横の印刷慣行に合わせる）
            digits = str(num) if num >= 10 else chr(0xFF10 + num)
            pre, post = fmt if num0 is not None else fmt_default
            if pre or (post and post not in "　 "):
                numstr = f"{pre}{digits}{post}"
            else:
                numstr = digits
            new_title = f"{numstr}　{rest}".strip("　 ")
        elif num0 is not None:
            # 番号は再構成できなかったが元の番号がある → 元の書式のまま
            mnum = _LEADNUM_RE.match(_MIDASHI_LINE_RE.match(out[i]).group(1))
            new_title = (mnum.group(0) + rest).strip("　 ") if mnum else rest
        else:
            new_title = rest.strip("　 ")
        if _replace_heading(out, i, new_title):
            n_changed += 1
    return n_changed


def _replace_heading(out, i, new_title):
    """行 i から始まる見出しブロックのタイトルを差し替える。"""
    m = _MIDASHI_LINE_RE.match(out[i])
    if not m or not new_title:
        return False
    old = m.group(1)
    if old == new_title:
        return False
    out[i] = f"［＃「{new_title}」は中見出し］"
    for k in range(i + 1, min(i + 3, len(out))):
        if out[k].strip("　 ") == old:
            out[k] = new_title
        elif _MIDASHI_LINE_RE.match(out[k]):
            out[k] = f"［＃「{new_title}」は中見出し終わり］"
    return True


# ── OCR系統誤りの後処理正規化 ──────────────────────────
#
# スキャン解像度を上げても残るOCRの系統誤り（NORMAL/SUPERFINE比較実験で
# 両画質に同数出現した字種）のうち、文脈から機械的に正せるものだけを直す。
# 濁点/半濁点混同（ボ↔ポ）・促音（ツ↔ッ）・「み」の当て字などは文脈では
# 判定できないため対象外（手作業校正の領分）。

# 和文文字（ダッシュ判定の文脈用）
_JW_CHARS = 'ぁ-ゖァ-ヺー㐀-鿿々〆〇'
# 長音「ー」の誤認識と判定する ｌ/Ｉ:
#   1) 直前がカタカナ・ー・へべぺ（カタカナと同形のひらがな）で直後がカタカナ
#   2) 直前がン・ム以外のカタカナで直後がかな（正文でカタカナ直後にダッシュが
#      来るのは「ン――」「ム――」のみと3冊の正解テキストで確認。ポリーも/ビューと
#      のような語末長音+助詞のパターンはこちらで拾う）
_DASH_ONBIKI_RE = re.compile(r'(?<=[ァ-ヺーへべぺ])[ｌＩ](?=[ァ-ヺ])')
_DASH_ONBIKI2_RE = re.compile(r'(?<=[ァ-ミメ-ヲヴ-ヺ])[ｌＩ](?=[ぁ-ゖ])')
# 残る和文に挟まれた ｌ/Ｉ はダッシュ「――」の誤認識（英数字隣接は ＧＩ・ＳＥＩＫＯ
# などの正当な表記があり得るため変換しない）
_DASH_RE = re.compile(
    rf'(?<=[{_JW_CHARS}、。？！」』）])[ｌＩ](?=[{_JW_CHARS}「『（])')
# 数字に隣接する ○/◯ は漢数字ゼロ「〇」の誤認識（一○四二年 など）。
# 数字が絡まない ○○ は伏せ字の可能性があるため変換しない
_DIGIT_CHARS = '0-9０-９〇一二三四五六七八九十百千万'
_CIRCLE_L_RE = re.compile(rf'(?<=[{_DIGIT_CHARS}])[○◯]')
_CIRCLE_R_RE = re.compile(rf'[○◯](?=[{_DIGIT_CHARS}])')

# 文末の句点「。」を中黒「・」に誤認識するOCR誤り。正当な中黒はカタカナ語・
# 人名（ドロシー・ギルマン）・漢字語の区切りがほぼ全てで、閉じ括弧の直前や
# 行末に来る正当例は正解テキスト5冊・中黒約530個中0件だった（グリックの
# 手作業校正版にも見逃された「〜よ・」「お休み・」の2件が残っていた）。
# ひらがな・ひらがな等の文中の中黒は正当例があるため触れない。
#   1) 和文字・点類の直後 + 閉じ括弧の直前 → 。（「……・」→「……。」含む）
#   2) 和文字の直後 + 行末 → 。（点類直後は目次のリーダー罫の可能性があり除外）
#   3) 和文字・閉じ括弧の直後 + 。の直前 → 除去（た・。→た。）
_DOT_CLOSE_RE  = re.compile(r'(?<=[ぁ-ゖァ-ヶー一-鿿々》…・．：])・(?=[」』）])')
_DOT_EOL_RE    = re.compile(r'(?<=[ぁ-ゖァ-ヶー一-鿿々》])・$', re.MULTILINE)
_DOT_PERIOD_RE = re.compile(r'(?<=[ぁ-ゖァ-ヶー一-鿿々」』）])・(?=。)')

# 小書きカナ→並字。直前の文字が拗音として成立する場合のみ小書きを残す。
# 対象は拗音（ャュョ）と数詞のヵのみ:
#   - ヶは地名（関ヶ原・市ヶ谷）で数詞以外にも正当なため対象外
#   - 小書き母音（ァィゥェォ）はオノマトペ・叫び声（ゴォーッ、ワァーッ、
#     キィーッ等）で任意の音の後に正当に付くため対象外（グリックの冒険の
#     手作業校正版と突き合わせて誤修正13箇所を確認済み）
_SMALL_KANA_VALID_PREV = {
    'ャ': 'キギシジチヂニヒビピミリ',
    'ュ': 'キギシジチヂニヒビピミリフヴテデ',
    'ョ': 'キギシジチヂニヒビピミリ',
    'ヵ': '0123456789０１２３４５６７８９一二三四五六七八九十数何幾',
}
_SMALL_TO_BIG = {'ャ': 'ヤ', 'ュ': 'ユ', 'ョ': 'ヨ', 'ヵ': 'カ'}


def _fix_quotes_line(line, stats):
    """〃(ditto)/″(prime) をダブルミニュート〝〟に。開閉は行内の対応で判定。"""
    out = []
    open_ = False
    for ch in line:
        if ch == '〝':
            open_ = True
        elif ch == '〟':
            open_ = False
        elif ch in '〃″':
            ch = '〟' if open_ else '〝'
            open_ = (ch == '〝')
            stats['引用符〝〟'] += 1
        out.append(ch)
    return ''.join(out)


def normalize_ocr_text(text):
    """OCR系統誤りを正規化した (テキスト, 修正数Counter) を返す。"""
    stats = Counter()

    def _count_sub(pattern, repl, s, key):
        s2, n = pattern.subn(repl, s)
        if n:
            stats[key] += n
        return s2

    # ダッシュ・長音（長音を先に確定させる）
    text = _count_sub(_DASH_ONBIKI_RE, 'ー', text, '長音ー')
    text = _count_sub(_DASH_ONBIKI2_RE, 'ー', text, '長音ー')
    text = _count_sub(_DASH_RE, '――', text, 'ダッシュ――')

    # 文末の中黒→句点
    text = _count_sub(_DOT_CLOSE_RE, '。', text, '文末。')
    text = _count_sub(_DOT_EOL_RE, '。', text, '文末。')
    text = _count_sub(_DOT_PERIOD_RE, '', text, '文末。')

    # ○→〇（数字連続を辿るため収束まで反復）
    while True:
        t2 = _CIRCLE_L_RE.sub('〇', text)
        t2 = _CIRCLE_R_RE.sub('〇', t2)
        if t2 == text:
            break
        stats['漢数字〇'] += sum(1 for a, b in zip(text, t2) if a != b)
        text = t2

    # 引用符（行単位で開閉を追跡）
    lines = [_fix_quotes_line(ln, stats) for ln in text.split('\n')]

    # 小書きカナ→並字
    fixed_lines = []
    for ln in lines:
        chars = list(ln)
        for i, ch in enumerate(chars):
            valid = _SMALL_KANA_VALID_PREV.get(ch)
            if valid is None:
                continue
            if i == 0 or chars[i - 1] not in valid:
                chars[i] = _SMALL_TO_BIG[ch]
                stats['小書きカナ並字化'] += 1
        fixed_lines.append(''.join(chars))

    return '\n'.join(fixed_lines), stats


# ── ルビ内漢字混入の自動訂正 ──────────────────────────
#
# 通常の作品でひらがなルビの途中に漢字がまじるのは、まぎらわしい仮名を
# OCRが漢字に誤認識したもの（こ→二、み→巳、な→念 等）。同じ親文字への
# 正常な（仮名のみの）ルビは同じ本の中に多数出現するので、本の中の
# 多数決＋かな部分の類似度で置換する（霧のむこうのふしぎな町で
# OK48/NG4を確認。NGはいずれも置換前より悪化しない仮名置換）。

_RUBY_PAIR_RE  = re.compile(r"(?:｜([^｜《》\n]+)|([一-鿿々〆〇ヶ]+))《([^《》]+)》")
_RUBY_KANJI_RE = re.compile(r"[一-鿿々〆〇]")
_RUBY_KANA_RE  = re.compile(r"[ぁ-ゖァ-ヶーゝゞヽヾ]")
# 「正常ルビ」判定。ゑ・ゐは現代の作品のルビにほぼ現れず、み等のOCR破損
# （すみか→すゑか）の方が多いため正常扱いしない
_RUBY_CLEAN_RE = re.compile(r"[ぁ-わをんゔゕゖァ-ワヲンヴヵヶーゝゞヽヾ]+")


def fix_ruby_kanji(text):
    """ルビ内に混入した漢字を本内の多数決で訂正した (テキスト, 修正数) を返す。"""
    # 第1パス: 親文字ごとの正常ルビ（仮名のみ）の頻度を集計
    readings = {}
    for m in _RUBY_PAIR_RE.finditer(text):
        parent, ruby = m.group(1) or m.group(2), m.group(3)
        if _RUBY_CLEAN_RE.fullmatch(ruby):
            readings.setdefault(parent, Counter())[ruby] += 1

    fixed = 0

    def _repl(m):
        nonlocal fixed
        parent, ruby = m.group(1) or m.group(2), m.group(3)
        if not _RUBY_KANJI_RE.search(ruby):
            return m.group(0)
        cands = readings.get(parent)
        if not cands:
            return m.group(0)          # 同じ親の正常ルビが本内にない → 保留
        # 頻度1の候補は、同じ親に頻度4以上の候補があるなら自身もOCR破損の
        # 疑いが濃いので除外（売《やつ》←売《う》×10 のような誤り候補対策）
        max_freq = max(cands.values())
        kana = "".join(_RUBY_KANA_RE.findall(ruby))
        scored = [(difflib.SequenceMatcher(None, kana, c).ratio(), f, c)
                  for c, f in cands.items() if f > 1 or max_freq < 4]
        top = max(r for r, _, _ in scored)
        if top < 0.5:
            return m.group(0)          # かな部分が似た候補なし → 保留
        # 最高類似度から0.25以内の候補のうち最頻のものを採用
        # （だいどぢ×1 より だいどころ×3、こシス×1 より こえ×37 を優先）
        _, _, best = max((f, r, c) for r, f, c in scored if r >= top - 0.25)
        fixed += 1
        prefix = "｜" + parent if m.group(1) else parent
        return f"{prefix}《{best}》"

    return _RUBY_PAIR_RE.sub(_repl, text), fixed


# ── ルビ読みの濁点・半濁点・小書き誤読の自動訂正 ──────────────
#
# 極小活字のルビでは ゛/゜ の混同（くび→くぴ・ばな→ぱな）、濁点の
# 有無（あたま→あだま）、小書き/並字の混同（ちょうめ→ちようめ・
# じゅず→じゆず）が誤りの最大勢力（地下室からのふしぎな旅・Vision経路の
# 全文実測: 不一致1148件中423件）。総ルビの本では同じ親文字への正しい
# ルビが多数出現するので、「濁点・半濁点・小書きの置換だけで一致する
# 読み（＝スケルトン一致）」が本内に圧倒的多数あればそれに合わせる。
#
# 選択は「音韻の事前知識票 → 本内頻度」の2段階:
#   1. 事前知識票: 日本語の読みでほぼ成立しない並びを「違反」として数え、
#      違反数が自分より少ない同型候補があれば頻度不問でそれを採用する。
#      違反 = 小書きゃゅょがイ段以外の直後 / 並字やゆよがイ段の直後 /
#      ひらがな半濁点ぱ行がっ・ん以外の直後。系統的な誤読（ゆび→ゆぴ、
#      しゃめん→しやめん）は本内で誤りの方が多数派になることがあり
#      （地下室で実測: 指《ゆぴ》が《ゆび》より高頻度）、頻度だけの多数決
#      では正解の側が壊されるため、この票を頻度より優先する。
#      実測（4冊）: 頻度のみ＝改善66/悪化15 → 事前知識票あり＝改善326/悪化6
#      （悪化6件は全てGOAL側の残存誤りや表記揺れで、実質的な悪化は無し）
#   2. 頻度: 違反数で差がつかない組（あたま/あだま等の濁点有無）は連濁
#      （橋: はし/ばし、本: ほん/ぼん/ぽん）と衝突しうるため保守的に、
#      定着候補（頻度2以上）が一意で、頻度 RUBY_VARIANT_MIN_FREQ 以上かつ
#      自身の RUBY_VARIANT_DOMINANCE 倍以上のときだけ訂正する

RUBY_VARIANT_MIN_FREQ = 4   # 頻度票での訂正先に要求する本内最低出現数
RUBY_VARIANT_DOMINANCE = 3  # 頻度票での訂正先に要求する自身との頻度比

_DAKUTEN_MARKS = ("゙", "゚")   # 結合用濁点・半濁点（NFD分解後）
_SMALL2BIG_KANA = str.maketrans(
    "ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶ",
    "あいうえおつやゆよわかけアイウエオツヤユヨワカケ")

_IDAN_KANA = set("きぎしじちぢにひびぴみりキギシジチヂニヒビピミリ")
_SMALL_YAYUYO = set("ゃゅょ")
_LARGE_YAYUYO = set("やゆよ")
_HANDAKU_HIRA = set("ぱぴぷぺぽ")   # カタカナのパ行は語頭でも正当（頁《ぺーじ》）
_SOKUON_N = set("っんッン")


def _kana_skeleton(s):
    """濁点・半濁点を除去し小書きを並字化した読みの骨格。OCRが混同しやすい
    字形クラス（ば/ぱ/は、よ/ょ、つ/っ…）を同一視するために使う。"""
    out = [ch for ch in unicodedata.normalize("NFD", s)
           if ch not in _DAKUTEN_MARKS]
    return "".join(out).translate(_SMALL2BIG_KANA)


def _reading_violations(s):
    """読みとして不自然な並びの数。同型候補の優先順位付けに使う。
    自由《じゆう》・器用《きよう》・視野《しや》のような例外は違反に
    数えてしまうが、訂正は同じ親文字にスケルトン一致の別候補が実在する
    ときしか起きないため、実害はない（4冊の実測で誤爆0）。"""
    v = 0
    for i, ch in enumerate(s):
        prev = s[i - 1] if i else ""
        if ch in _SMALL_YAYUYO and prev not in _IDAN_KANA:
            v += 1
        elif ch in _LARGE_YAYUYO and prev in _IDAN_KANA:
            v += 1
        elif ch in _HANDAKU_HIRA and prev not in _SOKUON_N:
            v += 1
    return v


def fix_ruby_variants(text):
    """濁点・半濁点・小書きの誤読ルビを本内多数決で訂正した (テキスト, 修正数)。"""
    readings = {}
    for m in _RUBY_PAIR_RE.finditer(text):
        parent, ruby = m.group(1) or m.group(2), m.group(3)
        if _RUBY_CLEAN_RE.fullmatch(ruby):
            readings.setdefault(parent, Counter())[ruby] += 1

    fixed = 0

    def _repl(m):
        nonlocal fixed
        parent, ruby = m.group(1) or m.group(2), m.group(3)
        if not _RUBY_CLEAN_RE.fullmatch(ruby):
            return m.group(0)
        cands = readings.get(parent)
        if not cands:
            return m.group(0)
        sk = _kana_skeleton(ruby)
        equivalents = [c for c in cands
                       if c != ruby and _kana_skeleton(c) == sk]
        if not equivalents:
            return m.group(0)
        vr = _reading_violations(ruby)
        best = None
        # 第1票: 音韻の事前知識（違反数が減る候補があれば頻度不問で採用）
        better = [c for c in equivalents if _reading_violations(c) < vr]
        if better:
            best = min(better, key=lambda c: (_reading_violations(c),
                                              -cands[c]))
        else:
            # 第2票: 本内頻度（定着候補が一意で圧倒的なときだけ）
            settled = [c for c in equivalents
                       if cands[c] >= 2 and _reading_violations(c) <= vr]
            if len(settled) == 1 and \
                    cands[settled[0]] >= max(RUBY_VARIANT_MIN_FREQ,
                                             cands[ruby] * RUBY_VARIANT_DOMINANCE):
                best = settled[0]
        if best is None:
            return m.group(0)
        fixed += 1
        prefix = "｜" + parent if m.group(1) else parent
        return f"{prefix}《{best}》"

    return _RUBY_PAIR_RE.sub(_repl, text), fixed


# ── メイン ──────────────────────────────────────────

def parse_pages_arg(arg, npages):
    """--pages "10-360" / "5,8,10-" → 0始まりのページ番号リスト"""
    if not arg:
        return list(range(npages))
    result = []
    for part in arg.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            a = int(a) - 1 if a else 0
            b = int(b) if b else npages
            result.extend(range(max(0, a), min(npages, b)))
        elif part:
            result.append(int(part) - 1)
    return sorted(set(i for i in result if 0 <= i < npages))


def parse_meta_from_filename(path):
    """「タイトル_作者名.pdf」形式のファイル名から (title, author) を推定する。
    区切りは最初の _ を優先、次いで -（mangaP2ePub と同方式）。
    区切りがなければ (ファイル名, "") を返す。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    for sep in ("_", "-"):
        if sep in stem:
            title, _, author = stem.partition(sep)
            title, author = title.strip(), author.strip()
            if title and author:
                return title, author
    return stem.strip(), ""


def inspect_page(doc, num, body_size):
    pg = analyze_page(doc[num], num, body_size)
    print(f"=== page {num + 1} (index {num})  body_size={body_size:.1f} ===")
    print(f"縦行 {len(pg.vlines)} 本:")
    for v in pg.vlines:
        print(f"  x={v.x0:.0f}-{v.x1:.0f} y={v.y0:.0f}-{v.y1:.0f} "
              f"size={v.size:.1f} len={len(v.cells)}: {v.text[:40]!r}")
    print(f"横行 {len(pg.hlines)} 本:")
    for h in pg.hlines:
        print(f"  x={h.x0:.0f}-{h.x1:.0f} y={h.y0:.0f}-{h.y1:.0f} "
              f"size={h.size:.1f}: {h.text!r}")
    print(f"ルビ {len(pg.rubies)} 個:")
    for r in pg.rubies:
        print(f"  x={r.x0:.0f}-{r.x1:.0f} y={r.y0:.0f}-{r.y1:.0f}: {r.text!r}")


# ══════════════════════════════════════════
#  ePub3生成（novel_downloader.py から移植）
# ══════════════════════════════════════════

def _parse_hex_color(hex_str: str) -> tuple:
    """#RRGGBB 形式のカラーコードを (R, G, B) タプルに変換する。"""
    h = hex_str.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"無効なカラーコード: {hex_str}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _darken_color(r: int, g: int, b: int, factor: float = 0.6) -> tuple:
    """色を暗くする。"""
    return int(r * factor), int(g * factor), int(b * factor)



# ── ePub内部で使うXML/HTMLテンプレート ──────────────────────────

def _make_epub_css(font_name: str = "", font_filename: str = "") -> str:
    """
    ePub本文用CSSを生成する。
    font_name / font_filename が指定された場合は @font-face を挿入し、
    body の font-family の先頭に追加する。
    """
    font_face = ""
    if font_name and font_filename:
        ext = Path(font_filename).suffix.lower()
        fmt_map = {".otf": "opentype", ".ttf": "truetype",
                   ".woff": "woff", ".woff2": "woff2"}
        fmt = fmt_map.get(ext, "opentype")
        font_face = (
            f'@font-face {{\n'
            f'  font-family: "{font_name}";\n'
            f'  src: url("../fonts/{font_filename}") format("{fmt}");\n'
            f'  font-weight: normal;\n'
            f'  font-style: normal;\n'
            f'}}\n\n'
        )
        custom_family = f'"{font_name}", '
    else:
        custom_family = ""

    return f"""\
@charset "UTF-8";

{font_face}/* ── 縦書き基本設定（フォールバック: class非対応環境・Amazon Kindle等） ── */
html, body {{
  -epub-writing-mode: vertical-rl;
  -webkit-writing-mode: vertical-rl;
  writing-mode: vertical-rl;
}}
html {{
  line-height: 2.0;
  font-size: 1em;
}}

/* ── DPFJガイド準拠: class対応RSはこちらが優先 ── */
html.vrtl,
html.vrtl body {{
  -epub-writing-mode: vertical-rl;
  -webkit-writing-mode: vertical-rl;
  writing-mode: vertical-rl;
}}
html.hltr,
html.hltr body {{
  -epub-writing-mode: horizontal-tb;
  -webkit-writing-mode: horizontal-tb;
  writing-mode: horizontal-tb;
}}
html.vrtl {{
  line-height: 2.0;
  font-size: 1em;
}}
html.hltr {{
  line-height: 1.8;
  font-size: 1em;
}}

/* 縦組みページの body フォント（serif-ja-v: RS が解釈する仮想フォント名） */
html.vrtl body {{
  font-family: {custom_family}serif-ja-v, serif-ja, "游明朝", "YuMincho",
               "ヒラギノ明朝 ProN", "HiraMinProN-W3", "Noto Serif CJK JP", serif;
}}

/* 横組みページの body フォント（serif-ja: RS が解釈する仮想フォント名） */
html.hltr body {{
  font-family: {custom_family}serif-ja, "游明朝", "YuMincho",
               "ヒラギノ明朝 ProN", "HiraMinProN-W3", "Noto Serif CJK JP", serif;
}}

/* フォールバック（class非対応環境） */
body {{
  margin: 1em;
  font-family: {custom_family}"游明朝", "YuMincho", "ヒラギノ明朝 ProN", "HiraMinProN-W3",
               "Noto Serif CJK JP", serif;
}}

/* ── 表紙 ── */
.cover-title {{
  font-size: 1.8em;
  font-weight: bold;
  margin-bottom: 1em;
  text-align: center;
}}

.cover-author {{
  font-size: 1.1em;
  text-align: center;
  margin-bottom: 2em;
}}

.cover-synopsis {{
  font-size: 0.9em;
  margin-top: 2em;
  border-top: 1px solid #999;
  padding-top: 1em;
}}

/* ── 本文 ── */
h2.ep-title {{
  font-size: 1.3em;
  font-weight: bold;
  margin-bottom: 1.5em;
  border-bottom: 1px solid #ccc;
  padding-bottom: 0.3em;
}}

p.body-line {{
  margin: 0;
  text-indent: 1em;
}}

p.body-blank {{
  margin: 0;
  height: 1em;
}}

/* ── 章内の意図的な改ページ（底本の改ページを反映） ── */
div.pagebreak {{
  break-before: page;
  page-break-before: always;
  margin: 0;
  padding: 0;
  height: 0;
}}

/* ── 本文内見出し（青空文庫 大見出し・中見出し・小見出し） ── */
p.midashi-oo {{
  font-size: 1.1em;
  font-weight: bold;
  text-indent: 0;
  margin: 1em 0;
  text-align: center;
}}

p.midashi-naka {{
  font-size: 1.0em;
  font-weight: bold;
  text-indent: 0;
  margin: 0.8em 0;
}}

p.midashi-sho {{
  font-size: 0.95em;
  font-weight: bold;
  text-indent: 0;
  margin: 0.5em 0;
}}

/* ── 目次（toc.xhtml） ── */
#toc ol {{
  list-style: decimal;
}}
#toc li.toc-prelim {{
  list-style: none;
}}
#toc li.toc-chapter {{
  list-style: none;
  margin-top: 0.8em;
  margin-bottom: 0.2em;
}}
#toc li.toc-chapter > a {{
  font-weight: bold;
  font-size: 0.95em;
}}

/* ── 奥付 ── */
.colophon {{
  font-size: 0.85em;
  border-top: 1px solid #999;
  padding-top: 1em;
  margin-top: 2em;
}}

/* ── リンク共通 ── */
a {{
  color: #4a6fa5;
  text-decoration: underline;
}}

a:visited {{
  color: #7a5fa5;
}}

/* 表紙ページのソースリンク */
.cover-source {{
  font-size: 0.85em;
  margin-top: 2em;
  text-align: center;
}}

/* ── 字下げ（青空文庫書式対応） ── */
/* ここからN字下げ: ブロック内の各段落テキストを先頭字下げなしで均一配置 */
div.aozora-indent > p.body-line,
div.aozora-indent > p.body-blank {{
  text-indent: 0;
}}
div.aozora-indent-1em  {{ padding-top: 1em;  }}
div.aozora-indent-2em  {{ padding-top: 2em;  }}
div.aozora-indent-3em  {{ padding-top: 3em;  }}
div.aozora-indent-4em  {{ padding-top: 4em;  }}
div.aozora-indent-5em  {{ padding-top: 5em;  }}
div.aozora-indent-6em  {{ padding-top: 6em;  }}
div.aozora-indent-7em  {{ padding-top: 7em;  }}
div.aozora-indent-8em  {{ padding-top: 8em;  }}
div.aozora-indent-9em  {{ padding-top: 9em;  }}
div.aozora-indent-10em {{ padding-top: 10em; }}

/* 改行天付き、折り返してN字下げ: 初行は天付き（indent 0）、折り返し行はN字下げ */
div.aozora-hanging-1em  {{ padding-top: 1em;  }}
div.aozora-hanging-1em  > p.body-line {{ text-indent: -1em;  }}
div.aozora-hanging-2em  {{ padding-top: 2em;  }}
div.aozora-hanging-2em  > p.body-line {{ text-indent: -2em;  }}
div.aozora-hanging-3em  {{ padding-top: 3em;  }}
div.aozora-hanging-3em  > p.body-line {{ text-indent: -3em;  }}
div.aozora-hanging-4em  {{ padding-top: 4em;  }}
div.aozora-hanging-4em  > p.body-line {{ text-indent: -4em;  }}
div.aozora-hanging-5em  {{ padding-top: 5em;  }}
div.aozora-hanging-5em  > p.body-line {{ text-indent: -5em;  }}
div.aozora-hanging-6em  {{ padding-top: 6em;  }}
div.aozora-hanging-6em  > p.body-line {{ text-indent: -6em;  }}
div.aozora-hanging-7em  {{ padding-top: 7em;  }}
div.aozora-hanging-7em  > p.body-line {{ text-indent: -7em;  }}
div.aozora-hanging-8em  {{ padding-top: 8em;  }}
div.aozora-hanging-8em  > p.body-line {{ text-indent: -8em;  }}
div.aozora-hanging-9em  {{ padding-top: 9em;  }}
div.aozora-hanging-9em  > p.body-line {{ text-indent: -9em;  }}
div.aozora-hanging-10em {{ padding-top: 10em; }}
div.aozora-hanging-10em > p.body-line {{ text-indent: -10em; }}

/* ── 縦中横（DPFJガイド準拠） ── */
.tcy {{
  -webkit-text-combine: horizontal;
  text-combine-upright: all;
  -epub-text-combine: horizontal;
}}

/* ── 図・イラスト（青空文庫 挿絵対応） ── */
p.illustration,
figure.illustration {{
  text-indent: 0;
  margin: 0.8em 0;
  text-align: center;
}}
img.illustration {{
  max-width: 100%;
}}
p.caption,
figcaption.caption {{
  font-size: 0.9em;
  text-indent: 0;
  text-align: center;
  margin-top: 0.3em;
}}

/* ── 横書き時の字下げ上書き（html.hltr スコープ） ── */
/* 縦書きでは padding-top が行送り方向の字下げになるが、横書きでは padding-left を使う */
html.hltr div.aozora-indent-1em  {{ padding-top: 0; padding-left: 1em;  }}
html.hltr div.aozora-indent-2em  {{ padding-top: 0; padding-left: 2em;  }}
html.hltr div.aozora-indent-3em  {{ padding-top: 0; padding-left: 3em;  }}
html.hltr div.aozora-indent-4em  {{ padding-top: 0; padding-left: 4em;  }}
html.hltr div.aozora-indent-5em  {{ padding-top: 0; padding-left: 5em;  }}
html.hltr div.aozora-indent-6em  {{ padding-top: 0; padding-left: 6em;  }}
html.hltr div.aozora-indent-7em  {{ padding-top: 0; padding-left: 7em;  }}
html.hltr div.aozora-indent-8em  {{ padding-top: 0; padding-left: 8em;  }}
html.hltr div.aozora-indent-9em  {{ padding-top: 0; padding-left: 9em;  }}
html.hltr div.aozora-indent-10em {{ padding-top: 0; padding-left: 10em; }}

html.hltr div.aozora-hanging-1em  {{ padding-top: 0; padding-left: 1em;  }}
html.hltr div.aozora-hanging-1em  > p.body-line {{ text-indent: -1em;  }}
html.hltr div.aozora-hanging-2em  {{ padding-top: 0; padding-left: 2em;  }}
html.hltr div.aozora-hanging-2em  > p.body-line {{ text-indent: -2em;  }}
html.hltr div.aozora-hanging-3em  {{ padding-top: 0; padding-left: 3em;  }}
html.hltr div.aozora-hanging-3em  > p.body-line {{ text-indent: -3em;  }}
html.hltr div.aozora-hanging-4em  {{ padding-top: 0; padding-left: 4em;  }}
html.hltr div.aozora-hanging-4em  > p.body-line {{ text-indent: -4em;  }}
html.hltr div.aozora-hanging-5em  {{ padding-top: 0; padding-left: 5em;  }}
html.hltr div.aozora-hanging-5em  > p.body-line {{ text-indent: -5em;  }}
html.hltr div.aozora-hanging-6em  {{ padding-top: 0; padding-left: 6em;  }}
html.hltr div.aozora-hanging-6em  > p.body-line {{ text-indent: -6em;  }}
html.hltr div.aozora-hanging-7em  {{ padding-top: 0; padding-left: 7em;  }}
html.hltr div.aozora-hanging-7em  > p.body-line {{ text-indent: -7em;  }}
html.hltr div.aozora-hanging-8em  {{ padding-top: 0; padding-left: 8em;  }}
html.hltr div.aozora-hanging-8em  > p.body-line {{ text-indent: -8em;  }}
html.hltr div.aozora-hanging-9em  {{ padding-top: 0; padding-left: 9em;  }}
html.hltr div.aozora-hanging-9em  > p.body-line {{ text-indent: -9em;  }}
html.hltr div.aozora-hanging-10em {{ padding-top: 0; padding-left: 10em; }}
html.hltr div.aozora-hanging-10em > p.body-line {{ text-indent: -10em; }}
"""

_XHTML_TMPL = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"
      xml:lang="ja" lang="ja"
      class="{html_class}">
<head>
  <meta charset="UTF-8"/>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="css/novel.css"/>
</head>
<body{epub_type}>
{body}
</body>
</html>
"""



# ルビベースとして使用できない文構造上の句読点・括弧類。
# クラス9の文字でも ＆ ♪ ★ 等のシンボル文字はルビベース可。
# 自動検出パス（パイプなし《》）でのみ使用する。
_PUNCT_NO_RUBY_BASE = frozenset(
    '。、！？…‥・「」『』（）【】〈〉《》：；―—–\u30fb\uff0e\uff0c\uff01\uff1f'
)


def _char_class(ch: str) -> int:
    """
    文字種を整数で返す（ルビ開始境界の自動判別に使用）。
    同じ値が連続する範囲を「同一文字種ブロック」として扱う。

    0: 漢字（CJK統合漢字・互換漢字・拡張領域）
    1: ひらがな
    2: カタカナ（半角カタカナを含む）
    3: 半角英字
    4: 半角数字
    5: 半角空白・記号
    6: 全角英字
    7: 全角数字
    8: 全角空白・その他記号
    9: 句読点・括弧・記号類（ルビベース不可）
   10: 上記以外のUnicode文字（キリル・ギリシャ文字等、ルビベース可）
    """
    cp = ord(ch)
    # 漢字（CJK統合漢字、互換漢字、拡張A/B/C/D/E/F）
    if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
            or 0x20000 <= cp <= 0x2A6DF or 0x2A700 <= cp <= 0x2CEAF
            or 0xF900 <= cp <= 0xFAFF):
        return 0
    # 青空文庫書式規定で漢字扱いとする特殊記号
    # 々(U+3005)=繰り返し、仝(U+4EDD)=同じ、〆(U+3006)=しめ、〇(U+3007)=零、ヶ(U+30F6)=ケ
    # ※仝はCJK範囲(0x4EDD)に含まれ上記で先にclass0になるが明示的に列挙
    if ch in '々仝〆〇ヶ':
        return 0
    # ひらがな
    if 0x3041 <= cp <= 0x309F:
        return 1
    # カタカナ（全角・半角）
    if 0x30A0 <= cp <= 0x30FF or 0xFF65 <= cp <= 0xFF9F:
        return 2
    # 半角英字
    if 0x0041 <= cp <= 0x005A or 0x0061 <= cp <= 0x007A:
        return 3
    # 半角数字
    if 0x0030 <= cp <= 0x0039:
        return 4
    # 半角空白・ASCII記号
    if 0x0020 <= cp <= 0x007E:
        return 5
    # 全角英字
    if 0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A:
        return 6
    # 全角数字
    if 0xFF10 <= cp <= 0xFF19:
        return 7
    # 全角空白
    if cp == 0x3000:
        return 8
    # キリル・ギリシャ文字等 Unicode 文字（Letter/Number/Mark カテゴリ）
    # → ルビベースとして有効なため class 9 とは区別する
    if unicodedata.category(ch)[0] in ('L', 'N', 'M'):
        return 10
    # 句読点・括弧・記号等（ルビベース不可）
    return 9


def _resolve_ruby_base(preceding: str) -> tuple[str, str]:
    """
    ルビ記号（《》）直前の文字列 preceding から、
    ルビが掛かるベース文字列と、その前の残余テキストを返す。

    ルール:
      - preceding の末尾から文字種が同一である連続ブロックをベースとする
      - ただし「その他(9)」は直前の文字種ブロックと合成しない
        （句読点等がルビベースに含まれないようにするため）

    戻り値: (before, base)
      before : ルビより前の残余テキスト
      base   : ルビのベース文字列
    """
    if not preceding:
        return "", ""
    # 末尾の文字種を基準にして同種ブロックを取り出す
    end_cls = _char_class(preceding[-1])
    i = len(preceding) - 1
    while i > 0 and _char_class(preceding[i - 1]) == end_cls:
        i -= 1
    return preceding[:i], preceding[i:]


def _ruby_needs_pipe(base: str, preceding: str = "", yomi: str = "") -> bool:
    """
    ルビのベース文字列に | を前置する必要があるか判定する。

    以下のいずれかの場合に True を返す:
    1. yomi（ルビテキスト）に漢字が含まれる
       （漢字ルビは _apply_ruby_auto の自動判別で地の文扱いされるため、
         スクレイパー段階で必ずパイプを付けて明示する）
    2. base が複数の文字種を含む
       （_resolve_ruby_base は末尾文字種のブロックしか取れないため、
         「俺以外の」→「の」のみになる）
    3. preceding の末尾文字が base[-1] と同じ文字種
       （自動検出が直前テキストまで延びる; 「氷村心白」→「氷村心白」全体になる）
    4. base の末尾文字がクラス9（記号・句読点）
       （_apply_ruby_auto の自動検出では文構造句読点として地の文扱いになる場合が
         あるため、スクレイパー段階でパイプを付けて明示する。
         例: ＆《アンド》）
    """
    if not base:
        return False
    if yomi and _has_kanji(yomi):
        return True
    end_cls = _char_class(base[-1])
    if end_cls == 9:
        return True
    if any(_char_class(ch) != end_cls for ch in base):
        return True
    if preceding and _char_class(preceding[-1]) == end_cls:
        return True
    return False


def _has_kanji(text: str) -> bool:
    """テキスト内に漢字（CJK文字）が含まれるかを返す。"""
    return any(_char_class(ch) == 0 for ch in text)


def _apply_ruby_auto(text: str) -> str:
    """
    青空文庫ルビ記法を処理してXHTML ruby タグに変換する。

    - 明示記号あり: |ベース《よみ》  → <ruby>ベース<rt>よみ</rt></ruby>
    - 明示記号なし: ベース《よみ》   → 《》直前の同一文字種ブロックを
                                         自動検出してルビベースとする

    ルビでない《》の判定（地の文として《》をそのまま出力）:
      - 《》内に漢字が含まれる場合（例: 《この部屋に誰かが潜んでいる》）
      - 有効なルビベースが見つからない場合（行頭・句読点直後など）

    テキストは _esc() 済みを想定しない（この関数内でエスケープする）。
    """
    result = []
    # パターン: ([|｜]ベース《よみ》) または (ベース《よみ》)
    # ASCII "|" と全角 "｜" の両方をルビ開始記号として認識する
    pattern = re.compile(r"[|｜]([^《|｜]+)《([^》]+)》|《([^》]+)》")
    pos = 0
    for m in pattern.finditer(text):
        chunk = text[pos:m.start()]
        if m.group(1) is not None:
            # "|ベース《よみ》" 形式：明示的ルビ指定はそのまま適用
            result.append(_esc(chunk))
            result.append(
                f"<ruby>{_esc(m.group(1))}<rt>{_esc(m.group(2))}</rt></ruby>"
            )
        else:
            yomi = m.group(3)
            # 《》内に漢字が含まれる → ルビではなく地の文
            if _has_kanji(yomi):
                result.append(_esc(chunk))
                result.append(_esc(f"《{yomi}》"))
            else:
                # "《よみ》" のみ：chunk の末尾から文字種境界でベースを切り出す
                before, base = _resolve_ruby_base(chunk)
                # ベースが文構造上の句読点・括弧類のみの場合は地の文扱い。
                # ＆ ♪ ★ 等のシンボル文字（クラス9だが _PUNCT_NO_RUBY_BASE 外）は
                # ルビベースとして有効とする。
                if base and all(ch in _PUNCT_NO_RUBY_BASE for ch in base):
                    base = ""
                    before = chunk
                result.append(_esc(before))
                if base:
                    result.append(
                        f"<ruby>{_esc(base)}<rt>{_esc(yomi)}</rt></ruby>"
                    )
                else:
                    # 有効なルビベースなし → 《》ごと地の文として出力
                    result.append(_esc(f"《{yomi}》"))
        pos = m.end()
    result.append(_esc(text[pos:]))
    return "".join(result)


# 青空文庫タグ処理用の正規表現（モジュールレベルで一度だけコンパイル）
# 見出し開始マーカー: ［＃「TEXT」は大見出し］ （終わりを含まない）
_MIDASHI_START_RE = re.compile(r"［＃「.+」は(大|中|小)見出し］")
# 見出し終了マーカー: ［＃「TEXT」は大見出し終わり］
_MIDASHI_END_RE   = re.compile(r"［＃「.+」は(大|中|小)見出し終わり］")
# 任意の青空文庫タグ（制御タグ除去用）
_AOZORA_ANY_TAG_RE = re.compile(r"［＃[^］]*］")
# 見出し CSS クラスマップ
_MIDASHI_CLASS = {"大": "midashi-oo", "中": "midashi-naka", "小": "midashi-sho"}

# 図・イラスト（青空文庫書式対応）
# キャプション付きは先にチェック（「の図」がキャプション付きにも含まれるため）
_FIG_CAP_RE = re.compile(
    r"［＃「([^」]*)」のキャプション付きの図（([^、）\s]+)(?:、横(\d+)×縦(\d+))?）入る］")
_FIG_PLAIN_RE = re.compile(
    r"［＃「([^」]*)」の図（([^、）\s]+)(?:、横(\d+)×縦(\d+))?）入る］")
_IS_CAPTION_LINE_RE  = re.compile(r".*［＃「[^」]*」はキャプション］")
_CAPTION_BLOCK_START_RE2 = re.compile(r"［＃ここからキャプション］")
_CAPTION_BLOCK_END_RE2   = re.compile(r"［＃ここでキャプション終わり］")
# 画像ファイル拡張子セット（ZIP 抽出用）
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg"}

# 縦中横タグ: ［＃縦中横］TEXT［＃縦中横終わり］ → <span class="tcy">TEXT</span>
# センチネル経由で _apply_ruby_auto のエスケープと干渉しない形で変換する
_TCY_RE          = re.compile(r"［＃縦中横］(.*?)［＃縦中横終わり］")
_TCY_SENTINEL_RE = re.compile(r"\x00TCY\x01(.*?)\x00TCYEND\x01")
# テキストノード内の1-3桁の連続数字・1-3文字の半角英字（縦中横自動検出）
# 数字: (?<!\d)/(?!\d) で4桁以上（年号等）は対象外
# 英字: (?<![A-Za-z])/(?![A-Za-z]) で4文字以上の英単語は対象外
#
# ただし、英数字が半角スペース・カンマ・ピリオド・ハイフン（コロン・スラッシュ・
# アポストロフィ含む）を介して別の英数字と連なる場合は「英語の文章/連結語」と見なし、
# まとめて横向きのまま残す（縦中横にしない）。例: "Men in Black" の Men/in、"U.S.A"。
# 第1選択肢が連結フレーズを丸ごと飲み込むため、内部の短いトークンは縦中横化されない。
_TCY_CONNECTOR   = r"[ ,.:/'\-]"
_TCY_DIGITS_RE   = re.compile(
    r"[A-Za-z0-9]+(?:" + _TCY_CONNECTOR + r"+[A-Za-z0-9]+)+"  # 連結フレーズ→そのまま
    r"|(?<!\d)\d{1,3}(?!\d)"                                  # 孤立した1-3桁数字→縦中横
    r"|(?<![A-Za-z])[A-Za-z]{1,3}(?![A-Za-z])"               # 孤立した1-3文字英字→縦中横
)
_TCY_CONNECTOR_RE = re.compile(_TCY_CONNECTOR)


def _tcy_wrap(m: "re.Match") -> str:
    """マッチ部を縦中横化する。連結フレーズ（連結記号を含む）はそのまま返す。"""
    s = m.group()
    if _TCY_CONNECTOR_RE.search(s):
        return s
    return f'<span class="tcy">{s}</span>'


def _apply_tcy_pre(text: str) -> str:
    """縦中横タグ内容をセンチネルに置換（_apply_ruby_auto 前に適用）。"""
    return _TCY_RE.sub(lambda m: f"\x00TCY\x01{m.group(1)}\x00TCYEND\x01", text)


def _apply_tcy_post(html: str) -> str:
    """センチネルを <span class="tcy"> に置換（_apply_ruby_auto 後に適用）。"""
    return _TCY_SENTINEL_RE.sub(
        lambda m: f'<span class="tcy">{m.group(1)}</span>', html
    )


def _auto_tcy_xhtml(html: str) -> str:
    """XHTML テキストノード内の2-3桁の連続数字を <span class="tcy"> でラップする。
    既存の tcy スパン内の数字は二重ラップしない。
    HTMLエンティティ（&#160; &amp; 等）は分割単位として保護し数値を誤変換しない。"""
    parts = re.split(r'(<[^>]+>|&#\d+;|&[a-zA-Z]+;)', html)
    out = []
    in_tcy = 0
    for part in parts:
        if not part:
            continue
        if part.startswith('<'):
            tag_lower = part.lower()
            if re.match(r'<span\b', tag_lower) and 'tcy' in tag_lower:
                in_tcy += 1
            elif tag_lower.startswith('</span') and in_tcy > 0:
                in_tcy -= 1
            out.append(part)
        elif part.startswith('&'):
            # HTMLエンティティ（&#160; &amp; 等）はそのまま素通し
            out.append(part)
        elif in_tcy > 0:
            out.append(part)
        else:
            out.append(_TCY_DIGITS_RE.sub(_tcy_wrap, part))
    return ''.join(out)


# 字下げ関連タグ（ここから〜 は単独行で使用）
# 改行天付き・折り返しN字下げは ここからN字下げ より先にチェックすること
_JISAGE_HANGING_RE = re.compile(
    r"［＃ここから改行天付き、折り返して([０-９一二三四五六七八九十\d]+)字下げ］")
_JISAGE_BLOCK_RE   = re.compile(
    r"［＃ここから([０-９一二三四五六七八九十\d]+)字下げ］")
_JISAGE_END_RE     = re.compile(r"［＃ここで字下げ終わり］")
_PAGE_BREAK_LINE_RE = re.compile(r"［＃改(?:ページ|丁)］")
_JISAGE_SINGLE_RE  = re.compile(
    r"^［＃([０-９一二三四五六七八九十\d]+)字下げ］")


def _jisage_to_int(s: str) -> int:
    """全角数字・漢数字を int に変換。変換できない場合は 1 を返す。"""
    s2 = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if s2.isdigit():
        return max(1, int(s2))
    kanji_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    n = sum(kanji_map.get(c, 0) for c in s2)
    return max(1, n) if n else 1


def _body_lines_to_xhtml(text: str, horizontal: bool = False) -> str:
    """
    本文テキスト（改行区切り）をXHTML要素列に変換する。

    - 空行                 → <p class="body-blank">
    - 通常行               → <p class="body-line">（青空文庫タグ除去・ルビ変換済み）
    - 大見出し行           → <p class="midashi-oo">
    - 中見出し行           → <p class="midashi-naka">
    - 小見出し行           → <p class="midashi-sho">

    青空文庫タグの処理:
      - 字下げ（N字下げ）・地付き等のレイアウトタグは全除去
      - 大/中/小見出しタグはインライン形式・ブロック形式の両方に対応
        インライン: TEXT［＃「TEXT」は大見出し］ → 同一行のTEXTを見出しとして出力
        ブロック:   ［＃「TEXT」は大見出し］     → 次の行をTEXTとして見出し出力
                    TEXT                          → 見出しテキスト行
                    ［＃「TEXT」は大見出し終わり］ → スキップ
      - その他の青空文庫タグは除去して地の文として扱う
    """
    result = []
    pending_heading    = None   # ブロック形式の見出し待ち: None or "大"/"中"/"小"
    indent_stack: list = []     # 字下げスタック: ("indent"|"hanging", n)
    pending_fig_html   = None   # キャプション付き図の <img> タグ（キャプション行待ち）
    in_caption_block   = False  # ここからキャプション〜ここでキャプション終わり 収集中
    caption_block_lines: list = []

    for raw in text.split("\n"):
        line = _apply_tcy_pre(raw.rstrip())

        # ── 複数行キャプション収集中 ──
        if in_caption_block:
            if _CAPTION_BLOCK_END_RE2.search(line):
                cap_html = "\n".join(
                    f'<p class="caption">{_apply_ruby_auto(_AOZORA_ANY_TAG_RE.sub("", l).strip())}</p>'
                    for l in caption_block_lines
                    if _AOZORA_ANY_TAG_RE.sub("", l).strip()
                )
                if pending_fig_html is not None:
                    result.append(
                        f'<figure class="illustration">{pending_fig_html}'
                        f'<figcaption class="caption">{cap_html}</figcaption></figure>')
                    pending_fig_html = None
                in_caption_block = False
                caption_block_lines = []
            else:
                caption_block_lines.append(line)
            continue

        # ── キャプション待ち（キャプション付き図の次行以降）──
        if pending_fig_html is not None:
            if _CAPTION_BLOCK_START_RE2.search(line):
                in_caption_block = True
                caption_block_lines = []
                continue
            if _IS_CAPTION_LINE_RE.search(line):
                cap_text = _apply_ruby_auto(_AOZORA_ANY_TAG_RE.sub("", line).strip())
                result.append(
                    f'<figure class="illustration">{pending_fig_html}'
                    f'<figcaption class="caption">{cap_text}</figcaption></figure>')
                pending_fig_html = None
                continue
            # 予期しない行: キャプションなしで図を閉じ、この行は通常処理へ
            result.append(f'<p class="illustration">{pending_fig_html}</p>')
            pending_fig_html = None
            # fall through（この行を通常処理）

        # ── ブロック形式見出し待ち（前行が開始マーカーのみだった場合） ──
        if pending_heading is not None:
            # 終了マーカー行が来た場合はスキップして待ちをリセット
            if _MIDASHI_END_RE.search(line):
                pending_heading = None
                continue
            if not line:
                result.append('<p class="body-blank">&#160;</p>')
                continue  # 空行でも待ち継続
            # この行が見出しテキスト
            cls     = _MIDASHI_CLASS[pending_heading]
            visible = _AOZORA_ANY_TAG_RE.sub("", line).strip()
            if visible:
                result.append(f'<p class="{cls}">{_apply_ruby_auto(visible)}</p>')
            pending_heading = None
            continue

        # ── 字下げ終わり ──
        if _JISAGE_END_RE.search(line):
            if indent_stack:
                indent_stack.pop()
                result.append('</div>')
            continue

        # ── 字下げブロック開始（改行天付き・折り返し）──
        m_hang = _JISAGE_HANGING_RE.search(line)
        if m_hang:
            n = _jisage_to_int(m_hang.group(1))
            indent_stack.append(("hanging", n))
            result.append(f'<div class="aozora-hanging aozora-hanging-{n}em">')
            continue

        # ── 字下げブロック開始（通常）──
        m_blk = _JISAGE_BLOCK_RE.search(line)
        if m_blk:
            n = _jisage_to_int(m_blk.group(1))
            indent_stack.append(("indent", n))
            result.append(f'<div class="aozora-indent aozora-indent-{n}em">')
            continue

        # ── 改ページ（章内の意図的な改ページ → 読書環境に改ページ指示）──
        if _PAGE_BREAK_LINE_RE.search(line):
            result.append('<div class="pagebreak"></div>')
            continue

        # ── 空行 ──
        if not line:
            result.append('<p class="body-blank">&#160;</p>')
            continue

        # ── 大/中/小見出し判定 ──
        start_m = _MIDASHI_START_RE.search(line)
        end_m   = _MIDASHI_END_RE.search(line)

        if start_m:
            level   = start_m.group(1)
            visible = _AOZORA_ANY_TAG_RE.sub("", line).strip()
            if visible:
                # インライン形式（字下げ等のタグを除去した可視テキストがある）
                cls = _MIDASHI_CLASS[level]
                result.append(f'<p class="{cls}">{_apply_ruby_auto(visible)}</p>')
            else:
                # 開始マーカーのみ → 次の行が見出しテキスト
                pending_heading = level
            continue

        if end_m:
            # 終了マーカーを除去した可視テキストがあれば見出しとして出力
            level   = end_m.group(1)
            visible = _AOZORA_ANY_TAG_RE.sub("", line).strip()
            if visible:
                cls = _MIDASHI_CLASS[level]
                result.append(f'<p class="{cls}">{_apply_ruby_auto(visible)}</p>')
            # 終了マーカーのみ行はスキップ
            continue

        # ── 図・イラスト ──
        m_fig_cap   = _FIG_CAP_RE.search(line)
        m_fig_plain = _FIG_PLAIN_RE.search(line)
        if m_fig_cap:
            alt, fname = m_fig_cap.group(1), m_fig_cap.group(2)
            w, h = m_fig_cap.group(3), m_fig_cap.group(4)
            size_attrs = (f' width="{w}" height="{h}"' if w and h else "")
            pending_fig_html = (
                f'<img class="illustration" src="images/{fname}"'
                f' alt="{_esc(alt)}"{size_attrs}/>')
            continue
        if m_fig_plain:
            alt, fname = m_fig_plain.group(1), m_fig_plain.group(2)
            w, h = m_fig_plain.group(3), m_fig_plain.group(4)
            size_attrs = (f' width="{w}" height="{h}"' if w and h else "")
            img_html = (
                f'<img class="illustration" src="images/{fname}"'
                f' alt="{_esc(alt)}"{size_attrs}/>')
            result.append(f'<p class="illustration">{img_html}</p>')
            continue

        # ── 単行字下げ（行頭に N字下げ タグ）──
        m_single = _JISAGE_SINGLE_RE.match(line)
        if m_single:
            n     = _jisage_to_int(m_single.group(1))
            clean = _AOZORA_ANY_TAG_RE.sub("", line).strip()
            if clean:
                result.append(
                    f'<p class="body-line" style="text-indent:{n}em;">'
                    f'{_apply_ruby_auto(clean)}</p>')
            else:
                result.append('<p class="body-blank">&#160;</p>')
            continue

        # ── 通常行：青空文庫タグを除去してルビ処理 ──
        clean = _AOZORA_ANY_TAG_RE.sub("", line)
        if not clean.strip():
            result.append('<p class="body-blank">&#160;</p>')
        else:
            result.append(f'<p class="body-line">{_apply_ruby_auto(clean)}</p>')

    # 未閉じの字下げブロックを閉じる（不正なテキストへの安全対策）
    for _ in indent_stack:
        result.append('</div>')
    # 未閉じのキャプション付き図を閉じる
    if pending_fig_html is not None:
        result.append(f'<p class="illustration">{pending_fig_html}</p>')

    # 縦中横センチネル→<span class="tcy"> 変換、および2-3桁数字の自動縦中横
    # 横書きモードでは縦中横は不要なのでスキップ
    if horizontal:
        return "\n".join(_apply_tcy_post(r) for r in result)
    return "\n".join(_auto_tcy_xhtml(_apply_tcy_post(r)) for r in result)


def _make_cover_xhtml(title: str, author: str, synopsis: str,
                      source_url: str = "", site_name: str = "",
                      horizontal: bool = False) -> str:
    """テキスト表紙XHTMLを生成する。底本URLをハイパーリンク付きで掲載する。"""
    syn_html = ""
    if synopsis:
        syn_lines = "\n".join(
            f'<p class="body-line">{_esc(l)}</p>' if l.strip()
            else '<p class="body-blank">&#160;</p>'
            for l in synopsis.split("\n")
        )
        syn_html = f'<div class="cover-synopsis">\n{syn_lines}\n</div>'

    source_html = ""
    if source_url:
        if site_name == "青空文庫":
            label = "青空文庫の図書カード"
        elif site_name:
            label = f'{_esc(site_name)}で読む'
        else:
            label = _esc(source_url)
        source_html = (
            f'<div class="cover-source">'
            f'<a href="{_esc(source_url)}">{label}</a>'
            f'</div>'
        )

    body = (
        f'<div class="cover-title">{_esc(title)}</div>\n'
        f'<div class="cover-author">{_esc(author)}</div>\n'
        f'{source_html}\n'
        f'{syn_html}'
    )
    return _XHTML_TMPL.format(title=_esc(title), body=body,
                               html_class="hltr" if horizontal else "vrtl",
                               epub_type='')


_VERTICAL_IMAGE_CSS = """\
@charset "UTF-8";

html, body {
  margin: 0;
  padding: 0;
  width: 100%;
  height: 100%;
}

body.fit_h {
  text-align: center;
}

span.img, figure.img {
  display: block;
  width: 100%;
  height: 100%;
  text-align: center;
  margin: 0;
  padding: 0;
}

span.img img, figure.img img {
  width: auto;
  height: 100%;
  display: inline-block;
  vertical-align: top;
}
"""


def _make_cover_image_xhtml(title: str, fmt: str = "jpg") -> str:
    """
    ePub3 標準準拠の表紙ページXHTMLを生成する。
      - 画像: images/cover.{fmt}（OEBPS/ からの相対パス）
      - epub:type="cover" を body に、epub:type="cover-image" を img に付与
      - CSS はインライン（別ファイル不要）
    """
    img_src = f"images/cover.{fmt}"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"
      xml:lang="ja" lang="ja"
      class="hltr">
<head>
  <meta charset="UTF-8"/>
  <title>{_esc(title)}</title>
  <style type="text/css">
    html, body {{ margin: 0; padding: 0; width: 100%; height: 100%; }}
    img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
  </style>
</head>
<body epub:type="cover">
  <img src="{img_src}" alt="{_esc(title)}" epub:type="cover-image"/>
</body>
</html>
"""


def _make_episode_xhtml(ep_title: str, body_text: str,
                        horizontal: bool = False,
                        show_title: bool = True) -> str:
    """1話分のXHTMLを生成する。show_title=False は画像ページ分割の続き用。"""
    h2 = f'<h2 class="ep-title">{_esc(ep_title)}</h2>\n' if show_title else ''
    body = h2 + _body_lines_to_xhtml(body_text, horizontal=horizontal)
    return _XHTML_TMPL.format(title=_esc(ep_title), body=body,
                               html_class="hltr" if horizontal else "vrtl",
                               epub_type='')


def _make_image_page_xhtml(fname: str, alt: str = "挿絵") -> str:
    """全画面表示の画像ページXHTML（挿絵・口絵・章頭用、表紙ページと同構造）。"""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xml:lang="ja" lang="ja"
      class="hltr">
<head>
  <meta charset="UTF-8"/>
  <title>{_esc(alt)}</title>
  <style type="text/css">
    html, body {{ margin: 0; padding: 0; width: 100%; height: 100%; }}
    img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
  </style>
</head>
<body>
  <img src="images/{_esc(fname)}" alt="{_esc(alt)}"/>
</body>
</html>
"""


def _split_episode_images(body: str) -> list:
    """
    エピソード本文から「改ページに挟まれた単独の図タグ」を専用画像ページとして
    分離する。戻り値: [("text", 本文), ("image", ファイル名, alt), ...]。
    段落中のインライン図タグはそのまま本文に残す（<figure> 描画）。
    分離した図の前後の［＃改ページ］はファイル境界が改ページになるため除去する。
    """
    lines = body.split("\n")
    segs, buf = [], []

    def _is_break(ln):
        return _PAGE_BREAK_LINE_RE.fullmatch(ln.strip()) is not None

    def _flush():
        txt = "\n".join(buf)
        if txt.strip():
            segs.append(("text", txt))
        buf.clear()

    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        m = (_FIG_PLAIN_RE.fullmatch(stripped)
             or _FIG_CAP_RE.fullmatch(stripped))
        if m:
            # 前後の非空行が改ページ（または本文の先頭/終端）なら単独ページ
            j = len(buf) - 1
            while j >= 0 and not buf[j].strip():
                j -= 1
            k = i + 1
            while k < n and not lines[k].strip():
                k += 1
            if (j < 0 or _is_break(buf[j])) and (k >= n or _is_break(lines[k])):
                if j >= 0:
                    del buf[j:]          # 直前の改ページを除去
                _flush()
                segs.append(("image", m.group(2), m.group(1) or "挿絵"))
                i = k + 1 if k < n else k   # 直後の改ページも消費
                continue
        buf.append(lines[i])
        i += 1
    _flush()
    return segs


def _make_colophon_xhtml(title: str, source_url: str, site_name: str,
                         horizontal: bool = False) -> str:
    """奥付XHTMLを生成する。底本URLはハイパーリンクとして出力する。"""
    today = date.today().strftime("%Y年%m月%d日")
    url_line = (
        f'<p class="body-line">　　　'
        f'<a href="{_esc(source_url)}">{_esc(source_url)}</a></p>\n'
        if source_url else ""
    )
    # xmlns:epub を body に付与するため XHTML_TMPL を直接使わず個別生成
    body = (
        f'<div class="colophon">\n'
        f'<p class="body-line">底本：「{_esc(title)}」{_esc(site_name)}</p>\n'
        f'{url_line}'
        f'<p class="body-line">入力：jisui2epub.py</p>\n'
        f'<p class="body-line">校正：未校正</p>\n'
        f'<p class="body-line">作成：{_esc(today)}</p>\n'
        f'</div>'
    )
    return _XHTML_TMPL.format(title="奥付", body=body,
                               html_class="hltr" if horizontal else "vrtl",
                               epub_type='')


def _make_toc_xhtml(title: str, episodes: list, cover_fmt: str = "",
                    horizontal: bool = False) -> str:
    """読者向け目次XHTML（toc.xhtml）を生成する。
    縦組みで spine に含まれる実際に読む目次ページ。
    nav.xhtml（RS向け機械読み取り専用）とは別ファイル。
    episodes: list[str] または list[dict{"title", "body", "group"?}]
    """
    def _norm(ep):
        if isinstance(ep, str):
            return {"title": ep, "group": None}
        return {"title": ep.get("title", ""), "group": ep.get("group") or None}
    normalized = [_norm(ep) for ep in episodes]

    prelim_items = ['<li class="toc-prelim"><a href="cover.xhtml">タイトルページ</a></li>']
    if cover_fmt:
        prelim_items.insert(0, '<li class="toc-prelim"><a href="cover-image.xhtml">表紙</a></li>')

    ep_items  = []
    num       = 0
    prev_group = None
    for ep in normalized:
        num += 1
        group = ep["group"]
        if group is not None and group != prev_group:
            ep_items.append(
                f'<li class="toc-chapter"><a href="ep{num:04d}.xhtml">{_esc(group)}</a></li>'
            )
            prev_group = group
        ep_items.append(
            f'<li value="{num}"><a href="ep{num:04d}.xhtml">{_esc(ep["title"])}</a></li>'
        )

    back_items = ['<li class="toc-prelim"><a href="colophon.xhtml">奥付</a></li>']
    toc_str = "\n    ".join(prelim_items + ep_items + back_items)

    body = (
        f'<h2 class="ep-title">{_esc(title)}</h2>\n'
        f'<ol id="toc">\n'
        f'  {toc_str}\n'
        f'</ol>'
    )
    return _XHTML_TMPL.format(title="目次", body=body,
                               html_class="hltr" if horizontal else "vrtl",
                               epub_type='')


def _make_nav_xhtml(title: str, episodes: list, cover_fmt: str = "",
                    horizontal: bool = False) -> str:
    """ナビゲーションドキュメント（nav.xhtml）を生成する。
    表紙・タイトルページ・奥付はナンバリングなしのリンクのみ。
    本文エピソードは 1 から始まる番号付きリストで表示し、
    episodes 要素に "group" キーがある場合は章/部単位でネストした <ol> にまとめる。

    episodes: list[str] または list[dict{"title", "body", "group"?}]
    """
    # 各エピソードを {"title": str, "group": str|None} に正規化
    def _norm(ep):
        if isinstance(ep, str):
            return {"title": ep, "group": None}
        return {"title": ep.get("title", ""), "group": ep.get("group") or None}
    normalized = [_norm(ep) for ep in episodes]

    # 前付け（ナンバリングなし）
    prelim_items = []
    if cover_fmt:
        prelim_items.append('<li class="toc-prelim"><a href="cover-image.xhtml">表紙</a></li>')
    prelim_items.append('<li class="toc-prelim"><a href="cover.xhtml">タイトルページ</a></li>')
    prelim_items.append('<li class="toc-prelim"><a href="toc.xhtml">目次</a></li>')

    # 本文エピソード
    # group が変わったとき（None→名前付き、または別の名前付き）にフラットな章ヘッダー行を挿入し、
    # エピソード行はすべて同じインデントレベルに並べる（ネストした <ol> は使わない）。
    # value 属性は全話通しの連番（ep{n:04d}.xhtml に対応）を明示する。
    ep_items  = []
    num       = 0   # 通し番号（ファイル名 ep{n:04d}.xhtml の n）
    prev_group = None  # 直前の章グループ名（None = 未設定）

    for ep in normalized:
        num  += 1
        group = ep["group"]
        # 新しい章グループに切り替わったときのみヘッダー行を挿入
        # ePub3 nav の <li> は <a> か (<span>+<ol>) のみ許可されるため、
        # 章ヘッダーはその章の先頭エピソードへのリンク (<a>) として出力する
        if group is not None and group != prev_group:
            ep_items.append(f'<li class="toc-chapter"><a href="ep{num:04d}.xhtml">{_esc(group)}</a></li>')
            prev_group = group
        ep_items.append(
            f'<li value="{num}"><a href="ep{num:04d}.xhtml">{_esc(ep["title"])}</a></li>'
        )

    # 後付け（ナンバリングなし）
    back_items = ['<li class="toc-prelim"><a href="colophon.xhtml">奥付</a></li>']

    toc_str = "\n    ".join(prelim_items + ep_items + back_items)

    # landmarks: カバー・本文開始・目次をリーダーが認識するための必須ナビ
    cover_href = "cover-image.xhtml" if cover_fmt else "cover.xhtml"
    body_start = "ep0001.xhtml" if episodes else "cover.xhtml"
    landmarks = f"""\
<nav epub:type="landmarks" id="landmarks">
  <ol>
    <li><a epub:type="cover"       href="{cover_href}">表紙</a></li>
    <li><a epub:type="toc"         href="toc.xhtml">目次</a></li>
    <li><a epub:type="bodymatter"  href="{body_start}">本文</a></li>
  </ol>
</nav>"""

    _nav_class = "hltr" if horizontal else "vrtl"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"
      xml:lang="ja" lang="ja"
      class="{_nav_class}">
<head><meta charset="UTF-8"/><title>{_esc(title)}</title>
<link rel="stylesheet" type="text/css" href="css/novel.css"/>
<style>
  #toc ol {{ list-style: decimal; }}
  #toc li.toc-prelim {{ list-style: none; }}
  #toc li.toc-chapter {{ list-style: none; margin-top: 0.8em; margin-bottom: 0.2em; }}
  #toc li.toc-chapter > a {{ font-weight: bold; font-size: 0.95em; }}
</style>
</head>
<body>
<nav epub:type="toc" id="toc">
  <h1>目次</h1>
  <ol>
    {toc_str}
  </ol>
</nav>
{landmarks}
</body>
</html>
"""


def _make_opf(title: str, author: str, book_id: str, ep_titles: list,
              cover_fmt: str = "", font_filename: str = "",
              toc_at_end: bool = False,
              inline_images: list = None,
              synopsis: str = "",
              horizontal: bool = False,
              doc_items: list = None) -> str:
    """
    OPF（package.opf）を生成する。
    cover_fmt: "png" | "svg" | "" (表紙画像なし)
    font_filename: 埋め込みフォントのファイル名（例: "AyatiShowaSerif-Regular.otf"）
    toc_at_end: True のとき目次を奥付の後に配置（デフォルト: 表紙の後・本文の前）
    inline_images: 本文中のインライン画像ファイル名リスト（青空文庫 ZIP 内の画像等）
    synopsis: あらすじ（dc:description に設定）
    """
    today    = date.today().strftime("%Y-%m-%d")
    now_iso  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="toc" href="toc.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="css" href="css/novel.css" media-type="text/css"/>',
    ]

    if font_filename:
        ext = Path(font_filename).suffix.lower()
        mime_map = {".otf": "font/otf", ".ttf": "font/ttf",
                    ".woff": "font/woff", ".woff2": "font/woff2"}
        font_mime = mime_map.get(ext, "font/otf")
        manifest_items.append(
            f'<item id="embedded-font" href="fonts/{font_filename}"'
            f' media-type="{font_mime}"/>'
        )

    if cover_fmt == "jpg":
        manifest_items += [
            '<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>',
            '<item id="cover-page" href="cover-image.xhtml" media-type="application/xhtml+xml"/>',
        ]
    elif cover_fmt == "png":
        manifest_items += [
            '<item id="cover-image" href="images/cover.png" media-type="image/png" properties="cover-image"/>',
            '<item id="cover-page" href="cover-image.xhtml" media-type="application/xhtml+xml"/>',
        ]
    elif cover_fmt == "svg":
        manifest_items += [
            '<item id="cover-image" href="images/cover.svg" media-type="image/svg+xml" properties="cover-image"/>',
            '<item id="cover-page" href="cover-image.xhtml" media-type="application/xhtml+xml"/>',
        ]

    manifest_items.append(
        '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
    )

    spine_items = []
    if cover_fmt:
        # 縦書き（RTL）は表紙を右ページに固定。横書き（LTR）はページスプレッド指定不要
        cover_spread = ('' if horizontal
                        else ' properties="page-spread-right"')
        spine_items.append(f'<itemref idref="cover-page" linear="yes"{cover_spread}/>')
    spine_items.append('<itemref idref="cover"/>')

    # 読者向け目次（toc.xhtml）を前配置（デフォルト）: 表紙の直後・本文の前
    if not toc_at_end:
        spine_items.append('<itemref idref="toc"/>')

    # doc_items: [(id, href)] — 画像ページ・分割ファイルを含む本文spine構成。
    # 未指定時は従来どおりエピソード1話=1ファイル
    if doc_items is None:
        doc_items = [(f"ep{i+1:04d}", f"ep{i+1:04d}.xhtml")
                     for i in range(len(ep_titles))]
    for did, href in doc_items:
        manifest_items.append(
            f'<item id="{did}" href="{href}" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'<itemref idref="{did}"/>')
    manifest_items.append(
        '<item id="colophon" href="colophon.xhtml" media-type="application/xhtml+xml"/>'
    )
    spine_items.append('<itemref idref="colophon"/>')

    # 読者向け目次（toc.xhtml）を後配置（--toc-at-end）: 奥付の後
    if toc_at_end:
        spine_items.append('<itemref idref="toc"/>')

    # nav.xhtml は spine に含めない（properties="nav" のみで RS が認識、DPFJガイド準拠）

    # インライン画像（青空文庫 ZIP 内の挿絵等）を manifest に追加
    if inline_images:
        _img_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".gif": "image/gif", ".bmp": "image/bmp", ".svg": "image/svg+xml"}
        for img_name in inline_images:
            ext = Path(img_name).suffix.lower()
            mime = _img_mime.get(ext, "image/png")
            # XML id として使えるよう英数字以外をアンダーバーに置換し先頭に "img-" を付与
            img_id = "img-" + re.sub(r"[^a-zA-Z0-9_-]", "_", img_name)
            manifest_items.append(
                f'<item id="{img_id}" href="images/{img_name}" media-type="{mime}"/>'
            )

    manifest_str = "\n    ".join(manifest_items)
    spine_str    = "\n    ".join(spine_items)
    cover_meta   = ('\n    <meta name="cover" content="cover-image"/>' if cover_fmt else "")
    desc_meta    = (f"\n    <dc:description>{_esc(synopsis)}</dc:description>" if synopsis else "")
    # 縦書き: iPad/iOS Kindle 縦書き対応のため primary-writing-mode を明示。横書きは不要
    writing_mode_meta = (
        "" if horizontal
        else '\n    <meta name="primary-writing-mode" content="horizontal-rl"/>'
    )
    page_dir = "ltr" if horizontal else "rtl"

    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf"
         version="3.0"
         unique-identifier="book-id"
         xml:lang="ja">

  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:{book_id}</dc:identifier>
    <dc:title>{_esc(title)}</dc:title>
    <dc:creator id="creator">{_esc(author)}</dc:creator>
    <meta refines="#creator" property="role" scheme="marc:relators">aut</meta>
    <dc:language>ja</dc:language>
    <dc:date>{today}</dc:date>{desc_meta}
    <meta property="dcterms:modified">{now_iso}</meta>{cover_meta}
    <meta property="rendition:layout">reflowable</meta>
    <meta property="rendition:orientation">auto</meta>
    <meta property="rendition:spread">none</meta>{writing_mode_meta}
  </metadata>

  <manifest>
    {manifest_str}
  </manifest>

  <spine page-progression-direction="{page_dir}">
    {spine_str}
  </spine>

</package>
"""


# ── フォントパス（Pillow用）─ 起動時に日本語グリフを持つフォントを自動探索 ──
_COVER_W, _COVER_H = 800, 1200

def _find_cjk_fonts() -> tuple:
    """
    日本語グリフを持つ TTC/OTF/TTF フォントを優先順で探して
    (bold_path, bold_index, medium_path, medium_index) を返す。
    見つからなければ (None, 0, None, 0)。

    探索順:
      0. 環境変数 NOVEL_DL_COVER_FONT で明示指定されたフォント（GUI・Android 用）
      1. fc-list コマンドで日本語対応フォントを列挙（Linux/macOS）
      2. OS別既知ディレクトリをグロブで再帰検索
      3. matplotlib の FontManager を利用（インストール済みの場合）
    """
    import os
    import glob
    import subprocess

    # ── 環境変数による明示指定（GUI・Android 用、最優先） ──────────
    # NOVEL_DL_COVER_FONT にフォントファイルのパスを設定すると、
    # 以降の探索をスキップして bold / medium ともそのフォントを使う。
    env_font = os.environ.get("NOVEL_DL_COVER_FONT", "")
    if env_font and os.path.isfile(env_font):
        return (env_font, 0, env_font, 0)

    # ── 探索ディレクトリ（再帰検索） ──────────────────────────────
    search_dirs = [
        # Linux: opentype / truetype
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.expanduser("~/.local/share/fonts"),
        os.path.expanduser("~/.fonts"),
        # macOS
        "/Library/Fonts",
        "/System/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
        # Windows
        r"C:\Windows\Fonts",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),
    ]

    def glob_find(pattern: str) -> str | None:
        """パターン（glob可）でフォントファイルを再帰検索し、最初のヒットを返す。"""
        for d in search_dirs:
            if not os.path.isdir(d):
                continue
            # まず直下を探し、次にサブディレクトリを再帰探索
            for hit in (glob.glob(os.path.join(d, pattern))
                        + glob.glob(os.path.join(d, "**", pattern), recursive=True)):
                if os.path.isfile(hit):
                    return hit
        return None

    # ── fc-list による探索（Linux / macOS） ────────────────────────
    def fclist_find_jp() -> list[str]:
        """fc-list :lang=ja でパスを列挙して返す。"""
        try:
            out = subprocess.check_output(
                ["fc-list", ":lang=ja", "--format=%{file}\n"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode("utf-8", errors="replace")
            return [p.strip() for p in out.splitlines() if p.strip()]
        except Exception:
            return []

    # ── matplotlib FontManager による探索 ─────────────────────────
    def mpl_find(name_keywords: list[str]) -> str | None:
        """matplotlib.font_manager でキーワードを含むフォントパスを返す。"""
        try:
            from matplotlib import font_manager as fm
            for entry in fm.fontManager.ttflist:
                low = entry.name.lower()
                if any(kw.lower() in low for kw in name_keywords):
                    if os.path.isfile(entry.fname):
                        return entry.fname
        except Exception:
            pass
        return None

    # ── TTCフェイスのJPインデックスを判定 ─────────────────────────
    def jp_index(path: str) -> int:
        """
        TTCファイルに含まれるフェイスのうち "JP" を名前に含むものの
        インデックスを返す。見つからなければ 0。
        """
        try:
            from PIL import ImageFont
            for i in range(20):
                try:
                    f = ImageFont.truetype(path, 12, index=i)
                    name = f.getname()[0].upper()
                    if "JP" in name:
                        return i
                except Exception:
                    break
        except Exception:
            pass
        return 0  # TTCでも index=0 が JP の場合が多い

    # ── 候補リスト: (boldパターン, mediumパターン) ────────────────
    # ファイル名はワイルドカード可。None は bold と同じパスを流用。
    CANDIDATES: list[tuple[str, str | None]] = [
        # Noto Serif CJK（明朝体・推奨）
        ("NotoSerifCJK-Bold.ttc",      "NotoSerifCJK-Medium.ttc"),
        ("NotoSerifCJK-Black.ttc",     "NotoSerifCJK-Regular.ttc"),
        ("Noto Serif CJK JP Bold.ttf", "Noto Serif CJK JP Regular.ttf"),
        # Noto Sans CJK（ゴシック体）
        ("NotoSansCJK-Bold.ttc",       "NotoSansCJK-Medium.ttc"),
        ("NotoSansCJK-Black.ttc",      "NotoSansCJK-Regular.ttc"),
        ("Noto Sans CJK JP Bold.ttf",  "Noto Sans CJK JP Regular.ttf"),
        # IPAex（日本語専用フリーフォント）
        ("ipaexg.ttf",                 "ipaexg.ttf"),
        ("ipaexm.ttf",                 "ipaexm.ttf"),
        ("ipag.ttf",                   "ipag.ttf"),
        ("ipam.ttf",                   "ipam.ttf"),
        # 源ノ明朝 / Source Han Serif
        ("SourceHanSerif*Bold*.otf",   "SourceHanSerif*Regular*.otf"),
        ("SourceHanSerif*Bold*.ttf",   "SourceHanSerif*Regular*.ttf"),
        # 源ノ角ゴシック / Source Han Sans
        ("SourceHanSans*Bold*.otf",    "SourceHanSans*Regular*.otf"),
        # ── Windows 標準・Office付属フォント ──────────────────────
        # BIZ UDP明朝 (BIZ UD Mincho) - Windows 11標準明朝体
        ("BIZ-UDMINCHOM*.TTC",         "BIZ-UDMINCHOM*.TTC"),
        # 游明朝 (Yu Mincho) - Windows + Microsoft Office付属明朝体
        ("yumindb.ttf",                "yumin.ttf"),
        # MS 明朝 (MS Mincho / MS PMincho) - Windows標準明朝体（常備）
        ("MSMINCHOM*.TTC",             "MSMINCHOM*.TTC"),
        ("msmincho.ttc",               "msmincho.ttc"),
        # HGS明朝E / HGP明朝E - Microsoft Office付属
        ("HGSMINCE.TTC",               "HGSMINCE.TTC"),
        ("HGPMINCE.TTC",               "HGPMINCE.TTC"),
        # 游ゴシック (Yu Gothic) - Windows 8.1以降
        ("YuGothB.ttc",                "YuGothM.ttc"),
        ("yugothb.ttf",                "yugothr.ttf"),
        # MS ゴシック (MS Gothic) - Windows標準ゴシック体（常備）
        ("msgothic.ttc",               "msgothic.ttc"),
        # Meiryo - Windows Vista以降
        ("meiryob.ttc",                "meiryo.ttc"),
        # ── 最終手段 ──────────────────────────────────────────────
        # WenQuanYi（Linux）
        ("wqy-zenhei.ttc",             "wqy-zenhei.ttc"),
        ("wqy-microhei.ttc",           "wqy-microhei.ttc"),
    ]

    # fc-list で日本語フォントのパスを取得しておく
    jp_paths = fclist_find_jp()

    def _resolve(pattern: str, fallback: str | None = None) -> tuple[str, int] | tuple[None, int]:
        """
        1) glob_find でファイルを探す
        2) 見つからなければ fc-list 結果からファイル名でマッチ
        3) それも失敗なら None
        """
        # glob にワイルドカードが含まれる場合は glob_find が対応
        path = glob_find(pattern)
        if path is None and jp_paths:
            # fc-list の結果からベース名でマッチ（ワイルドカードなし部分を使用）
            bare = pattern.replace("*", "").lower()
            for p in jp_paths:
                if bare in os.path.basename(p).lower():
                    path = p
                    break
        if path is None:
            return None, 0
        ext = os.path.splitext(path)[1].lower()
        idx = jp_index(path) if ext == ".ttc" else 0
        return path, idx

    bold_path = bold_idx = medium_path = medium_idx = None
    for bold_pat, med_pat in CANDIDATES:
        bp, bi = _resolve(bold_pat)
        if bp is None:
            continue
        if med_pat:
            mp, mi = _resolve(med_pat)
            if mp is None:
                mp, mi = bp, bi   # medium が見つからなければ bold で代替
        else:
            mp, mi = bp, bi
        bold_path, bold_idx     = bp, bi
        medium_path, medium_idx = mp, mi
        break

    # グロブ・fc-list で見つからなかった場合は matplotlib で最終試行
    if bold_path is None:
        for kws in [
            ["noto serif cjk", "jp"], ["noto sans cjk", "jp"],
            ["ipaex"], ["source han serif"], ["wenquanyi"],
            # Windows フォント
            ["biz ud mincho"], ["biz udp mincho"],
            ["yu mincho"], ["yumin"],
            ["ms mincho"], ["ms pmincho"],
            ["hgs mincho"], ["hgp mincho"],
            ["yu gothic"], ["ms gothic"],
            ["meiryo"],
        ]:
            p = mpl_find(kws)
            if p:
                bold_path = medium_path = p
                ext = os.path.splitext(p)[1].lower()
                bold_idx = medium_idx = jp_index(p) if ext == ".ttc" else 0
                break

    return bold_path, bold_idx, medium_path, medium_idx

# フォント検出は自動生成表紙（make_cover_image）でのみ必要なため遅延実行する
_JP_FONTS_CACHE: tuple | None = None


def _get_jp_fonts() -> tuple:
    """(_bold_path, _bold_idx, _medium_path, _medium_idx) を検出してキャッシュ。"""
    global _JP_FONTS_CACHE
    if _JP_FONTS_CACHE is not None:
        return _JP_FONTS_CACHE
    _JP_FONTS_CACHE = _find_cjk_fonts()
    bold_path, bold_idx, medium_path, medium_idx = _JP_FONTS_CACHE
    if bold_path:
        _b = os.path.basename(bold_path)
        _m = os.path.basename(medium_path) if medium_path else _b
        print(f"[情報] 日本語フォント検出: bold={_b}[{bold_idx}]  medium={_m}[{medium_idx}]",
              file=sys.stderr)
    else:
        print(
            "[警告] 日本語フォントが見つかりませんでした。JPEG表紙はSVGで代替されます。\n"
            "       フォントをインストールすると JPEG 表紙が生成されます:\n"
            "       [Linux]   sudo apt install fonts-noto-cjk\n"
            "                 または: sudo apt install fonts-ipafont\n"
            "       [Windows] BIZ UDP明朝 / MS明朝 / 游明朝 など日本語フォントが\n"
            "                 C:\\Windows\\Fonts に存在するか確認してください。\n"
            "                 Microsoft Office をインストールすると游明朝が追加されます。",
            file=sys.stderr
        )
    return _JP_FONTS_CACHE


def _make_cover_svg(title: str, author: str, cover_bg: str = "#16234b") -> bytes:
    """
    Pillow不要のSVG表紙を生成する。
    標準ライブラリのみで動作するフォールバック。
    """
    W, H = 800, 1200

    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    # タイトルを折り返す（1行20文字目安）
    MAX_CH = 14
    t_lines = []
    buf = ""
    for ch in title:
        buf += ch
        if len(buf) >= MAX_CH:
            t_lines.append(buf)
            buf = ""
    if buf:
        t_lines.append(buf)

    title_fs  = 72
    title_y0  = int(H * 0.25)
    line_gap  = title_fs + 20
    title_els = "\n".join(
        f'  <text x="400" y="{title_y0 + i * line_gap}" '
        f'font-size="{title_fs}" fill="#fff8d7" '
        f'text-anchor="middle" font-family="serif" font-weight="bold">'
        f'{esc(l)}</text>'
        for i, l in enumerate(t_lines)
    )

    # 作者名は下飾り線(H*0.80)より下のエリア中央に固定配置
    LINE_Y2  = int(H * 0.80)
    BOTTOM   = H - 50   # 下枠内側
    author_y = LINE_Y2 + (BOTTOM - LINE_Y2) // 2 + 56 // 3
    author_el = (
        f'  <text x="400" y="{author_y}" '
        f'font-size="56" fill="#dccda8" '
        f'text-anchor="middle" font-family="serif">'
        f'{esc(author)}</text>'
    )

    _r0, _g0, _b0 = _parse_hex_color(cover_bg)
    _r1, _g1, _b1 = _darken_color(_r0, _g0, _b0)
    _color_top    = f"#{_r1:02x}{_g1:02x}{_b1:02x}"
    _color_bottom = f"#{_r0:02x}{_g0:02x}{_b0:02x}"

    svg = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%"   stop-color="{_color_top}"/>
      <stop offset="100%" stop-color="{_color_bottom}"/>
    </linearGradient>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect x="38" y="38" width="{W-76}" height="{H-76}" fill="none" stroke="#c8b482" stroke-width="3"/>
  <rect x="50" y="50" width="{W-100}" height="{H-100}" fill="none" stroke="#b4a06e" stroke-width="1"/>
  <line x1="68" y1="{int(H*0.14)}" x2="{W-68}" y2="{int(H*0.14)}" stroke="#c8b482" stroke-width="1"/>
  <line x1="68" y1="{int(H*0.80)}" x2="{W-68}" y2="{int(H*0.80)}" stroke="#c8b482" stroke-width="1"/>
{title_els}
{author_el}
</svg>"""
    return svg.encode("utf-8")


def make_cover_image(title: str, author: str, cover_bg: str = "#16234b"):
    """
    書籍表紙を模したカバー画像を生成してバイト列で返す。
    戻り値: (data: bytes, fmt: str)
      fmt = "jpg"  Pillow利用可能時（JPEG形式）
      fmt = "svg"  Pillowなし時のフォールバック
    例外は捕捉して SVG フォールバックに切り替える。
    """
    if _PILLOW_AVAILABLE:
        try:
            _FONT_BOLD_PATH, _FONT_BOLD_IDX, _FONT_MEDIUM_PATH, \
                _FONT_MEDIUM_IDX = _get_jp_fonts()
            W, H = _COVER_W, _COVER_H
            img  = Image.new("RGB", (W, H))
            draw = ImageDraw.Draw(img)

            # 背景グラデーション（上: 暗め / 下: 指定色）
            _r0, _g0, _b0 = _parse_hex_color(cover_bg)
            _r1, _g1, _b1 = _darken_color(_r0, _g0, _b0)
            for y in range(H):
                t = y / H
                r = int(_r1 + (_r0 - _r1) * t)
                g = int(_g1 + (_g0 - _g1) * t)
                b = int(_b1 + (_b0 - _b1) * t)
                draw.line([(0, y), (W, y)], fill=(r, g, b))

            # 外枠（二重線）
            M    = 38
            GOLD     = (200, 180, 130)
            GOLD_DIM = (180, 160, 110)
            draw.rectangle([M,    M,    W-M,    H-M   ], outline=GOLD,     width=3)
            draw.rectangle([M+12, M+12, W-M-12, H-M-12], outline=GOLD_DIM, width=1)

            # レイアウト定数
            # LINE_Y1: タイトル領域の上端飾り線
            # LINE_Y2: タイトル領域の下端飾り線 ＝ 作者名エリアの上境界
            # 作者名は LINE_Y2 より下（下枠マージン内）に固定配置する
            LINE_Y1   = int(H * 0.14)
            LINE_Y2   = int(H * 0.80)
            AUTHOR_SZ = 56
            # 作者名エリア: LINE_Y2 ～ 下枠(H-M) の中央に配置
            # getbbox で ascent 分の余白を考慮し、視覚的中央を求める
            AUTHOR_AREA_TOP = LINE_Y2
            AUTHOR_AREA_BOT = H - M - 10          # 下枠内側ギリギリ
            draw.line([(M+30, LINE_Y1), (W-M-30, LINE_Y1)], fill=GOLD, width=1)
            draw.line([(M+30, LINE_Y2), (W-M-30, LINE_Y2)], fill=GOLD, width=1)

            def load_font(path, idx, size):
                """CJKフォントを読み込む。パスがNoneまたは失敗時はNoneを返す。"""
                if path is None:
                    return None
                try:
                    return ImageFont.truetype(path, size, index=idx)
                except Exception:
                    return None

            def wrap_text(text, font, max_w):
                lines, cur = [], ""
                for ch in text:
                    test = cur + ch
                    try:
                        w = font.getbbox(test)[2]
                    except Exception:
                        w = len(test) * (getattr(font, "size", 12))
                    if w > max_w:
                        lines.append(cur)
                        cur = ch
                    else:
                        cur = test
                if cur:
                    lines.append(cur)
                return lines

            max_title_w = W - M * 2 - 50
            # タイトル描画可能な縦幅（LINE_Y1 ～ LINE_Y2、上下に余白を確保）
            TITLE_PAD_TOP = int((LINE_Y2 - LINE_Y1) * 0.08)
            TITLE_PAD_BOT = int((LINE_Y2 - LINE_Y1) * 0.08)
            title_region_h = (LINE_Y2 - LINE_Y1) - TITLE_PAD_TOP - TITLE_PAD_BOT

            # CJKフォントが見つからない場合はSVGフォールバックへ
            if _FONT_BOLD_PATH is None:
                raise RuntimeError("CJK font not found")

            # タイトルが収まる最大フォントサイズを算出
            title_sz = 92
            while True:
                font_t = load_font(_FONT_BOLD_PATH, _FONT_BOLD_IDX, title_sz)
                if font_t is None:
                    raise RuntimeError("Failed to load bold font")
                lines  = wrap_text(title, font_t, max_title_w)
                if len(lines) * (title_sz + 18) <= title_region_h or title_sz <= 28:
                    break
                title_sz -= 4
            line_h = title_sz + 18
            # タイトルブロック全体を LINE_Y1～LINE_Y2 の中央に縦配置
            block_h   = len(lines) * line_h
            title_top = LINE_Y1 + TITLE_PAD_TOP + max(0, (title_region_h - block_h) // 2)
            for i, line in enumerate(lines):
                try:
                    lw = font_t.getbbox(line)[2]
                except Exception:
                    lw = len(line) * title_sz
                x = (W - lw) / 2
                y = title_top + i * line_h
                draw.text((x+3, y+3), line, font=font_t, fill=(0, 0, 0, 100))
                draw.text((x,   y  ), line, font=font_t, fill=(255, 248, 215))

            # ── 作者名：LINE_Y2 より下のエリア中央に固定配置 ──────────
            # 著者名が横幅に収まるようにフォントサイズを縮小
            author_sz = AUTHOR_SZ
            while author_sz >= 20:
                font_a = load_font(_FONT_MEDIUM_PATH, _FONT_MEDIUM_IDX, author_sz)
                if font_a is None:
                    font_a = font_t
                    break
                try:
                    _aw_test = font_a.getbbox(author)[2]
                except Exception:
                    _aw_test = len(author) * author_sz
                if _aw_test <= max_title_w:
                    break
                author_sz -= 4
            try:
                ab = font_a.getbbox(author)   # (left, top, right, bottom)
                aw = ab[2] - ab[0]
                ah = ab[3] - ab[1]
            except Exception:
                aw = len(author) * author_sz
                ah = author_sz
            ax = (W - aw) / 2
            # 作者名エリアの視覚的中央（ascent オフセットを補正）
            area_h = AUTHOR_AREA_BOT - AUTHOR_AREA_TOP
            ay = AUTHOR_AREA_TOP + (area_h - ah) / 2 - (ab[1] if 'ab' in locals() else 0)
            draw.text((ax+2, ay+2), author, font=font_a, fill=(0, 0, 0, 100))
            draw.text((ax,   ay  ), author, font=font_a, fill=(220, 205, 170))

            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=90, optimize=True)
            return buf.getvalue(), "jpg"

        except Exception as _png_err:
            import traceback as _tb
            print(
                "[警告] PillowでのJPEG表紙生成中にエラーが発生しました。SVGで代替します。\n"
                f"       エラー内容: {_png_err}\n"
                "       詳細:\n"
                + "".join(f"         {l}" for l in _tb.format_exc().splitlines(keepends=True))
            )

    # Pillow不在 or PNG生成失敗 → SVGフォールバック
    print(
        "[警告] SVGフォールバックで表紙を生成します。"
        "多くのePubリーダーではSVG表紙が正しく表示されない場合があります。"
    )
    return _make_cover_svg(title, author, cover_bg), "svg"


def build_epub(
    epub_path: str,
    title: str,
    author: str,
    synopsis: str,
    source_url: str,
    site_name: str,
    episodes: list,          # [{"title": str, "body": str}, ...]
    cover_bg: str = "#16234b",
    cover_image_path: str = "",  # ローカル表紙画像ファイルパス（JPEG/PNG）
    cover_image_data: bytes = b"",   # 表紙画像の生データ（cover_image_path より優先）
    cover_image_fmt: str = "jpg",    # cover_image_data の形式（"jpg" / "png"）
    font_path: str = "",
    toc_at_end: bool = False,
    images: dict = None,     # {"filename.png": bytes} — 本文中のインライン画像
    horizontal: bool = False,  # True: 横書きePub3を生成
):
    """
    ePub3ファイルを生成する。horizontal=True で横書き、False（デフォルト）で縦書き。

    ePub3構造（画像表紙あり）:
      mimetype
      META-INF/container.xml
      OEBPS/package.opf
      OEBPS/nav.xhtml
      OEBPS/css/novel.css           ← 本文CSS
      OEBPS/css/vertical_image.css  ← 画像表紙専用CSS
      OEBPS/images/0000.png         ← 表紙画像
      OEBPS/cover-image.xhtml       ← 【spine先頭】画像表紙ページ
      OEBPS/cover.xhtml             ← テキスト表紙（タイトル・著者・あらすじ）
      OEBPS/ep0001.xhtml … ep{N}.xhtml
      OEBPS/colophon.xhtml
    """
    book_id   = str(uuid.uuid4())
    ep_titles = [ep["title"] for ep in episodes]

    # エピソード本文から専用画像ページを分離して本文spine構成を決める。
    # 各エピソードの先頭ファイルは ep{n:04d}.xhtml（toc/nav のリンク先）で固定し、
    # 続きの本文は ep{n:04d}_2.xhtml…、画像ページは img{m:04d}.xhtml とする。
    doc_plan = []   # (id, href, seg, ep, show_title)
    img_page_no = 0
    for _i, _ep in enumerate(episodes):
        segs = _split_episode_images(_ep["body"]) or [("text", _ep["body"])]
        title_shown = False
        for _si, _seg in enumerate(segs):
            if _si == 0:
                did = f"ep{_i+1:04d}"
            elif _seg[0] == "text":
                did = f"ep{_i+1:04d}_{_si+1}"
            else:
                img_page_no += 1
                did = f"img{img_page_no:04d}"
            show_title = _seg[0] == "text" and not title_shown
            if _seg[0] == "text":
                title_shown = True
            doc_plan.append((did, did + ".xhtml", _seg, _ep, show_title))

    # 表紙画像：生データ指定 > 外部ファイル指定 > 自動生成
    if cover_image_data:
        cover_data, cover_fmt = cover_image_data, cover_image_fmt
    elif cover_image_path:
        if not os.path.isfile(cover_image_path):
            print(f"[警告] 表紙画像ファイルが見つかりません: {cover_image_path}")
            print("       自動生成の表紙を使用します。")
            cover_data, cover_fmt = make_cover_image(title, author, cover_bg)
        else:
            _ext = Path(cover_image_path).suffix.lower()
            if _ext in (".jpg", ".jpeg"):
                cover_fmt = "jpg"
            elif _ext == ".png":
                cover_fmt = "png"
            else:
                print(f"[警告] 非対応の画像形式です: {_ext}（対応: .jpg / .jpeg / .png）")
                print("       自動生成の表紙を使用します。")
                cover_data, cover_fmt = make_cover_image(title, author, cover_bg)
            if cover_fmt in ("jpg", "png"):
                with open(cover_image_path, "rb") as _f:
                    cover_data = _f.read()
                print(f"  表紙画像: {cover_image_path}")
    else:
        # 表紙画像を自動生成（Pillow利用可能時JPEG、なければSVGフォールバック）
        cover_data, cover_fmt = make_cover_image(title, author, cover_bg)

    # 埋め込みフォントの準備（CSS注入対策: " \ 改行を除去）
    if font_path and not os.path.isfile(font_path):
        print(f"[警告] フォントファイルが見つかりません: {font_path}")
        print("       埋め込みフォントなしで ePub を生成します。")
        font_path = ""
    _css_unsafe = re.compile(r'["\\\n\r]')
    font_filename = _css_unsafe.sub("", Path(font_path).name) if font_path else ""
    font_name     = _css_unsafe.sub("", Path(font_path).stem) if font_path else ""

    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype は圧縮なし・先頭に配置（ePub仕様）
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)

        # META-INF/container.xml
        zf.writestr("META-INF/container.xml", """\
<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/package.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""")

        # package.opf（cover_fmt / font_filename を渡して manifest/spine を決定）
        zf.writestr("OEBPS/package.opf",
                    _make_opf(title, author, book_id, ep_titles, cover_fmt,
                              font_filename=font_filename,
                              toc_at_end=toc_at_end,
                              inline_images=list(images.keys()) if images else None,
                              synopsis=synopsis,
                              horizontal=horizontal,
                              doc_items=[(d, h) for d, h, _s, _e, _t in doc_plan]))

        # nav.xhtml（RS向け機械読み取り専用、spine には linear="no" で含める）
        zf.writestr("OEBPS/nav.xhtml",
                    _make_nav_xhtml(title, episodes, cover_fmt,
                                    horizontal=horizontal))

        # toc.xhtml（読者向け目次、spine に linear="yes" で含める）
        zf.writestr("OEBPS/toc.xhtml",
                    _make_toc_xhtml(title, episodes, cover_fmt,
                                    horizontal=horizontal))

        # 本文CSS（フォント指定あり時は @font-face を追加）
        zf.writestr("OEBPS/css/novel.css",
                    _make_epub_css(font_name, font_filename))

        # 埋め込みフォント
        if font_path:
            with open(font_path, "rb") as _ff:
                zf.writestr(f"OEBPS/fonts/{font_filename}", _ff.read())

        # 表紙画像 + 表紙XHTML → spine 1ページ目
        zf.writestr(f"OEBPS/images/cover.{cover_fmt}", cover_data)
        zf.writestr("OEBPS/cover-image.xhtml",
                    _make_cover_image_xhtml(title, cover_fmt))

        # テキスト表紙（タイトル・著者・あらすじ）→ spine 2ページ目
        zf.writestr("OEBPS/cover.xhtml",
                    _make_cover_xhtml(title, author, synopsis,
                                      source_url=source_url, site_name=site_name,
                                      horizontal=horizontal))

        # 各話（本文パート＋専用画像ページ）
        for did, href, seg, ep, show_title in doc_plan:
            if seg[0] == "text":
                content = _make_episode_xhtml(ep["title"], seg[1],
                                              horizontal=horizontal,
                                              show_title=show_title)
            else:
                content = _make_image_page_xhtml(seg[1], seg[2])
            zf.writestr("OEBPS/" + href, content)

        # インライン画像（青空文庫 ZIP 内の挿絵等）
        if images:
            for img_name, img_bytes in images.items():
                zf.writestr(f"OEBPS/images/{img_name}", img_bytes)

        # 奥付
        zf.writestr("OEBPS/colophon.xhtml",
                    _make_colophon_xhtml(title, source_url, site_name,
                                         horizontal=horizontal))


def _strip_heading_block(lines: list) -> int:
    """先頭から見出しブロックを読み飛ばし、本文開始行インデックスを返す。"""
    i = 0
    # 先頭空行をスキップ
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return i
    ln = lines[i]
    start_m = _MIDASHI_START_RE.search(ln)
    if not start_m:
        return i
    visible = _AOZORA_ANY_TAG_RE.sub("", ln).strip()
    if visible:
        # インライン形式（タグ除去後も可視テキストがある）→ 1行だけスキップ
        return i + 1
    # ブロック形式: 開始タグ行 → テキスト行 → 終了タグ行
    i += 1  # 開始タグ行
    if i < len(lines):
        i += 1  # テキスト行
    if i < len(lines) and _MIDASHI_END_RE.search(lines[i]):
        i += 1  # 終了タグ行
    return i


def _split_aozora_by_headings(body_text: str) -> list:
    """
    青空文庫本文を大/中/小見出しタグの位置でチャプター分割する。
    見出しが存在しない場合は [] を返す。
    各セクションの body は見出し行を除いた本文のみ。
    Returns: [{"title": str, "body": str}, ...]
    """
    lines = body_text.split("\n")

    # 見出し行インデックスとタイトルを収集（終わりマーカーは除外）
    heading_positions: list = []
    for i, ln in enumerate(lines):
        m = re.search(r"「(.+)」は(大|中|小)見出し］", ln)
        if m and "終わり" not in ln:
            heading_positions.append((i, m.group(1)))

    if not heading_positions:
        return []

    sections: list = []

    # 最初の見出し前のテキスト（前文等）
    pre_text = "\n".join(lines[: heading_positions[0][0]]).strip()
    if pre_text:
        sections.append({"title": "", "body": pre_text})

    for j, (line_idx, title) in enumerate(heading_positions):
        next_idx = heading_positions[j + 1][0] if j + 1 < len(heading_positions) else len(lines)
        section_lines = lines[line_idx:next_idx]
        body_start = _strip_heading_block(section_lines)
        section_body = "\n".join(section_lines[body_start:]).strip()
        sections.append({"title": title, "body": section_body})

    return sections


def parse_aozora_text(content: str) -> tuple:
    """
    青空文庫書式テキスト（このツールが出力する形式）を解析して
    (title, author, synopsis, episodes) を返す。

    対応形式:
      - このツールが出力する青空文庫書式（見出しマーカー・PAGE_BREAK付き）
      - 先頭2行にタイトル・著者があるシンプルなテキスト

    episodes: [{"title": str, "body": str}, ...]
    """
    lines = content.split("\n")

    # 1行目: タイトル、2行目: 著者
    title  = lines[0].strip() if len(lines) > 0 else "（タイトル不明）"
    author = lines[1].strip() if len(lines) > 1 else "（作者不明）"

    # ヘッダー区切り線（---...）を2つ探してヘッダー範囲を確定する
    synopsis      = ""
    body_start_ln = 3          # ヘッダーが検出できなかった場合のデフォルト
    sep_count     = 0
    in_synopsis   = False
    for i in range(2, min(len(lines), 60)):
        ln = lines[i]
        if ln.startswith("----------"):   # 10文字以上のダッシュ列
            sep_count += 1
            in_synopsis = False
            if sep_count == 2:
                body_start_ln = i + 1
                break
        elif "【あらすじ】" in ln:
            in_synopsis = True
        elif in_synopsis:
            synopsis += ln + "\n"

    synopsis     = synopsis.strip()
    body_content = "\n".join(lines[body_start_ln:])

    # 奥付（"底本："で始まるブロック）を末尾から除去
    col_pos = body_content.rfind("\n\n底本：")
    if col_pos >= 0:
        body_content = body_content[:col_pos]

    # PAGE_BREAK で章・話に分割し、各セクションをさらに大/中/小見出しで分割
    raw_sections = body_content.split("［＃改ページ］")

    episodes = []
    for sec in raw_sections:
        sec = sec.strip()
        if not sec:
            continue

        subsections = _split_aozora_by_headings(sec)
        if subsections:
            for sub in subsections:
                ep_num = len(episodes) + 1
                episodes.append({
                    "title": sub["title"] or f"第{ep_num}話",
                    "body":  sub["body"],
                })
        else:
            ep_lines   = sec.split("\n")
            ep_title   = ""
            body_start = 0

            for li, ln in enumerate(ep_lines):
                # 見出し終わりマーカーが見つかったらその次行から本文
                if re.search(r"は(?:大|中|小)見出し終わり］", ln):
                    body_start = li + 1
                    break
                # 見出し開始マーカーからタイトルを取得
                m = re.search(r"「(.+?)」は(?:大|中|小)見出し］", ln)
                if m:
                    ep_title = m.group(1)

            body_text = "\n".join(ep_lines[body_start:]).strip()
            # 見出しマーカーのないセクション＝章内の意図的な改ページ。
            # 新しい章にはせず、前の章の本文に改ページ指示として連結する
            # （XHTML変換で break-before: page の区切りになる）
            had_marker = bool(ep_title) or body_start > 0
            if not had_marker and episodes:
                episodes[-1]["body"] = (episodes[-1]["body"].rstrip()
                                        + "\n［＃改ページ］\n" + body_text)
            else:
                episodes.append({
                    "title": ep_title or f"第{len(episodes) + 1}話",
                    "body":  body_text,
                })

    # 見出しマーカーがなく1セクションしかない場合はタイトルをそのまま使用
    if len(episodes) == 1 and episodes[0]["title"].startswith("第1話"):
        episodes[0]["title"] = title

    # エピソードが空 → ファイル全体を1エピソードとして扱う
    if not episodes and body_content.strip():
        episodes.append({"title": title, "body": body_content.strip()})

    return title, author, synopsis, episodes


def _pdf_cover_bytes(args, doc, npages):
    """--cover-page 指定に従いPDFページを表紙JPEGにレンダリングする。
    --cover-image 指定時や自動生成表紙（--cover-page 0）では b"" を返す。"""
    if args.cover_image or args.cover_page <= 0:
        return b""
    if args.cover_page > npages:
        print(f"警告: --cover-page {args.cover_page} はページ範囲外。"
              "表紙を自動生成します。", file=sys.stderr)
        return b""
    pix = doc[args.cover_page - 1].get_pixmap(matrix=fitz.Matrix(2, 2))
    return pix.tobytes("jpeg", jpg_quality=85)


def run_from_text(args, doc, npages):
    """校正済みの青空文庫形式テキストと元PDFからePubを再生成する。

    テキストはこのツールの出力形式（1行目タイトル・2行目著者）を想定。
    本文が参照する画像ページは、テキスト隣の <名前>_images/ ディレクトリに
    ファイルがあればそれを使い、なければファイル名 pNNNN.jpg のページ番号で
    PDFから再レンダリングする（校正でファイル名を変えなければ再現できる）。
    """
    txt_path = args.from_text
    if not os.path.exists(txt_path):
        print(f"エラー: ファイルが見つかりません: {txt_path}", file=sys.stderr)
        sys.exit(1)

    content = None
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            with open(txt_path, "r", encoding=enc) as f:
                content = f.read()
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        print(f"エラー: テキストを読み込めません（エンコーディング不明）: {txt_path}",
              file=sys.stderr)
        sys.exit(1)

    title, author, synopsis, episodes = parse_aozora_text(content)
    if args.title:
        title = args.title
    if args.author:
        author = args.author
    if not title:
        title = os.path.splitext(os.path.basename(txt_path))[0]
    if not episodes:
        print("エラー: ePub生成用の本文を抽出できませんでした。", file=sys.stderr)
        sys.exit(1)

    # 本文が参照する画像を収集
    img_dir = os.path.splitext(txt_path)[0] + "_images"
    images_data = {}
    rerendered = 0
    for ep in episodes:
        figs = list(_FIG_CAP_RE.finditer(ep["body"])) \
             + list(_FIG_PLAIN_RE.finditer(ep["body"]))
        for m in figs:
            fname = m.group(2)
            if fname in images_data:
                continue
            path = os.path.join(img_dir, fname)
            pm = re.fullmatch(r"p(\d+)\.jpe?g", fname, re.IGNORECASE)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    images_data[fname] = f.read()
            elif pm and 1 <= int(pm.group(1)) <= npages:
                images_data[fname] = render_image_page(doc, int(pm.group(1)) - 1)
                rerendered += 1
            else:
                print(f"警告: 画像が見つかりません: {fname}", file=sys.stderr)

    print(f"校正済みテキスト: {txt_path}")
    print(f"  タイトル: {title} / 著者: {author or '（なし）'} / {len(episodes)} 章"
          + (f" / 画像 {len(images_data)} 枚（PDFから再取得 {rerendered} 枚）"
             if images_data else ""))

    out = args.output or txt_path
    epub_path = os.path.splitext(out)[0] + ".epub"
    cover_bytes = _pdf_cover_bytes(args, doc, npages)
    print(f"📖 ePub生成中: {epub_path}")
    build_epub(epub_path, title, author, synopsis, "", "自炊PDF",
               episodes, cover_bg=args.cover_bg,
               cover_image_path=args.cover_image or "",
               cover_image_data=cover_bytes,
               images=images_data or None)
    print(f"✅ ePub出力完了: {epub_path}")


def main():
    ap = argparse.ArgumentParser(
        description="自炊PDF（OCR済み）→ 青空文庫形式テキスト変換")
    ap.add_argument("pdf", help="入力PDF（OCRテキスト層付き）")
    ap.add_argument("-o", "--output", help="出力テキストファイル")
    ap.add_argument("--title", help="タイトル（省略時は「タイトル_作者名.pdf」"
                                    "形式のファイル名から自動取得）")
    ap.add_argument("--author", default="", help="著者名（省略時はファイル名から自動取得）")
    ap.add_argument("--from-text", metavar="FILE",
                    help="校正済みの青空文庫形式テキストからePubを再生成する"
                         "（PDFは表紙・画像ページの取得にのみ使用。--epub 不要）")
    ap.add_argument("--pages", help="対象ページ範囲（例: 10-360 / 5,8,10-）")
    ap.add_argument("--ruby", choices=["aozora", "drop"], default="aozora",
                    help="ルビ処理: aozora=《》変換（既定） drop=除去")
    ap.add_argument("--no-indent", action="store_true",
                    help="段落頭の全角空白を入れない")
    ap.add_argument("--no-ocr-fix", action="store_true",
                    help="OCR系統誤りの後処理正規化（ｌ/Ｉ→――、○→〇、"
                         "〃″→〝〟、文末の・→。、小書きカナ並字化、"
                         "ルビ内漢字の自動訂正）を行わない")
    ap.add_argument("--no-toc-refine", action="store_true",
                    help="紙の目次ページとの照合による見出し精錬を行わない")
    ap.add_argument("--no-images", action="store_true",
                    help="挿絵・口絵・章頭ページの画像ページ化を行わない")
    ap.add_argument("--epub", action="store_true",
                    help="リフロー型縦書きePub3も生成する")
    ap.add_argument("--cover-page", type=int, default=1, metavar="N",
                    help="ePub表紙にするPDFページ番号（1始まり、既定=1、"
                         "0で表紙を自動生成）")
    ap.add_argument("--cover-image", metavar="FILE",
                    help="表紙画像ファイル（JPEG/PNG、--cover-page より優先）")
    ap.add_argument("--cover-bg", default="#16234b", metavar="#RRGGBB",
                    help="自動生成表紙の背景色")
    ap.add_argument("--inspect", metavar="N",
                    help="指定ページ（1始まり、カンマ区切り可）の解析結果を表示して終了")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    doc = fitz.open(args.pdf)
    npages = len(doc)

    if args.from_text:
        run_from_text(args, doc, npages)
        return

    page_nums = parse_pages_arg(args.pages, npages)
    # 表紙ページはePub表紙として使うため本文抽出から除外（帯・タイトルの
    # OCRノイズが冒頭に混入するのを防ぐ）。--cover-page 0 で無効化
    if args.cover_page > 0:
        page_nums = [i for i in page_nums if i != args.cover_page - 1]

    # 本文サイズ推定（対象ページの中央部からサンプリング）
    sample = page_nums[len(page_nums) // 4: len(page_nums) * 3 // 4] or page_nums
    body_size = detect_body_size(doc, sample[:80])
    if body_size <= 0:
        print("エラー: テキスト層が見つかりません。OCR済みPDFを指定してください。",
              file=sys.stderr)
        sys.exit(1)
    print(f"本文フォントサイズ: {body_size:.1f}pt  対象 {len(page_nums)}/{npages} ページ")

    if args.inspect:
        for s in args.inspect.split(","):
            inspect_page(doc, int(s) - 1, body_size)
        return

    # 全ページ解析
    pages = []
    for i in page_nums:
        pg = analyze_page(doc[i], i, body_size)
        if args.ruby == "drop":
            pg.rubies = []
        pages.append(pg)

    drop, headings, body_top, body_bottom, hashira_keys = \
        classify_marginals(pages, body_size)
    print(f"本文領域: y {body_top:.0f}〜{body_bottom:.0f} / "
          f"柱・ノンブル除去 {len(drop)} 行 / 見出し候補 {len(headings)} 個 / "
          f"柱パターン {len(hashira_keys)} 種")

    fn_title, fn_author = parse_meta_from_filename(args.pdf)
    title = args.title or fn_title
    author = args.author or fn_author
    out_base = f"{title}_{author}" if author else title
    out_path = args.output or os.path.join(
        os.path.dirname(os.path.abspath(args.pdf)) or ".", out_base + ".txt")

    # 画像ページ（挿絵・口絵・画像主体の章頭）の検出とレンダリング
    image_page_map = {}   # page_num -> ファイル名
    images_data = {}      # ファイル名 -> JPEGバイト列
    if not args.no_images:
        img_nums = classify_image_pages(doc, pages, drop)
        if args.cover_page > 0:
            img_nums.discard(args.cover_page - 1)   # 表紙ページは除外
        for pnum in sorted(img_nums):
            fname = f"p{pnum + 1:04d}.jpg"
            images_data[fname] = render_image_page(doc, pnum)
            image_page_map[pnum] = fname
        if image_page_map:
            img_dir = os.path.splitext(out_path)[0] + "_images"
            os.makedirs(img_dir, exist_ok=True)
            for fname, data in images_data.items():
                with open(os.path.join(img_dir, fname), "wb") as f:
                    f.write(data)
            print(f"画像ページ検出: {len(image_page_map)} 枚 → {img_dir}/")

    body = assemble_text(pages, drop, headings, body_size,
                         body_top, body_bottom, hashira_keys,
                         indent=not args.no_indent, verbose=args.verbose,
                         image_pages=image_page_map)

    if not args.no_toc_refine:
        body, toc_stats = refine_headings_with_toc(
            body, title, hashira_keys, verbose=args.verbose)
        if toc_stats:
            detail = " / ".join(f"{k} {v}" for k, v in toc_stats.items())
            print(f"目次照合: {detail}")

    if not args.no_ocr_fix:
        body, fix_stats = normalize_ocr_text(body)
        if fix_stats:
            detail = " / ".join(f"{k} {v}" for k, v in fix_stats.most_common())
            print(f"OCR後処理正規化: {sum(fix_stats.values())} 箇所（{detail}）")
        if args.ruby != "drop":
            body, ruby_fixed = fix_ruby_kanji(body)
            if ruby_fixed:
                print(f"ルビ内漢字の自動訂正: {ruby_fixed} 箇所")
            body, variant_fixed = fix_ruby_variants(body)
            if variant_fixed:
                print(f"ルビ濁点・小書き誤読の自動訂正: {variant_fixed} 箇所")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"{title}\n{author}\n\n{body}\n")
    print(f"✅ 青空文庫形式テキスト出力: {out_path}")

    if args.epub:
        epub_path = os.path.splitext(out_path)[0] + ".epub"
        _, _, synopsis, episodes = parse_aozora_text(
            f"{title}\n{author}\n\n{body}\n")
        if not episodes:
            print("エラー: ePub生成用の本文を抽出できませんでした。",
                  file=sys.stderr)
            sys.exit(1)
        cover_bytes = _pdf_cover_bytes(args, doc, npages)
        print(f"📖 ePub生成中: {epub_path}（{len(episodes)} 章）")
        build_epub(epub_path, title, author, synopsis, "", "自炊PDF",
                   episodes, cover_bg=args.cover_bg,
                   cover_image_path=args.cover_image or "",
                   cover_image_data=cover_bytes,
                   images=images_data or None)
        print(f"✅ ePub出力完了: {epub_path}")


if __name__ == "__main__":
    main()
