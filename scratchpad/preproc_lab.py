#!/usr/bin/env python3
"""再OCR前の画像前処理の効果を測るラボ。

やること:
  1. PDFの埋め込み画像（スキャン原本）を取り出す
  2. 前処理パイプライン（レシピ）を適用する
  3. OCR（YomiToku / Vision / DocAI）にかけて縦書き読み順にテキスト化する
  4. GOAL（正解ePub/txt）から切り出したページ正解と突き合わせて再現率を出す

jisui2epub.py・*_reocr.py には一切触らない（純粋な計測用の外付け）。
効果が確認できたレシピだけを後から *_reocr.py の画像デコード直後に差し込む。

  # ページ正解（GT）を作る（1回だけ。YomiTokuの素のOCRでGOAL内の位置を特定する）
  .venv/bin/python scratchpad/preproc_lab.py gt --book tl_u --pages 21,41,61,81,121
  # レシピを比較する
  .venv/bin/python scratchpad/preproc_lab.py run --book tl_u --recipes raw,gray,unsharp
"""
import argparse
import difflib
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "preproc_out")
os.makedirs(OUT, exist_ok=True)


# ---------------------------------------------------------------- 対象書籍

BOOKS = {
    # 1文字1スパンの古いScanSnap(2013)・300dpiカラーJPEG・紙焼けあり
    "tl_u": {
        "pdf": "temp_sample/START_タイム・リープ上_高畑京一郎.pdf",
        "goal": "temp_sample/タイム・リープ＜上＞ あしたはきのう_GOAL.epub",
    },
    "tl_d": {
        "pdf": "temp_sample/START_タイム・リープ下_高畑京一郎.pdf",
        "goal": "temp_sample/タイム・リープ＜下＞ あしたはきのう_GOAL.epub",
    },
    # 600dpi 1bit二値（300dpi 8bitのページが混在）・紙焼けは走査時に飛んでいる
    "guri": {
        "pdf": "temp_sample/start_jisui_scaned_グリックの冒険.pdf",
        "goal": "temp_sample/グリックの冒険_斎藤惇夫_GOAL.epub",
    },
    # 本命: 150dpi JPEG・黄変あり。同じ本の300dpi版(sute_s)が上限の目安になる
    "sute_n": {
        "pdf": "temp_sample/START_書を捨てよ、町へ出よう(NORMAL)_寺山修司.pdf",
        "goal": "temp_sample/GOAL_書を捨てよ、町へ出よう.txt",
    },
    "sute_s": {
        "pdf": "temp_sample/START_書を捨てよ、町へ出よう(SUPERFINE)_寺山修司.pdf",
        "goal": "temp_sample/GOAL_書を捨てよ、町へ出よう.txt",
    },
    # 本命2: 150dpi 8bit JPEG・黄変・JPEGノイズ・印字がぼけている（最悪クラス）
    "haruka": {
        "pdf": "temp_sample/はるかなる地球帝国_マリオン・ジマー・ブラッドリー.pdf",
        "goal": "temp_sample/[マリオン･ジマー･ブラッドリー] ダーコーヴァ年代記 "
                "はるかなる地球帝国_手作業GOAL.epub",
    },
    # 本命3: 300dpi 1bit二値。二値化の解像度が足りず濁点・画数の多い字が潰れる
    "hawk_u": {
        "pdf": "temp_sample/ホークミストレス上_マリオン・ジマー・ブラッドリー.pdf",
        "goal": "temp_sample/[マリオン･ジマー･ブラッドリー] ホークミストレス（上）_手作業GOAL.epub",
    },
    "hawk_d": {
        "pdf": "temp_sample/ホークミストレス下_マリオン・ジマー・ブラッドリー.pdf",
        "goal": "temp_sample/[マリオン･ジマー･ブラッドリー] ホークミストレス（下）_手作業GOAL.epub",
    },
    "ordo": {
        "pdf": "temp_sample/オルドーンの剣_マリオン・ジマー・ブラッドリー.pdf",
        "goal": "temp_sample/[Ｍ・Ｚ・ブラッドリー] オルドーンの剣_手作業GOAL.epub",
    },
    # 1bit二値の300dpi/600dpi対（濃淡情報なし。解像度とスムージングの実験用）
    "hina_n": {
        "pdf": "temp_sample/START_遠まわりする雛(NORMAL)_米澤穂信.pdf",
        "goal": "temp_sample/GOAL_遠まわりする雛_米澤穂信.epub",
    },
    "hina_s": {
        "pdf": "temp_sample/START_遠まわりする雛(SUPERFINE)_米澤穂信.pdf",
        "goal": "temp_sample/GOAL_遠まわりする雛_米澤穂信.epub",
    },
    # 横書き（200dpi 8bit・2段組と表と柱が混在する事典）。読み順を正しく
    # 組めないので採点は bag（文字集合再現率）で見る
    "fant": {
        "pdf": "temp_sample/シナリオのためのファンタジー事典_山北篤.pdf",
        "goal": "temp_sample/シナリオのためのファンタジー事典_GOAL.epub",
        "horizontal": True,
    },
}


def is_horizontal(book):
    return bool(BOOKS[book].get("horizontal"))


def book_path(book, key):
    return os.path.join(ROOT, BOOKS[book][key])


# ---------------------------------------------------------------- GOAL読み込み

