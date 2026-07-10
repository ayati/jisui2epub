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
を行い、novel_downloader.py --from-file でリフロー型ePub3に変換できる
青空文庫形式テキストを出力する。

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
import subprocess
import sys
import unicodedata
from collections import Counter

try:
    import fitz  # PyMuPDF
except ImportError:
    print("エラー: PyMuPDF が必要です。 pip install pymupdf", file=sys.stderr)
    sys.exit(1)

# ── 定数 ──────────────────────────────────────────

# ルビが付き得る文字（漢字・繰り返し記号など）
RUBYABLE_RE = re.compile(r'[㐀-鿿豈-﫿々〆ヶ〇]')
# ノンブル（ページ番号）: 半角/全角数字・漢数字のみ
NOMBRE_RE = re.compile(r'^[0-9０-９一二三四五六七八九十百]+$')
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
        if not self.cells:
            return self.size
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
    # 本文領域（縦行の上端・下端の中央値）
    tops, bottoms = [], []
    for pg in pages:
        body = [v for v in pg.vlines
                if len(v.cells) >= 3 and v.size >= body_size * 0.8]
        if body:
            tops.append(min(v.y0 for v in body))
            bottoms.append(max(v.y1 for v in body))
    body_top = statistics.median(tops) if tops else 0
    body_bottom = statistics.median(bottoms) if bottoms else 1e9
    cell = body_size

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
                  hashira_keys=None, indent=True, verbose=False):
    """全ページから青空文庫形式の本文を組み立てる。"""
    hashira_keys = hashira_keys or {}
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
            out.append("［＃改ページ］")
        out.append(f"［＃「{title}」は中見出し］")
        out.append(title)
        out.append(f"［＃「{title}」は中見出し終わり］")
        out.append("")
        first_heading_done = True
        prev_line_short = True
        last_heading_norm = (norm, digits)
        paras_since_heading = 0

    body_height = max(body_bottom - body_top, body_size)

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

        if page_headings:
            emit_heading("　".join(page_headings))
        for t in long_items:
            flush()
            out.append(("　" if indent else "") + t)

        if not body_lines:
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
        # ページ末尾: 段落継続は次ページに持ち越す

    flush()
    if junk_count[0]:
        print(f"挿絵ノイズ除去: {junk_count[0]} 行")
    return "\n".join(out)


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


def main():
    ap = argparse.ArgumentParser(
        description="自炊PDF（OCR済み）→ 青空文庫形式テキスト変換")
    ap.add_argument("pdf", help="入力PDF（OCRテキスト層付き）")
    ap.add_argument("-o", "--output", help="出力テキストファイル")
    ap.add_argument("--title", help="タイトル（省略時はファイル名）")
    ap.add_argument("--author", default="", help="著者名")
    ap.add_argument("--pages", help="対象ページ範囲（例: 10-360 / 5,8,10-）")
    ap.add_argument("--ruby", choices=["aozora", "drop"], default="aozora",
                    help="ルビ処理: aozora=《》変換（既定） drop=除去")
    ap.add_argument("--no-indent", action="store_true",
                    help="段落頭の全角空白を入れない")
    ap.add_argument("--epub", action="store_true",
                    help="novel_downloader.py --from-file でePubまで生成")
    ap.add_argument("--novel-downloader",
                    default=os.path.join(os.path.dirname(
                        os.path.abspath(__file__)),
                        "..", "novel_downloader", "novel_downloader.py"),
                    help="novel_downloader.py のパス")
    ap.add_argument("--inspect", metavar="N",
                    help="指定ページ（1始まり、カンマ区切り可）の解析結果を表示して終了")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    doc = fitz.open(args.pdf)
    npages = len(doc)
    page_nums = parse_pages_arg(args.pages, npages)

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

    body = assemble_text(pages, drop, headings, body_size,
                         body_top, body_bottom, hashira_keys,
                         indent=not args.no_indent, verbose=args.verbose)

    title = args.title or os.path.splitext(os.path.basename(args.pdf))[0]
    author = args.author

    out_path = args.output or os.path.join(
        os.path.dirname(os.path.abspath(args.pdf)) or ".", title + ".txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"{title}\n{author}\n\n{body}\n")
    print(f"✅ 青空文庫形式テキスト出力: {out_path}")

    if args.epub:
        nd = os.path.abspath(args.novel_downloader)
        if not os.path.exists(nd):
            print(f"エラー: novel_downloader.py が見つかりません: {nd}",
                  file=sys.stderr)
            sys.exit(1)
        cmd = [sys.executable, nd, "--from-file", out_path,
               "--title", title]
        if author:
            cmd += ["--author", author]
        print("📖 ePub生成:", " ".join(cmd))
        subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
