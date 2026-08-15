# 再OCRツールを Windows で使う（Vision / Document AI / YomiToku）

`vision_reocr.py` はGoogle Cloud Vision APIでスキャンPDFを再OCRし、透明テキスト層を
書き戻す前処理ツール（詳細は `README.md` を参照）。このドキュメントはWindows上で
Python版のスクリプトを直接動かすための手順をまとめたもの。より高精度な
Document AI版 `docai_reocr.py` を使う場合は7章も参照。

再OCRのバックエンドは4つあり、**このドキュメントはそのうち3つを扱う**。

| 使いたいもの | 参照先 | クラウド | 商用利用 |
|---|---|---|---|
| Vision API（迷ったらこれ） | このドキュメント 2〜4章 | 必要 | 可 |
| Document AI（最高精度・有料） | このドキュメント 7章 | 必要 | 可 |
| YomiToku（無料・オフライン） | このドキュメント 8章 | 不要 | **不可（非商用のみ）** |
| **NDLOCR-Lite（無料・オフライン・商用可）** | **[README_NDLOCR.md](README_NDLOCR.md)** | 不要 | 可 |

**クラウドを使いたくない場合は8章の `yomitoku_reocr.py` を参照**。GCPの
アカウント作成・課金設定・認証JSONがすべて不要になるため、以下の2章・3章を
まるごと飛ばせる（そのかわり非商用利用限定）。
**商用利用も必要なら NDLOCR-Lite（[README_NDLOCR.md](README_NDLOCR.md)）**を
選ぶ。ただし現時点ではルビが弱い。

必要な準備は3つ:

1. Python環境の準備
2. Google Cloud Vision APIの設定（GCPプロジェクト・課金・サービスアカウントキー）
3. 環境変数 `GOOGLE_APPLICATION_CREDENTIALS` の設定

以降のコマンドは **PowerShell** を前提とする（Windows 10/11標準）。従来の
コマンドプロンプト（cmd.exe）を使う場合の書き方も併記する。

---

## 1. Python環境の準備

### 1.1 Pythonのインストール

1. https://www.python.org/downloads/windows/ からPython 3.11以降のインストーラーを
   ダウンロードして実行する
2. インストーラーの最初の画面で **「Add python.exe to PATH」に必ずチェックを入れる**
   （これを忘れるとコマンドラインから `python` が使えない）
3. インストール後、PowerShellを開いて確認:

   ```powershell
   python --version
   pip --version
   ```

   バージョンが表示されなければPATHが通っていないので、PCを再起動するか
   インストーラーを「Modify」で再実行してPATH追加を有効にする。

### 1.2 プロジェクト一式の取得

Gitが使える場合:

```powershell
git clone https://github.com/<あなたのリポジトリ>/jisui2epub.git
cd jisui2epub
```

Gitを使わない場合は、リポジトリをZIPでダウンロードして展開し、そのフォルダに
`cd` する。

### 1.3 仮想環境の作成とライブラリのインストール

```powershell
# 仮想環境を作成
python -m venv .venv

# 仮想環境を有効化（PowerShell）
.venv\Scripts\Activate.ps1

# もし「このシステムではスクリプトの実行が無効になっています」というエラーが出たら、
# 一度だけ以下を実行してから再度 Activate.ps1 を実行する
# Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 仮想環境を有効化（cmd.exeの場合）
# .venv\Scripts\activate.bat

# 有効化に成功すると、プロンプトの先頭に (.venv) と表示される
# 必要なライブラリをインストール
pip install pymupdf google-cloud-vision
```

以降、`vision_reocr.py` を実行するときは毎回この仮想環境を有効化してから使う
（新しいPowerShellウィンドウを開くたびに `.venv\Scripts\Activate.ps1` が必要）。

---

## 2. Google Cloud Vision APIの設定

Vision APIを使うには、Googleアカウントに紐づくGCP（Google Cloud Platform）
プロジェクトが必要。すでにアカウント・プロジェクトがある場合は2.4から。

### 2.1 GCPプロジェクトの作成

