# DESIGN — ISBN 自動検出・NDL 書誌補完・OPF 書誌タグ網羅（jisui2epub）

対象: `jisui2epub/jisui2epub.py`
起票: 2026-07-25　ステータス: **フェーズ1（§3.5）・フェーズ2（§4.9）とも実装済み**
前提: `mangaP2ePub/DESIGN_ISBN.md` §4 で「jisui2epub 側は別タスクで独立実装」と決定済み。
本書はその別タスク分の設計。**ロジック（正規化・検出・NDL 照会）は移植するがコードは共有しない**
（依存が PyMuPDF only ⇔ pypdf+PIL、本文処理の核も別物のため）。

---

## 1. 現状と課題

| # | 課題 | 箇所 |
|---|---|---|
| 1.1 | `--isbn` の正規化が `re.sub(r"[-\s]", "", isbn)` だけ | jisui2epub.py:4488 |
| 1.2 | ISBN・発行日・出版社が**すべて手動指定**（実運用ではほぼ空のまま） | main():4813-4820 |
| 1.3 | `dc:date` が未指定時に**生成日**へフォールバック（意味論的に誤り） | `_make_opf`:3872 |
| 1.4 | `dc:creator` が1名固定・`file-as` なし・アクセシビリティ metadata なし | `_make_opf`:3887-3898 |
| 1.5 | 翻訳書・挿絵付きが多い（temp_sample 20タイトル中、訳者6・画家4）のに**訳者・画家を表現できない** | 同上 |

### 1.1 の実害
`--isbn "ISBN978-4-06-148668-3"` は `urn:isbn:ISBN9784061486683` になる。
yomikake は `classifySource()`（yomikake.html:7462）で `urn:isbn:` の後ろをそのまま
国立国会図書館サーチのキーに使うため、**接頭辞・全角ハイフンが残ると書誌リンクが不発**になる。

### 1.3 の実害
`dc:date` の意味は**原刊行日**。生成日を入れると「2026年7月刊行の霧のむこうのふしぎな町」に見える。
ePub 化日時は既に `dcterms:modified` にあるので二重化は不要。

---

## 2. 実測調査（PyMuPDF・`temp_sample` 26ファイル / 24タイトル、2026-07-25）

プローブ: `page.get_text()` → NFKC → **空白全除去（compact）** → ISBN/発行日を抽出。
実測に使った試作は `scratchpad/probe_bib_detect.py`（＋`probe_isbn_base.py`）に置いてある。
本節の表はこれで再現できる（`.venv/bin/python ../scratchpad/probe_bib_detect.py *.pdf`）。

| 本（入力PDF） | 頁 | ISBN 検出 | 頁 | 発行日 検出 | 判定 |
|---|---:|---|---:|---|---|
| タイム・リープ上 | 227 | — | — | — | 奥付に日付なし（OCR欠落）|
| タイム・リープ下 | 207 | — | — | 1997-01-25 | EXACT（漢数字西暦）|
| 書を捨てよ(NORMAL) | 320 | — | — | — | 同上 |
| 書を捨てよ(SUPERFINE) | 320 | — | — | — | ISBN以前・年月漢字が化け |
| 遠まわりする雛(NORMAL) | 403 | — | — | 2012-02 | **WRONG**（十一版・月誤読）|
| 遠まわりする雛(SUPERFINE) | 403 | 9784044271046 | 400 | 2010-07-25 | EXACT |
| 霧のむこうのふしぎな町 | 224 | 9784061486683 | 224 | 2004-12-15 | EXACT（新装版第1刷）|
| 地下室からのふしぎな旅 | 256 | 9784061487246 | 256 | 2006-04-15 | EXACT |
| 蘇我氏 | 288 | 9784121023537 | 288 | 2015-12 | 縮退（真 12-20、日が `2()` 化け）|
| 黒牢城 | 525 | 9784041147221 | 525 | 2024-06-25 | EXACT |
| ソフロニア嬢 | 417 | 9784150205874 | 417 | 2017-02-25 | EXACT（印刷日2/20を除外）|
| 伯爵夫人は超能力 | 355 | 9784087603583 | 355 | 1999-04-20 | EXACT |
| ほんものの魔法使 | 303 | 9784488560027 | 303 | 2021-05-14 | EXACT |
| グリックの冒険 | 367 | 9784001140453 | **357** | 2000-07-18 | EXACT |
| 30秒でわかる！DS（横書き）| 171 | 9784621311981 | 171 | — | 年月漢字が化け（令和7イドlOII30日）|
| ―― 以下、第2次検証で追加した ScanSnap サンプル ―― ||||||
| わかりやすい韮山反射炉の解説 | 182 | 9784990872007 | 182 | 2015-12-01 | EXACT（私家版寄り小出版社）|
| 白銀の墟玄の月1 | 377 | 9784101240626 | 377 | 2019-10-12 | EXACT（`令和元年十月十二日`）|
| 風の万里黎明の空上 | 368 | 9784101240565 | 368 | 2013-04 | 縮退（奥付2列が混線し日が分離）|
| 雪の女王（角川文庫1976）| 217 | — | — | 1976-08-30 | ISBN以前（旧角川分類コード）／日付 EXACT |
| 典座教訓・赴粥飯法 | 274 | 9784061589803 | 274 | 1991-07-10 | EXACT（ISBN-10 パスで検出。§2.3-F）|
| シナリオのためのファンタジー事典 | 345 | 9784815600785 | 345 | — | 奥付2列が完全インターリーブ（§2.3-D）|
| 哲夫の春休み | 371 | 9784001156416 | 371 | 2010-10-26 | EXACT |
| ―― 以下、フェーズ1実装後に追加したサンプル ―― ||||||
| 魔法使いの塔下 | 386 | 9784488577230 | 386 | 2016-10-21 | EXACT |
| 図南の翼 | 421 | 9784101240596 | 421 | 2019-08-30 | **WRONG**（初版行の「発行」が化け、後刷を採用。§4.1 注）|
| 諸国そばの本 | 150 | 9784533033773 | 150 | — | 奥付ページがスキャンに無い |
| マンガの歴史1 | 172 | 9784265008315 | 172 | 2017-08-31 | EXACT |

