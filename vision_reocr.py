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
  - Visionは改行で次の列頭に送られた文の続き1〜3文字（…土地はな｜く、）や
    段落頭のダッシュ──を丸ごと検出漏れすることがある（本によっては
    100箇所超）。旧OCR層をアンカーに欠落を検出し、該当ページだけ
    Document AIで再OCRして回収する（--gap-rescue参照）
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
import re
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
from jisui2epub import analyze_page, detect_body_size, KANA_RE, RUBY_SIZE_RATIO

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

# 見出し（大きめ活字）のサイズ信号を保存するための閾値と上限。
# 一律 target_body_fontsize で書き戻すと、章見出しが本文より大きい活字である
# という信号が消え、jisui2epub.py 本体の is_big 判定（本文比1.18以上）が
# 発火しなくなる（実例: 霧のむこうのふしぎな町。旧OCRは見出し16.8pt/本文
# 13.5pt=1.24倍で検出できていたが、全ページVision化で章見出しが全滅した）。
# → 縦書き列単位で「文字送りピッチ」（隣接文字のY開始位置差の中央値）が
# ページの本文ピッチ中央値の BIG_SIZE_RATIO 倍以上の列だけ、ピッチ比を
# 保って target_body_fontsize × (列ピッチ/本文ピッチ) で書き戻す。
# インク幅で判定・スケールしてはならない: Visionのインク幅は列ぐるみで
# ±20〜30%揺れる（実測・霧: 本文列が幅比1.23や1.34、逆に章見出しが幅比
# 0.96や1.18）。ピッチは安定しており、同じ列で見出し1.27〜1.39、
# 本文1.00〜1.05と明確に分離する（全224ページのシミュレーションで
# 誤検出は旧OCRでも見出し扱いだった巻末広告の飾りタイトルのみ）。
# 閾値1.21の根拠: 真の章見出しのピッチ比は霧の全8章＋あとがきで1.24〜1.45、
# ソフロニア嬢の章相当（危機その…）で1.24〜1.28と、1.24以上に分布する。
# 一方、誤検出候補の最大は黒牢城本文中のダッシュ強調行
# 「――いや。ただ一人だけ――」の1.20（字間が広い演出行）で、1.21が
# ちょうど両者を分離する（既存Vision PDF 5冊の文字位置による全ページ
# シミュレーションで確認。巻末広告・奥付の飾りタイトルは閾値を超えるが、
# それらは旧OCR直接変換でも見出し扱いになる既存挙動と同等）。
# is_big の1.18より上なので、拡大された列は本体側でそのまま見出し候補になる。
# 大きくする分にはルビ0.68倍しきい値の誤爆リスクはない。飾り文字対策として
# 上限 BIG_SIZE_MAX_SCALE 倍でキャップする。
# 横書きの見出し（岩波少年文庫型）は列グルーピングに乗らないため
# この機構では保存されない（未対応）
BIG_SIZE_RATIO = 1.21
BIG_SIZE_MAX_SCALE = 3.0
BIG_SIZE_MIN_CHARS = 4  # これ未満の短い列はピッチが不安定なので対象外

# Visionが半角で返すがこの作品群では正文が全角である記号の対応表
_FULLWIDTH_PUNCT = {"!": "！", "?": "？"}

# ルビ（振り仮名）に原理的に出現しない約物。Visionのシンボル単位インク幅は
# 「」・句読点・ダッシュ類では本文の文字でも細く出るため、幅だけでルビ判定
# するとこれらがルビサイズで書き戻され、jisui2epub.py側のルビ列グルーピング
# が隣の本当のルビに巻き込んで誤結合する（実測: 地下室の店《「みせ》×3・
# 大《。おお》・師匠《‐ししよう》）。このセットの文字は幅によらず常に本文
# として書き戻す。長音「ー」はカタカナ語のルビ（頁《ぺーじ》等）に正当に
# 現れるため含めない。中黒「・」は外国人名ルビの区切りに使う本も稀にあるが、
# 誤判定の実害の方がはるかに多いため含める
_NEVER_RUBY = set("、。，．・：；！？「」『』（）〔〕｛｝〈〉《》【】"
                  "…‥―—‐“”‘’〝〟゛゜´｀¨〜～※＊■□●○◎▲△▼▽")


def _is_ruby_symbol(text, rect, ruby_threshold):
    """このシンボルをルビとして書き戻すか（幅判定＋約物・半角英数の除外）"""
    return (not text.isascii() and text not in _NEVER_RUBY
            and rect.width < ruby_threshold)

# 一時的なAPIエラーのリトライ回数・待機秒（指数バックオフ）
MAX_RETRIES = 4
RETRY_BASE_WAIT = 2.0

# 何ページ処理するごとに出力PDFを保存するか（中断時の作業消失を防ぐ）
CHECKPOINT_EVERY = 20

# 二段OCR（極小ルビ回収）: Visionが検出しなかった旧OCRルビの領域だけを
# 切り出して拡大再OCRする。ページ全面の再OCR（ユニット2倍）でなく、
# 欠落領域の切り出しを1枚のタイル画像に詰めて送ることで、追加ユニットを
# 数%に抑える。実測: 高さ3ptの「て」「ひ」（全ページOCRでは検出不可）が
# 4x拡大で検出できる（地下室P.5）
RESCUE_UPSCALE = 4        # 切り出しの拡大倍率
RESCUE_MARGIN_X = 4.0     # 切り出し余白（pt、ルビ帯の左右）
RESCUE_MARGIN_Y = 12.0    # 切り出し余白（pt、上下の文脈。孤立文字はOCRされにくい）
RESCUE_TILE_MAX = 4000    # タイル画像の1辺上限（px）
RESCUE_TILE_GAP = 48      # タイル内の切り出し同士の白余白（px、隣接誤結合防止）
RESCUE_ACCEPT_TOL = 2.0   # 回収シンボルを受理する旧ルビ枠からの許容ずれ（pt）

# Vision APIの使用ユニット数（1リクエスト=1ユニット）。実行ごとにリセットし、
# チェックポイント・完了時にユーザーへ報告する
API_UNITS = {"page": 0, "rescue": 0}