1. https://console.cloud.google.com/ を開き、Googleアカウントでログイン
2. 画面上部のプロジェクト選択メニューから「新しいプロジェクト」を選択
3. プロジェクト名を入力して「作成」（例: `ocrpdf`）

### 2.2 Vision APIの有効化

1. 作成したプロジェクトを選択した状態で、以下のURLを開く
   （`project=...` の部分は自分のプロジェクトIDに置き換わる。検索バーで
   「Vision API」と入力してもたどり着ける）
   ```
   https://console.cloud.google.com/apis/library/vision.googleapis.com
   ```
2. 「有効にする」ボタンをクリック
3. 有効化直後はしばらく（数分程度）反映待ちになることがある

### 2.3 課金の有効化

Vision APIの呼び出しには課金設定が必須（月1000ユニットまでは無料枠。
詳細は「6. 料金について」）。

1. https://console.cloud.google.com/billing を開く
2. 対象プロジェクトに請求先アカウントが紐づいていなければ、案内に従って
   クレジットカード情報を登録し、請求先アカウントを作成・紐付ける
3. 「Cloud Vision API has not been used... billing to be enabled」という
   エラーが出た場合はこの手順が未完了

### 2.4 サービスアカウントキー（JSON）の発行

1. https://console.cloud.google.com/iam-admin/serviceaccounts を開く
   （対象プロジェクトが選択されていることを確認）
2. 「サービスアカウントを作成」をクリック
3. 名前を入力（例: `ocrpdf-vision`）して「作成して続行」
4. ロールの選択で「基本」→「編集者」、または「Cloud Vision」→
   「Cloud Vision AI サービス エージェント」程度の権限を付与して「続行」
   →「完了」
5. 作成したサービスアカウントの一覧から対象を選び、「キー」タブ→
   「鍵を追加」→「新しい鍵を作成」→形式は **JSON** を選択→「作成」
6. JSONファイルが自動的にダウンロードされる（例: `ocrpdf-xxxxxxxxxxxx.json`）

**このJSONファイルはGCPプロジェクトへのアクセス権そのものなので、
第三者と共有したりGitリポジトリにコミットしたりしないこと。**
`C:\Users\<ユーザー名>\keys\` のようなプロジェクト外の場所に保存するのが安全。

---

## 3. 環境変数 `GOOGLE_APPLICATION_CREDENTIALS` の設定

Vision APIのクライアントは、この環境変数が指す先のJSONキーを自動的に読みに行く。

### 3.1 そのPowerShellセッションだけで使う場合（毎回セット）

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\Users\<ユーザー名>\keys\ocrpdf-xxxxxxxxxxxx.json"
```

cmd.exeの場合:

```cmd
set GOOGLE_APPLICATION_CREDENTIALS=C:\Users\<ユーザー名>\keys\ocrpdf-xxxxxxxxxxxx.json
```

ウィンドウを閉じると消える。作業のたびに設定し直すのが面倒でなければこれで十分。

### 3.2 恒久的に設定する場合

**GUIから設定する方法:**

1. スタートメニューで「環境変数を編集」と検索して開く
   （「システムのプロパティ」→「環境変数」でも同じ画面に行ける）
2. 「ユーザー環境変数」の「新規」をクリック
3. 変数名: `GOOGLE_APPLICATION_CREDENTIALS`
4. 変数値: JSONキーのフルパス（例: `C:\Users\<ユーザー名>\keys\ocrpdf-xxxxxxxxxxxx.json`）
5. 「OK」で閉じる。**新しく開いたPowerShell/cmdウィンドウから反映される**
   （すでに開いているウィンドウには反映されない）

**PowerShellのコマンドで設定する方法（`setx`）:**

```powershell
setx GOOGLE_APPLICATION_CREDENTIALS "C:\Users\<ユーザー名>\keys\ocrpdf-xxxxxxxxxxxx.json"
```

これも新しく開いたウィンドウから反映される。

### 3.3 設定できているか確認

新しいPowerShellウィンドウを開いて:

```powershell
echo $env:GOOGLE_APPLICATION_CREDENTIALS
```

指定したパスが表示されればOK。cmd.exeなら `echo %GOOGLE_APPLICATION_CREDENTIALS%`。