def load_goal_text(path):
    """GOAL ePub/txt を「ルビを除いた本文の連結文字列」にする。"""
    if path.lower().endswith(".txt"):
        raw = open(path, "rb").read()
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            s = raw.decode("cp932", "replace")
        s = re.sub(r"《[^》]*》", "", s)
        s = re.sub(r"｜", "", s)
        s = re.sub(r"［＃[^］]*］", "", s)
        return normalize_for_match(s)

    z = zipfile.ZipFile(path)
    opf = [n for n in z.namelist() if n.endswith(".opf")]
    docs = []
    if opf:
        s = z.read(opf[0]).decode("utf-8", "replace")
        order = re.findall(r'idref="([^"]+)"', s)
        # **manifest の属性順を仮定しないこと**。href が id より前に来る
        # ePub が実在し（百億の昼と千億の夜）、`id="…"[^>]*href="…"` では
        # 1件も取れず下のファイル名順フォールバックに落ちる。章順が崩れて
        # GT が壊れる（実測: 本文再現率が 99%→73% に見えた）
        href = {}
        for item in re.findall(r"<item\b[^>]*>", s):
            i = re.search(r'\bid="([^"]+)"', item)
            h = re.search(r'\bhref="([^"]+)"', item)
            if i and h:
                href[i.group(1)] = h.group(1)
        base = os.path.dirname(opf[0])
        for i in order:
            h = href.get(i)
            if h:
                docs.append(os.path.normpath(os.path.join(base, h)).replace("\\", "/"))
    if not docs:
        docs = sorted(n for n in z.namelist()
                      if n.lower().endswith((".xhtml", ".html", ".htm")))
    parts = []
    for d in docs:
        try:
            t = z.read(d).decode("utf-8", "replace")
        except KeyError:
            continue
        # <head> ごと落とす。<title>の書名が1ファイルにつき1回ずつ本文に
        # 混ざり、章立ての細かい本（事典・グリック）ではGT の2%前後を占めた
        t = re.sub(r"<head[^>]*>.*?</head>", "", t, flags=re.S | re.I)
        t = re.sub(r"<style[^>]*>.*?</style>", "", t, flags=re.S | re.I)
        t = re.sub(r"<script[^>]*>.*?</script>", "", t, flags=re.S | re.I)
        t = re.sub(r"<rt[^>]*>.*?</rt>", "", t, flags=re.S)
        t = re.sub(r"<rp[^>]*>.*?</rp>", "", t, flags=re.S)
        t = re.sub(r"<[^>]+>", "", t)
        parts.append(html.unescape(t))
    return normalize_for_match("".join(parts))


def load_goal_rubies(path):
    """GOAL ePub の <rt> を読みの多重集合として返す（ルビ再現率の分母）。"""
    if not path.lower().endswith(".epub"):
        return []
    z = zipfile.ZipFile(path)
    out = []
    for n in z.namelist():
        if not n.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        t = z.read(n).decode("utf-8", "replace")
        for m in re.findall(r"<rt[^>]*>(.*?)</rt>", t, flags=re.S):
            r = normalize_for_match(re.sub(r"<[^>]+>", "", html.unescape(m)))
            if r:
                out.append(r)
    return out


_PUNCT_MAP = {
    "―": "—", "─": "—", "‐": "—", "－": "—", "ー": "ー",
    "…": "…", "‥": "…",
    "“": "「", "”": "」",
}


def normalize_for_match(s):
    """比較用の正規化。前処理レシピの優劣に無関係な表記揺れを潰す。"""
    s = unicodedata.normalize("NFKC", s)
    s = "".join(_PUNCT_MAP.get(c, c) for c in s)
    s = re.sub(r"—+", "—", s)
    s = re.sub(r"\.{2,}", "…", s)
    s = re.sub(r"…+", "…", s)
    s = re.sub(r"\s+", "", s)
    return s


# ---------------------------------------------------------------- 画像取得

def page_image(pdf_path, page_no):
    """1始まりページ番号の埋め込み画像を BGR numpy 配列で返す。"""
    import cv2

    doc = fitz.open(pdf_path)
    page = doc[page_no - 1]
    imgs = page.get_images(full=True)
    if not imgs:
        doc.close()
        return None
    best = max(imgs, key=lambda im: (im[2] or 0) * (im[3] or 0))
    info = doc.extract_image(best[0])
    doc.close()
    buf = np.frombuffer(info["image"], dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


# ---------------------------------------------------------------- 前処理レシピ
#
# 各レシピは BGR uint8 -> BGR uint8。OCRエンジンはどれもBGR/RGB画像を取るので
# 3チャンネルのまま返す（グレー化するレシピも最後に3chへ戻す）。

def _to_gray(img):
    import cv2
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _to_bgr(g):
    import cv2
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def op_raw(img):
    return img


def op_gray(img):
    return _to_bgr(_to_gray(img))


def op_gray_blue(img):
    """青チャンネルだけを使う。セピア（黄変）は青チャンネルで最もコントラストが
    高くなる（黄色い紙＝青が落ちる…のではなく、黄変は青の反射が落ちるので
    紙が暗くなる）。逆に赤チャンネルは紙が白く飛ぶ。両方試す価値がある。"""
    return _to_bgr(img[:, :, 0])


def op_gray_red(img):
    return _to_bgr(img[:, :, 2])


def _paper_level(g):
    """紙（背景）の輝度をヒストグラムの最頻値で推定する。"""
    hist = np.bincount(g.ravel(), minlength=256)
    return int(np.argmax(hist[64:]) + 64)


def op_whitebalance(img):
    """紙焼け除去: 大きなぼかしで背景（紙）の明るさムラを推定して割り算し、
    紙を一様な白に戻す。いわゆる flat-field 補正。"""
    import cv2
    g = _to_gray(img).astype(np.float32)
    bg = cv2.GaussianBlur(g, (0, 0), sigmaX=max(g.shape) / 40.0)
    bg = np.maximum(bg, 1.0)
    out = np.clip(g / bg * 220.0, 0, 255).astype(np.uint8)
    return _to_bgr(out)


def op_wb_stretch(img):
    """flat-field 補正＋コントラスト伸長（紙を255・インクを0に寄せる）。"""
    import cv2
    g = _to_gray(img).astype(np.float32)
    bg = cv2.GaussianBlur(g, (0, 0), sigmaX=max(g.shape) / 40.0)
    out = g / np.maximum(bg, 1.0)
    lo, hi = np.percentile(out, 2), 1.02
    out = np.clip((out - lo) / max(hi - lo, 1e-6) * 255.0, 0, 255).astype(np.uint8)
    return _to_bgr(out)


def op_clahe(img):
    import cv2
    g = _to_gray(img)
    return _to_bgr(cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g))


def op_unsharp(img):
    """アンシャープマスク。ぼけた印字の輪郭を立てる。"""
    import cv2
    g = _to_gray(img).astype(np.float32)
    blur = cv2.GaussianBlur(g, (0, 0), 1.2)
    out = np.clip(g * 2.0 - blur * 1.0, 0, 255).astype(np.uint8)
    return _to_bgr(out)


def op_up2_lanczos(img):
    import cv2
    return cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_LANCZOS4)


def op_up2_cubic(img):
    import cv2
    return cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)


