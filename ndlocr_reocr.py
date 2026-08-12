#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ndlocr_reocr.py — NDLOCR-Lite による再OCR（前処理）

設計書: DESIGN_NDLOCR実装.md ／ 調査報告: DESIGN_NDLOCR.md

国立国会図書館NDLラボの NDLOCR-Lite（CC BY 4.0）で自炊PDFを再OCRし、
透明テキスト層として書き戻す。vision_reocr.py の書き戻し系を import して
共有する（docai / yomitoku と同じ方針）。jisui2epub.py 本体は無改修で通す。

**現状はフェーズ2（本文＋ルビ）を検証中。** NDLOCR は `block_rubi` を
座標だけ返して認識しないので、箱を語に切って1語ずつ読む後段を自作している。
ルビの再現率はまだ Vision に届いていない（DESIGN_NDLOCR.md §7.10）。
`--no-ruby` で本文だけのフェーズ1相当に戻せる。

    # NDLOCR-Lite を任意の場所に clone しておく（モデルは同梱されている）
    git clone https://github.com/ndl-lab/ndlocr-lite
    pip install onnxruntime opencv-python-headless numpy PyYAML

    python3 ndlocr_reocr.py input.pdf --ndlocr-dir /path/to/ndlocr-lite

出力は既定で <入力>_ndlocr.pdf。

ライセンス: 本スクリプトは MIT。NDLOCR-Lite 本体（モデル重み含む）は
国立国会図書館NDLラボによる CC BY 4.0 の成果物で、**同梱しない**。
利用者が自分で clone したものを --ndlocr-dir で指す。
  https://github.com/ndl-lab/ndlocr-lite
