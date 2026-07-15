#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Cloud Vision API (document_text_detection) でスキャンPDFを再OCRし、
透明テキスト層として書き戻す前処理ツール。

自炊PDFに元々付いているスキャナ内蔵OCR（ScanSnap等）はしばしば文字化けが
ひどく、jisui2epub.py の抽出精度の上限になっている。本スクリプトで高精度な
Vision APIのテキスト層に差し替えてから jisui2epub.py にかけることで、
本文・ルビ双方の認識精度を底上げできる。

設計の要点（詳細は CLAUDE.md 参照）:
  - 各ページの埋め込み画像（スキャン原本）をそのままVisionに送る
    （再レンダリングによる劣化を避ける）
  - 縦書きは1文字ずつ box が積まれるため、文字（シンボル）単位で
    insert_text により不可視テキストを配置する
  - ルビは「縦書きの列幅」で判定する（列幅は本文と同じで高さだけ小さい
    句読点・小書き仮名を誤ってルビ扱いしないため）。ルビはVision実測幅の
    中央値で書き戻すことで、jisui2epub.py 本体の attach_rubies
    （フォントサイズ比0.68倍でルビを親文字に紐付け）とルビ列グルーピング
    がそのまま機能する
  - Visionのblock配列順は縦書き複数列の読み順と一致しないことがあるため、
    ブロックをX座標降順（右の列から）に並べ替えてから書き戻す
  - Visionは1文字だけの極小ルビを旧OCRより検出漏れしやすく、ルビ密度が
    非常に高い本では検出漏れが連鎖してルビの親文字対応付けが大きく崩れる。
    redaction前に旧OCRテキスト層も解析しておき、Visionが検出しなかった
    箇所だけ旧OCR側のルビで補完する
  - 書き戻す本文フォントサイズはVision実測の列幅（複数ページ事前OCRして
    キャリブレーション）と旧OCR自己申告フォントサイズの大きい方を採用する。
    小さい方を使うと、ルビ密度の高い本ではルビ対応付け精度は上がるが、
    別の本（旧OCR申告がVision実測よりかなり小さい場合）で本文が丸ごと
    ルビと誤判定される重大な回帰を起こすため、安全側の大きい方を選ぶ
    （詳細はCLAUDE.mdの「原因2」参照）

事前準備:
  .venv/bin/pip install google-cloud-vision
  Google Cloud Consoleで対象プロジェクトの Vision API を有効化し、
  課金を有効化した上で、サービスアカウントキーを用意する
  export GOOGLE_APPLICATION_CREDENTIALS="/path/to/key.json"

使い方:
  .venv/bin/python vision_reocr.py input.pdf
  .venv/bin/python vision_reocr.py input.pdf --start 10 --end 20 -o test.pdf
  .venv/bin/python vision_reocr.py input.pdf --start 100  # 中断後の再開に

