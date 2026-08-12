# NDLOCR-Lite で再OCRする — 導入ガイド（Windows向け）

自炊PDFのスキャナ内蔵OCR（ScanSnap等）は文字化けが多く、変換精度の上限に
なっています。`ndlocr_reocr.py` は国立国会図書館NDLラボの
**NDLOCR-Lite** でPDFを丸ごと読み直し、きれいな透明テキスト層に差し替えます。

**この経路の特徴**

- **完全無料。** クレジットカードの登録もアカウント作成も不要
- **完全オフライン。** インターネットに繋がなくても動く。本の中身が
  外部に送られない
- **商用利用も可**（CC BY 4.0）。YomiToku 経路は非商用限定なので、そこが違う
- **CPUだけで動く。** グラフィックボードは不要

**先に知っておいてほしいこと**

- **ルビ（ふりがな）はまだ苦手です。** 本文は実用水準ですが、ルビの正解率は
  65〜80%程度。ルビの多い本（児童文学・新書）をきれいに残したいなら
  `vision_reocr.py`（Google Cloud Vision）のほうが確実です
- **時代小説・古典には向きません。** 難しい漢字を似た形の常用漢字に
  置き換えてしまうことがあります（`有岡城`→`石岡城`、`殺戮`→`親戮`）
- ルビの無い本・実用書・現代小説の本文を直したい、という用途に向いています