**ISBN 20/26ファイル（20/24タイトル）。**未検出6のうち3タイトルは**本にISBNが無い**
（書を捨てよ・雪の女王＝1976年以前の旧角川分類コード `0197-216502-0946(0)`）か
**OCR全滅**（タイム・リープ上下）で、残るは遠まわりする雛(NORMAL)＝画質起因。
**ISBN が印刷されている本に限れば 20/22。**
**発行日 EXACT 16・縮退 2・WRONG 2・MISS 6。**
検出した ISBN 20件は**全件 NDL に実在し書名も一致**（§4.2）＝誤検出ゼロの裏付け。

### 2.1 縦組み小説に固有の壁（本設計の中核）

mangaP2ePub の実装をそのまま移しても**ほとんど検出できない**。原因は5つ。

1. **1文字1行問題**。ScanSnap OCR の縦組みページは `get_text()` が1文字ずつ改行して返す
   （旧世代OCRは1文字1スパン、新世代でも縦行は縦に積まれる）。
   `ISBN978-4-06-148668-3` は `I\nS\nB\nN\n9\n7\n8\n-\n…` になり、
   manga 版の区切りクラス `[-‐‑\s]?`（**0〜1個**）では `-` と `\n` の2連続で破断する。
   → **ページテキストを NFKC 後に空白全除去（compact）してから当てる**。
   実測: 30秒DS・霧・伯爵夫人・グリックは compact 化して初めて検出できた。
2. **漢数字西暦**（`二○一七年二月二十五日`）。縦組み奥付では西暦も漢数字が普通。
   `○`（U+25CB）と `〇`（U+3007）が混在する。manga 版の `(19|20)\d{2}` では拾えない。
3. **和暦年が3文字**（`平成二十二年`）。manga 版は `{1,2}` で `令和元`/`昭和57` 想定。
4. **キーワードが「発行」とは限らない**。集英社文庫は `1999年4月20日第1刷`、
   創元推理文庫は `2021年5月14日初版` で、日付の近傍に「発行」が無い。
   → ゲートを `発行|発売|刷|初版` に広げる。
5. **「日」が落ちる／化ける**（`二十五冊発行`・`一月二十五II初版`・`12月2()日初版`）。
   日を無条件に採ると **蘇我氏で 12-20 → 12-02 の誤りが出る**。
   → 日は「直後が `日`」または「1〜2文字の雑音を挟んで `発行|発売|刷|初版`」のときだけ採用し、
   それ以外は **YYYY-MM へ縮退**する。この規則で蘇我氏は安全に縮退し、
   ソフロニア（`二十五冊発行`）とタイム・リープ下（`二十五II初版発行`）は正しく日まで取れる。

### 2.2 そのほかの実測知見

- **走査窓は末尾20ページ以上必要**。グリックの冒険（岩波少年文庫）は奥付が p357/367 で、
  後ろに巻末広告が10ページある。manga 版の 10ページ窓では届かない。
  なお同書は p2（標題紙裏）にも同じ ISBN があり、**先頭側にも保険の窓を置く価値がある**。
- **誤検出は全ページ走査でも 0 件**。21ファイル（延べ約6,700ページ、追加5ファイルを含めても同じ）を全ページ走査しても
  `97[89]` アンカー＋チェックディジットを通る値は各書1種類（＝正解）だけだった。
  本文中の数字列が誤ヒットする懸念は実測上ない。
- **スキャン画質で結果が割れる**。遠まわりする雛は SUPERFINE で ISBN 検出・初版日 EXACT、
  NORMAL では ISBN 消失・初版行が化けて十一版の日付を誤採用（既知メモ
  `scan-quality-ocr-accuracy` と整合）。
- **再OCR（Vision/DocAI）PDFでも検出できる**が万能ではない。霧_vision は ISBN 検出可、
  発行日は奥付の4行のうち最終刷しか返さず **2020-10-02（新装版第34刷）**になった。
  → **NDL を OCR より優先**する根拠（NDL は 2004.12 を返す）。
- **複数刷・新装版**。霧は同一奥付に 1980初版／2003第64刷／2004新装版第1刷／2020新装版第34刷が並ぶ。
  ISBN が指すのは新装版なので `初版|第1刷` 優先＋最小日付で **2004-12-15** が正しい。
- **印刷日と発行日の並記**（早川書房）。`…二月二十日印刷 …二月二十五日発行` →
  直後が「印刷」の候補は捨てる。
- **カレンダー妥当性**は必須。遠まわり(NORMAL) は `二月三十日` を拾い 2012-02-30 という
  存在しない日付を出した。月末日チェックで年月へ縮退させる。

### 2.3 第2次検証（ScanSnap サンプル7冊追加、2026-07-25）— 設計の妥当性確認と3点の修正

追加7冊（韮山反射炉・白銀の墟・風の万里・雪の女王・典座教訓・ファンタジー事典・哲夫の春休み）は
**§3・§4 の仕様のまま ISBN 6/7・発行日 6/7（EXACT 5・縮退1・MISS1）で、誤りは0件**。
§2.1 の5対策はすべて効いた（`令和元年十月十二日`＝和暦「元」＋漢数字日、
`平成二十七年十二月一日第一刷発行`＝和暦3文字、`昭和五十一年八月三十日初版発行`＝初版優先で
四版1979を回避）。新たに判明した事象と、仕様への反映は以下。

- **D. 奥付が2段組でOCRが列をインターリーブする**（新パターン）。ファンタジー事典 p344 は
  `1212019fi202147月26日1月20日初版第1刷発行初版第2刷発行`——2019年7月26日(初版第1刷)と
  2021年1月20日(初版第2刷)が交互に混ざり「年」の字も消えている。**MISS になる＝安全側**なので
  仕様変更は不要。風の万里 p366 も同型の軽症（`平成二十五年四月…一日発行` と日が分離）で **2013-04 に縮退**、
  NDL の `2013.4` と粒度が一致する。→ **§4.4 の「NDL > OCR」優先順位がそのまま救済策になる**。
- **E. 「ISBN」キーワード窓は URL の `isbn` にも当たる**。ファンタジー事典は本文・奥付に
  `https://isbn2.sbcr.jp/00785/` があり3ページでキーワードにヒットするが、窓内に10桁トークンが
  できず**誤検出なし**。パス1(EAN-13)を先に走らせる2パス構成のまま問題ない。