料金: document_text_detection は月1000ユニットまで無料
  (https://cloud.google.com/vision/pricing)。367ページの本なら1冊あたり
  無料枠内に収まる
"""
import argparse
import os
import statistics
import sys
import time

import fitz  # PyMuPDF
from google.api_core import exceptions as gexc

# google.cloud.vision のimportはVision APIを実際に呼ぶ関数内で行う（遅延import）。
# 本モジュールの書き戻しヒューリスティック（_snap_column_x・
# insert_invisible_text・_gapfill_missing_rubies等）は docai_reocr.py からも
# importして共有しており、Document AIだけを使うユーザーが
# google-cloud-vision 未インストールでも動かせるようにするため
from jisui2epub import analyze_page, detect_body_size, RUBY_SIZE_RATIO

# ルビ判定閾値: 本文の縦書き列幅の何倍未満をルビ（小書き文字）とみなすか。
# jisui2epub.py 本体のルビ判定(RUBY_SIZE_RATIO=0.68倍以下)と揃えている
RUBY_WIDTH_RATIO = RUBY_SIZE_RATIO

# ルビと判定した文字に書き戻す際のフォントサイズ（本文フォントサイズに対する比率）。
# 実際の縦書きルビの視覚比率(およそ0.5)に合わせつつ、jisui2epub.py側の
# 0.68倍しきい値に対して余裕を持って下回るようにする
RUBY_FONTSIZE_RATIO = 0.5

# 【既知の限界・未適用】insert_text(fontname="japan")で実測した「指定
# fontsizeに対する実際の描画インク高さ」の比率（幅はほぼ等倍で高さだけ
# この比率で大きくなる。fontsize=10で検証: 幅10.0・高さ12.0）。文字
# （シンボル）単位で挿入する際、この分だけ縦の実寸が本来より大きく描画
# されるため、同一列内の隣接文字どうしが視覚的に重なり、jisui2epub.py
# 本体のVLine.cell_height()（実測インク高さの中央値）が本来の文字間隔
# より大きく計算され、ルビの親文字への対応付け（Y座標→文字インデックス
# 変換、attach_rubies）がずれることがある（実測: 地下室からのふしぎな旅
# P.4-13でcell_height()が本来の約1.16倍(11.52pt/9.94pt)に膨らんだ）。
#
# 挿入fontsizeをこの比率で割り引けば理論上は解消するが、そうすると
# 宣言フォントサイズ自体が縮み、旧OCRページ（申告サイズがVision実測の
# 文字間隔よりかなり大きいことがある）と混在する運用では、書き戻した
# 本文の宣言サイズがドキュメント全体の0.68倍しきい値を割り込み、本文が
# 丸ごとルビと誤判定される重大な回帰を引き起こすことを確認した。
# 列ごとにY座標を「実測間隔→宣言サイズ相当の間隔」へ引き伸ばす変換を
# 咬ませれば両立できる可能性があるが未実装。現状は割り引かず、
# キャリブレーション（_calibrate_body_fontsize）による書き戻しサイズの
# 適正化のみで対応している（この対応だけでもルビペア一致率は
# 地下室17.8%→46%、霧11.9%→44%まで改善済み）
FONT_HEIGHT_RATIO = 1.2

# Visionが半角で返すがこの作品群では正文が全角である記号の対応表
_FULLWIDTH_PUNCT = {"!": "！", "?": "？"}

# 一時的なAPIエラーのリトライ回数・待機秒（指数バックオフ）
MAX_RETRIES = 4
RETRY_BASE_WAIT = 2.0

# 何ページ処理するごとに出力PDFを保存するか（中断時の作業消失を防ぐ）
CHECKPOINT_EVERY = 20


def call_vision_with_retry(client, image):
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.document_text_detection(
                image=image, image_context={"language_hints": ["ja"]}
            )
            if response.error.message:
                raise RuntimeError(f"Vision APIエラー: {response.error.message}")
            return response
        except (
            gexc.ServiceUnavailable,
            gexc.DeadlineExceeded,
            gexc.ResourceExhausted,
            gexc.InternalServerError,
        ) as e:
            last_err = e
            wait = RETRY_BASE_WAIT * (2**attempt)
            print(f"    一時エラー({e.__class__.__name__})、{wait:.0f}秒後にリトライ...")
            time.sleep(wait)
    raise last_err


def largest_embedded_image(doc, page):
    """ページ内で最大面積の埋め込み画像を返す（複数画像がある場合、
    スキャン原本と思われるものを選ぶ）"""
    imgs = page.get_images(full=True)
    if not imgs:
        return None
    best = max(imgs, key=lambda im: (im[2] or 0) * (im[3] or 0))
    xref = best[0]
    base_image = doc.extract_image(xref)
    return base_image["image"], base_image["width"], base_image["height"]


def ocr_page_with_vision(client, doc, page_index):
    from google.cloud import vision

    page = doc[page_index]
    found = largest_embedded_image(doc, page)
    if found is None:
        return None, None
    image_bytes, img_w, img_h = found

    response = call_vision_with_retry(client, vision.Image(content=image_bytes))
    return response.full_text_annotation, (img_w, img_h)


def _iter_blocks(annotation, sx, sy):
    """Visionのblock単位で (text, rect) のリストを列挙。rectはPDFポイント座標。
    Visionが返すblock配列の順序は縦書き複数列レイアウトの読み順と一致しない
    ことがある（列の折り返しで語が2ブロックに分断され、かつブロック順が前後
    することがある）ためブロック単位でまとめて返し、呼び出し側でX座標降順
    （縦書きは右の列から読む）に並べ替えてから結合する"""
    for pageinfo in annotation.pages:
        for block in pageinfo.blocks:
            symbols = []
            for paragraph in block.paragraphs:
                for word in paragraph.words:
                    for symbol in word.symbols:
                        text = symbol.text
                        if not text.strip():
                            continue
                        verts = symbol.bounding_box.vertices
                        xs = [v.x * sx for v in verts]
                        ys = [v.y * sy for v in verts]
                        rect = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
                        if rect.is_empty:
                            continue
                        symbols.append((text, rect))
            if symbols:
                yield symbols


def collect_page_symbols(annotation, page, img_size):
    """Vision結果から (text, rect) を、縦書きの読み順（列のX座標降順）に
    並べ替えて返す。rectはPDFポイント座標。

    Visionが返すblock配列の順序は縦書き複数列レイアウトの読み順と一致しない
    ことがある（列の折り返しで語が2ブロックに分断され、かつブロック順が前後
    することがある）ためブロック単位でまとめてから、ブロックをX座標降順
    （縦書きは右の列から読む）に並べ替えて結合する"""
    img_w, img_h = img_size
    pw, ph = page.rect.width, page.rect.height
    sx, sy = pw / img_w, ph / img_h

    blocks = list(_iter_blocks(annotation, sx, sy))
    if not blocks:
        return []

    blocks.sort(
        key=lambda syms: sum(r.x0 + r.x1 for _, r in syms) / (2 * len(syms)),
        reverse=True,
    )
    return _snap_column_x([sym for block in blocks for sym in block])


# 縦書きの同一列が複数のVisionブロックに分断された際、ブロック間のX座標に
# 乗るジッター（実測0.1〜0.5pt程度）を「同一列」とみなして無視する許容量。
# 本文の列間隔（実測8〜14pt程度）よりは十分小さく、観測されたジッターよりは
# 十分大きい値
COLUMN_SNAP_TOLERANCE = 3.0


def _snap_column_x(symbols):
    """同一列が複数ブロックに分かれることで生じるX座標のジッターを、直前の
    列と同じ値に丸めて消す。

    jisui2epub.py本体の縦行クラスタリングは、スパンを「(X中心, Y)」で
    ソートしてから隣接するスパインをY方向のギャップでまとめる。列を跨いだ
    ブロック分断でX中心がわずかに（0.1pt程度）ぶれると、本来は後に読む
    はずの下側の文字群のX中心が上側の文字群よりわずかに小さくなり、ソート
    順が逆転することがある。その場合、上側の文字群を処理する時点では
    下側の文字群がまだ「はるか下のY座標にある別の列」として存在しており、
    Y方向のギャップしきい値を超えてしまうため、同一列なのに縦行が2本に
    分断される（実例: ほんものの魔法使P.12、「モプシーはあの…り」と
    「毛が多いので、事」が別々の縦行として抽出され、assemble_text側で
    本文が短い行に分断された）。ブロックをまたいでもX中心が近い（列間隔
    より十分小さい）連続シンボルは同一列として扱い、先頭シンボルの
    X座標に完全一致させることでソート順の逆転を防ぐ。

    半角の数字等（ノンブル等）はスナップの対象・基準のどちらにもしない。
    ノンブルは本文の列のすぐ近くに単独で出現し、かつ半角数字は文字幅が
    本文よりずっと狭いため、これを基準にしてしまうと、たまたまX中心が
    近いだけの本文列全体がノンブルの狭い幅に引きずられて上書きされ、
    本文がまるごとルビ幅未満と誤判定されることがある（実例: ソフロニア嬢
    P.15、ノンブル「14」の直後にあった本文120字が幅4pt程度に潰され、
    ルビとして誤抽出された）。ただし全角化予定の半角記号（！？、
    _FULLWIDTH_PUNCT参照）は本文中に現れる通常の句読点なので、この除外に
    含めず通常どおりスナップに参加させる（除外すると単独の孤立した記号として
    列からはみ出してしまう）"""
    result = []
    col_x0 = col_x1 = None
    for text, rect in symbols:
        if text.isascii() and text not in _FULLWIDTH_PUNCT:
            result.append((text, rect))
            continue
        xc = (rect.x0 + rect.x1) / 2
        if col_x0 is not None and abs(xc - (col_x0 + col_x1) / 2) < COLUMN_SNAP_TOLERANCE:
            rect = fitz.Rect(col_x0, rect.y0, col_x1, rect.y1)
        else:
            col_x0, col_x1 = rect.x0, rect.x1
        result.append((text, rect))
    return result


# 旧OCRのルビ列とVision検出のルビ文字を突き合わせる際のX一致許容量(pt)。
# ルビ列の幅（実測3〜5pt程度）に対して十分な余裕を見る
RUBY_GAP_X_TOLERANCE = 5.0


def _old_ruby_covered(old_ruby, vision_ruby_symbols):
    """旧OCRのルビ列(RubyRun)のX列・Y範囲に、Vision検出のルビ文字が
    1つでも重なっているか判定する"""
    for _, rect in vision_ruby_symbols:
        xc = (rect.x0 + rect.x1) / 2
        if abs(xc - old_ruby.xc) > RUBY_GAP_X_TOLERANCE:
            continue
        if rect.y1 >= old_ruby.y0 and rect.y0 <= old_ruby.y1:
            return True
    return False


def _gapfill_missing_rubies(symbols, old_rubies, ruby_threshold, target_ruby_fontsize):
    """Visionが検出しなかった旧OCR側のルビを補い、(text, rect, fontsize) の
    リストとして返す（呼び出し側でVision分とは別に挿入する）。

    Visionは1文字だけの極小ルビ（例: 手《て》・引《ひ》）を旧OCRより検出漏れ
    しやすい（実測: 地下室からのふしぎな旅P.5で確認。旧OCRのテキスト層には
    「て」「ひ」が単独行として存在するが、同じページをVisionで再OCRすると
    全く出現しない＝Vision側の検出漏れ）。検出漏れがあると、jisui2epub.py
    本体のルビ→親文字の対応付け（近接優先の貪欲マッチング）が後続のルビも
    巻き込んで連鎖的にずれてしまい、ルビ密度の高い本では正解とのルビペア
    一致率が2割以下まで落ち込むことを確認した（地下室P.4-13で17.8%、
    霧のむこうのふしぎな町P.7-12で11.9%）。

    redactionで消す前の旧OCRテキスト層を`analyze_page`で解析し（呼び出し側
    で実施）、Visionのルビ検出が同じ位置に何もない箇所だけ、旧OCR側の
    ルビをそのまま採用して埋める。Vision側に何か検出があればそちらを
    信頼し、重複挿入を避けるため補完しない（旧OCRは文字そのものの精度は
    Visionに劣るため、位置が分かっている場合はVisionを優先する）。

    フォントサイズは統一のtarget_ruby_fontsizeではなく、旧OCRのそのルビ自身の
    実測セル高さ（cell_h）から決める。統一サイズを使うと、旧OCRでは
    3pt程度しかない極小ルビの枠に対して大きすぎる文字を描画することになり、
    隣接する別のルビ（例: 手《て》の直後にある引《ひ》）の描画範囲と重なって
    しまい、jisui2epub.py側のルビ結合ロジック（Y方向の隙間で同一ルビ内と
    判定）が2つの別々のルビを「てひ」のように誤って1本に結合してしまう
    （実測で確認したバグ）。cell_hを使えば実寸に近いサイズで描画され、
    target_ruby_fontsizeより大きくなることもない（安全のためcapする）"""
    vision_ruby_symbols = [
        (t, r) for t, r in symbols if not t.isascii() and r.width < ruby_threshold
    ]
    filled = []
    for old_ruby in old_rubies:
        text = old_ruby.text.strip()
        if not text or _old_ruby_covered(old_ruby, vision_ruby_symbols):
            continue
        n = len(text)
        cell_h = (old_ruby.y1 - old_ruby.y0) / n
        fontsize = max(1.0, min(cell_h * 0.95, target_ruby_fontsize))
        for i, ch in enumerate(text):
            y0 = old_ruby.y0 + i * cell_h
            y1 = old_ruby.y0 + (i + 1) * cell_h
            filled.append((ch, fitz.Rect(old_ruby.x0, y0, old_ruby.x1, y1), fontsize))
    return filled


def insert_invisible_text(
    page, symbols, target_body_fontsize, target_ruby_fontsize, old_rubies=()
):
    """文字（シンボル）単位で不可視テキストを挿入。縦書きは1文字ずつboxが
    積まれているため、word単位で横書き前提のinsert_textboxを使うと文字が
    列幅に収まりきらず欠落する。文字単位でinsert_text（原点指定・行送り
    なし）を使うことで解決する。

    ルビ（小書きの読み仮名）は「このページ自身の」縦書き列幅（rect.width）
    の中央値に対して RUBY_WIDTH_RATIO 未満かどうかで判定する。句読点や
    小書き仮名（っ・ょ等）は本文と同じ列幅を保ったまま高さ（インク）だけが
    小さくなるため、高さでなく幅で判定することで誤ってルビ扱いしない。
    実際のルビの列幅は本文のおよそ半分と明確に分かれるため、ページ内の
    相対比較としては頑健。半角の数字・英字はルビには出現しないため
    判定対象から除く。

    一方、実際に書き戻すフォントサイズには、この判定に使った値をそのまま
    使わず target_body_fontsize / target_ruby_fontsize（呼び出し側で
    ドキュメント全体から求めた基準値）を使う。Visionのインク幅（文字の
    可視範囲）は、フォント設計上の字面外マージンを含む「フォントサイズ」
    そのものより一回り小さく出る（実測でおよそ0.66〜0.71倍）。ページごとの
    インク幅をそのままフォントサイズとして書き戻すと、旧OCRページ由来の
    ドキュメント全体の本文フォントサイズを基準にした0.68倍しきい値を
    わずかに割り込み、本文ページが丸ごとルビと誤判定されることがある
    （実例: ほんものの魔法使P.11、インク幅8.28pt／旧OCR本文12.5pt＝0.662倍
    でしきい値未満になり、本文の縦行25本が全てルビとして抽出され、
    assemble_text側で本文が1文字ずつの改行に分断された）。書き戻す
    フォントサイズをドキュメント全体で固定の基準値にすることで解消する。

    ただしルビの書き戻しフォントサイズだけは、基準値でなくこのページの
    ルビ実測幅の中央値を使う（target_ruby_fontsize は上限キャップ）。
    ルビのサイズはjisui2epub.py側のルビ列グルーピング（Y方向ギャップが
    size×1.7以内なら同一ルビ）の閾値になるため、実寸（4〜5pt程度）より
    大きい値（本文基準×0.5≈7pt）で書き戻すと閾値が緩みすぎて、親文字
    1文字を挟んで隣接する別の語のルビ同士（例: 中大兄皇子《なかのおおえの
    みこ》が中臣鎌足《なかとみのかまたり》…ギャップ≈本文1文字分9.5pt）
    が1本のルビに誤結合される（蘇我氏P.5-14で実測）。実測幅なら
    ギャップ閾値≈8ptで正しく分離され、同一語内のルビ（ピッチ≈5pt）は
    結合されたまま保たれる。本文側と違い、ルビのサイズが小さい分には
    0.68倍しきい値の判定はより安全側に働くため、実測値を使ってよい。

    old_rubies を渡すと、Visionが検出しなかった旧OCR側のルビを位置照合で
    補完する（_gapfill_missing_rubies参照）"""
    if not symbols:
        return 0, 0

    local_body_width = statistics.median(rect.width for _, rect in symbols)
    ruby_threshold = local_body_width * RUBY_WIDTH_RATIO

    ruby_widths = [
        rect.width for text, rect in symbols
        if not text.isascii() and rect.width < ruby_threshold
    ]
    if ruby_widths:
        ruby_fontsize = min(statistics.median(ruby_widths), target_ruby_fontsize)
    else:
        ruby_fontsize = target_ruby_fontsize

    gapfilled = (
        _gapfill_missing_rubies(symbols, old_rubies, ruby_threshold, ruby_fontsize)
        if old_rubies
        else []
    )

    count = 0
    ruby_count = 0
    for text, rect in symbols:
        is_ruby = not text.isascii() and rect.width < ruby_threshold
        text = _FULLWIDTH_PUNCT.get(text, text)
        fontsize = ruby_fontsize if is_ruby else target_body_fontsize
        # 挿入基準はベースライン指定だが、描画bboxの「上端」がVision実測の
        # 字面上端(rect.y0)に一致するようベースラインを置く
        # （baseline = y0 + ascender×fontsize。fontname="japan"のascenderは
        # 実測1.0、descenderは0.2）。従来のrect.y1（字面下端）基準だと、
        # フォントサイズが実際の字面より大きい場合にbbox上端＝セルY開始位置が
        # 実際の字面上端より上にずれる。本文（大きいサイズで書き戻す）と
        # ルビ（実寸で書き戻す）とでズレ量が違うため、attach_rubies の
        # 「ルビY座標→親文字インデックス」変換に系統的なバイアス
        # （実測+0.3〜0.5文字分）が乗り、親文字範囲が1文字後ろにはみ出す
        # （蘇我氏《そが》←正しくは蘇我《そが》氏、のような誤り）。
        # 上端合わせなら本文・ルビともセル開始位置が真の字面位置に一致し、
        # バイアスが消える
        page.insert_text(
            fitz.Point(rect.x0, rect.y0 + fontsize),
            text,
            fontsize=fontsize,
            fontname="japan",
            render_mode=3,  # 不可視
        )
        count += 1
        ruby_count += is_ruby

    # 旧OCRから補完したルビ（フォントサイズは実測セル高さ由来で個別に決まる）
    for text, rect, fontsize in gapfilled:
        page.insert_text(
            fitz.Point(rect.x0, rect.y0 + fontsize),
            text,
            fontsize=fontsize,
            fontname="japan",
            render_mode=3,  # 不可視
        )
        count += 1
        ruby_count += 1
    return count, ruby_count


def _atomic_save(doc, output_path):
    """一時ファイルに保存してから置き換える。チェックポイント保存中の中断で
    出力ファイルが壊れるのを防ぐ。--start で再開する場合、出力パスから
    開いたdocをそのまま同じ output_path に doc.save() することはできない
    （PyMuPDFの制約でincremental保存以外は不可）ため、この方式が必須"""
    tmp_path = output_path + ".tmp"
    doc.save(tmp_path, garbage=4, deflate=True)
    os.replace(tmp_path, output_path)


# 書き戻すフォントサイズの基準値を求めるために事前OCRするページ数・文字数の上限
CALIBRATION_MAX_PAGES = 15
CALIBRATION_TARGET_CHARS = 800


def _calibrate_body_fontsize(client, doc, start_page, end_page, cache):
    """処理対象ページ範囲の先頭からVisionで実際に数ページ分OCRし、実測の
    縦書き列幅（＝文字間隔）の中央値を書き戻しフォントサイズの基準値とする。
    OCR結果はcacheに保存し、後続のメインループで同じページを再OCRしない
    （API呼び出しの二重コストを避ける）。

    jisui2epub.py本体の`detect_body_size`（旧OCRの申告フォントサイズを
    文字数重み付き最頻値で求める）を基準値に使うと、スキャナ/OCRエンジンに
    よっては申告サイズが実際の文字間隔よりかなり大きいことがあり（実測:
    地下室からのふしぎな旅で申告13.8ptに対しVision実測列幅10.08pt、
    霧のむこうのふしぎな町でも同様）、そのフォントサイズで文字を書き戻すと
    隣接する文字どうしが視覚的に重なってしまう。重なるとjisui2epub.py本体の
    `VLine.cell_height()`が本来より大きく計算され、ルビの親文字への対応付け
    （Y座標→文字インデックス変換）が誤り、ルビ密度の高い本では正解との
    ルビペア一致率が2割以下まで落ち込む不具合の主因と判明した。Visionの
    実測列幅を基準にすることで、書き戻す文字が実際の文字間隔に収まり
    この重なりを防ぐ。

    1ページだけから求めると、本文行がほとんど無い扉ページ等で見出しの
    大きな文字に引きずられることがある（実例: ほんものの魔法使P.10、
    見出しのみで本文なし）ため、実測データが一定数たまるまで複数ページを
    サンプリングしてから中央値をとる"""
    widths = []
    for p1 in range(start_page, min(end_page, start_page + CALIBRATION_MAX_PAGES - 1) + 1):
        if len(widths) >= CALIBRATION_TARGET_CHARS:
            break
        idx = p1 - 1
        annotation, img_size = ocr_page_with_vision(client, doc, idx)
        cache[idx] = (annotation, img_size)
        if annotation is None:
            continue
        symbols = collect_page_symbols(annotation, doc[idx], img_size)
        widths.extend(rect.width for _, rect in symbols)
    return statistics.median(widths) if widths else None


def reocr_pdf(input_path, output_path, start_page, end_page):
    from google.cloud import vision

    client = vision.ImageAnnotatorClient()
    doc = fitz.open(input_path)
    end_page = min(end_page, len(doc))

    # 書き戻す本文フォントサイズの基準値を決める。
    # Vision実測（実際の文字間隔に忠実。地下室からのふしぎな旅・霧の
    # むこうのふしぎな町のような、ルビ密度が非常に高くVision実測が旧OCR
    # 申告よりかなり小さい本で、書き戻した文字が視覚的に重なりcell_height()
    # が狂ってルビの親文字対応付けが崩れるのを防ぐ）と、jisui2epub.py本体の
    # detect_body_size（旧OCR自己申告。ほんものの魔法使のような、Vision実測が
    # 旧OCR申告よりかなり小さい本で、混在するドキュメント全体の0.68倍
    # しきい値を割り込み本文が丸ごとルビ誤判定されるのを防ぐ）の
    # 大きい方を採用する。両者に大きな差がある本では一方の問題が必ず残る
    # トレードオフだが、「ルビ紐付けの精度が落ちる」方が「本文が丸ごと
    # 消失する」よりはるかに軽微な実害のため、大きい方＝安全側を選ぶ
    # （キャリブレーション時にOCRしたページはcacheに残し、メインループで
    # 再利用してAPI呼び出しの二重コストを避ける）
    vision_cache = {}
    vision_body_fontsize = _calibrate_body_fontsize(
        client, doc, start_page, end_page, vision_cache
    )
    old_ocr_body_size = detect_body_size(doc, range(len(doc))) or None

    if vision_body_fontsize is not None and old_ocr_body_size is not None:
        target_body_fontsize = max(vision_body_fontsize, old_ocr_body_size)
        print(
            f"本文フォントサイズ基準値: Vision実測{vision_body_fontsize:.2f}pt / "
            f"旧OCR申告{old_ocr_body_size:.2f}pt → 大きい方の"
            f"{target_body_fontsize:.2f}ptを採用"
        )
    elif vision_body_fontsize is not None:
        target_body_fontsize = vision_body_fontsize
        print(f"本文フォントサイズ基準値（Vision実測）: {target_body_fontsize:.2f}pt")
    else:
        target_body_fontsize = None
        print("キャリブレーションできる文字が見つからず、最初の処理ページから決めます")

    # 旧OCRテキスト層自体（ルビ補完のgapfill用）を解析する際は、旧OCRが
    # 自己申告するフォントサイズを使う。旧OCRのルビ／本文判定は旧OCR自身の
    # スケールに対して行わないと閾値がずれる（例: 地下室からのふしぎな旅の
    # 旧ルビ「て」の申告サイズは5.5pt。旧OCR自己申告の本文13.8pt基準なら
    # 0.68倍しきい値9.4pt未満で正しくルビ判定できる）
    old_ocr_body_size = old_ocr_body_size or target_body_fontsize

    total_chars = 0
    total_ruby = 0
    processed = 0
    t0 = time.time()

    try:
        for i, p1 in enumerate(range(start_page, end_page + 1), 1):
            idx = p1 - 1
            page = doc[idx]
            if idx in vision_cache:
                annotation, img_size = vision_cache.pop(idx)
            else:
                annotation, img_size = ocr_page_with_vision(client, doc, idx)
            if annotation is None:
                print(f"ページ{p1}: 埋め込み画像なし、スキップ")
                continue

            symbols = collect_page_symbols(annotation, page, img_size)

            if target_body_fontsize is None:
                if not symbols:
                    print(f"ページ{p1}: 文字なし、スキップ")
                    continue
                target_body_fontsize = statistics.median(
                    rect.width for _, rect in symbols
                )
                print(
                    f"  (参照フォントサイズなし。ページ{p1}のインク幅から"
                    f"{target_body_fontsize:.1f}ptを基準に採用)"
                )
            target_ruby_fontsize = target_body_fontsize * RUBY_FONTSIZE_RATIO

            # 除去（redaction）で消える前に、旧OCRテキスト層をjisui2epub.py
            # 本体と同じロジックで解析し、ルビ候補(RubyRun)を控えておく。
            # Visionが検出漏れしたルビをあとで補完するために使う
            old_rubies = analyze_page(page, idx, old_ocr_body_size).rubies

            # 既存テキスト層を除去（画像・線画は保護）
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
            elapsed = time.time() - t0
            print(f"ページ{p1}: {n}文字書き戻し（うちルビ{n_ruby}） [{elapsed:.0f}秒経過]")

            if i % CHECKPOINT_EVERY == 0:
                _atomic_save(doc, output_path)
                print(f"  [チェックポイント保存: {output_path}]")
    finally:
        # 例外時も直近のチェックポイント以降の分をできる限り保存する
        _atomic_save(doc, output_path)
        doc.close()

    elapsed = time.time() - t0
    print(
        f"完了: {processed}ページ処理 / "
        f"文字数{total_chars}（うちルビ{total_ruby}） / {elapsed:.0f}秒"
    )
    print(f"保存しました: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Google Cloud Vision APIでスキャンPDFを再OCRし、透明テキスト層を書き戻す"
    )
    parser.add_argument("input_pdf")
    parser.add_argument(
        "--start", type=int, default=1, help="開始ページ（1始まり、既定:1）"
    )
    parser.add_argument(
        "--end", type=int, default=None, help="終了ページ（1始まり、既定:最終ページ）"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="出力PDFパス（既定: <入力>_vision.pdf）"
    )
    args = parser.parse_args()

    if args.output:
        output_path = args.output
    else:
        stem = args.input_pdf.rsplit(".", 1)[0]
        output_path = f"{stem}_vision.pdf"

    end_page = args.end
    if end_page is None:
        with fitz.open(args.input_pdf) as d:
            end_page = len(d)

    # --start で中断分から再開する場合は、既存の出力ファイル（前回までの
    # チェックポイント）を土台にする。それ以外は常に元PDFから始める
    source = args.input_pdf
    if args.start > 1 and os.path.exists(output_path):
        print(f"既存の出力 {output_path} を土台に {args.start} ページ目から再開します")
        source = output_path

    try:
        reocr_pdf(source, output_path, args.start, end_page)
    except Exception:
        print(
            f"エラーで中断しました。{output_path} には直近のチェックポイントまでの"
            f"結果が保存されています。--start で続きから再開できます",
            file=sys.stderr,
        )
        raise


if __name__ == "__main__":
    main()