def call_vision_with_retry(client, image, kind="page"):
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.document_text_detection(
                image=image, image_context={"language_hints": ["ja"]}
            )
            if response.error.message:
                raise RuntimeError(f"Vision APIエラー: {response.error.message}")
            API_UNITS[kind] += 1
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
    return _dedup_symbols(
        _snap_column_x([sym for block in blocks for sym in block])
    )


# 縦書きの同一列が複数のVisionブロックに分断された際、ブロック間のX座標に
# 乗るジッター（実測0.1〜0.5pt程度）を「同一列」とみなして無視する許容量。
# 本文の列間隔（実測8〜14pt程度）よりは十分小さく、観測されたジッターよりは
# 十分大きい値
COLUMN_SNAP_TOLERANCE = 3.0

# 二重検出とみなす座標差（pt）。DocAIで常用していたが、Visionも装飾的な
# 章題文字などをまれに二重検出する（実測: 霧P.29の章見出しが
# 「2ピピココッット屋敷という下宿」化）ため両エンジンで共有する
DEDUP_TOLERANCE = 2.0


def _dedup_symbols(symbols):
    """同一文字がほぼ同一座標で二重検出されたものを除く"""
    kept = []
    seen = {}  # text -> [(x0, y0)]
    for text, rect in symbols:
        dup = False
        for x0, y0 in seen.get(text, ()):
            if (
                abs(rect.x0 - x0) < DEDUP_TOLERANCE
                and abs(rect.y0 - y0) < DEDUP_TOLERANCE
            ):
                dup = True
                break
        if dup:
            continue
        seen.setdefault(text, []).append((rect.x0, rect.y0))
        kept.append((text, rect))
    return kept


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
    return [
        item
        for g in _collect_gap_groups(
            symbols, old_rubies, ruby_threshold, target_ruby_fontsize)
        for item in g["items"]
    ]


def _collect_gap_groups(symbols, old_rubies, ruby_threshold, target_ruby_fontsize):
    """Vision検出がない旧OCRルビを「旧ルビ1本＝1グループ」で収集する。
    各グループは items（旧OCR座標そのままの補完文字列＝フォールバック用）と
    clip（旧ルビのbbox＝二段OCRの切り出し・受理範囲）、cap（ルビ書き戻し
    サイズの上限）を持つ。_gapfill_missing_rubies（従来の即時補完）と
    二段OCR（_rescue_missing_rubies）の両方がこのグループを共有する"""
    vision_ruby_symbols = [
        (t, r) for t, r in symbols if _is_ruby_symbol(t, r, ruby_threshold)
    ]
    groups = []
    for old_ruby in old_rubies:
        text = old_ruby.text.strip()
        # かなを含まない旧「ルビ」はノンブル・挿絵ノイズの誤分類
        # （旧OCRはノンブルを小フォントで申告するためルビ判定に食い込む）。
        # 補完するとノンブルがルビとして復活する（実測: 霧P.24のルビ'24'）
        if not text or not KANA_RE.search(text):
            continue
        if _old_ruby_covered(old_ruby, vision_ruby_symbols):
            continue
        n = len(text)
        cell_h = (old_ruby.y1 - old_ruby.y0) / n
        fontsize = max(1.0, min(cell_h * 0.95, target_ruby_fontsize))
        items = []
        for i, ch in enumerate(text):
            y0 = old_ruby.y0 + i * cell_h
            y1 = old_ruby.y0 + (i + 1) * cell_h
            items.append((ch, fitz.Rect(old_ruby.x0, y0, old_ruby.x1, y1), fontsize))
        groups.append({
            "items": items,
            "clip": fitz.Rect(old_ruby.x0, old_ruby.y0, old_ruby.x1, old_ruby.y1),
            "cap": target_ruby_fontsize,
        })
    return groups


def _ruby_params(symbols, target_ruby_fontsize):
    """ページのルビ判定しきい値と書き戻しルビフォントサイズを求める
    （insert_invisible_text と二段OCRのグループ収集が同条件を共有する）"""
    local_body_width = statistics.median(rect.width for _, rect in symbols)
    ruby_threshold = local_body_width * RUBY_WIDTH_RATIO
    ruby_widths = [
        rect.width for text, rect in symbols
        if _is_ruby_symbol(text, rect, ruby_threshold)
    ]
    if ruby_widths:
        ruby_fontsize = min(statistics.median(ruby_widths), target_ruby_fontsize)
    else:
        ruby_fontsize = target_ruby_fontsize
    return ruby_threshold, ruby_fontsize


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

    ruby_threshold, ruby_fontsize = _ruby_params(symbols, target_ruby_fontsize)

    gapfilled = (
        _gapfill_missing_rubies(symbols, old_rubies, ruby_threshold, ruby_fontsize)
        if old_rubies
        else []
    )

    count = 0
    ruby_count = 0
    # 見出しサイズ信号の保存（詳細はBIG_SIZE_RATIOのコメント参照）:
    # 縦書き列（_snap_column_xでX座標が完全一致している）ごとにピッチを
    # 実測し、本文ピッチより有意に大きい非ルビ列だけ拡大スケールを記録する
    big_scale_by_x = {}
    col_stats = {}
    cols = {}
    for text, rect in symbols:
        if not text.isascii():
            cols.setdefault(rect.x0, []).append(rect)
    body_pitches = []
    for x0, rects in cols.items():
        if len(rects) < BIG_SIZE_MIN_CHARS:
            continue
        ys = sorted(r.y0 for r in rects)
        diffs = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.5]
        if not diffs:
            continue
        wmed = statistics.median(r.width for r in rects)
        if wmed < ruby_threshold:
            continue  # ルビ列はピッチ基準にも拡大対象にもしない
        pitch = statistics.median(diffs)
        col_stats[x0] = pitch
        body_pitches.append(pitch)
    if len(body_pitches) >= 3:
        page_pitch = statistics.median(body_pitches)
        for x0, pitch in col_stats.items():
            if pitch >= page_pitch * BIG_SIZE_RATIO:
                big_scale_by_x[x0] = min(pitch / page_pitch, BIG_SIZE_MAX_SCALE)

    for text, rect in symbols:
        is_ruby = _is_ruby_symbol(text, rect, ruby_threshold)
        text = _FULLWIDTH_PUNCT.get(text, text)
        fontsize = ruby_fontsize if is_ruby else target_body_fontsize
        if not is_ruby and rect.x0 in big_scale_by_x:
            fontsize = target_body_fontsize * big_scale_by_x[rect.x0]
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