def op_up2_smooth(img):
    """拡大してからスムージング（拡大で出るリンギング・ジャギーを均す）。"""
    import cv2
    up = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    g = cv2.GaussianBlur(_to_gray(up), (0, 0), 0.8)
    return _to_bgr(g)


def op_down_half(img):
    """600dpi二値を300dpi相当へ。縮小補間そのものがアンチエイリアスになる。"""
    import cv2
    return cv2.resize(img, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)


def op_antialias(img):
    """二値画像の階段状の輪郭を軽く均す（濃淡を与える）。"""
    import cv2
    return _to_bgr(cv2.GaussianBlur(_to_gray(img), (0, 0), 1.0))


def op_bilateral(img):
    """エッジを保ったままノイズ・地合いのざらつきだけ落とす。"""
    import cv2
    return _to_bgr(cv2.bilateralFilter(_to_gray(img), 7, 50, 50))


def op_nlm(img):
    import cv2
    return _to_bgr(cv2.fastNlMeansDenoising(_to_gray(img), None, 7, 7, 21))


def op_otsu(img):
    import cv2
    g = _to_gray(img)
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return _to_bgr(b)


def op_sauvola(img):
    """適応二値化（Sauvola風）。紙焼けムラに強い古典手法。"""
    import cv2
    g = _to_gray(img).astype(np.float32)
    w = 25
    mean = cv2.boxFilter(g, -1, (w, w))
    sq = cv2.boxFilter(g * g, -1, (w, w))
    std = np.sqrt(np.maximum(sq - mean * mean, 0))
    th = mean * (1 + 0.2 * (std / 128.0 - 1))
    return _to_bgr(np.where(g > th, 255, 0).astype(np.uint8))


def op_despeckle(img):
    import cv2
    return _to_bgr(cv2.medianBlur(_to_gray(img), 3))


def op_gamma(g):
    """ガンマ補正。<1で暗部を持ち上げ（線が細くなる）、>1で線が太くなる。"""
    def f(img):
        lut = np.array([((i / 255.0) ** g) * 255 for i in range(256)], dtype=np.uint8)
        import cv2
        return cv2.LUT(img, lut)
    return f


def op_morph(kind, size=2):
    """形態素処理。二値化済みスキャンで潰れた/欠けた画線を調整する。

    dilate は黒画素を太らせる（濁点が薄くて消えかけている場合）、
    erode は細らせる（濁点が本体とくっついて塊になっている場合）。
    入力は「白地に黒字」なので黒を太らせる＝cv2.erode（明度が縮む）。
    """
    def f(img):
        import cv2
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        g = _to_gray(img)
        if kind == "thicken":
            g = cv2.erode(g, k)
        elif kind == "thin":
            g = cv2.dilate(g, k)
        elif kind == "open":      # 白地の小さな黒ノイズを消す
            g = cv2.morphologyEx(g, cv2.MORPH_OPEN, k)
        elif kind == "close":     # 画線の細い切れ目を繋ぐ
            g = cv2.morphologyEx(g, cv2.MORPH_CLOSE, k)
        return _to_bgr(g)
    return f


def op_blackhat_boost(size=5, amount=1.0):
    """ブラックハットで「カーネルより小さい暗い要素」＝濁点・半濁点・点画を
    抽出し、その分だけ元画像を暗くして強調する。太い画線は変わらない。"""
    def f(img):
        import cv2
        g = _to_gray(img)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        bh = cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, k)
        out = np.clip(g.astype(np.int16) - (bh.astype(np.int16) * amount), 0, 255)
        return _to_bgr(out.astype(np.uint8))
    return f


def op_unsharp_p(sigma=1.2, amount=1.0):
    """半径・強さを指定できるアンシャープマスク。濁点のような小さい特徴には
    小さいsigmaでないと効かない（既定の1.2は150dpiでは大きすぎる）。"""
    def f(img):
        import cv2
        g = _to_gray(img).astype(np.float32)
        blur = cv2.GaussianBlur(g, (0, 0), sigma)
        return _to_bgr(np.clip(g + (g - blur) * amount, 0, 255).astype(np.uint8))
    return f


def op_smooth_p(sigma=0.8):
    def f(img):
        import cv2
        return _to_bgr(cv2.GaussianBlur(_to_gray(img), (0, 0), sigma))
    return f


def op_scale(factor, interp="cubic"):
    def f(img):
        import cv2
        codes = {"cubic": cv2.INTER_CUBIC, "lanczos": cv2.INTER_LANCZOS4,
                 "area": cv2.INTER_AREA, "linear": cv2.INTER_LINEAR,
                 "nearest": cv2.INTER_NEAREST}
        return cv2.resize(img, None, fx=factor, fy=factor, interpolation=codes[interp])
    return f


def estimate_char_px(img):
    """画像から本文1文字のピクセル高さを推定する。

    大津法で二値化して連結成分を取り、高さの中央値を返す。日本語の本文は
    かな（小さい）と漢字（大きい）が混ざるが、偏の分離などで割れた成分も
    含めた中央値は概ね1文字の0.7倍に落ち着くので、係数で補正する。
    ノンブル・柱・挿絵の成分は中央値なのでほぼ効かない。
    """
    import cv2

    g = _to_gray(img)
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, _, stats, _ = cv2.connectedComponentsWithStats(b, 8)
    if n < 20:
        return None
    h = stats[1:, cv2.CC_STAT_HEIGHT]
    w = stats[1:, cv2.CC_STAT_WIDTH]
    a = stats[1:, cv2.CC_STAT_AREA]
    # 極小（ノイズ・濁点）と極大（罫線・図版）を落とす
    keep = (a >= 8) & (h >= 3) & (h <= g.shape[0] / 8) & (w <= g.shape[1] / 8)
    if keep.sum() < 20:
        return None
    return float(np.median(h[keep])) / 0.72