---

## 4. 実行

### 4.0 GUIで実行する（かんたん・推奨）

`forwindows\jisui_gui.exe` をダブルクリックすると、コマンド入力なしで
変換できるGUIが起動する:

1. PDFをウィンドウにドラッグ＆ドロップする（ウィンドウ内のどこでもよい。
   クリックしてファイル選択でも可）。タイトル・著者はファイル名
   `タイトル_著者名.pdf` から自動入力される
2. 本の種類（小説（縦書き）／横書きの本／漫画）を選ぶ
3. 「▶ 変換開始」を押す。完了したら「📖 ePubを開く」で確認できる

- 再OCR（Vision API）を使う場合は「先に再OCRで文字を読み直す」にチェック。
  初回はサービスアカウントJSON（§2.4で作ったもの）を選ぶダイアログが出て、
  以後は自動で使われる（環境変数の設定は不要）
- 出力テキストを校正したら「↻ 校正済みからePub再生成」でePubを作り直せる
- 文字の大きさは右上の「文字＋／−」で変更できる
- GUIは各exeを起動するだけのランチャーなので、以降のコマンドライン手順と
  同じ結果になる。細かいオプションを使いたい場合は下記のコマンドラインで

### 4.1 コマンドラインで実行する

仮想環境を有効化した状態（`(.venv)` がプロンプトに出ている状態）で実行する。

```powershell
# 仮想環境の有効化を忘れずに
.venv\Scripts\Activate.ps1

# 全ページ再OCR（既定出力は <入力>_vision.pdf）
python vision_reocr.py "本.pdf"

# 数ページだけ試す
python vision_reocr.py "本.pdf" --start 10 --end 20 -o test.pdf

# 途中で中断した場合、続きのページから再開
python vision_reocr.py "本.pdf" --start 150

# 再OCR後のPDFはjisui2epub.pyにそのままかける（Pythonから）
python jisui2epub.py "本_vision.pdf" --title "タイトル" --author "著者名"

# Pythonを入れず forwindows\jisui2epub.exe を使う場合はこちら
..\forwindows\jisui2epub.exe "本_vision.pdf" --title "タイトル" --author "著者名"

# 横書きの本（実用書など）は --horizontal を付ける（ePubも横書きになる。
# ルビのない実用書は --ruby drop 併用を推奨。v0.9.0〜。再OCRの横書き対応は未着手）
..\forwindows\jisui2epub.exe "横書きの本.pdf" --horizontal --ruby drop
```

**パスに関する注意:**

- ファイル名・フォルダ名にスペースや日本語を含む場合は必ず `"..."` で囲む
- 相対パスでも絶対パスでも動くが、`cd` で作業フォルダを移動してから実行するのが分かりやすい
- Windowsのパス区切りは `\` だが、PowerShellでは `/` も使える

### 4.2 画像前処理（v1.6.0〜、既定ON）

OCRにかける前に、スキャン画像を**「本文1文字が32pxになるよう拡縮し、
ぼかしで輪郭を均す」**前処理が自動で適用される。3つの再OCRツール共通で、
**特に設定は不要**。低解像度でスキャンした本や白黒2値でスキャンした本で
文字の読み取り精度が上がる（実測で +0.1〜+0.4ポイント）。

```powershell
# 既定は auto（劣化したスキャンのページだけ自動で適用）
python yomitoku_reocr.py "本.pdf"