迷ったときは README の
「[4つの再OCRバックエンドの選び方](README.md#4つの再ocrバックエンドの選び方)」を
見てください。

---

## 全体の流れ

1. Python を入れる
2. jisui2epub 一式を置く
3. 仮想環境（venv）を作ってライブラリを入れる
4. **NDLOCR-Lite をダウンロードする**（←この経路だけ必要）
5. 実行する

所要時間は初回30分ほど。2回目以降は5の実行だけです。

---

## 1. Python の準備

すでに `README_forWindows.md` の手順で Python と仮想環境を作ってある場合は、
**この章と2章・3章は飛ばして「4. NDLOCR-Lite の取得」へ進んでください。**

### 1.1 Python のインストール

<https://www.python.org/downloads/windows/> から Python 3.10 以降の
インストーラをダウンロードして実行します（**3.12 か 3.13 が無難**です。
理由は3章の注意書きにあります）。

**インストーラ最初の画面で「Add python.exe to PATH」に必ずチェックを入れてください。**
これを忘れると後でコマンドが見つからなくなります。

確認します。PowerShell を開いて（スタートメニューで「PowerShell」と検索）:

```powershell
python --version
```

`Python 3.12.x` のように表示されれば成功です。

### 1.2 jisui2epub 一式を置く

GitHub の [releases](https://github.com/ayati/jisui2epub/releases) から
zip をダウンロードして展開するか、Git があれば:

```powershell
git clone https://github.com/ayati/jisui2epub.git
```

展開先はどこでも構いませんが、**あとで何度も入力するので浅い場所を勧めます**
（例: `C:\jisui2epub`）。

---

## 2. 仮想環境（venv）を作る

仮想環境とは「このツール専用のPython部屋」です。他のソフトとライブラリが
衝突しないように分けておくもので、いらなくなったらフォルダごと消せます。

PowerShell で jisui2epub のフォルダに移動してから:

```powershell
# フォルダへ移動（自分の置き場所に読み替えてください）
cd C:\jisui2epub

# 仮想環境を作る（初回のみ・30秒ほど）
python -m venv .venv

# 仮想環境を有効化する（PowerShell を開くたびに毎回必要）
.venv\Scripts\Activate.ps1
```

有効化に成功すると、プロンプトの先頭に `(.venv)` と表示されます。

> **「このシステムではスクリプトの実行が無効になっています」と出たら**
> 一度だけ次を実行してから、もう一度 `Activate.ps1` を実行してください。
> このPowerShellウィンドウの中だけの設定なので安全です。
>
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> ```
>
> cmd.exe を使っている場合は `.venv\Scripts\activate.bat` です。

---

## 3. ライブラリを入れる

`(.venv)` が表示されている状態で、**次の1行だけ**実行します。

```powershell
pip install onnxruntime opencv-python-headless numpy PyYAML pillow pymupdf
```

合計200MB程度、数分かかります。これで必要なものは全部そろいます。

> **YomiToku と違って PyTorch は不要です。** NDLOCR-Lite は ONNX Runtime で
> 動くので、3GB近い CUDA 版を引いてしまう心配がありません。

### ⚠️ NDLOCR-Lite の `requirements.txt` は使わないでください

NDLOCR-Lite 本家の README には
`pip install -r requirements.txt` と書かれていますが、
**jisui2epub から使う場合これは実行しないでください。** 上の1行で足ります。

理由は2つあります。

1. **本家の `requirements.txt` は本家のGUI・CLI用**で、jisui2epub が使わない
   ものまで入っています（`flet`＝GUI framework、`reportlab`、`pypdfium2`、
   `networkx`、`protobuf` など）。jisui2epub は NDLOCR-Lite の文字検出
   （`deim.py`）と文字認識（`parseq.py`）だけを直接呼ぶので、PDFの読み書きは
   jisui2epub 側の PyMuPDF が担当します
2. **版がすべて固定されている**（`numpy==2.2.2` など）ため、**新しい Python
   では「その版のインストール済みパッケージ（wheel）が存在しない」状態になり、
   pip がソースからのビルドを始めます。** その結果
   `Microsoft Visual C++ 14.0 or greater is required` などのエラーで止まります
   （`numpy==2.2.2` の Windows 版が用意されているのは Python 3.13 まで。
   Python 3.14 で `-r requirements.txt` を実行するとこれが起きます）

上の1行は版を固定していないので、**pip が自分の Python に合う出来合いの版を
選んでくれます。C/C++ コンパイラ（Visual Studio Build Tools）は要りません。**

> **もし既にビルドエラーで詰まっている場合**は、Build Tools を入れる必要は
> ありません。`.venv` フォルダを丸ごと消して 2章からやり直し、3章の1行だけを
> 実行してください。それでも直らないときは「6. うまくいかないとき」の
> [NumPy のインストールでビルドエラーになる](#numpy-のインストールでビルドエラーになる--microsoft-visual-c-140-or-greater-is-required)
> を見てください。

---

## 4. NDLOCR-Lite の取得

**jisui2epub には NDLOCR-Lite が同梱されていません。**
国立国会図書館が CC BY 4.0 で公開しているものを、利用者が自分で
ダウンロードして使う形になっています（ライセンスを正しく守るための方式です）。

### 4.1 ダウンロード

Git がある場合:

```powershell
cd C:\
git clone https://github.com/ndl-lab/ndlocr-lite
```

Git が無い場合は <https://github.com/ndl-lab/ndlocr-lite> を開き、
緑の **Code** ボタン →**Download ZIP** で取得して展開します。

モデルファイル（AIの本体）はリポジトリに含まれているので、
別途ダウンロードする必要はありません。

### 4.2 ⚠️ 置き場所の注意 — フォルダ名に日本語を使わないこと

**NDLOCR-Lite は、パスに日本語（全角文字）が入っていると起動しません。**
これは本家の制限で、こちらでは直せません。

```
✅ C:\ndlocr-lite
✅ C:\tools\ndlocr-lite
❌ C:\ユーザー\書籍変換\ndlocr-lite     ← 日本語が入っている
❌ C:\Users\たろう\ndlocr-lite          ← ユーザー名が日本語
```

**Windows のユーザー名が日本語の人は要注意です。** デスクトップや
ドキュメントフォルダの下に置くと、パスに日本語が混ざります。
`C:\ndlocr-lite` のようにドライブ直下に置くのが確実です。

`ndlocr_reocr.py` は起動時にパスを検査して、日本語が混ざっていれば
分かるようにエラーを出します。

### 4.3 置き場所を覚えさせる（任意・おすすめ）

毎回 `--ndlocr-dir` を打つのが面倒なら、環境変数に設定しておけます。

```powershell
# このPowerShellウィンドウの中だけ
$env:NDLOCR_DIR = "C:\ndlocr-lite"

# 恒久的に設定する（一度だけ実行。以降のウィンドウで有効）
[Environment]::SetEnvironmentVariable("NDLOCR_DIR", "C:\ndlocr-lite", "User")
```

恒久設定をした場合は、**PowerShell を一度閉じて開き直す**と反映されます。

---

## 5. 実行

`(.venv)` が表示されている状態で:

```powershell
# まず数ページだけ試す（10〜20ページ目。1分ほどで終わる）
python ndlocr_reocr.py "C:\books\本.pdf" --ndlocr-dir C:\ndlocr-lite --start 10 --end 20 -o test.pdf

# 問題なさそうなら全ページ
python ndlocr_reocr.py "C:\books\本.pdf" --ndlocr-dir C:\ndlocr-lite
```

`NDLOCR_DIR` を設定してあれば `--ndlocr-dir` は省略できます。

出力は元のPDFと同じ場所に `本_ndlocr.pdf` として作られます（`-o` で変更可）。
**元のPDFは書き換えません。**

処理速度は約2.8秒/ページ。400ページの本で20分ほどです。

### 5.1 変換する

再OCRしたPDFを、いつもどおり jisui2epub にかけます。

```powershell
python jisui2epub.py "C:\books\本_ndlocr.pdf" --title "タイトル" --author "著者名" --epub
```

> **ファイル名の `_ndlocr` は著者名に混ざりません。** 「タイトル_著者名_ndlocr.pdf」の
> ように OCR 方式の名前を付けておいても、著者名からは自動で取り除かれます。

### 5.2 よく使うオプション

```powershell
# ルビの処理を止めて本文だけ差し替える（ルビの無い本・実用書向け。少し速い）
python ndlocr_reocr.py 本.pdf --no-ruby

# 途中で止まってしまったとき、続きから再開する
python ndlocr_reocr.py 本.pdf --start 240

# 画像の前処理を切る（既定は auto ＝劣化したページだけ自動で適用）
python ndlocr_reocr.py 本.pdf --preprocess off
```

長時間かかるので、20ページごとに途中結果が保存されます。
中断しても `--start` で続きから再開できます。

---

## 6. うまくいかないとき

### `エラー: … に src/ が見つかりません`

`--ndlocr-dir` に指定したフォルダが違います。指定するのは
**`src` フォルダが直下にあるフォルダ**です。
ZIP を展開すると `ndlocr-lite-main\ndlocr-lite-main\src\...` のように
二重になることがあるので、エクスプローラで `src` の場所を確認してください。

### `エラー: NDLOCR-Lite のパスに非ASCII文字が含まれています`

4.2 の通りです。パスのどこかに日本語が入っています。
`C:\ndlocr-lite` のような場所に移動してください。

### `エラー: NDLOCR-Lite の読み込みに失敗しました`

3章のライブラリが入っていません。`(.venv)` が表示されているか確認してから:

```powershell
pip install onnxruntime opencv-python-headless numpy PyYAML pillow pymupdf
```

`No module named 'PIL'` と出た場合も同じです（Pillow は NDLOCR-Lite 側が
使います。以前の版のこのガイドでは書き漏らしていました）。

### NumPy のインストールでビルドエラーになる / `Microsoft Visual C++ 14.0 or greater is required`

**NDLOCR-Lite の `requirements.txt` を使ったときに起きます。**
`pip install -r .\ndlocr-lite\requirements.txt` は実行しないでください
（理由は[3章の注意書き](#-ndlocr-lite-の-requirementstxt-は使わないでください)）。

固定された古い版の NumPy に、お使いの Python 用の出来合いの
インストール済みパッケージ（wheel）が無いため、pip がソースから
コンパイルしようとして失敗しています。同種のメッセージは他にも出ます。

```
error: Microsoft Visual C++ 14.0 or greater is required
error: metadata-generation-failed
ninja: build stopped / meson.build:… ERROR
```

**直し方（おすすめ）**

```powershell
# 1. 壊れかけの仮想環境を捨てる（PowerShellで .venv のある場所に移動してから）
deactivate                      # 有効化していれば
Remove-Item -Recurse -Force .venv

# 2. 作り直す
python -m venv .venv
.venv\Scripts\Activate.ps1

# 3. 版を固定しない、この1行だけを実行する
pip install onnxruntime opencv-python-headless numpy PyYAML pillow pymupdf
```

pip が自分の Python に合う版を選ぶので、コンパイラは不要です。

**それでも直らないとき**は、Python が新しすぎる可能性があります。
`python --version` で確認し、3.14 以降なら
[Python 3.13](https://www.python.org/downloads/windows/) を入れ直して
（3.14 と共存できます）、`py -3.13 -m venv .venv` で仮想環境を作り直して
ください。

> **Visual Studio Build Tools を入れる必要はありません。**
> ネット上には `winget install Microsoft.VisualStudio.2022.BuildTools` で
> C++ コンパイラを入れて解決する方法も出てきますが、数GBのダウンロードが
> 必要で、この用途には割に合いません。上のやり方でコンパイル自体を
> 回避できます。
>
> （NDLOCR-Lite 本家のGUI・CLIをそのまま使いたくて `requirements.txt` を
> 通したい場合は、Build Tools を入れるより **Python 3.13 以下を使う**ほうが
> 簡単です。）

### `python` が見つからない / `pip` が見つからない

1.1 の「Add python.exe to PATH」のチェックを忘れています。
Python を入れ直すか、インストーラを再実行して **Modify** から追加できます。

### 起動時に ONNX の警告が出る

`ONNX version_converter` に関する警告が出ることがありますが、
自動的に別の方法へ切り替わるので**実害はありません**。そのまま進んで大丈夫です。

### 出力のルビがおかしい

現状の既知の弱点です。冒頭の「先に知っておいてほしいこと」を参照してください。
ルビを捨ててしまってよければ、変換時に `--ruby drop` を付けられます。

```powershell
python jisui2epub.py 本_ndlocr.pdf --ruby drop --epub
```

---

## 7. NDLOCR-Lite を更新するとき

**動作確認しているのは commit `36d7c4d`（2026-08-04）です。**

`ndlocr_reocr.py` は速度のため NDLOCR-Lite の内部を直接呼んでいるので、
本家が大きく変わると動かなくなったり、**エラーは出ないのに精度だけ落ちたり**
することがあります。

更新したら、**必ず数ページ変換して出力を目で確認してください。**

```powershell
python ndlocr_reocr.py 本.pdf --start 10 --end 15 -o check.pdf
python jisui2epub.py check.pdf --title T --author A
# 出力された check.txt を開いて、本文が読める文章になっているか見る
```

うまくいかなくなった場合は、確認済みの版に戻せます。

```powershell
cd C:\ndlocr-lite
git checkout 36d7c4d
```

技術的な依存箇所の一覧は README の
「[想定している NDLOCR-Lite のバージョンと、更新時の注意](README.md#想定している-ndlocr-lite-のバージョンと更新時の注意)」に
まとめてあります。

---

## 8. ライセンス

**NDLOCR-Lite** © 国立国会図書館（NDLラボ）
<https://github.com/ndl-lab/ndlocr-lite>
Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

CC BY 4.0 は「出典を示せば、商用利用も改変も再配布もしてよい」ライセンスです。
jisui2epub は NDLOCR-Lite を同梱せず、利用者がダウンロードしたものを
呼び出すだけです。再OCRしたPDFやそこから作った ePub を第三者に配る場合は、
上の出典表示を残してください（個人で読むだけなら不要です）。

jisui2epub 本体は MIT ライセンスです。
