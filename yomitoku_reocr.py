#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YomiToku（ローカル日本語OCR）でスキャンPDFを再OCRし、透明テキスト層として
書き戻す前処理ツール。vision_reocr.py / docai_reocr.py のYomiToku版。

バックエンドの使い分け:
  - Vision版:   月1000ページまで無料・高速。まずこちらを推奨
  - DocAI版:    有料（$1.50/1000ページ）だが本文の誤読が少ない
  - YomiToku版（本ツール）: 完全ローカル・無料・GCP認証不要。CPUで2.5〜3秒/ページ。
    本文精度はVisionと互角（実測: 地下室P.4-15でVision 99.02%対98.89%）で、
    ルビはVisionより強い（同範囲の読み単位で再現率87.2%対81.5%）。
    ルビが語単位でまとまって返るためVision特有のルビ誤結合が原理的に起きず、
    Visionが検出漏れする1文字の極小ルビも自前で検出できる。
    挿絵ページでジャンク文字を返さない点も有利

【ライセンス上の注意】
  YomiToku 本体とそのモデル重みは CC BY-NC-SA 4.0（非商用）です。
    YomiToku (c) 2024 by Kotaro Kinoshita is licensed under CC BY-NC-SA 4.0
    https://creativecommons.org/licenses/by-nc-sa/4.0/
  本ファイル自体は jisui2epub と同じ MIT ライセンスで、YomiToku のコードは
  一切含まず公開APIを呼び出すだけです（同梱もしません）。ただし
  **本ツールを使う経路のみ非商用利用に限られます**。商用利用については
  https://www.mlism.com/ を参照してください。

事前準備（torchを先にCPU版で入れないとCUDA版≈3GBを引きます）:
  .venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
  .venv/bin/pip install yomitoku

使い方:
  .venv/bin/python yomitoku_reocr.py input.pdf                 # 既定出力 <入力>_yomitoku.pdf
  .venv/bin/python yomitoku_reocr.py input.pdf --start 100     # 中断からの再開
  .venv/bin/python yomitoku_reocr.py input.pdf --start 10 --end 20 -o test.pdf
  .venv/bin/python yomitoku_reocr.py input.pdf --no-lite       # 標準モデル（高精度・低速）

