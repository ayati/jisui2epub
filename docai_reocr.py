#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Cloud Document AI (Enterprise Document OCR) でスキャンPDFを再OCRし、
透明テキスト層として書き戻す前処理ツール。vision_reocr.py のDocument AI版。

vision_reocr.py（Cloud Vision API版）との使い分け:
  - Vision版: 月1000ページまで無料。まずこちらを推奨
  - DocAI版（本ツール）: 無料枠なし・$1.50/1000ページ（300ページの本≈70円）
    だが、本文の誤読が少なく（実測: 地下室からのふしぎな旅P.4-13の本文で
    Vision誤り14箇所42字に対しDocAI誤り7箇所30字・実質誤読1字）、
    Visionが検出漏れする1文字だけの極小ルビ（手《て》等）も自前で検出できる。
    少々のコストを払っても精度を求める場合の選択肢

書き戻しのヒューリスティック（列のX座標スナップ・ルビ実測サイズ・
字面上端合わせ・旧OCRからのルビ補完）は vision_reocr.py と共通
（同モジュールからimport。詳細な設計判断はCLAUDE.md参照）。

事前準備:
  .venv/bin/pip install google-cloud-documentai
  1. Google Cloud ConsoleでDocument AI APIを有効化し、課金を有効化する
  2. サービスアカウントキーを用意して環境変数に設定
     export GOOGLE_APPLICATION_CREDENTIALS="/path/to/key.json"
  3. OCRプロセッサを作成する（初回のみ。本ツールで作成できる）
     .venv/bin/python docai_reocr.py --create-processor

使い方:
  .venv/bin/python docai_reocr.py input.pdf                    # 全ページ、既定出力は <入力>_docai.pdf
  .venv/bin/python docai_reocr.py input.pdf --start 100        # 中断からの再開
  .venv/bin/python docai_reocr.py input.pdf --start 10 --end 20 -o test.pdf
  # プロセッサは自動検出される。複数ある場合などは明示指定も可能:
  .venv/bin/python docai_reocr.py input.pdf --processor projects/.../locations/us/processors/...

料金: Enterprise Document OCR は $1.50/1000ページの従量課金（無料枠なし）
  https://cloud.google.com/document-ai/pricing