def _flat_symbols(annotation):
    """full_text_annotation から (text, x0, y0, x1, y1) のフラット列を返す
    （タイル画像用。ブロック順の並べ替えは不要）"""
    out = []
    if annotation is None:
        return out
    for pageinfo in annotation.pages:
        for block in pageinfo.blocks:
            for para in block.paragraphs:
                for word in para.words:
                    for s in word.symbols:
                        v = s.bounding_box.vertices
                        xs = [p.x for p in v]
                        ys = [p.y for p in v]
                        out.append((s.text, min(xs), min(ys), max(xs), max(ys)))
    return out


def _pack_tiles(crops):
    """切り出し画像群をシェルフ法で白キャンバスに詰める。
    戻り値: [(canvasイメージ, [cropに"pos"=(x,y)を書き足したリスト]), ...]"""
    from PIL import Image
    tiles = []
    cur = []
    x = y = colw = 0
    for c in crops:
        w, h = c["img"].size
        if cur and (y + h > RESCUE_TILE_MAX):
            x += colw + RESCUE_TILE_GAP
            y = 0
            colw = 0
        if cur and (x + w > RESCUE_TILE_MAX):
            tiles.append(cur)
            cur = []
            x = y = colw = 0
        c["pos"] = (x, y)
        cur.append(c)
        y += h + RESCUE_TILE_GAP
        colw = max(colw, w)
    if cur:
        tiles.append(cur)
    out = []
    for placed in tiles:
        W = max(c["pos"][0] + c["img"].size[0] for c in placed)
        H = max(c["pos"][1] + c["img"].size[1] for c in placed)
        canvas = Image.new("L", (W, H), 255)
        for c in placed:
            canvas.paste(c["img"], c["pos"])
        out.append((canvas, placed))
    return out


def _rescue_missing_rubies(client, doc, pending, api=True):
    """二段OCR: Visionが検出しなかった旧OCRルビの領域（pending内の各グループ）
    を切り出し・拡大してタイル画像で再OCRし、検出できたかなを「Vision実測
    座標」で書き戻す。検出できなかったグループは従来どおり旧OCR座標の
    補完（items）にフォールバックする。

    背景: Visionの全ページOCRは高さ3pt級の極小ルビ（手《て》・引《ひ》）を
    検出できないが、同じ領域を4x拡大した画像なら検出できる（地下室P.5で
    実証）。従来のgapfillは旧OCRの申告座標をそのまま書き戻すため、旧OCRの
    座標ずれ（±1セル）ごと持ち込み、ルビの親ずれの残存原因になっていた。
    Vision実測座標で書き戻せばこの依存を絶てる。

    コスト: ページ全面の再OCR（ユニット2倍）ではなく、欠落領域だけを
    1枚のタイルに詰めて送るため、追加ユニットはチェックポイント区間
    （20ページ）あたり通常1〜2に収まる。

    api=False（例外時の後始末やPillow未導入時）は再OCRせず全グループを
    フォールバック補完だけして保存に備える。
    戻り値: (Vision回収グループ数, フォールバックグループ数, 使用タイル数)"""
    if not pending:
        return 0, 0, 0
    if api:
        try:
            import io
            from PIL import Image
        except ImportError:
            print("  [二段OCR: Pillow未インストールのため旧OCR座標の補完に"
                  "フォールバックします（.venv/bin/pip install Pillow で有効化）]")
            api = False
    if api:
        from google.cloud import vision
        img_cache = {}
        crops = []
        for g in pending:
            g["syms"] = None
            idx = g["page_idx"]
            if idx not in img_cache:
                found = largest_embedded_image(doc, doc[idx])
                if found is None:
                    img_cache[idx] = None
                else:
                    b, w, h = found
                    pr = doc[idx].rect
                    img_cache[idx] = (
                        Image.open(io.BytesIO(b)).convert("L"),
                        w / pr.width, h / pr.height,
                    )
            if img_cache[idx] is None:
                continue
            im, sx, sy = img_cache[idx]
            clip = g["clip"]
            x0 = max(0, int((clip.x0 - RESCUE_MARGIN_X) * sx))
            y0 = max(0, int((clip.y0 - RESCUE_MARGIN_Y) * sy))
            x1 = min(im.width, int((clip.x1 + RESCUE_MARGIN_X) * sx) + 1)
            y1 = min(im.height, int((clip.y1 + RESCUE_MARGIN_Y) * sy) + 1)
            if x1 <= x0 or y1 <= y0:
                continue
            crop = im.crop((x0, y0, x1, y1))
            crop = crop.resize(
                (crop.width * RESCUE_UPSCALE, crop.height * RESCUE_UPSCALE),
                Image.LANCZOS,
            )
            crops.append({"g": g, "img": crop, "src_x0": x0, "src_y0": y0,
                          "sx": sx, "sy": sy})
        n_tiles = 0
        for canvas, placed in _pack_tiles(crops):
            buf = io.BytesIO()
            canvas.save(buf, format="PNG")
            resp = call_vision_with_retry(
                client, vision.Image(content=buf.getvalue()), kind="rescue")
            n_tiles += 1
            syms = _flat_symbols(resp.full_text_annotation)
            for c in placed:
                cx, cy = c["pos"]
                w, h = c["img"].size
                found = []
                for t, sx0, sy0, sx1, sy1 in syms:
                    mx = (sx0 + sx1) / 2
                    my = (sy0 + sy1) / 2
                    if not (cx <= mx < cx + w and cy <= my < cy + h):
                        continue
                    # タイルpx → 切り出し前の元画像px → PDF pt
                    px0 = c["src_x0"] + (sx0 - cx) / RESCUE_UPSCALE
                    px1 = c["src_x0"] + (sx1 - cx) / RESCUE_UPSCALE
                    py0 = c["src_y0"] + (sy0 - cy) / RESCUE_UPSCALE
                    py1 = c["src_y0"] + (sy1 - cy) / RESCUE_UPSCALE
                    rect = fitz.Rect(px0 / c["sx"], py0 / c["sy"],
                                     px1 / c["sx"], py1 / c["sy"])
                    found.append((t, rect))
                c["g"]["syms"] = found
    else:
        n_tiles = 0
        for g in pending:
            g.setdefault("syms", None)

    rescued = fallback = 0
    for g in pending:
        page = doc[g["page_idx"]]
        clip = g["clip"]
        accepted = []
        for t, rect in (g["syms"] or []):
            # 受理条件: 旧ルビ枠の近傍（余白の文脈文字を拾わない）・かな1文字・
            # 約物や数字はルビとして書き戻さない（_NEVER_RUBY と同じ思想）
            if len(t) != 1 or t.isascii() or t in _NEVER_RUBY:
                continue
            if not KANA_RE.search(t):
                continue
            mx = (rect.x0 + rect.x1) / 2
            my = (rect.y0 + rect.y1) / 2
            if not (clip.x0 - RESCUE_ACCEPT_TOL <= mx <= clip.x1 + RESCUE_ACCEPT_TOL
                    and clip.y0 - RESCUE_ACCEPT_TOL <= my
                    <= clip.y1 + RESCUE_ACCEPT_TOL):
                continue
            accepted.append((t, rect))
        if accepted:
            # 文字は旧OCR・位置はVision実測のハイブリッドで書き戻す。
            # 拡大再OCRでも極小字の「読み」自体は誤りやすい（実測: ちか→
            # しょちか・くぴ→し・つ→わ）が、検出位置は正確（旧OCR申告が
            # 3ptずれていた「ひ」を正しい位置に補正できた）。旧OCRの読みは
            # 概ね正しく、残る誤りは後段の fix_ruby_kanji / fix_ruby_variants
            # が本内多数決で訂正できるため、テキストは旧OCRを採用し、
            # Visionからは検出範囲（Y範囲・X位置・文字幅）だけを取り込む
            ext_y0 = min(r.y0 for _, r in accepted)
            ext_y1 = max(r.y1 for _, r in accepted)
            x0 = statistics.median(r.x0 for _, r in accepted)
            widths = [r.width for _, r in accepted]
            fs = max(1.0, min(statistics.median(widths), g["cap"]))
            old_text = "".join(t for t, _, _ in g["items"])
            n = max(1, len(old_text))
            cell = max((ext_y1 - ext_y0) / n, 0.5)
            for k, t in enumerate(old_text):
                page.insert_text(
                    fitz.Point(x0, ext_y0 + k * cell + fs),
                    t, fontsize=fs, fontname="japan", render_mode=3)
            rescued += 1
        else:
            for t, rect, fs in g["items"]:
                page.insert_text(
                    fitz.Point(rect.x0, rect.y0 + fs),
                    t, fontsize=fs, fontname="japan", render_mode=3)
            fallback += 1
    pending.clear()
    return rescued, fallback, n_tiles