- **F. バーコード由来の13桁は後続数字で `(?!\d)` に弾かれることがある**。典座教訓 p274 は
  `N978406158980311111111N…` でパス1が不発、`ISBN4-06-158980-6` を拾うパス2が正解を返した
  （ISBN-10→13変換の結果もバーコードと一致）。→ **パス2は「旧本用の保険」ではなく必須**。
  この挙動は価格コード `192…` 対策の先読みを緩めずに済ませるためのもので、仕様変更は不要。
- **修正1（§4.2）: `dcndl:volume` は「上」「第1巻」を採用する**。初版の設計に書いた
  「数字を含まない値は無視」は誤り。NDL 実測は 白銀の墟=`第1巻` / 風の万里=`上` / 霧・地下室=`: 新装版`。
  → **先頭の `: ` を剥がし、`新装版|改訂|増補|新版|普及版` 等の版表示語を含む値だけ捨てる**。
- **修正2（§4.2）: 責任表示の角括弧を除去する**。典座教訓の NDL 責任表示は
  `道元 [著],中村璋八 [ほか]訳` で、manga 実装のままだと `dc:contributor` が
  `中村璋八[ほか]` になる。→ `_ndl_parse_responsibility` の前処理で `\[[^\]]*\]` を除去する。
- **修正3（§5）: ファイル名末尾のスキャン日が `dc:creator` を汚染する**。
  `わかりやすい韮山反射炉の解説_堀内永人20241221.pdf` → 現行 `parse_meta_from_filename()` は
  著者を `堀内永人20241221` と返す（`_strip_method_tag`:2545 は `_vision/_docai/_ocr` しか剥がさない）。
  NDL 照合では `堀内永人` と一致せず警告が出るだけだが、**この文字列は yomikake のしおりキーに入る**。
  → `_strip_method_tag` に「著者名末尾の6桁以上の連続数字（スキャン日）を剥がす」を追加する。
- **NDL は追加6件も全ヒット**（累計 **16/16**）。私家版寄りの小出版社（文盛堂書店・ISBN 978-4-9908720-0-7）
  でもヒットした。

---

## 3. フェーズ1 仕様 — ISBN

### 3.1 `normalize_isbn(raw) -> str | None`（manga と同一仕様・移植）

```
1. NFKC 正規化（全角ハイフン ‐‑－・全角空白・全角数字・全角ＩＳＢＮ → 半角）
2. 先頭の "ISBN[:：]?" を除去（大小無視）
3. 数字と X 以外を全除去
4. 13桁 かつ 97[89] 始まり かつ mod-10 通過 → その13桁
5. 10桁 かつ mod-11 通過 → "978"+先頭9桁 に検査数字を再計算して13桁化
6. それ以外 → None
```
`urn:isbn:978…` を渡しても正しく13桁を返す（冪等）。`--isbn` 明示指定と自動検出の**共通ゲート**。

### 3.2 `detect_isbn_from_doc(doc, max_pages=25, head_pages=5) -> str | None`（新設）

- **PyMuPDF 版**。`main()` / `run_from_text()` が既に開いている `fitz.Document` を渡す
  （pypdf は使わない＝依存を増やさない）。
- 各ページ `page.get_text()` → `unicodedata.normalize("NFKC", …)` → `re.sub(r"\s+", "", …)`。
- **パス1: 裸の EAN-13**（`97[89]`＋10桁）を最終ページ→先頭方向に探す。
- **パス2: 「ISBN」直後25文字の窓で ISBN-10**（旧奥付・講談社青い鳥文庫や岩波少年文庫）。
- 末尾 `max_pages` で見つからなければ**先頭 `head_pages`** も同じ2パスで走査（標題紙裏）。
- 全候補を `normalize_isbn()` で検証。ログ（実装では他のツール出力に合わせて絵文字表記）:
  `🔖 ISBN自動検出: 9784… （pN, EAN-13）` / `🔖 ISBN未検出（末尾25ページ）`。

区切り文字クラスは compact 済みテキスト前提でも**バーコード行の中黒などを吸収するため0個以上**にする:

```python
_ISBN_SEP  = r'[-‐‑－\s]*'
_EAN13_RE  = re.compile(r'(?<!\d)97[89](?:' + _ISBN_SEP + r'\d){10}(?!\d)')
_ISBN10_RE = re.compile(r'(?:\d' + _ISBN_SEP + r'){9}[\dXx]')
```

### 3.3 埋込方針（manga と同一）

- `dc:identifier`（この電子ファイルの id）＝ `urn:uuid` のまま。
- `dc:source`（**底本**＝派生元の紙の本）＝ `urn:isbn:{13桁}`。
- 自炊は「紙の本の私的複製」なので ISBN を識別子にはしない（意味論は manga §5.2 に準拠）。

### 3.4 CLI

- `--isbn` は `normalize_isbn()` を通す。不正なら警告して**自動検出へフォールバック**。
- `--no-isbn-detect` を追加（自動検出の無効化）。

### 3.5 実装内容（2026-07-25 完了）

`jisui2epub.py` への追加・変更（新規セクション「ISBN の正規化と自動検出」を
`parse_meta_from_filename` の直後に設置。`unicodedata` は既 import）:

| 追加/変更 | 内容 |
|---|---|
| `_ISBN_SEP` / `_EAN13_RE` / `_ISBN10_RE` / `_ISBN_KW_RE` | 正規表現（§3.2 のとおり）|
| `_isbn13_check_ok` / `_isbn10_check_ok` / `_isbn10_to_13` | チェックディジット |
| `normalize_isbn(raw)` | 共通ゲート（§3.1）|
| `_compact_page_text(page)` | NFKC＋空白全除去（§2.1-1）|
| `_find_isbn_in_texts(texts, order)` | 2パス探索。末尾走査と先頭走査で共有 |
| `detect_isbn_from_doc(doc, max_pages=25, head_pages=5)` | 検出本体 |
| `resolve_isbn(args, doc)` | `--isbn`(正規化) → 自動検出 の解決。CLI 側の窓口 |
| `build_epub` :4488 | `re.sub(r"[-\s]",…)` → `normalize_isbn()`（現行バグ解消）|
| `run_from_text` / `main` の ePub 生成直前 | `isbn = resolve_isbn(args, doc)` を挟み `build_epub(isbn=isbn)` |
| argparse | `--no-isbn-detect` 追加、`--isbn` の help 更新 |