# 従来どおりの動作に戻したいとき
python yomitoku_reocr.py "本.pdf" --preprocess off
```

きれいにスキャンできている本（スーパーファイン相当）には自動的に適用されない。
実行の最後に「画像前処理: ○○ページに適用 / ○○ページは対象外」と表示される。

**この機能は「すでに取り込んでしまった本」の救済策**です。スキャンし直せるなら
ScanSnapの画質を「スーパーファイン」にして取り込み直すほうが効果ははるかに
大きく、前処理で埋まるのは画質差の4分の1程度です（実測は `README.md` の
「スキャン画質は『スーパーファイン』推奨」を参照）。

---

## 5. トラブルシューティング

### `PERMISSION_DENIED ... Cloud Vision API has not been used ...`

Vision APIが有効化されていない。「2.2 Vision APIの有効化」を実施し、数分待ってから再実行。

### `PERMISSION_DENIED ... This API method requires billing to be enabled ...`

課金設定が未完了。「2.3 課金の有効化」を実施。

### `DefaultCredentialsError` / `Could not automatically determine credentials`

`GOOGLE_APPLICATION_CREDENTIALS` が設定されていないか、パスが間違っている。
「3.3 設定できているか確認」の手順で確認する。**環境変数をGUIや`setx`で設定した
直後は、既に開いているPowerShellウィンドウには反映されない**ので、ウィンドウを
開き直すこと。

### 日本語のファイル名・出力が文字化けする

- 従来のコマンドプロンプト（cmd.exe）ではなく、**PowerShellまたはWindows
  Terminal**を使う
- cmd.exeしか使えない場合は `chcp 65001` を実行してからにする（フォントが
  等幅TrueTypeでないと表示は乱れることがあるが、ファイルの内容自体は壊れない）

### `pip install google-cloud-vision` が失敗する

- `pip install --upgrade pip` を先に実行してから再試行
- 依存パッケージ（`grpcio`, `cryptography` 等）はWindows用のビルド済み
  パッケージ（wheel）が配布されているため、通常は追加のビルドツールなしで
  インストールできる。それでも失敗する場合はエラーメッセージ末尾のパッケージ名を
  確認し、そのパッケージだけ `pip install <パッケージ名> --only-binary=:all:`
  を試す

### 実行が途中で止まった・エラーで中断した

`vision_reocr.py` は20ページごとに出力PDFへチェックポイント保存している。
中断した場合は `--start` に続きのページ番号を指定すれば、既存の出力ファイルを
土台に再開できる（「4. 実行」の例を参照）。

---

## 6. 料金について

`document_text_detection` は**月1000ユニットまで無料**（2026年時点、詳細は
https://cloud.google.com/vision/pricing で要確認）。1ページ＝1ユニットなので、
300〜400ページ程度の本なら1冊あたり無料枠に収まる。複数冊まとめて処理する場合や
月をまたいで大量に処理する場合は、GCPコンソールの請求ページで使用量を確認すること。

**v1.1.0からの注意**: 本文ギャップ救済（`--gap-rescue auto`、既定ON）は、
Document AIが使える環境（7章のプロセッサ作成済み）だと欠落のあるページだけ
Document AIで再OCRする。これは**無料枠のない従量課金（$1.50/1000ページ）**で、
欠落の多い本でも1冊あたり数十円程度だが、完全無料で使いたい場合は
`--gap-rescue old` を指定する（旧OCRの文字で補完。詳細は `README.md`）。
課金ページ数は実行完了時に必ず表示される。

---

## 7. より高精度な docai_reocr.py（Document AI版）を使う場合

`docai_reocr.py` はOCRエンジンをGoogle Cloud Document AIに差し替えた版
（精度と料金の比較は `README.md` 参照。**無料枠なし・$1.50/1000ページ**）。
Windows上のセットアップ手順は本ドキュメントの1〜3章と同じで、以下だけ異なる:

```powershell
# ライブラリ（vision の代わりに/加えて）
pip install google-cloud-documentai

# Google Cloud Consoleで「Document AI API」を有効化（課金必須）した上で、
# OCRプロセッサを作成（初回のみ）
python docai_reocr.py --create-processor