def op_normalize(target_px=26.0, sigma_frac=0.055, max_scale=4.0):
    """「1文字が target_px になるよう拡縮してから平滑化する」適応レシピ。

    実測で最良点が本ごとに違って見えたのは解像度の違いだけで、出力側の
    1文字あたり画素数で揃えると同じ所に来る:
      はるかなる(150dpi・1文字13px) → 2倍拡大してσ1.2 が最良
      ホークミストレス(300dpi・1文字26px) → 等倍でσ1.2〜1.8 が最良
    どちらも「1文字≒26px・σ≒1.5」。ならば本ごとに調整せず自動で合わせられる。
    縮小方向にも効かせる（600dpiは細かすぎて検出が荒れるため）。
    """
    def f(img):
        import cv2
        est = estimate_char_px(img)
        if not est or est <= 0:
            return _to_bgr(cv2.GaussianBlur(_to_gray(img), (0, 0), 1.0))
        scale = float(np.clip(target_px / est, 1.0 / max_scale, max_scale))
        out = cv2.resize(img, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC if scale >= 1
                         else cv2.INTER_AREA)
        return _to_bgr(cv2.GaussianBlur(_to_gray(out), (0, 0),
                                        max(target_px * sigma_frac, 0.3)))
    return f


def op_normalize_abs(target_px, sigma, max_scale=4.0):
    """op_normalize の σ を target から切り離した版（格子探索用）。

    op_normalize は σ = target × 係数 としていたため、target を変えると σ も
    動いてしまい、どちらが効いているのか分離できていなかった。σ=0 は
    平滑化なし（拡縮だけの効果を見る対照）。
    """
    def f(img):
        import cv2
        est = estimate_char_px(img)
        if not est or est <= 0:
            return op_gray(img)
        scale = float(np.clip(target_px / est, 1.0 / max_scale, max_scale))
        out = cv2.resize(img, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC if scale >= 1
                         else cv2.INTER_AREA)
        g = _to_gray(out)
        if sigma > 0:
            g = cv2.GaussianBlur(g, (0, 0), sigma)
        return _to_bgr(g)
    return f


# ---- 超解像（Real-ESRGAN）
#
# DN_SuperBook_PDF_Converter が使っているのと同じ Real-ESRGAN（Apache-2.0）。
# 本家は CUDA 前提だが、ここは spandrel + torch CPU で動かす（Radeon 780M は
# ROCm 対象外のため。実測で realesr-general-x4v3 は 150dpi ページ1枚 数秒）。
# 重みは scratchpad/preproc_out/models/ に置く（配布物には含めない）。

MODELS = os.path.join(OUT, "models")
_SR_CACHE = {}


def _sr_model(name):
    if name not in _SR_CACHE:
        import torch
        from spandrel import ModelLoader

        torch.set_num_threads(os.cpu_count() or 4)
        d = ModelLoader().load_from_file(os.path.join(MODELS, name))
        _SR_CACHE[name] = (d.model.eval(), int(d.scale))
    return _SR_CACHE[name]


def _sr_infer(img, name, tile=400, overlap=32):
    """タイル分割して超解像をかける（1ページ丸ごとだとメモリを食うため）。"""
    import torch

    net, scale = _sr_model(name)
    h, w = img.shape[:2]
    out = np.zeros((h * scale, w * scale, 3), dtype=np.float32)
    with torch.no_grad():
        for y in range(0, h, tile):
            for x in range(0, w, tile):
                y0, x0 = max(0, y - overlap), max(0, x - overlap)
                y1, x1 = min(h, y + tile + overlap), min(w, x + tile + overlap)
                patch = img[y0:y1, x0:x1]
                t = torch.from_numpy(patch[:, :, ::-1].copy()).permute(2, 0, 1)
                t = t.float().div(255).unsqueeze(0)
                r = net(t)[0].clamp(0, 1).permute(1, 2, 0).numpy()
                # overlap分を捨てて中心だけ貼る
                ty, tx = (y - y0) * scale, (x - x0) * scale
                ey = min(tile, h - y) * scale
                ex = min(tile, w - x) * scale
                out[y * scale:y * scale + ey, x * scale:x * scale + ex] = \
                    r[ty:ty + ey, tx:tx + ex]
    return (out[:, :, ::-1] * 255).astype(np.uint8)


def op_sr(name, final=None):
    """超解像。final を指定すると元寸比 final 倍まで縮小して返す
    （x4のまま渡すとOCRには大きすぎ、処理も重いため。x4で作ってから
    x2に落とすと、単純なx2拡大よりなめらかな画像になる）。"""
    def f(img):
        import cv2
        h, w = img.shape[:2]
        out = _sr_infer(img, name)
        if final is not None:
            out = cv2.resize(out, (int(w * final), int(h * final)),
                             interpolation=cv2.INTER_AREA)
        return out
    return f


def _compose(*ops):
    def f(img):
        for o in ops:
            img = o(img)
        return img
    return f