設計との差分（実装時の判断）:

- 検出は **ePub を生成するときだけ**呼ぶ（テキスト出力のみの実行にコストをかけない）。
- ログは `[isbn] …` ではなく本ツールの既存出力に合わせて `🔖` 表記にした。
- `resolve_isbn()` を別関数にしたことで、フェーズ2の `resolve_bib_meta()`（§5-5）は
  この関数を吸収する形に育てられる。

**検証結果（2026-07-25）**:

- `normalize_isbn` ユニット18件（接頭辞・全角ＩＳＢＮ・全角ハイフン・空白区切り・
  `urn:isbn:` 付き・ISBN-10→13・末尾X・価格コード `1929979004637`・チェック不正・
  `978/979` 以外の13桁・旧角川分類コード・空/None・冪等）**全件一致**。
- 検出を temp_sample **26ファイル**で実測 → **20件検出**。§2 の表と完全一致し、
  新規4冊（魔法使いの塔下・図南の翼・諸国そばの本・マンガの歴史1）も全件検出。
  検出した20件は**すべて NDL に実在し書名も一致**（誤検出0）。
- 回帰: 霧のむこうのふしぎな町で `--epub` 実行し、`--no-isbn-detect` 版と比較 →
  **テキスト出力は完全一致、ePub の差分は `<dc:source>` 1行のみ**（他は uuid と
  生成時刻の自然差）。
- E2E: `--from-text` 経路／`--isbn "ISBN978-4-06-148668-3"`（正規化）／不正値
  `--isbn 12345`（警告＋自動検出フォールバック）／`--no-isbn-detect`（出力なし）を確認。
  生成 OPF は全て XML well-formed。
- `--pages 20-40` で本文範囲を絞っても**奥付は PDF 全体の末尾から検出**できることを
  新規4冊で確認（設計どおり）。
- 実行コスト: 525ページの黒牢城で 0.25 秒（走査は最大25ページ分のテキスト抽出のみ）。

---

## 4. フェーズ2 仕様 — 発行日 / NDL / OPF 書誌タグ

### 4.1 `detect_pubdate_from_pdf` → `detect_pubdate_from_doc(doc, max_pages=20)`

manga 版に §2.1 の5点を足した jisui2epub 版。

```python
_K   = {"〇":0,"○":0,"零":0,"一":1,…,"十":10,"元":1}      # ○(U+25CB) を含めるのが要点
_ERA = {"大正":1911,"昭和":1925,"平成":1988,"令和":2018}
_D   = r"[0-9〇○零一二三四五六七八九十元]"
_PUBDATE = re.compile(
    r"(?:(昭和|平成|令和|大正)\s*(" + _D + r"{1,3})"     # 和暦（漢数字3文字まで）
    r"|((?:19|20)\d{2})"                                 # 西暦（ASCII）
    r"|([〇○零一二三四五六七八九]{4}))\s*年"              # 西暦（漢数字4桁）
    r"\s*(" + _D + r"{1,3})?\s*月"
    r"(?:\s*(" + _D + r"{1,3})\s*(?:日|.{0,2}(?:発行|発売|刷|初版)))?")
_FIRST   = re.compile(r"初版|新装版第1刷|新装版第一刷|第\s*1\s*刷|第\s*一\s*刷")
_DATE_KW = re.compile(r"発行|発売|刷|初版|印刷")
```

採用ルール（順に適用）:

1. compact 化したページテキストを**末尾から**走査し、`_DATE_KW` を含むページのみ対象。
2. 各マッチの前後窓（-10/+12字）に `発行|発売|刷|初版` が無ければ捨てる。
   直後が `印刷` の候補も捨てる（早川書房対策）。
3. 年が 1945〜2035 外なら捨てる。月が1〜12外なら年のみ。
   **日はその月の実日数を超えたら捨てて年月へ縮退**（2012-02-30 対策）。
4. `_FIRST` に一致する候補があればその中の最小日付、無ければ全候補の最小日付。
5. ログ `[date] auto-detected 2004-12-15 (page 212, 奥付)` / `[date] not found in colophon`。
6. **スキャン日・変換日は絶対に使わない**（変換日時は `dcterms:modified`）。

> **注（図南の翼で実測）**: 初版行の「発行」が化けると（`平成二十五年十月一III発`）その候補が
> ゲートに落ち、同じページの後刷（`令和元年八月三十日十…`）だけが残って **2019-08-30 を初版として
> 出してしまう**。キーワードゲートを `発` 単体まで緩めるのは誤爆が増えるので採らない。
> NDL は同 ISBN に `2013.10.1` を返すため、**§4.4 の「NDL > OCR」で解決する**
> （`--no-ndl` 運用ではこの型の WRONG が残ることを README に明記する）。

`--no-date-detect` で無効化。**誤った日付を出さない（安全側の失敗）** を最優先とし、
壊れた奥付では黙って何も出さない。

### 4.2 NDL（国立国会図書館サーチ）照会 — 実測 **20/20 ヒット**

`https://ndlsearch.ndl.go.jp/api/opensearch?isbn=<13桁>`（RSS/XML・認証不要・標準ライブラリのみ）。
検出できた20冊すべてでヒットした（manga は 7/9。文庫・新書中心の当ツールの方が索引カバレッジが高く、
私家版寄りの小出版社＝文盛堂書店の1冊も引けた）。