# ── 本文ギャップ救済 ─────────────────────────────────────────────
# Visionは「改行で次の列頭に送られた文の続き1〜3文字」（…土地はな｜く、）や
# 独白の段落頭ダッシュ「──」を、画像に鮮明に印字されていてもブロック統合の
# 過程で丸ごと返さないことがある（実測: 黒牢城でかな・漢字を含む欠落105
# グループ＋ダッシュのみ72グループ/525ページ。GOAL照合で列頭48件は全件本物・
# 誤検出0。霧0・地下室2・ソフロニア5件と本によって発生率が桁で違う）。
# 切り出し拡大・列ストリップ・text_detection切替のいずれを試してもVisionは
# 同じ文字を返さない（「く、」で実測）ため、Vision内での回収は不可能。
# redaction前の旧OCR層をアンカーに欠落を検出し、Document AI（同じ画像で
# 「く、」「ねば」を正しく読めることを実測済み）で該当ページだけ再OCRして
# 欠落枠内のシンボルを実測座標で取り込む。DocAIが使えない場合は旧OCRの
# 文字をそのまま書き戻す（「ぐ、」のように化けていることもあるが、文が
# 無言で欠けるよりは校正で発見しやすい）
GAP_X_TOLERANCE = 5.0     # 同一列とみなすX中心差(pt)。列間隔(約13pt)より十分小さく
GAP_COVER_OVERLAP = 0.3   # 旧OCR文字を「Vision検出済み」とみなすY重なり率
GAP_SAME_CHAR_TOL = 1.5   # 同一文字ならこの文字高さ倍率以内のY中心差でカバー済み
                          # とみなす。旧OCRの化けたスパンはセル位置が半セル近く
                          # ドリフトし、Vision検出済みの文字が重なり率30%を割って
                          # 幻の欠落になることがある（実測: 黒牢城p5で「退」が
                          # 二重挿入され「退退かば地獄」化）
GAP_GROUP_JOIN = 8.0      # 同一欠落グループとみなす隣接文字のY間隔(pt)
GAP_MAX_CHARS = 4         # これより長い欠落は挿絵・図版由来ノイズとして無視
GAP_BAND_TOP_TOL = 2.0    # 本文帯上端の許容余白(pt)。列頭欠落は他列の上端と揃う
GAP_BAND_BOT_TOL = 6.0    # 本文帯下端の許容余白(pt)
GAP_BAND_MIN_COL = 5      # 本文帯の推定に使う列の最小文字数
GAP_MIN_VISION_CHARS = 10  # Vision本文がこれ未満のページは検出しない（扉・挿絵）
GAP_ACCEPT_TOL = 2.0      # DocAI回収シンボルを受理する欠落枠からの許容ずれ(pt)
GAP_SNAP_TOL = 4.0        # 回収文字を最寄りVision本文列のx0に吸着させる許容(pt)。
                          # 旧OCR申告列とVision列は最大3pt程度ずれる（実測:
                          # 黒牢城p5の「ね無」旧136.8 vs Vision列139.8）ため
                          # COLUMN_SNAP_TOLERANCEより広く、列間隔(約13pt)の
                          # 半分よりは十分小さい値にする