設計の詳細は DESIGN_YomiToku.md を参照。
"""
import argparse
import os
import statistics
import sys
import time

import fitz  # PyMuPDF

from jisui2epub import analyze_page, detect_body_size
from vision_reocr import (
    CHECKPOINT_EVERY,
    RUBY_FONTSIZE_RATIO,
    _atomic_save,
    _dedup_symbols,
    _open_source_pdf,
    _snap_column_x,
    insert_invisible_text,
    largest_embedded_image,
)

# `--lite` 相当の設定。yomitoku の CLI が組み立てるものと同じ値を公開APIに
# 渡しているだけで、YomiToku のコードのコピーではない（DESIGN_YomiToku.md §1）。
# parseq-tiny-dynw-v4（32px・192次元の軽量認識器）＋検出器のONNX推論＋
# 動的幅バッチという構成で、CPUでの実行を現実的な速度にする
CONFIG_LITE_CPU = {
    "text_detector": {"infer_onnx": True},
    "text_recognizer": {
        "model_name": "parseq-tiny-dynw-v4",
        "dynamic_width": True,
        "batch_bucketing": True,
        "source_downscale": True,
        "num_parallel_batches": 4,
    },
}

# YomiTokuの推奨入力解像度（短辺）。これを下回るページは精度が落ちるので警告する
MIN_SHORT_SIDE = 720

# YomiTokuの検出ボックス（DBNet）は unclip 処理で実インクより一回り大きく出る。
# 一方、vision_reocr.py の書き戻し（_ruby_params・_is_ruby_symbol）と
# jisui2epub.py 側のしきい値は、いずれも「実インク幅」で較正されている。
# 補正せずに検出ボックス幅をそのまま渡すと、ルビの書き戻しフォントサイズが
# 過大になり（実測: 地下室p5で5.88pt。同じルビをVisionは3.84ptで書き戻す）、
#   (1) 描画スパンが背高になる（高さ≈1.2×fontsize。7.06pt対4.61pt）
#   (2) ルビ列グルーピングのギャップ許容 size×1.7 が緩む（9.996pt対6.53pt）
# の両方が効いて、隣接する別語のルビが1本に誤結合される（実測: 下《した》＋
# 妹《いもうと》→妹《したいもうと》、車《くるま》＋音《おと》など5箇所/1頁）。
# 0.68 は CLAUDE.md 記載の「実インク幅／フォントサイズ＝0.66〜0.71倍」と、
# 同一グリフでのVision実測比（3.84/5.88＝0.65）の重なる範囲。
#
# 全セルに一律で掛けるため、ルビ判定（ページ中央値に対する相対比）や
# 本文サイズの基準値（旧OCR申告との max）には影響しない。縮小は幅のみで、
# y座標には触れない（検出ボックス上端はVisionの字面上端と実測で一致する
# ため。309.48pt 対 309.72pt。上下も縮めると系統的な下方バイアスが乗る）
INK_WIDTH_RATIO = 0.68

# キャリブレーション（書き戻しフォントサイズ基準値の実測）に使うページ数・文字数
CALIBRATION_MAX_PAGES = 15
CALIBRATION_TARGET_CHARS = 800

# 認識信頼度がこれ未満の行を校正用TSVに記録する。実測（地下室P.4-15）では
# 誤読が低スコアに強く集中する（やつきょく=0.07・くるま=0.26・ねんいじょう=0.44）。
# ただし書き戻しの採否には使わない（機械的に捨てると本文に穴が開く）
LOWCONF_THRESHOLD = 0.5


def make_ocr(device="cpu", lite=True):
    """YomiTokuのOCR（検出＋認識）を初期化する。

    DocumentAnalyzer ではなく OCR 単体を使う。DocumentAnalyzer はレイアウト
    解析・表構造認識・読み順推定まで行うが、jisui2epub は列クラスタリングも
    読み順も見出し判定も自前で持つため不要で、実測で約1.8倍遅い
    （地下室p5の同一画像で DocumentAnalyzer 4.73〜5.29秒/頁に対し
    OCR 2.50〜2.95秒/頁。検出語数はどちらも46で完全に同一）。
    初期化も約70秒（初回モデルDL込み）に対し約1.9秒で済む。

    importは関数内で行う（yomitoku 未導入でも他のバックエンドが動くように。
    vision_reocr.py が google.cloud.vision を遅延importしているのと同じ）"""
    try:
        from yomitoku import OCR
    except ImportError:
        print(
            "yomitoku がインストールされていません。次の順で導入してください:\n"
            "  pip install --index-url https://download.pytorch.org/whl/cpu "
            "torch torchvision\n"
            "  pip install yomitoku\n"
            "（torchを先にCPU版で入れないとCUDA版≈3GBがダウンロードされます）",
            file=sys.stderr,
        )
        sys.exit(1)

    configs = CONFIG_LITE_CPU if lite else {}
    return OCR(configs=configs, device=device)


def _decode_image(image_bytes):
    """埋め込み画像のバイト列を YomiToku が期待する BGR の numpy 配列にする。
    yomitoku.data.functions.load_image はパス受け取りなので使わない
    （一時ファイルを作らずに済ませる）"""
    import cv2
    import numpy as np

    buf = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return img


def ocr_page_with_yomitoku(ocr, doc, page_index):
    """ページの埋め込み画像をYomiTokuでOCRし、(words, img_size)を返す。
    ページ再レンダリングはせず埋め込み画像（スキャン原本）をそのまま使う
    （解像度劣化と処理コストを避けるため。Vision/DocAI版と同じ方針）"""
    page = doc[page_index]
    found = largest_embedded_image(doc, page)
    if found is None:
        return None, None
    image_bytes, img_w, img_h = found

    img = _decode_image(image_bytes)
    if img is None:
        return None, None

    result, _ = ocr(img)
    return result.words, (img_w, img_h)


def _expand_line_to_cells(text, points, sx, sy):
    """YomiTokuの「行ポリゴン＋行テキスト」を文字セル (text, Rect) に展開する。

    Vision/DocAI が文字（シンボル）単位のbboxを返すのに対し、YomiTokuは
    行単位のポリゴンと行全体のテキストしか返さないため、行ボックスを
    文字数で等分割して文字セルを合成する。

    等分割してよい根拠: jisui2epub.py の attach_rubies はセルの実Y開始位置を
    bisectで引く方式で、VLine.cell_height() も「セルY開始位置の隣接差の
    中央値」なので、等間隔セルでは真のピッチと一致する（CLAUDE.md記載の
    「等間隔セル＝1行1スパン旧OCRでは従来と完全同値」）。つまり等分割は
    下流の想定内で、新たなヒューリスティックを要さない。

    書字方向は direction フィールドを信用せずボックスのアスペクト比で決める。
    実測（地下室p5）で2文字の縦ルビ「かわ」(w=46,h=81) が
    direction='horizontal' と申告されるなど、短い行での誤申告が頻発する。
    アスペクト比は同ページの全行で正しかった。

    空白は幅ゼロとして扱う（セルを作らない）。YomiTokuは隣接する別語のルビを
    1ボックスにまとめる際に境界へ空白を入れるが（実測: 'ねん い じょう'
    ＝一年《ねん》以上《いじょう》）、ボックスの実寸は空白を除いた文字数×
    ルビピッチとほぼ一致する（249px÷6字＝41.5px。同ページの他のルビの
    ピッチ36〜44pxの範囲内）。空白にも幅を配ると全体のピッチが縮んで
    Y座標→親文字インデックス変換がずれるため、幅ゼロが正しい。
    なお、これによって同一ボックス内の隣接ルビは1本に結合されるが、
    それは親文字が隣り合う「年以上《ねんいじょう》」型の結合であり、
    eval_ruby.py も正解として許容する。Visionで問題になった誤結合は
    ルビの無い親文字を挟んだ別語どうしの結合で、そちらはYomiTokuでは
    別ボックスになるため物理的なギャップが残り発生しない"""
    xs = [p[0] * sx for p in points]
    ys = [p[1] * sy for p in points]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)

    chars = [c for c in text if not c.isspace()]
    if not chars:
        return []

    def cell(cx0, cy0, cx1, cy1):
        """幅だけを実インク相当に縮めたセル（INK_WIDTH_RATIO参照）。
        中心を保って縮めるので列のX中心は変わらない"""
        xc = (cx0 + cx1) / 2
        half = (cx1 - cx0) * INK_WIDTH_RATIO / 2
        return fitz.Rect(xc - half, cy0, xc + half, cy1)

    if len(chars) == 1:
        return [(chars[0], cell(x0, y0, x1, y1))]

    n = len(chars)
    if (y1 - y0) >= (x1 - x0):  # 縦書き
        step = (y1 - y0) / n
        return [
            (c, cell(x0, y0 + i * step, x1, y0 + (i + 1) * step))
            for i, c in enumerate(chars)
        ]
    step = (x1 - x0) / n
    return [
        (c, cell(x0 + i * step, y0, x0 + (i + 1) * step, y1))
        for i, c in enumerate(chars)
    ]


# 語頭の「一」に続きやすい字。行末が「一」でも、それが一番・一度・一カ所の
# ような語の先頭で単に列で折り返しただけなら訂正してはならない
_ICHI_FOLLOW = set("番度人つ緒方応瞬言体般部種件日時年回面様歩生行目切等同致"
                   "カヵケヶ月所")

# 「が開いてから何行以内なら「引用が開いている」とみなすか。無制限にすると、
# 本のどこか1箇所で「の対応が壊れただけで以降ずっと depth>0 になり、
# 無関係な行（章見出し等）で誤爆する（ソフロニア嬢417ページで実測: 窓なしで
# 誤爆5件、窓4で1件）。会話は数列で閉じるので4行あれば足りる
_QUOTE_WINDOW = 4


class BracketFixState:
    """ページをまたいで引用の開閉を追跡するための状態"""

    __slots__ = ("depth", "since_open", "fixed")

    def __init__(self):
        self.depth = 0
        self.since_open = 10 ** 9   # 直近に「が開いてから何行経ったか
        self.fixed = 0


def fix_closing_bracket_lines(ordered, state):
    """行末の「一」を「」」に訂正する（YomiToku固有の誤読への対策）。

    縦書きの鉤括弧「」」は組版上コの字型で、YomiTokuの認識器はこれを横棒
    「一」と誤読することがある（実測: 黒牢城525ページ中47件・どこよりも
    遠い場所にいる君へ81ページ中23件。Visionは同条件で0〜1件）。括弧の
    対応が壊れるため会話文主体の小説では実害が大きい。

    **この訂正はjisui2epub.py側（全バックエンド共有）に置いてはならない。**
    Visionのように別の誤り方をするエンジンの出力に当てると、OCRが崩れた本で
    単語を壊す（実測: ソフロニア嬢のVision再OCR版で「ソフロニア」が列を
    またいで「ソフロ’一」＋「ア」に分断されており、末尾の「一」を「」」に
    すると「ソフロ’」ア」になる）。YomiTokuの認識器固有の癖なので、
    バックエンド側で閉じるのが正しい。

    **判定は「段落の末尾」ではなく「行（縦の列）の末尾」で行う**。「」」が
    消えるとその段落が後続の地の文と結合してしまい、組版済みテキストでは
    位置を特定できない（実例:「…って思って一なぜこの高校を選んだのか…」）。

    規則:
      1. 行を読み順に走査し「『」『』で引用の深さを追跡する
      2. 行末が「一」かつ引用が開いていれば「」」に置換する
      3. ただし直近の「から_QUOTE_WINDOW行を超えていれば置換しない
      4. 次の行の先頭が_ICHI_FOLLOWなら置換しない（一番・一カ所…を守る）

    実測（どこよりも遠い場所にいる君へP.10-90）: 23件中22件が正しい「」」で、
    残り1件も正解が「』」＝閉じ括弧であることに変わりはない。
    """
    for i, w in enumerate(ordered):
        s = w.content.rstrip()
        if not s:
            continue
        opened = s.count("「") + s.count("『")
        closed = s.count("」") + s.count("』")
        if opened > closed:
            state.since_open = 0
        if (s.endswith("一") and state.depth + opened > 0
                and state.since_open <= _QUOTE_WINDOW):
            nxt = ""
            for nw in ordered[i + 1:]:
                if nw.content.strip():
                    nxt = nw.content.lstrip()[:1]
                    break
            if nxt not in _ICHI_FOLLOW:
                w.content = s[:-1] + "」" + w.content[len(s):]
                closed += 1
                state.fixed += 1
        state.depth = max(0, state.depth + opened - closed)
        state.since_open += 1


def collect_page_symbols_yomitoku(words, page, img_size, bracket_state=None):
    """YomiTokuのwords（行）から (text, rect) を縦書きの読み順で返す。

    words は検出順で読み順ではないため、Vision/DocAI版と同じく行ボックスの
    X中心の降順（縦書きは右の列から読む）に並べ替えてから文字セルに展開する。
    見出し（横書き）やノンブル（欄外）はこの並べ替えでは正しい位置に来ないが、
    jisui2epub.py 本体の classify_marginals が別途座標ベースで再分類する"""
    img_w, img_h = img_size
    sx = page.rect.width / img_w
    sy = page.rect.height / img_h

    ordered = sorted(
        words,
        key=lambda w: -sum(p[0] for p in w.points) / len(w.points),
    )

    if bracket_state is not None:
        fix_closing_bracket_lines(ordered, bracket_state)

    symbols = []
    for w in ordered:
        symbols.extend(_expand_line_to_cells(w.content, w.points, sx, sy))

    # 同一列に展開したセルはX座標が既に完全一致しているが、列をまたぐ
    # ジッターの吸収には効くので Vision/DocAI と同じ後処理を通す
    return _dedup_symbols(_snap_column_x(symbols))


def collect_lowconf(words, page_no, threshold=LOWCONF_THRESHOLD):
    """認識信頼度の低い行を校正用に拾う（書き戻しの採否には使わない）"""
    out = []
    for w in words:
        score = getattr(w, "rec_score", None)
        if score is not None and score < threshold:
            xs = [p[0] for p in w.points]
            ys = [p[1] for p in w.points]
            out.append((page_no, min(xs), min(ys), w.content, score))
    return out


def _calibrate_body_fontsize(ocr, doc, start_page, end_page, cache):
    """処理対象ページ先頭から数ページ分を実際にOCRし、実測セル幅の中央値を得る
    （vision_reocr._calibrate_body_fontsizeと同じ方針。詳細はCLAUDE.md参照）"""
    widths = []
    last = min(end_page, start_page + CALIBRATION_MAX_PAGES - 1)
    for p1 in range(start_page, last + 1):
        if len(widths) >= CALIBRATION_TARGET_CHARS:
            break
        idx = p1 - 1
        words, img_size = ocr_page_with_yomitoku(ocr, doc, idx)
        cache[idx] = (words, img_size)
        if words is None:
            continue
        symbols = collect_page_symbols_yomitoku(words, doc[idx], img_size)
        widths.extend(rect.width for _, rect in symbols)
    return statistics.median(widths) if widths else None


def _warn_low_resolution(doc, start_page, end_page):
    """短辺がYomiTokuの推奨解像度を下回るページがあれば警告する"""
    low = []
    for p1 in range(start_page, min(end_page, start_page + 20) + 1):
        found = largest_embedded_image(doc, doc[p1 - 1])
        if found is None:
            continue
        _, w, h = found
        if min(w, h) < MIN_SHORT_SIDE:
            low.append((p1, min(w, h)))
    if low:
        p1, short = low[0]
        print(
            f"警告: 短辺{short}pxのページがあります（ページ{p1}ほか{len(low)}枚）。"
            f"YomiTokuは短辺{MIN_SHORT_SIDE}px以上を推奨します。"
            f"精度が落ちる場合はより高解像度でスキャンし直してください"
        )


def reocr_pdf(ocr, input_path, output_path, start_page, end_page, lowconf_path=None):
    # --start再開時（入力＝出力）はメモリから開く（Windowsでチェックポイント
    # のos.replaceが自分自身のハンドルと衝突してWinError 5になる対策。
    # 詳細は vision_reocr._open_source_pdf）
    doc = _open_source_pdf(input_path, output_path)
    end_page = min(end_page, len(doc))

    _warn_low_resolution(doc, start_page, end_page)

    # 書き戻す本文フォントサイズの基準値: YomiToku実測セル幅と旧OCR申告の
    # 大きい方（旧OCRページと混在しても本文がルビ誤判定されない安全側。
    # CLAUDE.md参照）
    cache = {}
    yomi_body = _calibrate_body_fontsize(ocr, doc, start_page, end_page, cache)
    old_ocr_body = detect_body_size(doc, range(len(doc))) or None
    if yomi_body is not None and old_ocr_body is not None:
        target_body_fontsize = max(yomi_body, old_ocr_body)
        print(
            f"本文フォントサイズ基準値: YomiToku実測{yomi_body:.2f}pt / "
            f"旧OCR申告{old_ocr_body:.2f}pt → 大きい方の"
            f"{target_body_fontsize:.2f}ptを採用"
        )
    elif yomi_body is not None:
        target_body_fontsize = yomi_body
        print(f"本文フォントサイズ基準値（YomiToku実測）: {target_body_fontsize:.2f}pt")
    else:
        target_body_fontsize = None
        print("キャリブレーションできる文字が見つからず、最初の処理ページから決めます")
    old_ocr_body = old_ocr_body or target_body_fontsize

    total_chars = 0
    total_ruby = 0
    processed = 0
    lowconf = []
    bracket_state = BracketFixState()
    total_pages = end_page - start_page + 1
    t0 = time.time()

    try:
        for i, p1 in enumerate(range(start_page, end_page + 1), 1):
            idx = p1 - 1
            page = doc[idx]
            if idx in cache:
                words, img_size = cache.pop(idx)
            else:
                words, img_size = ocr_page_with_yomitoku(ocr, doc, idx)
            if words is None:
                print(f"ページ{p1}: 埋め込み画像なし、スキップ")
                continue

            symbols = collect_page_symbols_yomitoku(
                words, page, img_size, bracket_state
            )
            lowconf.extend(collect_lowconf(words, p1))

            if target_body_fontsize is None:
                if not symbols:
                    print(f"ページ{p1}: 文字なし、スキップ")
                    continue
                target_body_fontsize = statistics.median(
                    rect.width for _, rect in symbols
                )
                old_ocr_body = old_ocr_body or target_body_fontsize
                print(
                    f"  (参照フォントサイズなし。ページ{p1}のセル幅から"
                    f"{target_body_fontsize:.1f}ptを基準に採用)"
                )
            target_ruby_fontsize = target_body_fontsize * RUBY_FONTSIZE_RATIO

            # 除去（redaction）で消える前に旧OCRテキスト層を解析し、
            # YomiTokuが検出しなかったルビの補完に使う。YomiTokuのルビ検出は
            # Visionより強い（読み単位の再現率87.2%対81.5%）が漏れは残るため、
            # 位置照合で重複を防ぎつつ併用する
            old_rubies = analyze_page(page, idx, old_ocr_body).rubies

            page.add_redact_annot(page.rect)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            )

            n, n_ruby = insert_invisible_text(
                page, symbols, target_body_fontsize, target_ruby_fontsize, old_rubies
            )
            total_chars += n
            total_ruby += n_ruby
            processed += 1

            # 課金がないぶん時間が主なコストなので、残り時間を出す
            elapsed = time.time() - t0
            per_page = elapsed / i
            remain = per_page * (total_pages - i)
            print(
                f"ページ{p1}: {n}文字書き戻し（うちルビ{n_ruby}） "
                f"[{elapsed:.0f}秒経過 / {per_page:.1f}秒per頁 / 残り約{remain / 60:.0f}分]"
            )

            if i % CHECKPOINT_EVERY == 0:
                _atomic_save(doc, output_path)
                print(f"  [チェックポイント保存: {output_path}]")
    finally:
        _atomic_save(doc, output_path, final=True)
        doc.close()

    if lowconf_path and lowconf:
        with open(lowconf_path, "w", encoding="utf-8") as f:
            f.write("page\tx\ty\ttext\trec_score\n")
            for p1, x, y, text, score in lowconf:
                f.write(f"{p1}\t{x}\t{y}\t{text}\t{score:.3f}\n")
        print(f"信頼度の低い行 {len(lowconf)}件 を記録しました: {lowconf_path}")

    elapsed = time.time() - t0
    if bracket_state.fixed:
        print(
            f"縦書き鉤括弧の誤読（一→」）を訂正: {bracket_state.fixed} 箇所"
        )
    print(
        f"完了: {processed}ページ処理 / "
        f"文字数{total_chars}（うちルビ{total_ruby}） / {elapsed:.0f}秒"
    )
    print(f"保存しました: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="YomiTokuでスキャンPDFを再OCRし、透明テキスト層を書き戻す"
    )
    parser.add_argument("input_pdf")
    parser.add_argument(
        "--start", type=int, default=1, help="開始ページ（1始まり、既定:1）"
    )
    parser.add_argument(
        "--end", type=int, default=None, help="終了ページ（1始まり、既定:最終ページ）"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="出力PDFパス（既定: <入力>_yomitoku.pdf）"
    )
    parser.add_argument(
        "--device", default="cpu",
        help="推論デバイス（既定: cpu）。gpuはCUDA環境向けで本環境では未検証",
    )
    parser.add_argument(
        "--no-lite", action="store_true",
        help="軽量モデルでなく標準モデルを使う。約2倍遅く、実測では精度が"
             "向上しなかったため通常は不要（黒牢城P.128-132で一致率95.21%と同一、"
             "稀用漢字はむしろ悪化）",
    )
    parser.add_argument(
        "--no-lowconf-report", action="store_true",
        help="信頼度の低い行のTSV出力（<出力>_lowconf.tsv）を行わない",
    )
    args = parser.parse_args()

    if args.output:
        output_path = args.output
    else:
        stem = args.input_pdf.rsplit(".", 1)[0]
        output_path = f"{stem}_yomitoku.pdf"

    lowconf_path = None
    if not args.no_lowconf_report:
        lowconf_path = f"{output_path.rsplit('.', 1)[0]}_lowconf.tsv"

    end_page = args.end
    if end_page is None:
        with fitz.open(args.input_pdf) as d:
            end_page = len(d)

    # --start での中断再開は既存の出力ファイルを土台にする（vision_reocr.pyと同じ）
    source = args.input_pdf
    if args.start > 1 and os.path.exists(output_path):
        print(f"既存の出力 {output_path} を土台に {args.start} ページ目から再開します")
        source = output_path

    print(f"モデル: {'lite (parseq-tiny-dynw-v4)' if not args.no_lite else '標準'} / "
          f"デバイス: {args.device}")
    ocr = make_ocr(device=args.device, lite=not args.no_lite)

    try:
        reocr_pdf(ocr, source, output_path, args.start, end_page, lowconf_path)
    except Exception:
        print(
            f"エラーで中断しました。{output_path} には直近のチェックポイントまでの"
            f"結果が保存されています。--start で続きから再開できます",
            file=sys.stderr,
        )
        raise


if __name__ == "__main__":
    main()