# 実行（使い方は vision_reocr.py と同じ。既定出力は <入力>_docai.pdf）
python docai_reocr.py "本.pdf"
```

エラー `OCRプロセッサが見つかりません` が出た場合は `--create-processor` を
先に実行する。`PERMISSION_DENIED` 系のエラーは本ドキュメント5章と同様に
API有効化・課金設定・認証情報を確認すること。

---

## 8. クラウドを使わない yomitoku_reocr.py（ローカルOCR版）

`yomitoku_reocr.py` はOCRエンジンを [YomiToku](https://github.com/kotaro-kinoshita/yomitoku)
（日本語特化のローカルOCR）に差し替えた版。**GCPのアカウント・課金・認証JSONが
すべて不要**になるため、本ドキュメントの2章・3章を丸ごと省略できる。
ネット接続も（初回のモデルダウンロードを除き）不要。

精度はVisionと互角以上（実測の比較表は `README.md` 参照）。処理はCPUで
2.5〜3秒/ページ程度なので、400ページの本で約17分かかる。

> **⚠️ ライセンスに注意**
> YomiToku 本体とモデル重みは **CC BY-NC-SA 4.0（非商用）** です。
> 個人の自炊・学術研究は無償で使えますが、**業務・商用での利用はできません**
> （jisui2epub 自体はMITですが、この経路だけ制限がかかります）。
> 商用利用は <https://www.mlism.com/> を参照してください。
>
> YomiToku © 2024 by Kotaro Kinoshita is licensed under CC BY-NC-SA 4.0
> <https://creativecommons.org/licenses/by-nc-sa/4.0/>

### 8.0 GUIから使う（推奨）

`jisui_gui.exe` の「先に再OCRで文字を読み直す」にチェックを入れると、
再OCRエンジンの選択肢が出る。**既定は YomiToku**（無料・オフライン・
Google Cloudの登録不要）。

- 未インストールなら「未インストール」と表示されるので、
  **「インストール手順」ボタン**を押すと手順が表示される
- Pythonを入れてもGUIが見つけられない場合は「**Pythonを選ぶ**」で
  `python.exe` を直接指定する
- Vision / Document AI を選んだときだけ、従来どおり認証JSONの選択欄が出る

> **なぜ YomiToku だけ Python が必要なのか**
> YomiToku は非商用ライセンス（CC BY-NC-SA 4.0）のため、exe に同梱して
> 配布することができません（配布物が非商用扱いになってしまいます）。
> そのため `yomitoku_reocr.py` はスクリプトのまま同梱し、お使いのPythonで
> 実行する形にしています。Vision / Document AI は exe に同梱されているので
> Python は不要です。

### 8.1 インストール

1章の仮想環境を作ったあと、**必ずこの順番で**インストールする。

```powershell
# 1) PyTorch を CPU版で先に入れる（この指定を省くとCUDA版≈3GBがダウンロードされる）
pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision

# 2) YomiToku 本体と PyMuPDF
pip install yomitoku pymupdf
```

**さらに、次の3つの `.py` を同じフォルダ（`jisui_gui.exe` と同じ場所）に置く。**

```
yomitoku_reocr.py
vision_reocr.py
jisui2epub.py
```

`yomitoku_reocr.py` は単体では動かない。透明テキスト層の書き戻し処理を
`vision_reocr.py` と、ページ解析を `jisui2epub.py` と共有しているため
（同じ処理を二重に持たないための設計で、`docai_reocr.py` も同様）。
1つでも欠けると `ModuleNotFoundError: No module named 'vision_reocr'` で
止まる。GUIから使う場合は不足しているファイル名を教えてくれる。

NVIDIAのGPUを積んでいるPCで速度を出したい場合のみ、1)を省いて
`pip install yomitoku` だけ実行し、実行時に `--device gpu` を付ける
（CUDA対応GPUが必要。AMDの内蔵GPUでは使えないためCPUで動かすこと）。

### 8.2 実行

```powershell
# 全ページ再OCR（既定出力は <入力>_yomitoku.pdf）
python yomitoku_reocr.py "本.pdf"

# 数ページだけ試す
python yomitoku_reocr.py "本.pdf" --start 10 --end 20 -o test.pdf

# 途中で中断した場合、続きのページから再開
python yomitoku_reocr.py "本.pdf" --start 100
```

初回実行時のみ、モデルファイルがHugging Faceから自動ダウンロードされる
（数分かかることがある）。起動時に `No Adapter To Version $17 for Resize` という
ONNX関連のエラーがログに出るが、自動でPyTorch推論に切り替わるため無視してよい。

処理中は1ページごとに残り時間の目安が表示される。認識信頼度の低い行は
`<出力>_lowconf.tsv` に記録されるので、校正時にそこを重点的に見るとよい。