| 本 | 責任表示（自然形＋役割） | 読み | 版元 | 発行 | シリーズ | NDC |
|---|---|---|---|---|---|---|
| 遠まわりする雛 | 米澤穂信 [著] | ヨネザワ, ホノブ | 角川書店 | 2010.7 | 角川文庫 ; 16368 | 913.6 |
| 霧のむこうのふしぎな町 | 柏葉幸子 作 ; 杉田比呂美 絵 | カシワバ, サチコ / スギタ, ヒロミ | 講談社 | 2004.12 | — (volume=": 新装版") | 913.6 |
| 蘇我氏 : 古代豪族の興亡 | 倉本一宏 著 | クラモト, カズヒロ | 中央公論新社 | 2015.12 | 中公新書 ; 2353 | 210.3 |
| 黒牢城 | 米澤穂信 [著] | ヨネザワ, ホノブ | KADOKAWA | 2024.6 | 角川文庫 ; よ23-21 | 913.6 |
| ソフロニア嬢、倫敦で恋に陥落する | ゲイル・キャリガー 著, 川野靖子 訳 | カワノ, ヤスコ | 早川書房 | 2017.2 | ハヤカワ文庫FT ; 587 | 933.7 |
| 伯爵夫人は超能力 | ドロシー・ギルマン [著], 柳沢由実子 訳 | ヤナギサワ, ユミコ | 集英社 | 1999.4 | 集英社文庫 | 933.7 |
| ほんものの魔法使 | ポール・ギャリコ 著, 矢川澄子 訳 | ヤガワ, スミコ | 東京創元社 | 2021.5 | 創元推理文庫 | 933.7 |
| グリックの冒険 | 斎藤惇夫 作, 薮内正幸 画 | サイトウ, アツオ / ヤブウチ, マサユキ | 岩波書店 | 2000.7 | 岩波少年文庫 | 913 |
| 30秒でわかる！…50の理論 | L.ヴィッタート 編, 山形浩生 訳 | ヤマガタ, ヒロオ | 丸善出版 | 2025.10 | — | 417 |
| 地下室からのふしぎな旅 | 柏葉幸子 作 : 杉田比呂美 絵 | カシワバ, サチコ / スギタ, ヒロミ | 講談社 | 2006.4 | — (volume=": 新装版") | 913.6 |
| わかりやすい韮山反射炉の解説 : 平成27(2015)年世界文化遺産 | 堀内永人 著 | ホリウチ, ナガト | 文盛堂書店 | 2015.12 | — | — |
| 白銀(しろがね)の墟 玄(くろ)の月 | 小野不由美 著 | オノ, フユミ | 新潮社 | 2019.10 | 新潮文庫 ; お-37-62. 十二国記（volume=`第1巻`）| — |
| 風の万里黎明の空 | 小野不由美 著 | オノ, フユミ | 新潮社 | 2013.4 | 新潮文庫 ; お-37-56. 十二国記（volume=`上`）| — |
| 典座教訓・赴粥飯法 | 道元 [著], 中村璋八 [ほか]訳 | ドウゲン / ナカムラ, ショウハチ | 講談社 | 1991.7 | 講談社学術文庫 | — |
| シナリオのためのファンタジー事典 : … | 山北篤 著 | ヤマキタ, アツシ | SBクリエイティブ | 2019.7 | NEXT CREATOR | — |
| 哲夫の春休み | 斎藤惇夫 作, 金井田英津子 画 | サイトウ, アツオ / カナイダ, エツコ | 岩波書店 | 2010.10 | — | — |

移植する関数は manga と同じ（`fetch_ndl_by_isbn` / `_ndl_kana_title` / `_ndl_kana_name` /
`_ndl_name_key` / `_ndl_parse_responsibility`）。jisui2epub 固有の注意:

- **翻訳書・挿絵付きが多い**（NDL ヒット16冊中、訳者6・画家/絵4）。責任表示から `訳→trl` `絵/画→ill/art` を必ず分離する。
- **`dcndl:volume` は巻表示（`上`・`第1巻`）と版表示（`: 新装版`）の両方が来る**（§2.3 修正1）。
  先頭の `: ` を剥がし、`新装版|改訂|増補|新版|普及版` 等を含む値だけ捨てて、残りは巻として採用する。
- **責任表示に角括弧が混じる**（`道元 [著]` / `中村璋八 [ほか]訳`）。役割判定の前に
  `\[[^\]]*\]` を除去する（§2.3 修正2）。除かないと `dc:contributor` が `中村璋八[ほか]` になる。
- **`dcterms:issued` は `YYYY.M` だけでなく `YYYY.M.D(和暦)` も来る**（図南の翼=`2013.10.1(平25)`）。
  `(\d{4})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?` で日まで拾い、括弧以降は捨てる。
  この1件は OCR 側が後刷を拾って WRONG になる本（§4.1 注）なので、日まで取れる価値が大きい。
- `dc:title` は副題が `" : "` 区切りで入る（`蘇我氏 : 古代豪族の興亡`）。
  ファイル名（`蘇我氏一古代豪族の興亡`）と食い違うが、**本文の `dc:title` はファイル名優先**（§4.3）。
- `dcndl:creatorTranscription` は生年（`1978-`）や職業が付く → `_ndl_kana_name` で除去。
- 失敗（オフライン・未ヒット・タイムアウト）は**警告のみで続行**。`--no-ndl` で無効化。タイムアウト8秒・リトライなし。

### 4.3 ★ `dc:creator` を増やしてはならない（yomikake のしおりキー）

yomikake は `state.bookCreators = [...metadata > dc:creator]` を `'・'` で連結し、
`makeBookKey(title, creator)`（yomikake.html:7555）でしおりキーを作る。
**`dc:creator` を1つ増やすだけで既存のしおりが全部割れる。**
jisui2epub は校正しながら `--from-text` で**同じ本を何度も再生成する**運用なので、これは致命的。

決定:

| 役割 | 出力先 | 理由 |
|---|---|---|
| 著者（ファイル名/`--author`）| `dc:creator` id=creator01 role=aut | 従来と同一文字列＝しおりキー不変 |
| 訳者・画家・編者（NDL 由来）| **`dc:contributor`** role=trl/ill/art/edt | bookKey に影響しない |
| `--creator-source ndl` 指定時 | `dc:creator` を NDL 自然形に置換 | **しおりキーが変わる旨を警告ログに出す** |

既定は `--creator-source filename`。NDL はファイル名著者の**読み（file-as）補完**と
**追加役割者（contributor）**にだけ使う。ファイル名と NDL の表記が食い違えば警告のみ
（manga と同じ。実測: 押見修三/押見修造のような異体字がある）。

> yomikake 側は現在 `dc:contributor` を表示しない。訳者を書誌ブロックに出したければ
> yomikake 側の対応が要る（**別タスク**。ePub 側は先に正しく持たせておく）。

### 4.4 フィールド優先順位