# 旧OCRが縦書きダッシュ──を誤読する定番の文字（フォールバック補完時に─へ直す。
# 「一」（漢数字）や長音「ー」は正当な本文に頻出するため絶対に含めない）
GAP_DASH_CHARS = set("ｌＩ｜丨")
_GAP_TEXT_RE = re.compile(r"[ぁ-んァ-ヶー一-龥々]")

# 本文ギャップ救済の実績（実行ごとにリセットし、完了時に報告する）
GAP_STATS = {"pages": 0, "groups": 0, "docai": 0, "old": 0, "docai_pages": 0}


def _collect_old_body_chars(page, min_size):
    """redactionで消す前の旧OCR層から、本文サイズの文字 (text, rect) を集める
    （本文ギャップ検出のアンカー。ルビ・柱等の小活字はmin_sizeで、ノンブル等の
    半角英数はisasciiで除外する）"""
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", ()):
            for span in line["spans"]:
                if span["size"] < min_size:
                    continue
                for c in span["chars"]:
                    ch = c["c"]
                    if ch.strip() and not ch.isascii():
                        out.append((ch, fitz.Rect(c["bbox"])))
    return out


def _detect_body_gaps(symbols, old_chars, ruby_threshold):
    """旧OCRにあってVisionに無い本文文字を欠落グループとして返す。
    各グループは chars=[(text, rect), ...]（旧OCR文字と座標）と clip
    （欠落枠＝DocAI回収シンボルの受理範囲）を持つ。

    誤検出への防御:
    - 本文帯の外は見ない（柱・ノンブル・挿絵キャプションの旧OCRノイズを
      拾わない）。帯はVision本文列（GAP_BAND_MIN_COL文字以上の列）の
      上端25%分位・下端75%分位から取る。文字単位の分位で取ってはならない:
      ページ最上段の文字（＝各列の1文字目）は全文字の2%程度しかなく、
      まさに回収したい列頭欠落の位置が帯の外に出てしまう（黒牢城p5の
      「ぐ、」が0.1pt差で弾かれた実測あり）
    - カバレッジ判定はルビ含む全Visionシンボルに対して行う。Visionは
      短い断片列をルビ幅で書き戻すことがあり（黒牢城p5の「と。」）、
      本文シンボル限定だと検出済みの文字を欠落と誤認して二重挿入する
    - GAP_MAX_CHARSより長い欠落は無視（図版ページの旧OCRジャンク）
    - かな・漢字を含まないグループは無視（旧OCRの記号ノイズ）。ただし
      ダッシュ誤読の定番（GAP_DASH_CHARSのみ・2文字以下）は許可
    - 1文字だけのグループはVision本文列と整列している場合（＝既存列の
      頭や途中の欠け）のみ採用。列に整列しない孤立単字は、余白の罫線や
      汚れを旧OCRが漢字と誤認したジャンク（実測: 黒牢城p6右余白の「一」、
      p3の「鰹」）がほとんどのため捨てる
    - 旧OCRが「次の列の頭数文字」を前の列の末尾に食い込ませて申告し、
      Vision側は正しく次列頭に検出しているケースでは、欠落テキスト
      （先頭1文字が化けている場合も考慮）が左隣のVision列の先頭と
      一致したらスキップ（同じ文字の二重書き戻しを防ぐ）"""
    if not old_chars:
        return []
    allsyms = [(t, r) for t, r in symbols if not t.isascii()]
    # 本文帯・列推定は「幅がルビ閾値以上の文字」だけで行う。_is_ruby_symbolを
    # 使うと約物（「や。はルビ幅でも常に本文扱い）がルビ列に本文列を捏造し、
    # x0吸着先がずれる（実測: 黒牢城p5のルビ列内の「にね無が吸着した）
    body = [(t, r) for t, r in allsyms if r.width >= ruby_threshold]
    if len(body) < GAP_MIN_VISION_CHARS:
        return []

    # 本文帯: 本文列の上端・下端の分位から推定
    bcols = {}
    for t, r in body:
        bcols.setdefault(round(r.x0, 1), []).append(r)
    col_tops = []
    col_bots = []
    for rs in bcols.values():
        if len(rs) >= GAP_BAND_MIN_COL:
            col_tops.append(min(r.y0 for r in rs))
            col_bots.append(max(r.y1 for r in rs))
    if col_tops:
        col_tops.sort()
        col_bots.sort()
        band_top = col_tops[len(col_tops) // 4] - GAP_BAND_TOP_TOL
        band_bot = col_bots[(3 * len(col_bots)) // 4] + GAP_BAND_BOT_TOL
    else:
        band_top = min(r.y0 for _, r in body) - GAP_BAND_TOP_TOL
        band_bot = max(r.y1 for _, r in body) + GAP_BAND_BOT_TOL

    # X中心のバケツ分けで近傍検索を高速化（カバレッジはルビ含む全シンボル）
    buckets = {}
    for t, r in allsyms:
        k = int(((r.x0 + r.x1) / 2) // GAP_X_TOLERANCE)
        buckets.setdefault(k, []).append((t, r))
    missing = []
    for ch, r in old_chars:
        if r.y0 < band_top or r.y1 > band_bot:
            continue
        xc = (r.x0 + r.x1) / 2
        yc = (r.y0 + r.y1) / 2
        h = r.y1 - r.y0
        k = int(xc // GAP_X_TOLERANCE)
        covered = False
        for kk in (k - 1, k, k + 1):
            for vt, vr in buckets.get(kk, ()):
                if abs((vr.x0 + vr.x1) / 2 - xc) >= GAP_X_TOLERANCE:
                    continue
                if (min(r.y1, vr.y1) - max(r.y0, vr.y0)
                        > h * GAP_COVER_OVERLAP):
                    covered = True
                    break
                # 同一文字が同列のすぐ近くにあれば、旧OCRのセル位置ドリフト
                # による幻の欠落とみなす（GAP_SAME_CHAR_TOL参照）
                if (vt == ch and abs((vr.y0 + vr.y1) / 2 - yc)
                        < h * GAP_SAME_CHAR_TOL):
                    covered = True
                    break
            if covered:
                break
        if not covered:
            missing.append((ch, r))
    if not missing:
        return []

    # Vision全列の先頭テキスト（読み順＝X降順）。「左隣の列頭との重複」
    # チェックに使う（断片列がルビ幅で書き戻されている場合も対象に
    # なるよう、本文列だけでなく全シンボルの列で作る）
    vcols = {}
    for t, r in allsyms:
        vcols.setdefault(round(r.x0, 1), []).append((t, r))
    col_heads = [
        (x0, "".join(t for t, _ in sorted(cs, key=lambda c: c[1].y0)[:GAP_MAX_CHARS + 1]))
        for x0, cs in sorted(vcols.items(), key=lambda kv: -kv[0])
    ]

    missing.sort(key=lambda t: (-t[1].x0, t[1].y0))
    raw_groups = []
    for ch, r in missing:
        if (raw_groups
                and abs(raw_groups[-1][-1][1].x0 - r.x0) < 3.0
                and r.y0 - raw_groups[-1][-1][1].y1 < GAP_GROUP_JOIN):
            raw_groups[-1].append((ch, r))
        else:
            raw_groups.append([(ch, r)])

    groups = []
    for g in raw_groups:
        if len(g) > GAP_MAX_CHARS:
            continue
        text = "".join(ch for ch, _ in g)
        has_text = bool(_GAP_TEXT_RE.search(text))
        dash_only = len(g) <= 2 and all(ch in GAP_DASH_CHARS for ch in text)
        if not (has_text or dash_only):
            continue
        # 左隣のVision列頭に同じテキストが既にあれば重複なのでスキップ。
        # 旧OCRは断片の先頭（多くはダッシュ）を別の記号に化かすことが
        # あるため、先頭1文字を除いた形でも照合する
        nodash = "".join(ch for ch in text if ch not in GAP_DASH_CHARS)
        gx = g[0][1].x0
        if nodash:
            dup = False
            for x0, head in col_heads:
                if x0 < gx - GAP_X_TOLERANCE:
                    dup = head.startswith(nodash) or (
                        len(nodash) >= 2 and head.startswith(nodash[1:]))
                    break
            if dup:
                continue
        # 最寄りのVision本文列（挿入時のx0吸着先。整列判定にも使う）。
        # 1文字だけの「列」は吸着先にしない（ルビ列内の約物「等が本文サイズで
        # 書き戻されて偽の列を作ることがある。実測: 黒牢城p5）
        snap_x = None
        best = GAP_SNAP_TOL
        for x0, rs in bcols.items():
            if len(rs) >= 2 and abs(x0 - gx) < best:
                best = abs(x0 - gx)
                snap_x = x0
        if len(g) == 1 and snap_x is None and not dash_only:
            continue
        clip = fitz.Rect(g[0][1])
        for _, r in g[1:]:
            clip |= r
        groups.append({"chars": g, "clip": clip, "snap_x": snap_x})
    return groups


# 本文ギャップ救済用のDocument AIクライアント（遅延初期化・実行内キャッシュ。
# Falseは「利用不可と判定済み」＝以降は旧OCR文字の補完にフォールバック）
_DOCAI_CTX = None


def _get_docai_context():
    global _DOCAI_CTX
    if _DOCAI_CTX is not None:
        return _DOCAI_CTX or None
    try:
        import docai_reocr
        location = os.environ.get("DOCAI_LOCATION", "us")
        client = docai_reocr.make_client(location)
        processor = os.environ.get("DOCAI_PROCESSOR")
        if not processor:
            project_id = docai_reocr._default_project_id()
            if project_id:
                processor = docai_reocr.find_processor(client, project_id, location)
        if not processor:
            raise RuntimeError(
                "OCRプロセッサ未作成（docai_reocr.py --create-processor で作成）")
        _DOCAI_CTX = (client, processor)
        print("  [本文ギャップ救済: 欠落のあるページはDocument AIで再OCRして"
              "回収します（$1.50/1000ページの従量課金）]")
        return _DOCAI_CTX
    except Exception as e:
        print(f"  [本文ギャップ救済: Document AIが使えないため旧OCR文字の補完に"
              f"フォールバックします（{e}）]")
        _DOCAI_CTX = False
        return None


def _fill_gaps_from_old(page, groups, fontsize):
    """欠落グループを旧OCRの文字のまま書き戻す（フォールバック）。
    ダッシュ──の定番誤読（ｌ等）だけは─に直す。文字が化けている可能性は
    あるが、文が無言で欠けるより校正で発見しやすい。x0は最寄りのVision
    本文列に吸着させ、jisui2epub.py本体の縦行クラスタリングで既存列と
    確実に同じ縦行へまとまるようにする"""
    n = 0
    for g in groups:
        for ch, rect in g["chars"]:
            ch = "─" if ch in GAP_DASH_CHARS else ch
            x0 = g["snap_x"] if g["snap_x"] is not None else rect.x0
            page.insert_text(
                fitz.Point(x0, rect.y0 + fontsize),
                ch, fontsize=fontsize, fontname="japan", render_mode=3)
            n += 1
    GAP_STATS["old"] += len(groups)
    return n


def _rescue_body_gaps(doc, page_idx, groups, symbols, fontsize, mode):
    """検出した本文欠落グループを書き戻す。mode="auto"はDocument AIでページを
    再OCRし、欠落枠内のシンボルだけを「文字も位置もDocAI」で取り込む
    （ルビの二段OCRと違い欠落文字は本文サイズなのでDocAIの読みを信頼できる。
    Visionはどう切り出しても同じ文字を返さないため、再OCRエンジンには
    DocAIが必須）。DocAI不可・検出なしのグループは旧OCR文字の補完に落とす。
    受理したDocAIシンボルのうち、既存のVisionシンボルとY重なりがあるものは
    挿入しない（旧OCRとVisionの座標ずれで欠落枠が検出済み文字の位置に
    かかった場合の二重挿入を防ぐ）"""
    page = doc[page_idx]
    GAP_STATS["pages"] += 1
    GAP_STATS["groups"] += len(groups)
    texts = " / ".join(
        f"「{''.join(ch for ch, _ in g['chars'])}」" for g in groups[:8]
    ) + ("…" if len(groups) > 8 else "")
    ctx = _get_docai_context() if mode == "auto" else None
    if ctx is None:
        n = _fill_gaps_from_old(page, groups, fontsize)
        print(f"  [本文ギャップ: 欠落{len(groups)}箇所を旧OCR文字で補完: {texts}]")
        return n
    client, processor = ctx
    try:
        import docai_reocr
        document, img_size = docai_reocr.ocr_page_with_docai(
            client, processor, doc, page_idx)
        GAP_STATS["docai_pages"] += 1
        docai_syms = (
            docai_reocr.collect_page_symbols_docai(document, page, img_size)
            if document is not None else [])
    except Exception as e:
        print(f"  [本文ギャップ: DocAI再OCRに失敗、欠落{len(groups)}箇所を"
              f"旧OCR文字で補完します（{e}）: {texts}]")
        return _fill_gaps_from_old(page, groups, fontsize)

    def _covered_by_vision(r):
        xc = (r.x0 + r.x1) / 2
        for _, vr in symbols:
            if abs((vr.x0 + vr.x1) / 2 - xc) >= GAP_X_TOLERANCE:
                continue
            if (min(r.y1, vr.y1) - max(r.y0, vr.y0)
                    > (r.y1 - r.y0) * GAP_COVER_OVERLAP):
                return True
        return False

    n = 0
    fallback_groups = []
    rescued = 0
    outcomes = []
    for g in groups:
        clip = g["clip"]
        old_text = "".join(ch for ch, _ in g["chars"])
        accepted = []
        for t, r in docai_syms:
            mx = (r.x0 + r.x1) / 2
            my = (r.y0 + r.y1) / 2
            if (clip.x0 - GAP_ACCEPT_TOL <= mx <= clip.x1 + GAP_ACCEPT_TOL
                    and clip.y0 - GAP_ACCEPT_TOL <= my <= clip.y1 + GAP_ACCEPT_TOL
                    and not _covered_by_vision(r)):
                accepted.append((t, r))
        if accepted:
            accepted.sort(key=lambda tr: tr[1].y0)
            for t, r in accepted:
                t = _FULLWIDTH_PUNCT.get(t, t)
                x0 = g["snap_x"] if g["snap_x"] is not None else r.x0
                page.insert_text(
                    fitz.Point(x0, r.y0 + fontsize),
                    t, fontsize=fontsize, fontname="japan", render_mode=3)
                n += 1
            rescued += 1
            outcomes.append(
                f"「{old_text}」→DocAI「{''.join(t for t, _ in accepted)}」")
        else:
            fallback_groups.append(g)
            outcomes.append(f"「{old_text}」→旧OCR補完")
    if fallback_groups:
        n += _fill_gaps_from_old(page, fallback_groups, fontsize)
    GAP_STATS["docai"] += rescued
    print(f"  [本文ギャップ: 欠落{len(groups)}箇所中{rescued}箇所をDocAI再OCRで"
          f"回収（旧OCR補完{len(fallback_groups)}箇所）: "
          + " / ".join(outcomes[:8])
          + ("…" if len(outcomes) > 8 else "") + "]")
    return n


def _atomic_save(doc, output_path, final=False):
    """一時ファイルに保存してから置き換える。チェックポイント保存中の中断で
    出力ファイルが壊れるのを防ぐ。--start で再開する場合、出力パスから
    開いたdocをそのまま同じ output_path に doc.save() することはできない
    （PyMuPDFの制約でincremental保存以外は不可）ため、この方式が必須。

    garbage回収は最終保存（final=True、以降docに書き込まない）に限る。
    garbage付き保存はin-memoryのxrefを再番号付けするが、PyMuPDFが
    insert_text用にキャッシュしているフォントxrefは古い番号のまま残るため、
    保存後に処理したページの /Resources/Font/japan が無関係なオブジェクトを
    指し、テキスト抽出がUTF-16BEコードポイント素通しの文字化けになる
    （実測: 霧_scansnap 224ページ中チェックポイント直後のp21以降が全滅）。

    最終保存もgarbage=2（未参照除去＋xref圧縮）までにとどめる。
    garbage=4（ストリーム重複排除）は、--start再開などで「本ツールが生成
    したPDFを開き直したdoc」に対して病的に遅くなる（実測: 霧30MBで
    10分以上完了せずCPU100%。元のスキャンPDFから開いたdocなら数秒）。
    redactionの残骸（旧テキスト層）はgarbage=2で除去できる"""
    tmp_path = output_path + ".tmp"
    doc.save(tmp_path, garbage=2 if final else 0, deflate=True)
    os.replace(tmp_path, output_path)


def _open_source_pdf(input_path, output_path):
    """処理の土台にするPDFを開く。--start再開時は入力＝出力ファイル自身の
    ため、通常の fitz.open だとOSのファイルハンドルを掴んだまま
    _atomic_save の os.replace で自分自身を置き換えることになる。
    POSIXでは開いているファイルのrename置換は正常動作だが、Windowsでは
    ERROR_ACCESS_DENIED（WinError 5）で失敗する（MuPDFはfopenで開くため
    FILE_SHARE_DELETEが付かない。実測: 地下室の--start再開の初回
    チェックポイントで必ず落ちる）。バイト列で読み込んでメモリから
    開けばハンドルを掴まないため、Windowsでも置換できる。
    新規実行（入力≠出力）は従来どおりファイルから開く（メモリ節約）。"""
    if os.path.abspath(input_path) == os.path.abspath(output_path):
        with open(input_path, "rb") as f:
            return fitz.open(stream=f.read(), filetype="pdf")
    return fitz.open(input_path)


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


def reocr_pdf(input_path, output_path, start_page, end_page, ruby_rescue=True,
              gap_rescue="auto"):
    from google.cloud import vision

    client = vision.ImageAnnotatorClient()
    doc = _open_source_pdf(input_path, output_path)
    end_page = min(end_page, len(doc))
    API_UNITS["page"] = API_UNITS["rescue"] = 0
    for k in GAP_STATS:
        GAP_STATS[k] = 0

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
    rescue_stats = [0, 0, 0]  # Vision回収 / フォールバック / タイル数
    pending = []  # 二段OCR待ちの欠落ルビグループ
    t0 = time.time()

    def _flush_rescue():
        if not pending:
            return
        r, f, t = _rescue_missing_rubies(client, doc, pending)
        rescue_stats[0] += r
        rescue_stats[1] += f
        rescue_stats[2] += t
        print(f"  [二段OCR: 欠落ルビ{r + f}箇所中{r}箇所をVision再検出で回収"
              f"（タイル{t}枚・旧OCR補完へのフォールバック{f}箇所）]")

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

            # 同じく redaction 前に、本文ギャップ救済（Visionの列頭断片・
            # ダッシュ検出漏れの回収）用に旧OCRの本文文字も控えて欠落を検出する
            gap_groups = []
            if gap_rescue != "off" and symbols and old_ocr_body_size:
                thr_gap, _ = _ruby_params(symbols, target_ruby_fontsize)
                old_body_chars = _collect_old_body_chars(
                    page, old_ocr_body_size * RUBY_WIDTH_RATIO)
                gap_groups = _detect_body_gaps(symbols, old_body_chars, thr_gap)

            # 既存テキスト層を除去（画像・線画は保護）
            page.add_redact_annot(page.rect)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            )

            # 二段OCRが有効なら、旧OCR補完（gapfill）を即時挿入せず
            # 欠落グループとして保留し、チェックポイントごとにまとめて
            # 拡大再OCRで回収する（検出不可分のみ従来の補完に落ちる）
            if ruby_rescue and old_rubies and symbols:
                thr, rfs = _ruby_params(symbols, target_ruby_fontsize)
                groups = _collect_gap_groups(symbols, old_rubies, thr, rfs)
                for g in groups:
                    g["page_idx"] = idx
                pending.extend(groups)
                old_for_insert = ()
            else:
                old_for_insert = old_rubies

            n, n_ruby = insert_invisible_text(
                page, symbols, target_body_fontsize, target_ruby_fontsize,
                old_for_insert
            )
            if gap_groups:
                n += _rescue_body_gaps(
                    doc, idx, gap_groups, symbols, target_body_fontsize,
                    gap_rescue)
            total_chars += n
            total_ruby += n_ruby
            processed += 1
            elapsed = time.time() - t0
            print(f"ページ{p1}: {n}文字書き戻し（うちルビ{n_ruby}） [{elapsed:.0f}秒経過]")

            if i % CHECKPOINT_EVERY == 0:
                _flush_rescue()
                _atomic_save(doc, output_path)
                print(f"  [チェックポイント保存: {output_path}"
                      f"（APIユニット累計 {API_UNITS['page'] + API_UNITS['rescue']}）]")
        _flush_rescue()
    finally:
        # 例外時も直近のチェックポイント以降の分をできる限り保存する。
        # 未処理の欠落グループが残っていたら（＝二段OCRの前に例外）、
        # APIを呼ばず従来の旧OCR補完だけ済ませてから保存する
        if pending:
            _rescue_missing_rubies(client, doc, pending, api=False)
        _atomic_save(doc, output_path, final=True)
        doc.close()
        units = API_UNITS["page"] + API_UNITS["rescue"]
        print(f"Vision API使用ユニット: 合計{units}"
              f"（ページOCR {API_UNITS['page']} ＋ 二段OCR {API_UNITS['rescue']}。"
              f"無料枠は月1000ユニット）")

    elapsed = time.time() - t0
    print(
        f"完了: {processed}ページ処理 / "
        f"文字数{total_chars}（うちルビ{total_ruby}） / {elapsed:.0f}秒"
    )
    if rescue_stats[0] + rescue_stats[1]:
        print(f"二段OCR: 欠落ルビ{rescue_stats[0] + rescue_stats[1]}箇所中"
              f"{rescue_stats[0]}箇所をVision再検出で回収"
              f"（タイル{rescue_stats[2]}枚・フォールバック{rescue_stats[1]}箇所）")
    if GAP_STATS["groups"]:
        print(f"本文ギャップ救済: 旧OCRにあってVisionに無い本文欠落"
              f"{GAP_STATS['groups']}箇所（{GAP_STATS['pages']}ページ）を検出、"
              f"DocAI再OCRで{GAP_STATS['docai']}箇所回収 / "
              f"旧OCR文字補完{GAP_STATS['old']}箇所")
    if GAP_STATS["docai_pages"]:
        print(f"Document AI課金ページ数: {GAP_STATS['docai_pages']}"
              f"（$1.50/1000ページ・無料枠なし）")
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
    parser.add_argument(
        "--no-ruby-rescue", action="store_true",
        help="二段OCR（極小ルビの拡大再OCR回収）を無効化し、"
             "従来どおり旧OCR座標の補完だけを行う",
    )
    parser.add_argument(
        "--gap-rescue", choices=["auto", "old", "off"], default="auto",
        help="Visionが検出漏れした本文断片（改行後の文末数文字・段落頭の──等）"
             "の回収方法。auto=Document AIが使えれば欠落のあるページだけDocAIで"
             "再OCRして回収（$1.50/1000ページ）、使えなければ旧OCR文字で補完 / "
             "old=常に旧OCR文字で補完（無料） / off=回収しない（既定: auto）",
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
        reocr_pdf(source, output_path, args.start, end_page,
                  ruby_rescue=not args.no_ruby_rescue,
                  gap_rescue=args.gap_rescue)
    except Exception:
        print(
            f"エラーで中断しました。{output_path} には直近のチェックポイントまでの"
            f"結果が保存されています。--start で続きから再開できます",
            file=sys.stderr,
        )
        raise


if __name__ == "__main__":
    main()
