# CLAUDE.md

## jisui2epub

自炊PDF（OCRテキスト層付き縦書き小説）→ 青空文庫形式テキスト → リフロー型ePub3 変換ツール。
単一ファイル `jisui2epub.py`、依存は PyMuPDF のみ（`.venv` にインストール済み）。

```bash
# 実行
.venv/bin/python jisui2epub.py input.pdf --title T --author A [--epub]
# 構文チェック
.venv/bin/python -m py_compile jisui2epub.py
# ページ構造デバッグ（ヒューリスティック調整時に必須）
.venv/bin/python jisui2epub.py input.pdf --inspect <1始まりページ番号>
```

## アーキテクチャ

パイプライン: `analyze_page`（スパン→縦行/横行/ルビ分類）→ `classify_marginals`
（全ページ横断で柱・ノンブル・挿絵ノイズ特定）→ `attach_rubies`（ルビ→親文字対応）
→ `assemble_text`（段落復元・見出し検出・青空文庫形式出力）→ novel_downloader.py --from-file でePub化。

重要な知見（temp_sample の2冊で検証済み）:

- **OCR世代でスパン構造が違う**: 古いScanSnap(2013)は1文字1スパン、新しい(2026)は1行1スパン。
  `analyze_page` はどちらもx中心クラスタリングで縦行に統合する
- **pypdfはCMap解釈に失敗して文字化けする**。PyMuPDF必須
- **柱・ノンブルは本文よりフォントが小さく、ルビ判定(0.68倍以下)に食い込む**ことがある
  → classify_marginals で本文領域外のルビをマージン行に移送してから判定
- **章見出しの2方式**: 岩波少年文庫型＝本文より大きい横書き行（サイズ比1.18+）。
  創元推理文庫型＝本文と同サイズの縦行だが柱と同一テキスト・ページ右端・深い字下げ(1.5字超)
- **柱のOCRは毎ページ揺れる**（靴棚→腱棚/腱場/農場）→ 数字除去＋difflibあいまい一致(0.66)で照合
- **重複見出し**: 章の扉ページと本文ページ両方に章題が出る本がある → 数字前方一致＋
  類似度0.75で15段落以内なら排除（(1)(2)(3)連続章を潰さないため数字は必須比較）
- **マージン判定は行のy中心で行う**。端座標だと数ptの差で判定漏れする

テスト用サンプル: `temp_sample/` に2冊分の入力PDF・手作業校正済みテキスト（ゴール）・
公式ePub（比較用）がある。ヒューリスティック変更時は両方の本で章見出し一覧
（`grep 中見出し］`）と第1章の一致率を確認すること。