"""
import argparse
import json
import os
import statistics
import sys
import time

import fitz  # PyMuPDF
from google.api_core import exceptions as gexc
from google.api_core.client_options import ClientOptions
from google.cloud import documentai_v1 as documentai

from jisui2epub import analyze_page, detect_body_size
from vision_reocr import (
    CHECKPOINT_EVERY,
    MAX_RETRIES,
    RETRY_BASE_WAIT,
    RUBY_FONTSIZE_RATIO,
    _atomic_save,
    _dedup_symbols,
    _open_source_pdf,
    _snap_column_x,
    insert_invisible_text,
    largest_embedded_image,
)

# プロセッサ自動作成時の表示名
PROCESSOR_DISPLAY_NAME = "jisui-ocr"

_MIME = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg",
         "tiff": "image/tiff", "bmp": "image/bmp"}

# キャリブレーション（書き戻しフォントサイズ基準値の実測）に使うページ数・文字数
CALIBRATION_MAX_PAGES = 15
CALIBRATION_TARGET_CHARS = 800

# 同一文字の二重検出（DocAIがまれに同じルビ字形を2回返す。実測:
# 地下室からのふしぎな旅の住所《じゅうしょ》が「じじゅゅううししょ」化）の
# 除去は vision_reocr._dedup_symbols を共有する（Vision側にも同症状があった
# ため本体を移管した）


def _default_project_id():
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path or not os.path.exists(cred_path):
        return None
    with open(cred_path) as f:
        return json.load(f).get("project_id")


def make_client(location):
    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    return documentai.DocumentProcessorServiceClient(client_options=opts)


def find_processor(client, project_id, location):
    """プロジェクト内の有効なOCRプロセッサを探して名前を返す（なければNone）"""
    parent = f"projects/{project_id}/locations/{location}"
    for proc in client.list_processors(parent=parent):
        if proc.type_ == "OCR_PROCESSOR" and proc.state.name == "ENABLED":
            return proc.name
    return None


def create_processor(client, project_id, location):
    parent = f"projects/{project_id}/locations/{location}"
    proc = client.create_processor(
        parent=parent,
        processor=documentai.Processor(
            display_name=PROCESSOR_DISPLAY_NAME, type_="OCR_PROCESSOR"
        ),
    )
    return proc.name


def call_docai_with_retry(client, request):
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            return client.process_document(request=request)
        except (
            gexc.ServiceUnavailable,
            gexc.DeadlineExceeded,
            gexc.ResourceExhausted,
            gexc.InternalServerError,
        ) as e:
            last_err = e
            wait = RETRY_BASE_WAIT * (2 ** attempt)
            print(f"    一時エラー({e.__class__.__name__})、{wait:.0f}秒後にリトライ...")
            time.sleep(wait)
    raise last_err


def ocr_page_with_docai(client, processor_name, doc, page_index):
    """ページの埋め込み画像をDocument AIでOCRし、(document, img_size)を返す"""
    page = doc[page_index]
    found = largest_embedded_image(doc, page)
    if found is None:
        return None, None
    image_bytes, img_w, img_h = found

    imgs = page.get_images(full=True)
    best = max(imgs, key=lambda im: (im[2] or 0) * (im[3] or 0))
    ext = doc.extract_image(best[0])["ext"]
    mime = _MIME.get(ext, "image/png")

    request = documentai.ProcessRequest(
        name=processor_name,
        raw_document=documentai.RawDocument(content=image_bytes, mime_type=mime),
        process_options=documentai.ProcessOptions(
            ocr_config=documentai.OcrConfig(
                enable_symbol=True,
                hints=documentai.OcrConfig.Hints(language_hints=["ja"]),
            )
        ),
    )
    result = call_docai_with_retry(client, request)
    return result.document, (img_w, img_h)


def _sym_rect(sym, sx, sy, img_w, img_h):
    poly = sym.layout.bounding_poly
    if poly.vertices:
        xs = [v.x * sx for v in poly.vertices]
        ys = [v.y * sy for v in poly.vertices]
    else:
        xs = [v.x * img_w * sx for v in poly.normalized_vertices]
        ys = [v.y * img_h * sy for v in poly.normalized_vertices]
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def collect_page_symbols_docai(document, page, img_size):
    """Document AI結果から (text, rect) を縦書きの読み順で返す。
    vision_reocr.collect_page_symbols と同じ後処理（ブロックX座標降順ソート＋
    列スナップ）を適用する。Document AIはシンボルがページ直下のフラットな
    配列で来るため、text_anchorの文字範囲でブロックに帰属させてから並べ替える"""
    img_w, img_h = img_size
    pw, ph = page.rect.width, page.rect.height
    sx, sy = pw / img_w, ph / img_h

    blocks = []
    for pg in document.pages:
        block_ranges = []
        for blk in pg.blocks:
            segs = blk.layout.text_anchor.text_segments
            if segs:
                block_ranges.append(
                    (int(segs[0].start_index), int(segs[-1].end_index))
                )
        block_syms = [[] for _ in block_ranges]
        stray = []
        for sym in pg.symbols:
            segs = sym.layout.text_anchor.text_segments
            if not segs:
                continue
            s0 = int(segs[0].start_index)
            text = document.text[s0:int(segs[0].end_index)]
            if not text.strip():
                continue
            rect = _sym_rect(sym, sx, sy, img_w, img_h)
            if rect.is_empty:
                continue
            for bi, (b0, b1) in enumerate(block_ranges):
                if b0 <= s0 < b1:
                    block_syms[bi].append((text, rect))
                    break
            else:
                stray.append([(text, rect)])
        blocks = [syms for syms in block_syms if syms] + stray

    if not blocks:
        return []
    blocks.sort(
        key=lambda syms: sum(r.x0 + r.x1 for _, r in syms) / (2 * len(syms)),
        reverse=True,
    )
    return _dedup_symbols(
        _snap_column_x([sym for block in blocks for sym in block])
    )


def _calibrate_body_fontsize(client, processor_name, doc, start_page, end_page, cache):
    """処理対象ページ先頭から数ページ分を実際にOCRし、実測列幅の中央値を得る
    （vision_reocr._calibrate_body_fontsizeと同じ方針。詳細はCLAUDE.md参照）"""
    widths = []
    last = min(end_page, start_page + CALIBRATION_MAX_PAGES - 1)
    for p1 in range(start_page, last + 1):
        if len(widths) >= CALIBRATION_TARGET_CHARS:
            break
        idx = p1 - 1
        document, img_size = ocr_page_with_docai(client, processor_name, doc, idx)
        cache[idx] = (document, img_size)
        if document is None:
            continue
        symbols = collect_page_symbols_docai(document, doc[idx], img_size)
        widths.extend(rect.width for _, rect in symbols)
    return statistics.median(widths) if widths else None


def reocr_pdf(client, processor_name, input_path, output_path, start_page, end_page):
    # --start再開時（入力＝出力）はメモリから開く（Windowsでチェックポイント
    # のos.replaceが自分自身のハンドルと衝突してWinError 5になる対策。
    # 詳細は vision_reocr._open_source_pdf）
    doc = _open_source_pdf(input_path, output_path)
    end_page = min(end_page, len(doc))

    # 書き戻す本文フォントサイズの基準値: DocAI実測列幅と旧OCR申告の大きい方
    # （旧OCRページと混在しても本文がルビ誤判定されない安全側。CLAUDE.md参照）
    cache = {}
    docai_body = _calibrate_body_fontsize(
        client, processor_name, doc, start_page, end_page, cache
    )
    old_ocr_body = detect_body_size(doc, range(len(doc))) or None
    if docai_body is not None and old_ocr_body is not None:
        target_body_fontsize = max(docai_body, old_ocr_body)
        print(
            f"本文フォントサイズ基準値: DocAI実測{docai_body:.2f}pt / "
            f"旧OCR申告{old_ocr_body:.2f}pt → 大きい方の"
            f"{target_body_fontsize:.2f}ptを採用"
        )
    elif docai_body is not None:
        target_body_fontsize = docai_body
        print(f"本文フォントサイズ基準値（DocAI実測）: {target_body_fontsize:.2f}pt")
    else:
        target_body_fontsize = None
        print("キャリブレーションできる文字が見つからず、最初の処理ページから決めます")
    old_ocr_body = old_ocr_body or target_body_fontsize

    total_chars = 0
    total_ruby = 0
    processed = 0
    t0 = time.time()

    try:
        for i, p1 in enumerate(range(start_page, end_page + 1), 1):
            idx = p1 - 1
            page = doc[idx]
            if idx in cache:
                document, img_size = cache.pop(idx)
            else:
                document, img_size = ocr_page_with_docai(
                    client, processor_name, doc, idx
                )
            if document is None:
                print(f"ページ{p1}: 埋め込み画像なし、スキップ")
                continue

            symbols = collect_page_symbols_docai(document, page, img_size)

            if target_body_fontsize is None:
                if not symbols:
                    print(f"ページ{p1}: 文字なし、スキップ")
                    continue
                target_body_fontsize = statistics.median(
                    rect.width for _, rect in symbols
                )
                old_ocr_body = old_ocr_body or target_body_fontsize
                print(
                    f"  (参照フォントサイズなし。ページ{p1}のインク幅から"
                    f"{target_body_fontsize:.1f}ptを基準に採用)"
                )
            target_ruby_fontsize = target_body_fontsize * RUBY_FONTSIZE_RATIO

            # 除去（redaction）で消える前に旧OCRテキスト層を解析し、
            # DocAIが検出しなかったルビの補完に使う（vision_reocr.pyと同じ。
            # DocAIは極小ルビの検出漏れがVisionより少ないため補完の出番は
            # 少ないが、位置照合で重複は防がれるため安全に併用できる）
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
            elapsed = time.time() - t0
            print(f"ページ{p1}: {n}文字書き戻し（うちルビ{n_ruby}） [{elapsed:.0f}秒経過]")

            if i % CHECKPOINT_EVERY == 0:
                _atomic_save(doc, output_path)
                print(f"  [チェックポイント保存: {output_path}]")
    finally:
        _atomic_save(doc, output_path, final=True)
        doc.close()

    elapsed = time.time() - t0
    print(
        f"完了: {processed}ページ処理 / "
        f"文字数{total_chars}（うちルビ{total_ruby}） / {elapsed:.0f}秒"
    )
    print(f"保存しました: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Google Cloud Document AIでスキャンPDFを再OCRし、透明テキスト層を書き戻す"
    )
    parser.add_argument("input_pdf", nargs="?")
    parser.add_argument(
        "--start", type=int, default=1, help="開始ページ（1始まり、既定:1）"
    )
    parser.add_argument(
        "--end", type=int, default=None, help="終了ページ（1始まり、既定:最終ページ）"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="出力PDFパス（既定: <入力>_docai.pdf）"
    )
    parser.add_argument(
        "--processor", default=os.environ.get("DOCAI_PROCESSOR"),
        help="プロセッサ名（projects/.../locations/.../processors/...）。"
             "省略時は環境変数DOCAI_PROCESSOR、それもなければ自動検出",
    )
    parser.add_argument(
        "--location", default="us", help="プロセッサのリージョン（既定: us）"
    )
    parser.add_argument(
        "--create-processor", action="store_true",
        help="OCRプロセッサを新規作成して終了（初回セットアップ用）",
    )
    args = parser.parse_args()

    client = make_client(args.location)

    if args.create_processor:
        project_id = _default_project_id()
        if not project_id:
            print("GOOGLE_APPLICATION_CREDENTIALS からプロジェクトIDを取得できません",
                  file=sys.stderr)
            sys.exit(1)
        existing = find_processor(client, project_id, args.location)
        if existing:
            print(f"既存のOCRプロセッサがあります: {existing}")
            return
        name = create_processor(client, project_id, args.location)
        print(f"プロセッサを作成しました: {name}")
        return

    if not args.input_pdf:
        print("入力PDFを指定してください（初回セットアップは --create-processor）",
              file=sys.stderr)
        sys.exit(1)

    processor_name = args.processor
    if not processor_name:
        project_id = _default_project_id()
        if project_id:
            processor_name = find_processor(client, project_id, args.location)
    if not processor_name:
        print(
            "OCRプロセッサが見つかりません。--create-processor で作成するか、"
            "--processor で明示指定してください",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"プロセッサ: {processor_name}")

    if args.output:
        output_path = args.output
    else:
        stem = args.input_pdf.rsplit(".", 1)[0]
        output_path = f"{stem}_docai.pdf"

    end_page = args.end
    if end_page is None:
        with fitz.open(args.input_pdf) as d:
            end_page = len(d)

    # --start での中断再開は既存の出力ファイルを土台にする（vision_reocr.pyと同じ）
    source = args.input_pdf
    if args.start > 1 and os.path.exists(output_path):
        print(f"既存の出力 {output_path} を土台に {args.start} ページ目から再開します")
        source = output_path

    try:
        reocr_pdf(client, processor_name, source, output_path, args.start, end_page)
    except Exception:
        print(
            f"エラーで中断しました。{output_path} には直近のチェックポイントまでの"
            f"結果が保存されています。--start で続きから再開できます",
            file=sys.stderr,
        )
        raise


if __name__ == "__main__":
    main()