RECIPES = {
    "raw": op_raw,
    "gray": op_gray,
    "blue": op_gray_blue,
    "red": op_gray_red,
    "wb": op_whitebalance,
    "wbs": op_wb_stretch,
    "clahe": op_clahe,
    "unsharp": op_unsharp,
    "up2": op_up2_cubic,
    "up2lanczos": op_up2_lanczos,
    "up2smooth": op_up2_smooth,
    "down": op_down_half,
    "antialias": op_antialias,
    "bilateral": op_bilateral,
    "nlm": op_nlm,
    "otsu": op_otsu,
    "sauvola": op_sauvola,
    "despeckle": op_despeckle,
    # 複合
    "wb_unsharp": _compose(op_whitebalance, op_unsharp),
    "wb_up2": _compose(op_whitebalance, op_up2_cubic),
    "wbs_up2": _compose(op_wb_stretch, op_up2_cubic),
    "wb_up2smooth": _compose(op_whitebalance, op_up2_smooth),
    "wbs_up2smooth": _compose(op_wb_stretch, op_up2_smooth),
    "wb_unsharp_up2": _compose(op_whitebalance, op_unsharp, op_up2_cubic),
    "blue_wb": _compose(op_gray_blue, op_whitebalance),
    "blue_wb_up2": _compose(op_gray_blue, op_whitebalance, op_up2_cubic),
    "bilateral_wb": _compose(op_bilateral, op_whitebalance),
    "down_unsharp": _compose(op_down_half, op_unsharp),
    "antialias_up2": _compose(op_antialias, op_up2_cubic),

    # ---- 濁点・小さな点画を狙うレシピ
    "gamma08": op_gamma(0.8),
    "gamma12": op_gamma(1.2),
    "thicken": op_morph("thicken", 2),
    "thin": op_morph("thin", 2),
    "mclose": op_morph("close", 2),
    "mopen": op_morph("open", 2),
    "blackhat": op_blackhat_boost(5, 1.0),
    "blackhat_w": op_blackhat_boost(9, 0.7),
    "sharp_fine": op_unsharp_p(0.6, 1.0),
    "sharp_fine2": op_unsharp_p(0.6, 2.0),
    "sharp_mid": op_unsharp_p(0.9, 1.2),
    "up2_sharpfine": _compose(op_up2_cubic, op_unsharp_p(1.0, 1.0)),
    "up2_blackhat": _compose(op_up2_cubic, op_blackhat_boost(9, 1.0)),
    "wb_blackhat_up2": _compose(op_whitebalance, op_blackhat_boost(5, 1.0),
                                op_up2_cubic),
    "up2smooth_s5": _compose(op_up2_cubic, op_smooth_p(0.5)),
    "up2smooth_s12": _compose(op_up2_cubic, op_smooth_p(1.2)),
    # 平滑化の強さ探索。150dpi(haruka)ではσ1.2が最良だったので周辺を刻む
    "up2smooth_s10": _compose(op_up2_cubic, op_smooth_p(1.0)),
    "up2smooth_s15": _compose(op_up2_cubic, op_smooth_p(1.5)),
    "up2smooth_s18": _compose(op_up2_cubic, op_smooth_p(1.8)),
    "up2smooth_s22": _compose(op_up2_cubic, op_smooth_p(2.2)),
    # 元が300dpiなら1文字の画素数が2倍になるので、必要な平滑化も強くなるはず
    "up2smooth_s28": _compose(op_up2_cubic, op_smooth_p(2.8)),
    "up2smooth_s35": _compose(op_up2_cubic, op_smooth_p(3.5)),
    "smooth_s12": op_smooth_p(1.2),
    "smooth_s18": op_smooth_p(1.8),
    # target と σ を分離した格子探索（σは絶対値。target×係数で連動させない）
    **{f"t{t}s{int(s*10):02d}": op_normalize_abs(float(t), s)
       for t, s in [(20, 1.2), (26, 1.2), (32, 1.2), (40, 1.2), (52, 1.2),
                    (32, 0.0), (32, 0.6), (32, 1.0), (32, 1.4), (32, 1.8),
                    (32, 2.4), (20, 0.8), (26, 1.0), (40, 1.5), (52, 2.0)]},
    # 適応レシピ（本命候補）: 1文字の画素数を揃えてから平滑化する
    "norm": op_normalize(26.0, 0.055),
    "norm22": op_normalize(22.0, 0.055),
    "norm32": op_normalize(32.0, 0.055),
    "norm40": op_normalize(40.0, 0.055),
    "norm_s7": op_normalize(26.0, 0.07),
    "norm_s4": op_normalize(26.0, 0.04),
    "wb_norm": _compose(op_whitebalance, op_normalize(26.0, 0.055)),
    "up2lan_s12": _compose(op_up2_lanczos, op_smooth_p(1.2)),
    "up2lin_s12": _compose(op_scale(2, "linear"), op_smooth_p(1.2)),
    "up3smooth_s18": _compose(op_scale(3), op_smooth_p(1.8)),
    "up4smooth_s24": _compose(op_scale(4), op_smooth_p(2.4)),
    "wb_up2smooth_s12": _compose(op_whitebalance, op_up2_cubic, op_smooth_p(1.2)),
    "up2smooth_s12_thick": _compose(op_up2_cubic, op_smooth_p(1.2),
                                    op_morph("thicken", 2)),
    "up3smooth": _compose(op_scale(3), op_smooth_p(1.0)),
    "up4": op_scale(4),

    # ---- 超解像（Real-ESRGAN）
    "sr4": op_sr("realesr-general-x4v3.pth"),
    "sr4_x2": op_sr("realesr-general-x4v3.pth", final=2),
    "sr4_x1": op_sr("realesr-general-x4v3.pth", final=1),
    "sr2": op_sr("RealESRGAN_x2plus.pth"),
    "sr4big_x2": op_sr("RealESRGAN_x4plus.pth", final=2),
    "sr4big": op_sr("RealESRGAN_x4plus.pth"),
    "wb_sr4_x2": _compose(op_whitebalance, op_sr("realesr-general-x4v3.pth", final=2)),
    "wb_sr4": _compose(op_whitebalance, op_sr("realesr-general-x4v3.pth")),
    "sr4_x2_sharp": _compose(op_sr("realesr-general-x4v3.pth", final=2),
                             op_unsharp_p(1.0, 0.8)),
}


# ---------------------------------------------------------------- OCRバックエンド

_YOMI = {}


def ocr_yomitoku(img):
    """(words, meta) を返す。words は [(text, x0, y0, x1, y1, score, vertical)]。"""
    import yomitoku_reocr as Y

    if "ocr" not in _YOMI:
        _YOMI["ocr"] = Y.make_ocr()
    result, _ = _YOMI["ocr"](img)
    out = []
    for w in result.words:
        pts = np.array(w.points, dtype=float)
        x0, y0 = pts[:, 0].min(), pts[:, 1].min()
        x1, y1 = pts[:, 0].max(), pts[:, 1].max()
        # direction は短い行で誤申告するのでアスペクト比で決める（本体と同じ方針）
        vertical = (y1 - y0) > (x1 - x0)
        out.append((w.content, x0, y0, x1, y1, float(w.rec_score), vertical))
    return out, {"scores": [o[5] for o in out]}


def ocr_vision(img):
    import cv2
    from google.cloud import vision as gv

    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("encode failed")
    client = _YOMI.setdefault("vision", gv.ImageAnnotatorClient())
    resp = client.document_text_detection(image=gv.Image(content=buf.tobytes()))
    ann = resp.full_text_annotation
    out = []
    for page in ann.pages:
        for block in page.blocks:
            for para in block.paragraphs:
                for word in para.words:
                    for sym in word.symbols:
                        v = sym.bounding_box.vertices
                        xs = [p.x for p in v]
                        ys = [p.y for p in v]
                        out.append((sym.text, min(xs), min(ys), max(xs), max(ys),
                                    float(sym.confidence),
                                    (max(ys) - min(ys)) > (max(xs) - min(xs))))
    return out, {"scores": [o[5] for o in out], "symbol_level": True}