"""

import argparse
import os
import statistics
import sys
import time

import fitz  # PyMuPDF

from jisui2epub import detect_body_size
from vision_reocr import (
    CHECKPOINT_EVERY,
    _FULLWIDTH_PUNCT,
    _atomic_save,
    _dedup_symbols,
    _open_source_pdf,
    _snap_column_x,
    insert_invisible_text,
    largest_embedded_image,
    preprocess_array,
    prepare_preprocess,
    preprocess_report,
    set_preprocess_mode,
)

# ── NDLOCR-Lite 側の定数（DESIGN_NDLOCR実装.md §4.1）────────────────

# 検出の確信度しきい値。既定0.25→0.10で正解+7・適合率据え置きの実測。
# `--det-score-threshold` は deim.py で受け取るだけで使われていないので無意味
DET_CONF = 0.10
DET_IOU = 0.2

# 本文として書き戻すクラス。block_pillar（柱）・block_folio（ノンブル）は
# フェーズ1では**書き戻す**（従来バックエンドと同じ前提を保ち、
# jisui2epub.py の classify_marginals に判定を任せる。DESIGN §3.4.3）
BODY_CLASSES = ("line_main", "line_title", "line_caption", "line_ad",
                "line_note", "line_note_tochu")
MARGIN_CLASSES = ("block_pillar", "block_folio")

# ── ルビ後段（フェーズ2）────────────────────────────────
# NDLOCR は block_rubi を**座標だけ返して認識しない**（DESIGN_NDLOCR.md §2）。
# 箱を語に切り、1語ずつ認識して、細いセルとして本文と同じ symbol 列に足す。
# ルビか本文かの判定は insert_invisible_text がセル幅で行うので、
# こちらは「ルビ箱の幅のセルを作る」だけでよい（本文列幅のおよそ半分）。
RUBY_CLASS = "block_rubi"
# 縦方向のインクの切れ目がルビ字幅のこの割合以上なら語境界。
# 1.0 だと語間が埋まって隣語と結合する（下の RUBY_INK_MIN_FRAC 参照）
RUBY_GAP_RATIO = 1.2
# ルビ字幅に対してこの割合未満の切片は捨てる（濁点・ノイズ）
RUBY_MIN_RUN_RATIO = 0.4
# インクとみなす輝度のしきい値
RUBY_INK_LEVEL = 160
# **その行を「インクあり」とみなす暗画素数（箱幅に対する割合）。**
# 「1画素でも暗ければインク」にすると、語間の余白に散るスキャンノイズで
# 空白が細切れになり、連続空白が RUBY_GAP_RATIO に届かず**隣の語と結合する**
# （百億 p135 実測: 自我即空《じがそくくう》＋色即是空《しきそくぜくう》が
# 1語 `がそくくうしきそくぜ` になる。語間の連続空白は1画素判定で35px、
# 2画素以上の判定なら79px。min_gap は37px）。
# 較正は3冊×30ページの「GOAL の読み辞書との一致率」で行った:
#   ink,gap =0.00,1.0（旧）→ 百億75.6% 三国志94.8% 地下室91.0%
#   ink,gap =0.08,1.2（現）→ 百億78.7% 三国志94.8% 地下室91.4%
# ink をさらに上げると地下室は伸びるが三国志が落ちるので、**3冊とも
# 悪化しない組み合わせ**を選んだ。ink と gap を同時に振ると切り分けられない
# ので、格子で両方を振って選んでいる
RUBY_INK_MIN_FRAC = 0.08
# **ルビ箱の幅は実インクより広いので縮めてから渡す。** YomiToku(DBNet) と
# 同型の問題で、補正しないとルビの書き戻しフォントサイズが過大になり、
# jisui2epub のルビ列グルーピング（Yギャップが size×1.7 以内なら同一ルビ）が
# 緩んで**隣接する別語のルビが結合する**（実測: 車《くるま》＋音《おと》→
# 音《くるまおと》、下《した》＋妹《いもうと》→妹《したいもうと》）。
# 実測の裏付け: 同じ本で NDLOCR のルビ書き戻しが5.04〜6.6pt、Vision は
# 3.84〜4.56pt で約1.35倍太い（1/1.35 ≒ 0.74）。
# **本文側（INK_WIDTH_RATIO）は 1.0 のまま触らない**（本文の往復再現率
# 98.09% は検証済みで、そちらを動かす理由がない）
RUBY_INK_RATIO = 0.74
WITH_RUBY = True    # --no-ruby で False
_KANA_ONLY = None   # 遅延コンパイル（re は関数内で使う）

# 認識モデルの切替。PARSEQ は入力幅の違う3本があり（24x256=30字級・
# 24x384=50字級・24x768=100字級）、**行に対して不適切なモデルを使うと
# 途中で打ち切られて無言で脱落する**。
#
# 本家 src/ocr.py:process_cascade は DEIM の4本目の出力ヘッド pred_char_count
# （3→30級・2→50級・それ以外→100級）で初段を選び、上限長に達したら昇格するが、
# **この規則は自炊書籍の本文列には合わない**。実測（地下室P.4-15の全行に3本とも
# かけたグリッド）では pred_char_count が 3 を一度も返さず、推定28〜39字の本文列が
# ほぼ全て 100級に送られる。100級は多くの行で50級と同点だが、**1行だけ39字の列を
# 15字に破綻させ、本文1列がまるごと欠落した**（往復後の再現率で1.0pt相当）。
# モデル間で8字以上食い違った33行のうち31行で50級が最良だった。
#
# 代わりに幾何で選ぶ。日本語書籍の本文は正方字が等ピッチで並ぶので、
# **列の縦横比がそのまま期待字数**になる（上の列は 3211/91=35.3 に対し正解39字）。
# 本家が学習ヘッドを要するのは任意の資料（見出しの疎な字送り・手書き）を
# 相手にするためで、こちらは組版が既知という前提の分だけ有利に使える。
REC_CAPACITY = {"rec30": 25, "rec50": 45, "rec100": 98}
# 期待字数がこの値未満なら当該モデルを初段にする（上限長より余裕を持たせる）
REC_GEOMETRY_MAX = (("rec30", 22.0), ("rec50", 42.0), ("rec100", None))
# 出力が期待字数のこの割合を下回ったら破綻とみなし1段上へ振り直す
REC_SHORT_RATIO = 0.7
# 100字級でも飽和した横長行は半分に割って読み、連結する（本家と同じ）
CASCADE_SPLIT_LEN = 98

# 検出ボックスを実インク幅に縮める比。YomiToku(DBNet) では 0.68 が必須だったが、
# **DEIM のボックスが実インクとどれだけずれるかは未測定**のため既定は無補正。
# DESIGN_NDLOCR実装.md §6.6(c)。--ink-ratio で振れるようにしてある
INK_WIDTH_RATIO = 1.0

# 柱・ノンブル（MARGIN_CLASSES）の書き戻しサイズ。本文サイズに対する下限・上限。
# **本文と同じサイズで書き戻してはならない。** jisui2epub の
# classify_marginals は「マージン内 かつ 本文の0.95倍未満」を柱の決め手の
# 1つに使っており（jisui2epub.py の drop 判定）、本文サイズで書き戻すと
# その経路が空振りして、柱が中見出しに昇格する事故が起きる
# （百億の昼と千億の夜: 原本の柱9.5pt/本文12.5pt が再OCR後は両方12.5pt）。
# 実測サイズをそのまま使うと今度はルビしきい値（jisui2epub の
# RUBY_SIZE_RATIO=0.68「以下」）を割り込んで柱がルビ扱いされうるので、
# **両端から余裕を取って** 0.75〜0.85 倍に丸める。0.92 まで許すと、
# 重複検出で横に伸びた箱のセル幅が上限に張り付いて 0.95 の判定境界まで
# 0.4pt しか残らない（百億 p27 で実測）。原本の柱は 9.5/12.5＝0.76 なので
# この帯にそのまま収まる。
MARGIN_FONT_MIN_RATIO = 0.75
MARGIN_FONT_MAX_RATIO = 0.85

CALIBRATION_MAX_PAGES = 15
CALIBRATION_TARGET_CHARS = 800

_MODELS = {}   # 遅延ロードしたモデルを保持


# ── NDLOCR-Lite の読み込み ────────────────────────────────────

def load_ndlocr(ndlocr_dir, det_conf=DET_CONF):
    """NDLOCR-Lite の src を sys.path に載せ、検出器と認識器を構築する。

    CLI（ocr.py）を呼ばずに deim.py / parseq.py を直接使う。CLI 経由だと
    1ページごとにモデルを読み直して8秒×ページ数かかるため。
    """
    src = os.path.join(ndlocr_dir, "src")
    if not os.path.isdir(src):
        sys.exit(f"エラー: {ndlocr_dir} に src/ が見つかりません。\n"
                 f"  git clone https://github.com/ndl-lab/ndlocr-lite")
    # NDLOCR は全角文字を含むパスに置けない（本家READMEの注意書き）
    if any(ord(ch) > 0x7F for ch in os.path.abspath(src)):
        sys.exit(f"エラー: NDLOCR-Lite のパスに非ASCII文字が含まれています。\n"
                 f"  {src}\n  半角英数字だけのパスに置き直してください")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from deim import DEIM
        from parseq import PARSEQ
        from yaml import safe_load
    except ImportError as e:
        sys.exit(f"エラー: NDLOCR-Lite の読み込みに失敗しました（{e}）。\n"
                 f"  pip install onnxruntime opencv-python-headless numpy PyYAML")

    model = os.path.join(src, "model")
    cfg = os.path.join(src, "config")
    det = DEIM(model_path=os.path.join(model, "deim-s-1024x1024.onnx"),
               class_mapping_path=os.path.join(cfg, "ndl.yaml"),
               score_threshold=0.2, conf_threshold=det_conf,
               iou_threshold=DET_IOU, device="cpu")
    with open(os.path.join(cfg, "NDLmoji.yaml"), encoding="utf-8") as f:
        charlist = list(safe_load(f)["model"]["charset_train"])

    def rec(name):
        return PARSEQ(model_path=os.path.join(model, name),
                      charlist=charlist, device="cpu")

    _MODELS.update(
        det=det,
        rec30=rec("parseq-ndl-24x256-30-tiny-189epoch-tegaki3-r8data-202604.onnx"),
        rec50=rec("parseq-ndl-24x384-50-tiny-300epoch-tegaki3-r8data-202604.onnx"),
        rec100=rec("parseq-ndl-24x768-100-tiny-153epoch-tegaki3-r8data-202604.onnx"),
    )
    return _MODELS


def _import_cv2():
    import cv2
    import numpy as np
    return cv2, np


# ── ページOCR ────────────────────────────────────────────

def _read_cascade(crop):
    """行画像を PARSEQ で読む。モデルは期待字数（＝行の縦横比）で選ぶ。

    初段で選んだモデルの出力が上限長に達した（＝打ち切られた）か、
    期待字数に対して短すぎる（＝破綻した）ときだけ1段上へ振り直し、
    長いほうを採る。詳細は REC_GEOMETRY_MAX 付近のコメント。
    """
    h, w = crop.shape[:2]
    expected = max(h, w) / max(min(h, w), 1)
    order = [n for n, _ in REC_GEOMETRY_MAX]
    start = next(i for i, (_, hi) in enumerate(REC_GEOMETRY_MAX)
                 if hi is None or expected < hi)

    best = ""
    for name in order[start:]:
        text = _MODELS[name].read(crop)
        if len(text) > len(best):
            best = text
        saturated = len(text) >= REC_CAPACITY[name]
        too_short = len(text) < expected * REC_SHORT_RATIO
        if not saturated and not too_short:
            return text
    # 100字級でも飽和した横長行は半分に割って読み、連結する
    if len(best) >= CASCADE_SPLIT_LEN and h < w:
        rec = _MODELS["rec100"]
        return rec.read(crop[:, :w // 2]) + rec.read(crop[:, w // 2:])
    return best


def _dedup_boxes(boxes):
    """重なり合う検出箱を潰す（確信度の高い・大きいほうを残す）。

    **DEIM の NMS（IOU 0.2）は重複を抑えない。** ルビ箱の実測で
    地下室P.4-15は186箱中122組、蘇我氏は33箱中11組が5割以上重なっていた。
    潰さないと**同じ読みが2回書き戻され、Y順で交互に噛み合う**
    （中大兄皇子《なかのおおえのみこ》→《なかのなおかのおおえおのえみのこみのこ》）。

    **同じ重複が本文・柱の箱にもある。** 百億の昼と千億の夜 p27 の実測:

        block_pillar conf=0.83 (355,142,836,204)   ← 本物の柱
        block_pillar conf=0.17 (242,142,835,203)   ← 柱＋ノンブルを覆う低確信度箱
        block_folio  conf=0.51 (241,143,303,195)   ┐ 同一座標・別クラス
        block_pillar conf=0.17 (241,143,303,195)   ┘

    これを全部書き戻すと柱が `29影絵の海第一章影絵の海` と二重化し、
    `norm_hashira` の頻度照合が外れて**柱が中見出しに昇格する**
    （百億でジャンク見出し22個・三国志で51個）。マージン行に重複箱を持つ
    ページは実測で13〜20%あった。

    **クラスをまたいで比較する**のが要点で、上の folio/pillar は座標が
    完全一致するので同一クラス内の比較では消せない。縦書きの本文列は
    互いに重ならないので、5割超の重なりは実質「同じものの二重検出」。
    boxes は (conf, x0, y0, x1, y1) のリスト。
    """
    order = sorted(boxes, key=lambda b: (-b[0], -((b[3] - b[1]) * (b[4] - b[2]))))
    kept = []
    for b in order:
        conf, x0, y0, x1, y1 = b[:5]
        area = (x1 - x0) * (y1 - y0)
        drop = False
        for k in kept:
            _, kx0, ky0, kx1, ky1 = k[:5]
            ox = min(x1, kx1) - max(x0, kx0)
            oy = min(y1, ky1) - max(y0, ky0)
            if ox <= 0 or oy <= 0:
                continue
            if ox * oy > 0.5 * min(area, (kx1 - kx0) * (ky1 - ky0)):
                drop = True
                break
        if not drop:
            kept.append(b)      # 余分な要素（クラス名等）はそのまま残す
    return kept


def _split_ruby_runs(crop):
    """縦書きルビ列を、行方向（Y）のインクの切れ目で語に分割する。

    ルビは「親語ごとのまとまり」で振られるので、箱をそのまま1語として
    読ませると隣の語まで繋がる。切れ目の基準はルビ字幅（＝箱の幅）。

    **「1画素でも暗ければインク」にしてはならない**（RUBY_INK_MIN_FRAC）。
    語間の余白に散るスキャンノイズで空白が分断され、語が結合する。
    """
    cv2, np = _import_cv2()
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    w = crop.shape[1]
    ink_min = max(1, int(w * RUBY_INK_MIN_FRAC))
    on = (g < RUBY_INK_LEVEL).sum(axis=1) >= ink_min
    min_gap = max(int(w * RUBY_GAP_RATIO), 3)
    runs, start, gap = [], None, 0
    for i, v in enumerate(on):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                runs.append((start, i - gap))
                start = None
    if start is not None:
        runs.append((start, len(on) - 1))
    return [(a, b) for a, b in runs if b - a >= w * RUBY_MIN_RUN_RATIO]


def _read_ruby(sub):
    """ルビ切片を読み、仮名だけを返す（読みは仮名のみのはず）。

    30字モデルで仮名が出なければ50字モデルで読み直す。実測（地下室6ページ）で
    仮名が出なかった切片の41%が50字モデルなら仮名になる（'s a'→ちょうめ）。
    """
    global _KANA_ONLY
    import re
    if _KANA_ONLY is None:
        _KANA_ONLY = re.compile(r'[^ぁ-んァ-ヶー]')
    for name in ("rec30", "rec50"):
        raw = _MODELS[name].read(sub)
        if not raw.strip():
            continue
        txt = _KANA_ONLY.sub('', raw)
        if txt:
            return txt
    return ""


def ocr_page_with_ndlocr(doc, page_index, with_ruby=True):
    """ページの埋め込み画像を NDLOCR にかけ、(行リスト, (w, h)) を返す。

    行リストの要素は dict(cls, box=(x0,y0,x1,y1), text)。座標は画像ピクセル。
    埋め込み画像が無いページは (None, None)。
    """
    cv2, np = _import_cv2()
    found = largest_embedded_image(doc, doc[page_index])
    if found is None:
        return None, None
    image_bytes, w, h = found
    img = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8),
                       cv2.IMREAD_COLOR)
    if img is None:
        return None, None
    img, applied = preprocess_array(img, engine="ndlocr")
    if applied:
        h, w = img.shape[:2]

    det = _MODELS["det"]
    lines = []
    ruby_boxes = []
    text_boxes = []
    for d in det.detect(img):
        cls = d["class_name"]
        x0, y0, x1, y1 = [int(v) for v in d["box"]]
        if x1 <= x0 or y1 <= y0:
            continue
        if with_ruby and cls == RUBY_CLASS:
            ruby_boxes.append((float(d["confidence"]), x0, y0, x1, y1))
            continue
        if cls not in BODY_CLASSES and cls not in MARGIN_CLASSES:
            continue
        text_boxes.append((float(d["confidence"]), x0, y0, x1, y1, cls))

    # 本文・柱の箱も重複を潰してから読む（_dedup_boxes の説明を参照）。
    # 読む前に潰すので認識の呼び出しも減る
    for _, x0, y0, x1, y1, cls in _dedup_boxes(text_boxes):
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        text = _read_cascade(crop)
        if not text.strip():
            continue
        lines.append({"cls": cls, "box": (x0, y0, x1, y1), "text": text})

    # ルビは重複を潰してから語に切る（_dedup_boxes の説明を参照）。
    # 語ごとに切って読み、切片ごとに1行として積む。位置は実測なので
    # 親文字への対応付けは jisui2epub の attach_rubies に任せられる
    for _, x0, y0, x1, y1 in _dedup_boxes(ruby_boxes):
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        # 幅は実インク相当に縮める（中心は保つ）。RUBY_INK_RATIO 参照
        rxc = (x0 + x1) / 2
        rhalf = (x1 - x0) * RUBY_INK_RATIO / 2
        for a, b in _split_ruby_runs(crop):
            txt = _read_ruby(crop[a:b + 1])
            if txt:
                lines.append({"cls": RUBY_CLASS, "text": txt,
                              "box": (rxc - rhalf, y0 + a,
                                      rxc + rhalf, y0 + b + 1)})
    return lines, (w, h)


# ── 行 → 文字セル ────────────────────────────────────────

def expand_line_to_cells(text, box, sx, sy):
    """行ボックスと行テキストを文字セル (text, Rect) に等分割で展開する。

    NDLOCR は Vision/DocAI と違い文字単位の座標を返さない（行単位の
    軸並行 bbox のみ）。YomiToku と同じ状況なので同じ方針を採る。

    等分割してよい根拠は実測にある: 親範囲とルビ範囲のずれは中央値0.17セル・
    90%点0.58セルで、親位置の誤りは 3.6%（Vision の 4.0% より良い）。
    下流の attach_rubies はセルの実Y開始位置を bisect で引き、
    cell_height() も隣接差の中央値なので、等間隔セルは想定内。

    書字方向は縦横比で決める（NDLOCR の isVertical も同じ判定）。
    """
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return []
    x0, y0, x1, y1 = box
    x0, x1 = x0 * sx, x1 * sx
    y0, y1 = y0 * sy, y1 * sy

    def cell(cx0, cy0, cx1, cy1):
        """幅だけを実インク相当に縮めたセル。中心を保つので列X中心は不変"""
        if INK_WIDTH_RATIO >= 1.0:
            return fitz.Rect(cx0, cy0, cx1, cy1)
        xc = (cx0 + cx1) / 2
        half = (cx1 - cx0) * INK_WIDTH_RATIO / 2
        return fitz.Rect(xc - half, cy0, xc + half, cy1)

    n = len(chars)
    if n == 1:
        return [(chars[0], cell(x0, y0, x1, y1))]
    if (y1 - y0) >= (x1 - x0):                      # 縦書き
        step = (y1 - y0) / n
        return [(c, cell(x0, y0 + i * step, x1, y0 + (i + 1) * step))
                for i, c in enumerate(chars)]
    step = (x1 - x0) / n                            # 横書き
    return [(c, cell(x0 + i * step, y0, x0 + (i + 1) * step, y1))
            for i, c in enumerate(chars)]


def collect_page_symbols_ndlocr(lines, page, img_size, split_margin=False):
    """行リストを読み順（縦書きは右の列から）に並べて文字セル列にする。

    split_margin=True なら (本文セル, 柱・ノンブルのセル) に分けて返す。
    分ける理由は2つある。(1) 柱は本文より小さく書き戻す必要がある
    （MARGIN_FONT_MIN_RATIO 参照）。(2) `_snap_column_x` にノンブルを
    混ぜたくない（半角数字を基準に本文列のX座標が潰される事故が
    vision_reocr で実測されている。CLAUDE.md のソフロニア嬢P.15）。

    **【未検証】読み順は単純な x 降順で組んでいる。** NDLOCR の CLI は
    ndl_parser.convert_to_xml_string3 がブロック統合と ORDER を計算するが、
    ここでは通していない。段組・図版まわりで CLI と食い違う可能性がある
    （DESIGN_NDLOCR実装.md §6.6(d)）。
    """
    if not lines:
        return ([], []) if split_margin else []
    img_w, img_h = img_size
    sx = page.rect.width / img_w
    sy = page.rect.height / img_h

    def is_vertical(ln):
        x0, y0, x1, y1 = ln["box"]
        return (y1 - y0) >= (x1 - x0)

    margin_lines = []
    if split_margin:
        margin_lines = [ln for ln in lines if ln["cls"] in MARGIN_CLASSES]
        lines = [ln for ln in lines if ln["cls"] not in MARGIN_CLASSES]
        if not lines:
            return [], _cells_of(margin_lines, sx, sy)

    vertical = sum(1 for ln in lines if is_vertical(ln)) >= len(lines) / 2
    if vertical:
        lines = sorted(lines, key=lambda ln: -(ln["box"][0] + ln["box"][2]) / 2)
    else:
        lines = sorted(lines, key=lambda ln: (ln["box"][1] + ln["box"][3]) / 2)

    symbols = []
    for ln in lines:
        symbols.extend(expand_line_to_cells(ln["text"], ln["box"], sx, sy))
    symbols = _dedup_symbols(_snap_column_x(symbols))
    if split_margin:
        return symbols, _cells_of(margin_lines, sx, sy)
    return symbols


def _cells_of(lines, sx, sy):
    """柱・ノンブル行をセルにする（列スナップは掛けない）。"""
    cells = []
    for ln in lines:
        cells.extend(expand_line_to_cells(ln["text"], ln["box"], sx, sy))
    return _dedup_symbols(cells)


def insert_margin_text(page, symbols, body_fontsize):
    """柱・ノンブルを本文より小さいサイズで書き戻す。

    NDLOCR は柱・ノンブルを block_pillar / block_folio として分類して
    返してくるので、その区別を活かして原本に近いサイズを復元する。
    書き戻しの原点は insert_invisible_text と同じ「bbox上端合わせ」。
    """
    lo = body_fontsize * MARGIN_FONT_MIN_RATIO
    hi = body_fontsize * MARGIN_FONT_MAX_RATIO
    n = 0
    for text, rect in symbols:
        text = _FULLWIDTH_PUNCT.get(text, text)
        fontsize = min(max(rect.width, lo), hi)
        page.insert_text(fitz.Point(rect.x0, rect.y0 + fontsize), text,
                         fontsize=fontsize, fontname="japan", render_mode=3)
        n += 1
    return n


# ── 書き戻しフォントサイズの基準値 ──────────────────────────────

def _calibrate_body_fontsize(doc, start_page, end_page, cache):
    """先頭数ページを実際にOCRして実測セル幅の中央値を得る。

    vision_reocr / yomitoku_reocr と同じ方針。ページごとの実測値をそのまま
    書き戻しサイズにすると、ドキュメント全体の基準に対して0.68倍を割り込み
    本文が丸ごとルビ誤判定される事故が起きるため、全体で固定値を使う
    （CLAUDE.md の vision_reocr「原因」節）。

    **ルビのセルは必ず除外する。** ルビ幅は本文のおよそ半分なので、混ぜると
    中央値が下がって本文の書き戻しサイズが小さくなり、まさに上の事故の方向へ
    寄る（受け入れ基準1・DESIGN_NDLOCR実装.md フェーズ2）。
    """
    widths = []
    last = min(end_page, start_page + CALIBRATION_MAX_PAGES - 1)
    for p1 in range(start_page, last + 1):
        if len(widths) >= CALIBRATION_TARGET_CHARS:
            break
        idx = p1 - 1
        lines, img_size = ocr_page_with_ndlocr(doc, idx, with_ruby=WITH_RUBY)
        cache[idx] = (lines, img_size)
        if not lines:
            continue
        # ルビに加えて柱・ノンブルも除く。どちらも本文より細いので、
        # 混ぜると中央値が下がり本文の書き戻しサイズが小さくなる
        body = [ln for ln in lines
                if ln["cls"] != RUBY_CLASS and ln["cls"] not in MARGIN_CLASSES]
        if not body:
            continue
        for _, rect in collect_page_symbols_ndlocr(body, doc[idx], img_size):
            widths.append(rect.width)
    return statistics.median(widths) if widths else None


# ── 本体 ────────────────────────────────────────────────

def reocr_pdf(input_path, output_path, start_page, end_page):
    # --start 再開時（入力＝出力）はメモリから開く（WinError 5 対策）
    doc = _open_source_pdf(input_path, output_path)
    end_page = min(end_page, len(doc))
    prepare_preprocess(doc, start_page, end_page)

    cache = {}
    ndl_body = _calibrate_body_fontsize(doc, start_page, end_page, cache)
    old_ocr_body = detect_body_size(doc, range(len(doc))) or None
    if ndl_body is not None and old_ocr_body is not None:
        target_body_fontsize = max(ndl_body, old_ocr_body)
        print(f"本文フォントサイズ基準値: NDLOCR実測{ndl_body:.2f}pt / "
              f"旧OCR申告{old_ocr_body:.2f}pt → "
              f"{target_body_fontsize:.2f}ptを採用")
    elif ndl_body is not None:
        target_body_fontsize = ndl_body
        print(f"本文フォントサイズ基準値（NDLOCR実測）: {target_body_fontsize:.2f}pt")
    else:
        target_body_fontsize = None
        print("キャリブレーションできる文字がなく、最初の処理ページから決めます")

    total_chars = total_ruby = processed = 0
    total_pages = end_page - start_page + 1
    t0 = time.time()

    try:
        for i, p1 in enumerate(range(start_page, end_page + 1), 1):
            idx = p1 - 1
            page = doc[idx]
            if idx in cache:
                lines, img_size = cache.pop(idx)
            else:
                lines, img_size = ocr_page_with_ndlocr(doc, idx,
                                                       with_ruby=WITH_RUBY)
            if not lines:
                print(f"ページ{p1}: 埋め込み画像なし／認識なし、スキップ")
                continue

            symbols, margins = collect_page_symbols_ndlocr(
                lines, page, img_size, split_margin=True)
            # 柱・ノンブルしか無いページ（挿絵ページ）は、基準サイズが
            # まだ決まっていないと書き戻せないので従来どおり素通しにする
            if not symbols and (not margins or target_body_fontsize is None):
                continue
            if target_body_fontsize is None and symbols:
                target_body_fontsize = statistics.median(
                    r.width for _, r in symbols)
                print(f"本文フォントサイズ基準値: {target_body_fontsize:.2f}pt")

            # 旧テキスト層を除去してから書き戻す（画像・線画は保護される）
            page.add_redact_annot(page.rect)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

            nchar, nruby = insert_invisible_text(
                page, symbols, target_body_fontsize,
                target_body_fontsize * 0.5)
            # 柱・ノンブルは本文より小さく書き戻す（MARGIN_FONT_MIN_RATIO）
            nchar += insert_margin_text(page, margins, target_body_fontsize)
            total_chars += nchar
            total_ruby += nruby
            processed += 1

            el = time.time() - t0
            eta = el / i * (total_pages - i)
            print(f"ページ{p1}/{end_page}: 本文{nchar}字 ルビ{nruby}字 "
                  f"（{el:.0f}秒経過 / 残り約{eta:.0f}秒）")

            if processed % CHECKPOINT_EVERY == 0:
                _atomic_save(doc, output_path)
                print(f"  チェックポイント保存（{processed}ページ処理済み）")
    except KeyboardInterrupt:
        print("\n中断されました。ここまでの結果を保存します")

    _atomic_save(doc, output_path, final=True)
    print(f"\n✅ 完了: {output_path}")
    print(f"   {processed}ページ / 本文{total_chars}字 / ルビ{total_ruby}字")
    preprocess_report()


def main():
    global INK_WIDTH_RATIO, WITH_RUBY, RUBY_INK_RATIO
    ap = argparse.ArgumentParser(
        description="NDLOCR-Lite で自炊PDFを再OCRし透明テキスト層を書き戻す"
                    "（本文＋ルビ。--no-ruby で本文のみ）")
    ap.add_argument("pdf", help="入力PDF")
    ap.add_argument("-o", "--output", help="出力PDF（既定: <入力>_ndlocr.pdf）")
    ap.add_argument("--ndlocr-dir", default=os.environ.get("NDLOCR_DIR", ""),
                    help="NDLOCR-Lite の clone 先（環境変数 NDLOCR_DIR も可）")
    ap.add_argument("--start", type=int, default=1, help="開始ページ（1始まり）")
    ap.add_argument("--end", type=int, default=10**9, help="終了ページ")
    ap.add_argument("--det-conf", type=float, default=DET_CONF,
                    help=f"検出の確信度しきい値（既定 {DET_CONF}）")
    ap.add_argument("--ink-ratio", type=float, default=INK_WIDTH_RATIO,
                    help="検出ボックスを実インク幅に縮める比（既定1.0＝無補正。"
                         "DEIM のずれは未測定）")
    ap.add_argument("--ruby-ink-ratio", type=float, default=RUBY_INK_RATIO,
                    help=f"ルビ箱を実インク幅に縮める比"
                         f"（既定 {RUBY_INK_RATIO}。過大だと隣語のルビが結合する）")
    ap.add_argument("--no-ruby", action="store_true",
                    help="ルビ後段を止めて本文だけ書き戻す")
    ap.add_argument("--preprocess", choices=("auto", "on", "off"),
                    default="auto", help="再OCR前の画像前処理（既定 auto）")
    args = ap.parse_args()

    if not args.ndlocr_dir:
        sys.exit("エラー: --ndlocr-dir で NDLOCR-Lite の clone 先を指定してください\n"
                 "  git clone https://github.com/ndl-lab/ndlocr-lite")

    INK_WIDTH_RATIO = args.ink_ratio
    WITH_RUBY = not args.no_ruby
    RUBY_INK_RATIO = args.ruby_ink_ratio
    set_preprocess_mode(args.preprocess)

    out = args.output or (os.path.splitext(args.pdf)[0] + "_ndlocr.pdf")
    load_ndlocr(args.ndlocr_dir, args.det_conf)
    print(f"NDLOCR-Lite（CC BY 4.0 / 国立国会図書館NDLラボ）で再OCRします")
    reocr_pdf(args.pdf, out, args.start, args.end)


if __name__ == "__main__":
    main()