| フィールド | 優先順 |
|---|---|
| `dc:title` | `--title` > ファイル名（**NDL は使わない**＝しおりキー安定）|
| `dc:creator`（本文）| `--author` > ファイル名 > (`--creator-source ndl` 時のみ NDL) |
| file-as（読み）| `--*-kana` > NDL |
| `dc:contributor` | NDL（`--no-ndl` なら出さない）|
| `dc:publisher` | `--publisher` > NDL |
| `dc:date` | `--date` > NDL `dcterms:issued` > OCR 奥付検出 > **出力しない** |
| `dc:source` | `--isbn`(正規化) > 自動検出 > 出力しない |
| `dc:subject`(NDC) / `dcndl:seriesTitle` | NDL のみ |
| `dc:description` | `--description` 手動のみ（自動取得は不可能。manga §5.6 と同結論：あらすじはカバー折返し＝自炊に含まれない）|

### 4.5 OPF 出力仕様

`_make_opf`（3761）のテンプレートに以下を追加する。`prefix` に `schema:` と `dcndl:` を足す。

```xml
<package … prefix="schema: http://schema.org/ dcndl: http://ndl.go.jp/dcndl/terms/">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:…</dc:identifier>
    <dc:title id="title">霧のむこうのふしぎな町</dc:title>
    <meta refines="#title" property="file-as">キリ ノ ムコウ ノ フシギナ マチ</meta>
    <dc:creator id="creator01">柏葉幸子</dc:creator>
    <meta refines="#creator01" property="role" scheme="marc:relators">aut</meta>
    <meta refines="#creator01" property="display-seq">1</meta>
    <meta refines="#creator01" property="file-as">カシワバ サチコ</meta>
    <dc:contributor id="contrib01">杉田比呂美</dc:contributor>
    <meta refines="#contrib01" property="role" scheme="marc:relators">ill</meta>
    <meta refines="#contrib01" property="file-as">スギタ ヒロミ</meta>
    <dc:publisher id="publisher">講談社</dc:publisher>
    <dc:language>ja</dc:language>
    <dc:date>2004-12-15</dc:date>
    <dc:source>urn:isbn:9784061486683</dc:source>
    <dc:subject>NDC 913.6</dc:subject>
    <!-- NDL が seriesTitle を返す本（例: 黒牢城）だけ次の1行を出す -->
    <!-- <meta property="dcndl:seriesTitle">角川文庫</meta> -->
    <meta property="dcterms:modified">…Z</meta>
    <!-- アクセシビリティ（§4.6）／rendition・primary-writing-mode は現状維持 -->
  </metadata>
```

**互換のための実装制約**: `_make_opf` / `build_epub` は novel_downloader.py からの移植コードで、
本家との差分は最小に保つ規約がある（CLAUDE.md）。したがって

- 書誌断片の組み立ては**新関数 `build_bib_meta()`（jisui2epub 独自）**に置く。
- `_make_opf` / `build_epub` への追加は **`bib: dict | None = None` の1引数だけ**。
  `bib is None`（既定）のとき出力は**従来と1バイト一致**させる（回帰ゼロ）。
- 検出・NDL 照会・優先順位解決は呼び出し側の新関数 `resolve_bib_meta(args, doc)` が行い、
  `build_epub` には**解決済みの値だけ**渡す（build_epub は PDF を知らないままにする）。

### 4.6 アクセシビリティ metadata（EPUB Accessibility 1.1 / schema.org）

リフロー型テキストの小説なので manga（visual 主体）とは出し分けが逆になる。

```xml
<meta property="schema:accessMode">textual</meta>
<meta property="schema:accessMode">visual</meta>              <!-- 挿絵/画像ページがある本のみ -->
<meta property="schema:accessModeSufficient">textual</meta>
<meta property="schema:accessibilityFeature">displayTransformability</meta>
<meta property="schema:accessibilityFeature">readingOrder</meta>
<meta property="schema:accessibilityFeature">structuralNavigation</meta>
<meta property="schema:accessibilityFeature">tableOfContents</meta>
<meta property="schema:accessibilityFeature">rubyAnnotations</meta>  <!-- --ruby aozora かつルビ出力あり -->
<meta property="schema:accessibilityHazard">none</meta>
<meta property="schema:accessibilitySummary">リフロー型の縦組みテキスト。…</meta>
```

- `accessModeSufficient` は **textual のみ**（挿絵が読めなくても本文は完結する）。
- 挿絵 `<img>` に代替テキストは無い（3374/3382 行は `alt` 無し、3549 はキャプション流用）ので
  `alternativeText` は主張しない。**将来 `alt=""` を明示的に付けるのは別タスク**（装飾画像扱い）。
- `--horizontal` でも内容は同じ（書字方向は `page-progression-direction` の担当）。
- 老眼・弱視ユーザー向けにリフローを選んでいる本ツールでは `displayTransformability` の宣言が実質的意味を持つ。

### 4.7 `dc:date` の生成日フォールバックを廃止

`_make_opf`:3872 の `dc_date = pub_date or today` をやめ、**`pub_date` が空なら `<dc:date>` を出さない**。
EPUB 3.2 で `dc:date` は任意、yomikake も `dc:date` を読んでいない（実装確認済み）ため影響なし。

### 4.8 `colophon.xhtml` の拡充（任意・低優先）

現状は「底本：「タイトル」自炊PDF／入力：jisui2epub.py／校正：未校正／作成：日付」
（`_make_colophon_xhtml`:3600）。青空文庫の奥付慣例に寄せて、判明した書誌を出す:

```
底本：「霧のむこうのふしぎな町」講談社青い鳥文庫
　　　2004年12月15日 新装版第1刷発行
　　　ISBN 978-4-06-148668-3
入力：jisui2epub.py（自炊PDFのOCRテキスト層より）
校正：未校正
作成：2026年7月25日
```

### 4.9 実装内容（2026-07-25 完了）

`jisui2epub.py` への追加・変更（`import calendar` 追加。`urllib`/`ElementTree` は関数内 import）:

| 追加/変更 | 内容 |
|---|---|
| `_kanji_num` / `_pubdate_to_iso` / `detect_pubdate_from_doc` | 奥付の発行日検出（§4.1）。`_PUBDATE_RE` は和暦3文字・漢数字西暦・日の採用条件つき |
| `fetch_ndl_by_isbn` ＋ `_ndl_kana_title`/`_ndl_kana_name`/`_ndl_name_key`/`_ndl_parse_responsibility`/`_ndl_volume`/`_ndl_issued_to_iso`/`_ndl_same_person`/`_fit_title_kana` | NDL 照会と正規化（§4.2）|
| `_ndl_plausible` | 引いた書誌がこの本のものか検証（下記・設計追加）|
| `_access_meta(has_images, has_ruby)` | アクセシビリティ metadata（§4.6）|
| `build_bib_meta(...)` | OPF metadata 断片の組み立て（§4.5）|
| `_resolve_creators(...)` | dc:creator（ファイル名優先）と dc:contributor（NDL の追加役割者）の決定（§4.3）|
| `resolve_bib_meta(args, doc, title, author, …)` | 優先順位の解決。フェーズ1の `resolve_isbn` を内部で呼ぶ |
| `_make_opf(..., bib=None)` | `bib` があれば書誌つき metadata＋`prefix`（schema:/dcndl:）。`bib=None` は従来出力のまま |
| `build_epub(..., bib=None)` | 素通し。奥付にも publisher/pub_date/isbn を渡す |
| `_pubdate_ja` / `_make_colophon_xhtml(..., publisher, pub_date, isbn)` | 奥付の書誌行（§4.8）|
| `_strip_method_tag` | 著者名末尾の6桁以上の数字（スキャン日）を除去（§2.3 修正3）|
| argparse | `--no-date-detect` / `--no-ndl` / `--creator-source` / `--title-kana` / `--author-kana` / `--publisher-kana` / `--description` |

実装時に足した仕様（実測で必要と判明した分）:

1. **NDL の年月＋奥付の日を合成する**。NDL の `dcterms:issued` は多くが `YYYY.M` 止まりだが、
   奥付OCRは日まで読めていることが多い。**奥付の日付が NDL の年月で始まる（＝同じ版）ときだけ**
   詳しい方を採用する（霧 2004-12＋15日→`2004-12-15`）。食い違うときは NDL を信じる。
2. **読みは「正規形」で英字判定する**（`fetch_ndl_by_isbn`）。責任表示の自然形は
   原著者も片仮名（`ゲイル・キャリガー`）なので、それで判定すると
   `dcndl:creatorTranscription`（訳者の読み）を原著者に付けてしまう。
   NDL の `dc:creator` 正規形（`Carriger, Gail`）で判定すると翻訳書5冊すべてで読みが正しい人に付く。
   **同じ不具合が mangaP2ePub の `fetch_ndl_by_isbn` にもある**（洋書の責任表示が英字表記のため
   temp_sample では顕在化しなかった）。
3. **同一人物の二重出力を防ぐ**（`_ndl_same_person`）。ファイル名の著者は
   `ゲイル・キャリガー_START` のように余分な語が付くことがあり、完全一致だけだと
   NDL 側の同一人物が contributor として二重に出た。キーの包含関係で同一人物とみなす。
4. **NDL の役割をこちらの creator に反映する**（表示名は変えない＝しおりキー不変）。
   `Ｌ．ヴィッタート編` は `aut` ではなく `edt` になる。
5. **`_ndl_plausible`: 引いた書誌が別の本なら丸ごと捨てる**。`--isbn` の打ち間違いで
   まったく違う本の読み・版元・刊行日が入るのを防ぐ。書名の類似（difflib 0.5）か
   著者一致のどちらかで採用。実測: 正しい20冊は全件採用（誤爆0）、
   洋書ISBNを故意に渡したケースは正しく棄却。
6. **書名の読みを dc:title の範囲に合わせる**（`_fit_title_kana`）。NDL の書名は副題を
   `" : "` で連ねるので、ファイル名由来の題に副題が無ければ読みも主題までにする
   （`ショコク ソバ ノ ホン : ソバ ノ サト ト ウマイ ミセ 250` → `ショコク ソバ ノ ホン`）。
7. **ファイル名に著者が無い本は NDL で補う**（諸国そばの本 → `そば道楽の会`）。
   この場合だけ dc:creator が空から変わるので、しおりキーが変わる旨をログに出す。

**検証結果（2026-07-25）**:

- **レガシー経路の完全一致**: `git show HEAD:jisui2epub.py` の `_make_opf` と
  現行の `_make_opf(bib=None)` を4通りの引数で比較し**全て1バイト一致**。
  `_make_colophon_xhtml` も既定引数で一致（novel_downloader.py との互換を維持）。
- **本文の不変**: 霧のむこうを全ページ変換し、フェーズ1の出力と**テキスト完全一致**・
  章見出し15で不変。ePub 内で変わるのは `package.opf` と `colophon.xhtml` のみ。
- **発行日検出**: 25ファイルで §2 の表を再現（EXACT 16・縮退2・WRONG 2・MISS 5)。
- **NDL 連携**: 20冊で照会・採用を確認。**図南の翼のフェーズ1 WRONG（2019-08-30＝後刷）は
  NDL 優先で `2013-10-01` に是正**された（設計どおり）。30秒DS は奥付OCRが MISS でも
  NDL から `2025-10` を取得。
- **E2E**: 翻訳書（trl）・挿絵つき（ill）・著者名なし（NDL補完）・ISBN無し（雪の女王＝
  奥付OCRのみ）・`--no-ndl`・`--creator-source ndl`・`--horizontal`＋`--ruby drop`・
  手動指定7種（--publisher/--date/--*-kana/--description）・`--no-images`。
  生成 OPF はすべて XML well-formed。
- **オフライン**: エンドポイントを到達不能にして `URLError` を発生させ、
  警告のみで変換が完走することを確認。
- **アクセシビリティの出し分け**: 画像ページあり→`visual` 追加、`--no-images`→textual のみ、
  `--ruby drop`→`rubyAnnotations` なし。

---

## 5. 実装単位と影響範囲