def ocr_docai(img):
    import cv2
    import docai_reocr as D

    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("encode failed")
    ctx = _YOMI.get("docai")
    if ctx is None:
        loc = os.environ.get("DOCAI_LOCATION", "us")
        client = D.make_client(loc)
        name = os.environ.get("DOCAI_PROCESSOR") or D.find_processor(
            client, D._default_project_id(), loc)
        if not name:
            raise RuntimeError("DocAIのプロセッサが見つからない。"
                               "docai_reocr.py --create-processor を先に実行する")
        ctx = _YOMI["docai"] = (client, name)
    client, name = ctx
    from google.cloud import documentai as da

    req = da.ProcessRequest(
        name=name,
        raw_document=da.RawDocument(content=buf.tobytes(), mime_type="image/png"),
        process_options=da.ProcessOptions(
            ocr_config=da.OcrConfig(enable_symbol=True)),
    )
    res = client.process_document(request=req)
    doc = res.document
    out = []
    for page in doc.pages:
        w, h = page.dimension.width, page.dimension.height
        for sym in page.symbols:
            seg = sym.layout.text_anchor.text_segments
            if not seg:
                continue
            t = doc.text[int(seg[0].start_index):int(seg[0].end_index)]
            v = sym.layout.bounding_poly.vertices or None
            if v:
                xs = [p.x for p in v]
                ys = [p.y for p in v]
            else:
                nv = sym.layout.bounding_poly.normalized_vertices
                xs = [p.x * w for p in nv]
                ys = [p.y * h for p in nv]
            out.append((t, min(xs), min(ys), max(xs), max(ys), 1.0,
                        (max(ys) - min(ys)) > (max(xs) - min(xs))))
    return out, {"scores": [], "symbol_level": True}


BACKENDS = {"yomitoku": ocr_yomitoku, "vision": ocr_vision, "docai": ocr_docai}


# ---------------------------------------------------------------- 読み順の組み立て

RUBY_RATIO = 0.68  # jisui2epub 本体と同じルビ判定しきい値


def assemble_horizontal(words, symbol_level=False):
    """横書きページを「上の行から、行内は左→右」で組み立てる。

    段組・表・柱が混在する誌面（事典など）では行の並べ替えだけでは正しい
    読み順にならないが、前処理レシピの比較には文字集合再現率（bag）を使うので
    実害はない。行内の順序さえ保てればGOALとの位置合わせ（一意6-gram）は効く。
    """
    if not words:
        return "", []
    heights = np.array([w[4] - w[2] for w in words])
    med = float(np.median(heights)) or 1.0
    body, ruby = [], []
    for w in words:
        (ruby if (w[4] - w[2]) < med * RUBY_RATIO else body).append(w)
    if symbol_level:
        body, ruby = words, []

    def order(items):
        # y中心が本文行高の半分以内なら同じ行とみなし、行内は左→右
        items = sorted(items, key=lambda w: ((w[2] + w[4]) / 2, w[1]))
        rows, cur, cy = [], [], None
        for w in items:
            c = (w[2] + w[4]) / 2
            if cy is None or abs(c - cy) <= med * 0.5:
                cur.append(w)
                cy = c if cy is None else (cy * len(cur) + c) / (len(cur) + 1)
            else:
                rows.append(cur)
                cur, cy = [w], c
        rows.append(cur)
        seq = []
        for r in rows:
            seq.extend(sorted(r, key=lambda w: w[1]))
        return seq

    return (normalize_for_match("".join(w[0] for w in order(body))),
            [r for r in (normalize_for_match(w[0]) for w in order(ruby)) if r])


def assemble(words, img_w, img_h, symbol_level=False):
    """縦書きの読み順（右の列から）でテキスト化し、(本文, ルビ) を返す。

    ルビ判定は本体と同じく「列幅が本文中央値の0.68倍未満」。
    柱・ノンブルは除かない（レシピ間で同条件なので相対比較には影響しない）。
    """
    if symbol_level:
        # Vision/DocAI は1文字単位で返るためboxがほぼ正方形になり、アスペクト比では
        # 縦横を判定できない。対象書籍はすべて縦組みなので全て縦として列に組む
        # （横書き見出しは列クラスタに散るが、再現率の分母を下げない挿入になるだけ）
        words = [(w[0], w[1], w[2], w[3], w[4], w[5], True) for w in words]
    vert = [w for w in words if w[6]]
    if not vert:
        return "".join(w[0] for w in words), []
    widths = np.array([w[3] - w[1] for w in vert])
    med = float(np.median(widths))
    body, ruby = [], []
    for w in vert:
        (ruby if (w[3] - w[1]) < med * RUBY_RATIO else body).append(w)
    horiz = [w for w in words if not w[6]]

    # 列にまとめる: x中心が近いものを同じ列とみなす
    def order(items):
        if not items:
            return []
        items = sorted(items, key=lambda w: (-(w[1] + w[3]) / 2, w[2]))
        cols, cur, cx = [], [], None
        tol = med * 0.6
        for w in items:
            c = (w[1] + w[3]) / 2
            if cx is None or abs(c - cx) <= tol:
                cur.append(w)
                cx = c if cx is None else (cx * len(cur) + c) / (len(cur) + 1)
            else:
                cols.append(cur)
                cur, cx = [w], c
        cols.append(cur)
        seq = []
        for col in cols:
            seq.extend(sorted(col, key=lambda w: w[2]))
        return seq

    body_txt = "".join(w[0] for w in order(body))
    # 横書き行（見出し・柱）は末尾に足す。挿入として扱われるだけで再現率は下げない
    body_txt += "".join(w[0] for w in sorted(horiz, key=lambda w: (w[2], w[1])))
    ruby_list = [normalize_for_match(w[0]) for w in order(ruby)]
    return normalize_for_match(body_txt), [r for r in ruby_list if r]


# ---------------------------------------------------------------- 採点

def score(gt, hyp):
    """GT に対する再現率（一致文字数/GT長）と挿入率を返す。

    柱・ノンブル・ルビの混入は「挿入」になるだけで再現率を下げない。
    前処理の良し悪し＝本文をどれだけ取りこぼさず正しく読めたか、を見たいので
    再現率を主指標にする。
    """
    if not gt:
        return 0.0, 0.0
    sm = difflib.SequenceMatcher(None, gt, hyp, autojunk=False)
    match = sum(b.size for b in sm.get_matching_blocks())
    return match / len(gt), (len(hyp) - match) / len(gt)


