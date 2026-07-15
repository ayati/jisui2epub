# vision_reocr.py を Windows で使う

`vision_reocr.py` はGoogle Cloud Vision APIでスキャンPDFを再OCRし、透明テキスト層を
書き戻す前処理ツール（詳細は `README.md` を参照）。このドキュメントはWindows上で
Python版のスクリプトを直接動かすための手順をまとめたもの。

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
python jisui2epub.py "本_vision.pdf" --title "タイトル" --author "著者名" --epub

# Pythonを入れず forwindows\jisui2epub.exe を使う場合はこちら
..\forwindows\jisui2epub.exe "本_vision.pdf" --title "タイトル" --author "著者名" --epub
```

**パスに関する注意:**

- ファイル名・フォルダ名にスペースや日本語を含む場合は必ず `"..."` で囲む
- 相対パスでも絶対パスでも動くが、`cd` で作業フォルダを移動してから実行するのが分かりやすい
- Windowsのパス区切りは `\` だが、PowerShellでは `/` も使える

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