| # | 作業 | 触る場所 |
|---|---|---|
| 1 | ISBN ヘルパ群（`_isbn13_check_ok`/`_isbn10_check_ok`/`_isbn10_to_13`/`normalize_isbn`/`detect_isbn_from_doc`）を新設 | `parse_meta_from_filename`(2557) の直後に新セクション |
| 2 | `build_epub` の `re.sub(r"[-\s]",…)`(4488) を `normalize_isbn()` に置換 | 4488 |
| 3 | `detect_pubdate_from_doc`＋`_kanji_num`/`_pubdate_to_iso` 新設 | 同セクション |
| 4 | `fetch_ndl_by_isbn` ほか NDL 群を新設（`urllib`/`ElementTree` は関数内 import）| 同セクション |
| 5 | `resolve_bib_meta(args, doc)`＋`_resolve_creators()`＋`build_bib_meta()`＋`_access_meta()` を新設 | ePub3生成セクション先頭 |
| 6 | `_make_opf` に `bib=None` 引数、テンプレへ `{…_meta}` プレースホルダ、`prefix` 追加、dc:date 生成日廃止 | 3761-3909 |
| 7 | `build_epub` に `bib=None` を素通しで追加 | 4376-4498 |
| 8 | 2つの呼び出し側で `resolve_bib_meta()` を呼ぶ | `run_from_text`(4801) / `main`(4985) |
| 9 | argparse に新オプション（§6）、`--isbn`/`--date`/`--publisher` の help 更新 | 4813-4852 |
| 10 | `_make_colophon_xhtml` の書誌行（任意）| 3600 |
| 11 | `_strip_method_tag` に「著者名末尾の6桁以上の連続数字（スキャン日）除去」を追加（§2.3 修正3）| 2545 |
| 12 | README / CLAUDE.md 追記 | — |

`analyze_page` 以降の**本文パイプラインには一切触れない**（本文・ルビ・見出しの出力は不変）。

## 6. 追加 CLI オプション

| オプション | 既定 | 内容 |
|---|---|---|
| `--no-isbn-detect` | 検出ON | 奥付/裏表紙からの ISBN 自動検出を無効化 |
| `--no-date-detect` | 検出ON | 奥付からの発行日自動検出を無効化 |
| `--no-ndl` | 照会ON | NDL 書誌照会を無効化（オフライン時も自動で継続）|
| `--creator-source {filename,ndl}` | `filename` | `dc:creator` 本文の採用元。`ndl` 指定時はしおりキーが変わる旨を警告 |
| `--title-kana` / `--author-kana` / `--publisher-kana` | — | file-as の手動指定（NDL より優先）|
| `--description` | — | あらすじ（`dc:description`。現状 `synopsis=""` 固定を活かす）|

`--rights` は見送り（自炊本に自動生成できる権利表示はない）。

## 7. 検証計画

1. **ユニット**: `normalize_isbn`（`ISBN` 接頭辞・全角ハイフン・全角数字・ISBN-10→13・
   価格コード `192…` 除外・チェックディジット不正・冪等）／`_pubdate_to_iso`
   （漢数字西暦・和暦3桁・2月30日・日の化け）。
2. **検出の実測再現**: §2 の表を temp_sample 26ファイルで再現（ISBN 20件・日付 EXACT 16/縮退2/WRONG 2/MISS 6）。
   正解表は memory（`isbn-detect-ground-truth` の jisui2epub 版）に保存する。
3. **回帰（最重要）**: `bib=None` 経路で `_make_opf` の出力が既存と**1バイト一致**すること。
   `--no-ndl --no-isbn-detect --no-date-detect` で生成した ePub が従来版と一致すること
   （差分は `dc:date` 生成日の削除のみ）。
4. **本文不変**: 5冊で章見出し一覧（`grep 中見出し］`）・画像ページ検出数・ルビペア一致率が不変。
5. **E2E**: 霧（ISBN+日付+NDL・挿絵あり）／ソフロニア（翻訳・contributor trl）／
   グリック（旧OCR・巻末広告10ページ越しの検出・画家 ill）／30秒DS（`--horizontal`）／
   タイム・リープ上（全部 MISS でも壊れないこと）。生成 OPF の XML well-formed を確認。
6. **yomikake 実機**: 書誌ブロックに出版社・ISBN リンク（NDL サーチ）が出ること、
   **しおりキーが従来と一致**すること（`--creator-source filename` 既定時）。

## 8. 非目標（今回やらない）

- あらすじの自動取得（カバー折返し・帯は自炊に含まれない。`--description` 手動のみ）。
- `dc:rights`。
- `dc:identifier` を ISBN にすること（意味論上ここは `urn:uuid`）。
- 挿絵 `<img>` の `alt` 付与・`alternativeText` 主張（別タスク）。
- yomikake 側の `dc:contributor` 表示対応（別タスク）。
- `jisui_gui.py` への露出（新オプションはすべて既定ONなので**GUI は無改修で恩恵を受ける**。
  無効化スイッチが要るなら別タスク）。

## 9. 進捗

- [x] 実測調査（temp_sample 14ファイル・ISBN/発行日・NDL 10/10・誤検出0）— 2026-07-25
- [x] 仕様確定（§3・§4）— 2026-07-25
- [x] **第2次検証（ScanSnap サンプル7冊追加、§2.3）**: ISBN 6/7・発行日 6/7・誤り0・NDL 累計 16/16。
      仕様は妥当と確認、修正3点（volume の巻表示・責任表示の角括弧・ファイル名末尾のスキャン日）を反映 — 2026-07-25
- [x] **フェーズ1 実装完了**（2026-07-25、§3.5）: `normalize_isbn` / `detect_isbn_from_doc` /
      `resolve_isbn` 新設、`build_epub` の正規化バグ修正、`--no-isbn-detect` 追加。
      ユニット18件・26ファイル実測（20件検出・誤検出0）・回帰（ePub 差分は `dc:source` 1行のみ）
      ・E2E（--from-text／不正値フォールバック／--pages 範囲外の奥付）を確認。
- [x] 第3次検証（追加サンプル4冊: 魔法使いの塔下・図南の翼・諸国そばの本・マンガの歴史1）:
      ISBN 4/4 検出（NDL で書名一致）。発行日は EXACT2・WRONG1・MISS1 で、
      WRONG の型（初版行の「発行」化け）を §4.1 に注記。
- [x] **フェーズ2 実装完了**（2026-07-25、§4.9）: 奥付の発行日検出・NDL 照会・file-as・
      dc:contributor（訳者/画家）・NDC・シリーズ・アクセシビリティ・`--creator-source` ほか。
      実装時に7点（NDL年月＋奥付日の合成／読みの人物対応／同一人物の二重出力防止／役割反映／
      別書誌の棄却／書名読みの切り詰め／著者名のNDL補完）を仕様に追加。
- [x] 回帰検証（§7）: レガシー `_make_opf(bib=None)` は1バイト一致、本文テキストと章見出しは不変、
      ePub の差分は package.opf と colophon.xhtml のみ。オフライン・XML妥当性も確認。
- [x] README / CLAUDE.md 追記（フェーズ2分）