_SMALL_KANA = set("ぁぃぅぇぉっゃゅょァィゥェォッャュョヮゕゖ")


def _split_mark(c):
    """濁点・半濁点を分離する。が→(か, U+3099)、ぱ→(は, U+309A)。"""
    d = unicodedata.normalize("NFD", c)
    if len(d) > 1 and d[1] in ("゙", "゚"):
        return d[0], d[1]
    return c, ""


def error_profile(gt, hyp):
    """誤りを種類別に数える。全体再現率だと1文字の濁点落ちが埋もれるため、
    YomiTokuの既知の弱点（濁点脱落・小書き仮名）を個別に追えるようにする。

    返り値: {"濁点": n, "小書き": n, "その他置換": n, "脱落": n, "余分": n}
    """
    prof = {"濁点": 0, "小書き": 0, "その他置換": 0, "脱落": 0, "余分": 0}
    sm = difflib.SequenceMatcher(None, gt, hyp, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            prof["脱落"] += i2 - i1
            continue
        if tag == "insert":
            prof["余分"] += j2 - j1
            continue
        # replace: 長さが同じなら1文字ずつ見比べる
        a, b = gt[i1:i2], hyp[j1:j2]
        if len(a) != len(b):
            prof["脱落"] += max(0, len(a) - len(b))
            prof["余分"] += max(0, len(b) - len(a))
            prof["その他置換"] += min(len(a), len(b))
            continue
        for x, y in zip(a, b):
            bx, mx = _split_mark(x)
            by, my = _split_mark(y)
            if bx == by and mx != my:
                prof["濁点"] += 1
            elif (x in _SMALL_KANA) != (y in _SMALL_KANA) and \
                    unicodedata.normalize("NFKC", x.translate(_SMALL_TR)) == \
                    unicodedata.normalize("NFKC", y.translate(_SMALL_TR)):
                prof["小書き"] += 1
            else:
                prof["その他置換"] += 1
    return prof


_SMALL_TR = str.maketrans("ぁぃぅぇぉっゃゅょァィゥェォッャュョ",
                          "あいうえおつやゆよアイウエオツヤユヨ")


def bag_score(gt, hyp):
    """文字集合（多重集合）としての再現率。読み順に一切依存しない。

    段組・表が混在して読み順を正しく組めない誌面でも、前処理レシピの比較には
    使える（余計に読んだ文字は分子に入らないだけで減点にならない）。
    """
    from collections import Counter
    if not gt:
        return 0.0
    g, h = Counter(gt), Counter(hyp)
    return sum(min(v, h[k]) for k, v in g.items()) / len(gt)


def ruby_score(gt_rubies, hyp_rubies):
    """ルビの多重集合一致率（ページ単位の正解ルビが取れないので参考値）。"""
    from collections import Counter
    if not gt_rubies:
        return None
    g, h = Counter(gt_rubies), Counter(hyp_rubies)
    hit = sum(min(v, h[k]) for k, v in g.items())
    return hit / sum(g.values())


# ---------------------------------------------------------------- GT作成

def gt_path(book):
    return os.path.join(OUT, f"gt_{book}.json")


def _anchor_span(goal, hyp, n=6):
    """OCR結果とGOALの一意なn-gram一致から (GOAL上の開始, 終了) を推定する。

    GOAL全体で一意に出現するn-gramだけを使い、ずれ(goal位置-hyp位置)の中央値から
    大きく外れた一致は捨てる（柱・ルビの偶然一致対策）。
    """
    pairs = []
    for i in range(0, max(0, len(hyp) - n)):
        g = hyp[i:i + n]
        j = goal.find(g)
        if j >= 0 and goal.find(g, j + 1) < 0:
            pairs.append((i, j))
    if len(pairs) < 5:
        return None
    offs = np.array([j - i for i, j in pairs])
    med = np.median(offs)
    keep = [(i, j) for (i, j), o in zip(pairs, offs) if abs(o - med) <= 40]
    if len(keep) < 5:
        return None
    return min(j for _, j in keep), max(j for _, j in keep) + n


def build_gt(book, pages, backend="yomitoku"):
    """GOAL 全文を「ページごとの区間」に分割して保存する。

    各ページの素のOCR結果からGOAL上の位置を突き止め、**ページnの正解を
    [ページnの開始, ページn+1の開始) とする**。ページ末尾をベースラインOCRが
    どこまで読めたかで切ると、ベースライン有利のGTになってしまうため
    （前処理で余分に1行読めた分が「挿入」に化ける）、境界は必ず次ページ側から
    決める。したがって --pages には連続したページを渡すこと。
    """
    goal = load_goal_text(book_path(book, "goal"))
    pdf = book_path(book, "pdf")
    fn = BACKENDS[backend]
    spans = {}
    for pno in pages:
        img = page_image(pdf, pno)
        if img is None:
            print(f"  p{pno}: 画像なし。スキップ")
            continue
        words, meta = fn(img)
        sym = meta.get("symbol_level", False)
        hyp, _ = (assemble_horizontal(words, sym) if is_horizontal(book)
                  else assemble(words, img.shape[1], img.shape[0], sym))
        if len(hyp) < 40:
            print(f"  p{pno}: OCR結果が短すぎる({len(hyp)}字)。スキップ（挿絵頁か）")
            continue
        sp = _anchor_span(goal, hyp)
        if sp is None:
            print(f"  p{pno}: GOAL内の位置を特定できず。スキップ")
            continue
        spans[pno] = (sp, hyp)

    ordered = sorted(spans)
    data = {}
    for idx, pno in enumerate(ordered):
        (s, e), hyp = spans[pno]
        nxt = ordered[idx + 1] if idx + 1 < len(ordered) else None
        if nxt == pno + 1:
            e = spans[nxt][0][0]  # 次ページの開始で切る＝正解がページを分割する
        gt = goal[s:e]
        r, ins = score(gt, hyp)
        data[str(pno)] = {"gt": gt, "start": s, "end": e,
                          "baseline_recall": round(r, 4),
                          "baseline_insert": round(ins, 4),
                          "closed": nxt == pno + 1}
        mark = "" if nxt == pno + 1 else "  ※末尾が次ページ未確定"
        print(f"  p{pno}: GT {len(gt)}字 (GOAL {s}..{e}) 素のOCR再現率 "
              f"{r*100:.1f}% 挿入 {ins*100:.1f}%{mark}")
    with open(gt_path(book), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"→ {gt_path(book)} に {len(data)} ページ分を保存")


# ---------------------------------------------------------------- 実行

def run(book, recipes, backend, pages=None, dump=False):
    with open(gt_path(book), encoding="utf-8") as f:
        gt = json.load(f)
    # 末尾が次ページで閉じていないGTはベースライン有利なので採点に使わない
    gt = {k: v for k, v in gt.items() if v.get("closed", True)}
    if pages:
        gt = {k: v for k, v in gt.items() if int(k) in pages}
    pdf = book_path(book, "pdf")
    fn = BACKENDS[backend]
    cache = {int(k): page_image(pdf, int(k)) for k in gt}

    rows = []
    for name in recipes:
        recipe = RECIPES[name]
        rec, ins, sc, bag, secs, npix = [], [], [], [], 0.0, 0
        prof = {"濁点": 0, "小書き": 0, "その他置換": 0, "脱落": 0, "余分": 0}
        for k, v in sorted(gt.items(), key=lambda kv: int(kv[0])):
            img = recipe(cache[int(k)].copy())
            npix = img.shape[0] * img.shape[1]
            if dump:
                import cv2
                cv2.imwrite(os.path.join(OUT, f"{book}_p{k}_{name}.png"), img)
            t0 = time.time()
            words, meta = fn(img)
            secs += time.time() - t0
            sym = meta.get("symbol_level", False)
            hyp, _rb = (assemble_horizontal(words, sym) if is_horizontal(book)
                        else assemble(words, img.shape[1], img.shape[0], sym))
            bag.append(bag_score(v["gt"], hyp))
            r, i = score(v["gt"], hyp)
            for kk, vv in error_profile(v["gt"], hyp).items():
                prof[kk] += vv
            rec.append(r)
            ins.append(i)
            if meta.get("scores"):
                sc.append(float(np.mean(meta["scores"])))
        rows.append({
            "recipe": name, "recall": float(np.mean(rec)),
            "bag": float(np.mean(bag)),
            "insert": float(np.mean(ins)),
            "score": float(np.mean(sc)) if sc else None,
            "sec_per_page": secs / max(len(rec), 1),
            "mpix": npix / 1e6,
            "prof": prof,
            "per_page": [round(x, 4) for x in rec],
        })

    rows.sort(key=lambda r: -(r["bag"] if is_horizontal(book) else r["recall"]))
    base = next((r for r in rows if r["recipe"] == "raw"), rows[-1])
    print(f"\n=== {book} / {backend} / {len(gt)}ページ ===")
    print(f"{'recipe':16s} {'再現率':>7s} {'差':>7s} {'文字集合':>8s} {'差':>7s} {'挿入':>7s} {'conf':>6s} "
          f"{'濁点':>5s} {'小書':>5s} {'置換':>5s} {'脱落':>5s} {'秒/頁':>6s} {'Mpx':>5s}")
    for r in rows:
        d = (r["recall"] - base["recall"]) * 100
        s = f"{r['score']:.3f}" if r["score"] is not None else "  -  "
        p = r["prof"]
        db = (r["bag"] - base["bag"]) * 100
        print(f"{r['recipe']:16s} {r['recall']*100:6.2f}% {d:+6.2f} {r['bag']*100:7.2f}% {db:+6.2f} "
              f"{r['insert']*100:6.2f}% "
              f"{s:>6s} {p['濁点']:5d} {p['小書き']:5d} {p['その他置換']:5d} {p['脱落']:5d} "
              f"{r['sec_per_page']:6.2f} {r['mpix']:5.1f}")
    with open(os.path.join(OUT, f"result_{book}_{backend}.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)


def scan(book, first, last, step):
    """正解なしで「読みにくいページ」を探す。

    YomiToku の rec_score は GOAL 照合の再現率と同じ向きに動く（二値化レシピで
    再現率と信頼度が同時に落ちるのを確認済み）ので、GTを作る前に候補ページを
    絞るのに使える。ローカル実行なので何ページ流しても無料。
    """
    pdf = book_path(book, "pdf")
    doc = fitz.open(pdf)
    n = len(doc)
    doc.close()
    rows = []
    for pno in range(first, min(last, n) + 1, step):
        img = page_image(pdf, pno)
        if img is None:
            continue
        words, meta = ocr_yomitoku(img)
        sc = meta["scores"]
        if len(sc) < 5:
            continue
        rows.append((float(np.mean(sc)), float(np.percentile(sc, 10)), len(sc), pno))
    rows.sort()
    print(f"{'page':>5s} {'平均conf':>8s} {'下位10%':>8s} {'行数':>5s}")
    for m, p10, k, pno in rows:
        print(f"{pno:5d} {m:8.3f} {p10:8.3f} {k:5d}")
    with open(os.path.join(OUT, f"scan_{book}.json"), "w", encoding="utf-8") as f:
        json.dump([{"page": p, "mean": m, "p10": q, "lines": k}
                   for m, q, k, p in rows], f, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["gt", "run", "list", "scan"])
    ap.add_argument("--range", default="1,9999,10",
                    help="scan用: 開始,終了,間隔")
    ap.add_argument("--book", default="tl_u")
    ap.add_argument("--pages", default="")
    ap.add_argument("--recipes", default="raw")
    ap.add_argument("--backend", default="yomitoku", choices=list(BACKENDS))
    ap.add_argument("--dump", action="store_true", help="前処理後の画像も保存する")
    a = ap.parse_args()

    if a.cmd == "list":
        print("books:", ", ".join(BOOKS))
        print("recipes:", ", ".join(RECIPES))
        return
    pages = [int(x) for x in a.pages.split(",") if x.strip()]
    if a.cmd == "scan":
        f, l, s = (int(x) for x in a.range.split(","))
        scan(a.book, f, l, s)
    elif a.cmd == "gt":
        build_gt(a.book, pages)
    else:
        recipes = [r.strip() for r in a.recipes.split(",") if r.strip()]
        if recipes == ["all"]:
            recipes = list(RECIPES)
        run(a.book, recipes, a.backend, pages, a.dump)


if __name__ == "__main__":
    main()
