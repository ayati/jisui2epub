#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自炊変換GUI — 自炊PDFをePubに変換するWindows向けランチャー。

jisui2epub / vision_reocr / docai_reocr / yomitoku_reocr / ndlocr_reocr /
manga_p2epub を子プロセスとして起動する薄いGUI（設計は DESIGN_WindowsGUI.md）。
ツール本体には一切手を入れず、公開CLIオプションの組み立てだけを行う。

依存: 標準ライブラリのみ（Tkinter）。tkinterdnd2 があればドラッグ&ドロップも
有効になる（無ければクリック選択のみに自動退化）。

使い方:
    python jisui_gui.py                     # GUI起動
    python jisui_gui.py --smoke             # UI構築の自動テスト（CI用）
    python jisui_gui.py --print-jobs 本.pdf --type horizontal --reocr
                                            # コマンド組み立ての確認（実行しない）

exe化（Windows上で）:
    pyinstaller --onefile --noconsole jisui_gui.py
    → forwindows/ の他のexeと同じフォルダに置くと自動で見つける
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

APP_NAME = "jisui2epubGUI"
IS_WIN = sys.platform.startswith("win")

TOOL_NAMES = ("jisui2epub", "vision_reocr", "docai_reocr",
              "yomitoku_reocr", "ndlocr_reocr", "manga_p2epub")

BOOK_TYPES = (
    ("novel", "小説（縦書き）"),
    ("horizontal", "横書きの本（実用書など）"),
    ("manga", "漫画（画像そのまま・固定レイアウト）"),
)

# 完了メッセージから出力パスを拾う（jisui2epub / manga_p2epub の実出力に対応）
_RE_TXT = re.compile(r"青空文庫形式テキスト出力:\s*(.+)")
_RE_EPUB = re.compile(r"ePub出力完了:\s*(.+)")
_RE_MANGA = re.compile(r"\[done\]\s+(.+?\.epub)")


# ── 設定 ────────────────────────────────────────────

def settings_path():
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(Path.home(), ".config")
    return Path(base) / APP_NAME / "settings.json"


DEFAULT_SETTINGS = {
    "font_size": 14,
    "book_type": "novel",
    "reocr": False,
    "reocr_engine": "yomitoku",   # yomitoku / ndlocr / vision / docai
    "python_path": "",            # yomitokuを入れたPython（手動指定用）
    "ndlocr_python": "",          # NDLOCRの依存を入れたPython（手動指定用）
    "ndlocr_dir": "",             # NDLOCR-Lite の clone 先
    "ruby_drop_horizontal": True,
    "gcp_json": "",
    "tool_paths": {},             # 名前 -> フルパス（手動上書き用）
    "last_dir": "",
    "last_job": None,             # {pdf, txt, epub, horizontal}
    "geometry": "",
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    p = settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ── ツール解決・コマンド組み立て（GUI非依存・--print-jobsでテスト可能） ──

def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _script_python(script: Path):
    """スクリプトのリポジトリの .venv があればそれを使う（開発時用）。"""
    for cand in (script.parent / ".venv" / "bin" / "python",
                 script.parent / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand)
    return sys.executable


def _has_modules(python_exe, modules):
    """そのPythonに modules が全部入っているか。

    import せず find_spec だけで判定する（yomitoku は torch を読むので
    import すると数秒、onnxruntime も同様に重い。find_spec なら数百ms）。
    """
    code = ("import importlib.util,sys;"
            "sys.exit(0 if all(importlib.util.find_spec(m) for m in %r) else 1)"
            % (list(modules),))
    try:
        r = subprocess.run(
            [python_exe, "-c", code],
            capture_output=True, timeout=30,
            creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0),
        )
        return r.returncode == 0
    except Exception:
        return False


def _has_yomitoku(python_exe):
    return _has_modules(python_exe, ("yomitoku",))


def _has_ndlocr_deps(python_exe):
    """NDLOCR-Lite を動かせるPythonか。NDLOCR本体は別途cloneするので
    見るのは依存ライブラリだけ（onnxruntime / opencv / PyYAML / PyMuPDF）。"""
    return _has_modules(python_exe, ("onnxruntime", "cv2", "yaml", "fitz"))


def _find_python(settings, has_func, manual_key):
    """条件を満たす Python を探す。見つからなければ None。

    設定の手動パス → 同フォルダの .venv → 実行中のPython（開発時）→
    PATH上の py/python の順に見る。
    """
    cands = []
    manual = settings.get(manual_key, "")
    if manual:
        cands.append(manual)
    here = app_dir()
    cands += [str(here / ".venv" / "bin" / "python"),
              str(here / ".venv" / "Scripts" / "python.exe")]
    if not getattr(sys, "frozen", False):
        cands.append(sys.executable)
    cands += ["py", "python", "python3"] if IS_WIN else ["python3", "python"]
    for c in cands:
        if os.path.sep in c and not Path(c).exists():
            continue
        if has_func(c):
            return c
    return None


def find_yomitoku_python(settings):
    """yomitoku が入っている Python を探す。見つからなければ None。

    **yomitoku_reocr.py は exe 化してはならない**（PyInstallerがyomitoku本体の
    コードをexeに取り込み、CC BY-NC-SA素材の再配布物になってしまう。本リポジトリ
    のリリースzipはMITなので表記が矛盾する）。そのためGUIからは常に
    「ユーザーのPython＋同梱の.py」という形で起動する。詳細はDESIGN_YomiToku.md §1。
    """
    return _find_python(settings, _has_yomitoku, "python_path")


def find_ndlocr_python(settings):
    """NDLOCRの依存が入っている Python を探す。見つからなければ None。

    **こちらは yomitoku と違いライセンス上の制約ではない**（NDLOCR-Lite は
    CC BY 4.0 で、そもそも本ツールに同梱しない）。exe を探さないのは
    実務上の理由で、(1) NDLOCR本体は利用者が別途cloneした外部フォルダに
    あり実行時にsys.pathへ載せる方式なので exe 化の利点が薄い、
    (2) onnxruntime と opencv を同梱すると exe が肥大する、の2点。
    """
    return _find_python(settings, _has_ndlocr_deps, "ndlocr_python")


# 再OCRエンジンの選択肢。YomiTokuを先頭＝既定にしている。Google Cloudは
# アカウント作成・クレジットカード登録・毎月の請求という敷居があり、
# 一般ユーザーが最初に試す先としては重いため
ENGINE_CHOICES = ("yomitoku", "ndlocr", "vision", "docai")


# インストール用のpipコマンド。ダイアログの「コピー」ボタンで
# クリップボードに入れる（メッセージ全文からの手作業コピーは実機で
# 「行ごとに貼り直す」手間が発生したため）
YOMITOKU_PIP_COMMANDS = (
    "pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision\n"
    "pip install yomitoku pymupdf"
)

# yomitoku_reocr.py が動くのに同じフォルダに必要な.py。書き戻しの
# ヒューリスティックを vision_reocr.py と、ページ解析を jisui2epub.py と
# 共有しているため（二重実装による乖離を防ぐ設計。docai_reocr.pyと同じ）
YOMITOKU_REQUIRED_SCRIPTS = ("yomitoku_reocr.py", "vision_reocr.py", "jisui2epub.py")

YOMITOKU_INSTALL_HELP = (
    "YomiToku（無料・オフラインの再OCR）を使うには、お使いのPythonに\n"
    "YomiTokuをインストールしてください。\n"
    "\n"
    "1) https://www.python.org/downloads/windows/ からPythonを入れる\n"
    "   （インストーラーの「Add python.exe to PATH」に必ずチェック）\n"
    "\n"
    "2) PowerShellを開いて、次の2行を順番に実行する\n"
    "   （下の「pipコマンドをコピー」ボタンでコピーできます）\n"
    "\n"
    f"{YOMITOKU_PIP_COMMANDS}\n"
    "\n"
    "※ 1行目を省くとCUDA版（約3GB）がダウンロードされます。必ず先に実行してください。\n"
    "※ 初回の変換時にモデルファイル（数百MB）が自動ダウンロードされます。\n"
    "\n"
    "3) 次の3つのファイルを、jisui_gui.exe と同じフォルダに置く\n"
    "     " + " / ".join(YOMITOKU_REQUIRED_SCRIPTS) + "\n"
    "   （YomiTokuはライセンス上exeに同梱できないため、スクリプトのまま\n"
    "     動かします。3つとも必要で、互いをimportして使います）\n"
    "\n"
    "【ライセンス】YomiToku は CC BY-NC-SA 4.0（非商用）です。\n"
    "個人の自炊・学術研究には無償で使えますが、業務・商用では使えません。\n"
    "商用でお使いの場合は Vision / Document AI を選んでください。\n"
    "YomiToku (c) 2024 by Kotaro Kinoshita / CC BY-NC-SA 4.0"
)


def missing_yomitoku_scripts():
    """yomitoku_reocr.py の実行に必要で、同じフォルダに無い.pyを返す。"""
    here = app_dir()
    return [n for n in YOMITOKU_REQUIRED_SCRIPTS if not (here / n).exists()]


# ── NDLOCR-Lite（無料・オフライン・商用可）────────────────────

# ndlocr_reocr.py も yomitoku と同様に同じフォルダの.pyをimportして使う
NDLOCR_REQUIRED_SCRIPTS = ("ndlocr_reocr.py", "vision_reocr.py", "jisui2epub.py")

NDLOCR_PIP_COMMANDS = "pip install onnxruntime opencv-python-headless numpy PyYAML pymupdf"

# 動作確認済みの版。ここが変わると静かに精度が落ちる経路があるため明示する
NDLOCR_TESTED_COMMIT = "36d7c4d"

NDLOCR_INSTALL_HELP = (
    "NDLOCR-Lite（無料・オフライン・商用利用も可の再OCR）を使うには、\n"
    "**初期設定が2つ**必要です。詳しい手順は同梱の\n"
    "  README_NDLOCR.md（Windows向け導入ガイド）\n"
    "に画面つきでまとめてあります。ここでは要点だけ示します。\n"
    "\n"
    "──【1】必要なライブラリを入れる ──────────────\n"
    "\n"
    "PowerShellを開いて次の1行を実行します\n"
    "（下の「pipコマンドをコピー」ボタンでコピーできます）\n"
    "\n"
    f"{NDLOCR_PIP_COMMANDS}\n"
    "\n"
    "※ YomiTokuと違って PyTorch は不要です（約200MBで済みます）。\n"
    "\n"
    "──【2】NDLOCR-Lite 本体をダウンロードする ──────\n"
    "\n"
    "  https://github.com/ndl-lab/ndlocr-lite\n"
    "\n"
    "を開き、緑の「Code」ボタン →「Download ZIP」で取得して展開します\n"
    "（Gitがあれば git clone でも可）。モデルファイルは同梱されているので\n"
    "別途ダウンロードは不要です。\n"
    "\n"
    "★★ 置き場所に注意 ★★\n"
    "  NDLOCR-Lite は、フォルダのパスに日本語（全角文字）が入っていると\n"
    "  起動しません。C:\\ndlocr-lite のような場所に置いてください。\n"
    "  Windowsのユーザー名が日本語の方は、デスクトップやドキュメントの\n"
    "  下に置くとパスに日本語が混ざるので特にご注意ください。\n"
    "\n"
    "展開したら、この画面の「NDLOCR-Liteフォルダを選ぶ」ボタンで\n"
    "そのフォルダ（直下に src フォルダがあるもの）を指定してください。\n"
    "\n"
    "──【3】このフォルダに置くファイル ─────────────\n"
    "\n"
    "     " + " / ".join(NDLOCR_REQUIRED_SCRIPTS) + "\n"
    "  （3つとも必要で、互いをimportして使います）\n"
    "\n"
    "──【できること・できないこと】────────────────\n"
    "\n"
    "○ 完全無料・完全オフライン。アカウント登録もカード登録も不要\n"
    "○ 商用利用もできます（CC BY 4.0）\n"
    "× ルビ（ふりがな）はまだ苦手です（正解率65〜80%）。ルビの多い本を\n"
    "  きれいに残したいなら Vision を選んでください\n"
    "× 時代小説・古典には向きません（難しい漢字を似た字に置き換えます）\n"
    "\n"
    f"※ 動作確認しているのは NDLOCR-Lite の commit {NDLOCR_TESTED_COMMIT}"
    "（2026-08-04）です。\n"
    "  更新したら数ページ変換して出力を確認してください。\n"
    "\n"
    "【ライセンス】NDLOCR-Lite (c) 国立国会図書館（NDLラボ）/ CC BY 4.0\n"
    "https://github.com/ndl-lab/ndlocr-lite\n"
    "出典を表示すれば商用利用も可能です。"
)


def missing_ndlocr_scripts():
    """ndlocr_reocr.py の実行に必要で、同じフォルダに無い.pyを返す。"""
    here = app_dir()
    return [n for n in NDLOCR_REQUIRED_SCRIPTS if not (here / n).exists()]


def check_ndlocr_dir(path):
    """NDLOCR-Lite のフォルダとして使えるか。使えれば ""、駄目なら理由。

    ndlocr_reocr.py 側と同じ検査をGUIでも先にやる（実行してから
    落ちるより、設定した時点で分かるほうが親切なため）。
    """
    if not path:
        return "NDLOCR-Liteのフォルダが未設定です"
    p = Path(path)
    if not p.is_dir():
        return f"フォルダが見つかりません: {path}"
    if not (p / "src").is_dir():
        return ("そのフォルダに src がありません。ZIPを展開すると\n"
                "ndlocr-lite-main の中にもう一段 ndlocr-lite-main が\n"
                "できることがあります。src が直下にあるフォルダを選んでください")
    if any(ord(ch) > 0x7F for ch in os.path.abspath(str(p))):
        return ("パスに日本語（全角文字）が含まれています。NDLOCR-Liteは\n"
                "この場合起動しません。C:\\ndlocr-lite のような\n"
                "半角英数字だけのパスに移動してください")
    return ""


def resolve_tool(name, settings):
    """ツール名 → 起動コマンド（リスト）を解決する。見つからなければ None。

    優先順: 設定の手動パス → GUIと同じフォルダのexe → 同フォルダの.py →
    開発時の既知の場所（mangaは隣のリポジトリ）。

    yomitoku_reocr だけは例外で、exeを一切探さない（上記 find_yomitoku_python
    のコメント参照）。ユーザーのPythonと同梱の.pyを組み合わせて起動する。
    """
    manual = settings.get("tool_paths", {}).get(name, "")
    if manual and Path(manual).exists():
        p = Path(manual)
        return [str(p)] if p.suffix.lower() == ".exe" else [_script_python(p), str(p)]

    here = app_dir()

    if name == "yomitoku_reocr":
        # 3つの.pyが揃っていないと import で落ちるので、起動前に確認する
        # （実機で「yomitoku_reocr.pyだけコピーして ModuleNotFoundError」が発生）
        if missing_yomitoku_scripts():
            return None
        py = find_yomitoku_python(settings)
        return [py, str(here / "yomitoku_reocr.py")] if py else None

    if name == "ndlocr_reocr":
        # yomitoku と同じく exe を探さず「ユーザーのPython＋同梱の.py」で起動する
        # （理由は find_ndlocr_python のコメント参照。ライセンス上の制約ではない）
        if missing_ndlocr_scripts():
            return None
        py = find_ndlocr_python(settings)
        return [py, str(here / "ndlocr_reocr.py")] if py else None

    script_name = "manga_p2epub.py" if name == "manga_p2epub" else f"{name}.py"
    exes = [here / f"{name}.exe", here / "forwindows" / f"{name}.exe"]
    scripts = [
        here / script_name,
        here.parent / "mangaP2ePub" / script_name if name == "manga_p2epub" else None,
    ]
    # Windowsはexe優先（Python環境不要）、開発環境（WSL等）は.py優先
    candidates = exes + scripts if IS_WIN else scripts + exes
    for c in candidates:
        if c is None or not c.exists():
            continue
        if c.suffix.lower() == ".exe":
            return [str(c)]
        return [_script_python(c), str(c)]
    return None


def parse_meta_from_filename(path):
    """「タイトル_著者名.pdf」→ (title, author)。jisui2epub と同一仕様
    （最初の _ を優先、次いで -。GUIはPyMuPDF非依存のため複製している）。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    for sep in ("_", "-"):
        if sep in stem:
            title, _, author = stem.partition(sep)
            title, author = title.strip(), author.strip()
            if title and author:
                return title, author
    return stem.strip(), ""


class Job:
    """1つの子プロセス実行。expect_* は完了後のフォールバック出力パス。"""

    def __init__(self, label, argv, env_extra=None,
                 expect_txt="", expect_epub=""):
        self.label = label
        self.argv = argv
        self.env_extra = env_extra or {}
        self.expect_txt = expect_txt
        self.expect_epub = expect_epub

    def __repr__(self):
        return f"[{self.label}] {subprocess.list2cmdline(self.argv)}"


def build_jobs(cfg, settings):
    """設定 dict からジョブ列を組み立てる。

    cfg: pdf, book_type(novel/horizontal/manga), title, author,
         reocr(bool), reocr_engine, reocr_start, reocr_end,
         ruby_drop(bool), pages, cover_page, direction, quality
    戻り値: (jobs, エラーメッセージ or "")
    """
    pdf = Path(cfg["pdf"])
    if not pdf.exists():
        return [], f"PDFが見つかりません: {pdf}"
    btype = cfg["book_type"]
    title = (cfg.get("title") or "").strip()
    author = (cfg.get("author") or "").strip()
    jobs = []

    src_pdf = pdf
    if cfg.get("reocr") and btype != "manga":
        engine = cfg.get("reocr_engine") or "yomitoku"
        tool = resolve_tool(f"{engine}_reocr", settings)
        if tool is None:
            if engine == "yomitoku":
                lack = missing_yomitoku_scripts()
                if lack:
                    return [], (
                        "YomiTokuの実行に必要なファイルが足りません。\n"
                        f"次のファイルを {app_dir()} に置いてください:\n"
                        "    " + " / ".join(lack) + "\n\n"
                        "（yomitoku_reocr.py は vision_reocr.py と jisui2epub.py の\n"
                        "　処理を共有しているため、3つとも必要です）"
                    )
                return [], ("YomiTokuが見つかりません。\n\n" + YOMITOKU_INSTALL_HELP)
            if engine == "ndlocr":
                lack = missing_ndlocr_scripts()
                if lack:
                    return [], (
                        "NDLOCRの実行に必要なファイルが足りません。\n"
                        f"次のファイルを {app_dir()} に置いてください:\n"
                        "    " + " / ".join(lack) + "\n\n"
                        "（ndlocr_reocr.py は vision_reocr.py と jisui2epub.py の\n"
                        "　処理を共有しているため、3つとも必要です）"
                    )
                return [], ("NDLOCRを動かすライブラリが見つかりません。\n\n"
                            + NDLOCR_INSTALL_HELP)
            return [], f"{engine}_reocr が見つかりません（設定でパスを指定してください）"
        env_extra = {}
        if engine not in ("yomitoku", "ndlocr"):
            # Vision / DocAI はGoogle Cloudの認証が要る。ローカル2種は不要
            gcp = settings.get("gcp_json", "")
            if not gcp or not Path(gcp).exists():
                return [], ("再OCRにはGoogle Cloudの認証JSONが必要です。"
                            "「認証JSONを選ぶ」で設定してください")
            env_extra["GOOGLE_APPLICATION_CREDENTIALS"] = gcp
        argv = tool + [str(pdf)]
        if engine == "ndlocr":
            # NDLOCR本体は別途cloneした外部フォルダ。設定した時点で検査済みだが
            # フォルダが消えている・移動した場合もあるのでここでも見る
            ndl_dir = settings.get("ndlocr_dir", "")
            err = check_ndlocr_dir(ndl_dir)
            if err:
                return [], ("NDLOCR-Liteのフォルダの設定に問題があります。\n\n"
                            + err + "\n\n"
                            "「NDLOCR-Liteフォルダを選ぶ」で指定し直してください。\n"
                            "手順は README_NDLOCR.md を参照してください。")
            argv += ["--ndlocr-dir", ndl_dir]
        if cfg.get("reocr_start"):
            argv += ["--start", str(cfg["reocr_start"])]
        if cfg.get("reocr_end"):
            argv += ["--end", str(cfg["reocr_end"])]
        out_pdf = pdf.with_name(f"{pdf.stem}_{engine}.pdf")
        jobs.append(Job(f"再OCR（{engine}）", argv, env_extra=env_extra))
        src_pdf = out_pdf

    if btype == "manga":
        tool = resolve_tool("manga_p2epub", settings)
        if tool is None:
            return [], "manga_p2epub が見つかりません（設定でパスを指定してください）"
        argv = tool + [str(src_pdf), "--force"]
        if title:
            argv += ["--title", title]
        if author:
            argv += ["--author", author]
        if cfg.get("direction") in ("rtl", "ltr"):
            argv += ["--direction", cfg["direction"]]
        if cfg.get("quality"):
            argv += ["--quality", str(cfg["quality"])]
        base = f"{title}_{author}" if author else (title or src_pdf.stem)
        jobs.append(Job("漫画ePub変換", argv,
                        expect_epub=str(src_pdf.with_name(base + ".epub"))))
        return jobs, ""

    tool = resolve_tool("jisui2epub", settings)
    if tool is None:
        return [], "jisui2epub が見つかりません（設定でパスを指定してください）"
    argv = tool + [str(src_pdf), "--epub"]
    if title:
        argv += ["--title", title]
    if author:
        argv += ["--author", author]
    if btype == "horizontal":
        argv += ["--horizontal"]
    if cfg.get("ruby_drop"):
        argv += ["--ruby", "drop"]
    if cfg.get("pages"):
        argv += ["--pages", cfg["pages"]]
    if cfg.get("cover_page") not in (None, ""):
        argv += ["--cover-page", str(cfg["cover_page"])]
    base = f"{title}_{author}" if author else (title or src_pdf.stem)
    out_base = src_pdf.parent / base
    jobs.append(Job("横書き変換" if btype == "horizontal" else "縦書き変換",
                    argv,
                    expect_txt=str(out_base) + ".txt",
                    expect_epub=str(out_base) + ".epub"))
    return jobs, ""


def build_regen_job(last_job, settings):
    """校正済みテキストからePubを再生成するジョブ。"""
    tool = resolve_tool("jisui2epub", settings)
    if tool is None or not last_job:
        return None
    pdf, txt = last_job.get("pdf", ""), last_job.get("txt", "")
    if not (pdf and txt and Path(pdf).exists() and Path(txt).exists()):
        return None
    argv = tool + [pdf, "--from-text", txt]
    if last_job.get("horizontal"):
        argv += ["--horizontal"]
    return Job("校正済みテキストからePub再生成", argv,
               expect_epub=str(Path(txt).with_suffix(".epub")))


def decode_line(raw: bytes) -> str:
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ── 実行スレッド ──────────────────────────────────────

class JobRunner(threading.Thread):
    """ジョブ列を順に実行し、出力行を queue に流す。"""

    def __init__(self, jobs, out_q):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.q = out_q
        self.proc = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def run(self):
        results = {"txt": "", "epub": "", "rc": 0}
        for job in self.jobs:
            if self.cancelled:
                break
            self.q.put(("stage", job.label))
            self.q.put(("log", f"▶ {job.label}: "
                               f"{subprocess.list2cmdline(job.argv)}\n"))
            env = os.environ.copy()
            # パイプ起動時にツール側stdoutがcp932になり✅等の絵文字で
            # UnicodeEncodeErrorに落ちるのを防ぐ（Python製exeはこの環境
            # 変数を尊重する。GUI側のデコードはutf-8優先なので整合する）
            env["PYTHONIOENCODING"] = "utf-8:replace"
            env.update(job.env_extra)
            flags = 0x08000000 if IS_WIN else 0   # CREATE_NO_WINDOW
            try:
                self.proc = subprocess.Popen(
                    job.argv, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env,
                    creationflags=flags)
            except OSError as e:
                self.q.put(("log", f"起動エラー: {e}\n"))
                results["rc"] = -1
                break
            for raw in self.proc.stdout:
                line = decode_line(raw)
                self.q.put(("log", line))
                m = _RE_TXT.search(line)
                if m:
                    results["txt"] = m.group(1).strip()
                m = _RE_EPUB.search(line) or _RE_MANGA.search(line)
                if m:
                    results["epub"] = m.group(1).strip().split(" (")[0]
            rc = self.proc.wait()
            if self.cancelled:
                self.q.put(("log", "⏹ 中止しました\n"))
                results["rc"] = -2
                break
            if rc != 0:
                self.q.put(("log", f"✖ エラー終了（コード {rc}）\n"))
                results["rc"] = rc
                break
            # ログから拾えなかった場合は既定の出力パスで補完
            if not results["txt"] and job.expect_txt and \
                    Path(job.expect_txt).exists():
                results["txt"] = job.expect_txt
            if not results["epub"] and job.expect_epub and \
                    Path(job.expect_epub).exists():
                results["epub"] = job.expect_epub
        self.q.put(("done", results))


# ── GUI ─────────────────────────────────────────────

def open_path(path):
    if not path:
        return
    if IS_WIN:
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, font as tkfont
        self.tk, self.ttk = tk, ttk
        self.root = root
        self.settings = load_settings()
        self.q = queue.Queue()
        self.runner = None
        self.pdf_path = ""
        self._dropped_paths = []   # Win32 D&Dコールバック→poll() の受け渡し

        root.title("自炊PDF → ePub 変換")
        if self.settings.get("geometry"):
            try:
                root.geometry(self.settings["geometry"])
            except tk.TclError:
                pass

        # フォント（弱視対応: 既定14pt・±ボタンで変更、設定に保存）
        size = int(self.settings.get("font_size", 14))
        self.ui_font = tkfont.Font(family="", size=size)
        self.big_font = tkfont.Font(family="", size=size + 4, weight="bold")
        self.log_font = tkfont.Font(family="", size=max(size - 3, 9))
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass
        style = ttk.Style(root)
        style.configure(".", font=self.ui_font)
        style.configure("Big.TButton", font=self.big_font, padding=8)

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)

        # ── ヘッダ（文字サイズ） ──
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="自炊PDF → ePub 変換",
                  font=self.big_font).pack(side="left")
        ttk.Button(head, text="文字 −", width=7,
                   command=lambda: self.change_font(-1)).pack(side="right")
        ttk.Button(head, text="文字 ＋", width=7,
                   command=lambda: self.change_font(+1)).pack(side="right")

        # ── ファイル選択（ドロップ枠） ──
        self.drop = tk.Label(
            outer, text="ここにPDFをドラッグ＆ドロップ（ウィンドウ内どこでも可）\n"
                        "（またはクリックしてファイルを選ぶ）",
            relief="ridge", bd=2, height=3, font=self.ui_font,
            cursor="hand2")
        self.drop.pack(fill="x", pady=(10, 4))
        self.drop.bind("<Button-1>", lambda e: self.pick_file())
        self._setup_dnd()

        self.file_var = tk.StringVar(value="ファイル: （未選択）")
        ttk.Label(outer, textvariable=self.file_var,
                  wraplength=640).pack(fill="x")

        meta = ttk.Frame(outer)
        meta.pack(fill="x", pady=4)
        ttk.Label(meta, text="タイトル:").pack(side="left")
        self.title_var = tk.StringVar()
        ttk.Entry(meta, textvariable=self.title_var, width=26,
                  font=self.ui_font).pack(side="left", padx=(2, 10))
        ttk.Label(meta, text="著者:").pack(side="left")
        self.author_var = tk.StringVar()
        ttk.Entry(meta, textvariable=self.author_var, width=16,
                  font=self.ui_font).pack(side="left", padx=2)

        # ── 本の種類 ──
        box = ttk.LabelFrame(outer, text="本の種類", padding=6)
        box.pack(fill="x", pady=6)
        self.type_var = tk.StringVar(value=self.settings.get("book_type",
                                                             "novel"))
        for key, label in BOOK_TYPES:
            ttk.Radiobutton(box, text=label, value=key,
                            variable=self.type_var,
                            command=self.on_type_change).pack(anchor="w")

        self.reocr_var = tk.BooleanVar(value=bool(self.settings.get("reocr")))
        self.reocr_chk = ttk.Checkbutton(
            outer, text="先に再OCRで文字を読み直す（高精度化）",
            variable=self.reocr_var, command=self.on_reocr_toggle)
        self.reocr_chk.pack(anchor="w", pady=(0, 4))

        # ── 詳細設定（折りたたみ） ──
        self.adv_open = False
        self.adv_btn = ttk.Button(outer, text="▸ 詳細設定（ふだんは触らなくてよい）",
                                  command=self.toggle_adv)
        self.adv_btn.pack(anchor="w")
        self.adv = ttk.Frame(outer, padding=(16, 4, 0, 4))
        self._build_advanced()

        # ── 実行 ──
        self.run_btn = ttk.Button(outer, text="▶ 変換開始",
                                  style="Big.TButton", command=self.on_run)
        self.run_btn.pack(fill="x", pady=8)

        self.stage_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.stage_var).pack(fill="x")
        self.prog = ttk.Progressbar(outer, mode="indeterminate")
        self.prog.pack(fill="x", pady=(0, 4))

        self.log = tk.Text(outer, height=12, font=self.log_font,
                           state="disabled", wrap="none")
        self.log.pack(fill="both", expand=True)

        # ── 完了後のボタン ──
        after = ttk.Frame(outer)
        after.pack(fill="x", pady=(6, 0))
        self.btn_epub = ttk.Button(after, text="📖 ePubを開く",
                                   command=lambda: open_path(self.result_epub),
                                   state="disabled")
        self.btn_epub.pack(side="left", padx=2)
        self.btn_folder = ttk.Button(after, text="📁 フォルダを開く",
                                     command=self.open_folder,
                                     state="disabled")
        self.btn_folder.pack(side="left", padx=2)
        self.btn_txt = ttk.Button(after, text="✏ テキストを校正する",
                                  command=lambda: open_path(self.result_txt),
                                  state="disabled")
        self.btn_txt.pack(side="left", padx=2)
        self.btn_regen = ttk.Button(after, text="↻ 校正済みからePub再生成",
                                    command=self.on_regen, state="disabled")
        self.btn_regen.pack(side="left", padx=2)

        self.result_txt = ""
        self.result_epub = ""
        self._restore_last_job()
        self.on_type_change()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)

    # ── 詳細設定 ──
    def _build_advanced(self):
        tk, ttk = self.tk, self.ttk
        s = self.settings
        f = self.adv
        # 小説/横書き
        self.novel_frame = ttk.Frame(f)
        r1 = ttk.Frame(self.novel_frame)
        r1.pack(anchor="w")
        ttk.Label(r1, text="ページ範囲(例 10-360):").pack(side="left")
        self.pages_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.pages_var, width=10,
                  font=self.ui_font).pack(side="left", padx=(2, 10))
        ttk.Label(r1, text="表紙ページ:").pack(side="left")
        self.cover_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.cover_var, width=4,
                  font=self.ui_font).pack(side="left", padx=2)
        self.ruby_var = tk.BooleanVar(value=False)
        self.ruby_chk = ttk.Checkbutton(
            self.novel_frame, text="ルビを付けない（--ruby drop。実用書向け）",
            variable=self.ruby_var)
        self.ruby_chk.pack(anchor="w")
        # 漫画
        self.manga_frame = ttk.Frame(f)
        r2 = ttk.Frame(self.manga_frame)
        r2.pack(anchor="w")
        ttk.Label(r2, text="綴じ方向:").pack(side="left")
        self.dir_var = tk.StringVar(value="rtl")
        ttk.Combobox(r2, textvariable=self.dir_var, width=12,
                     values=("rtl", "ltr"), state="readonly",
                     font=self.ui_font).pack(side="left", padx=(2, 10))
        ttk.Label(r2, text="JPEG品質:").pack(side="left")
        self.quality_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.quality_var, width=4,
                  font=self.ui_font).pack(side="left", padx=2)
        # 再OCR
        self.reocr_frame = ttk.Frame(f)
        r3 = ttk.Frame(self.reocr_frame)
        r3.pack(anchor="w")
        ttk.Label(r3, text="再OCRエンジン:").pack(side="left")
        self.engine_var = tk.StringVar(value=s.get("reocr_engine", "yomitoku"))
        cb = ttk.Combobox(r3, textvariable=self.engine_var, width=20,
                          values=ENGINE_CHOICES,
                          state="readonly", font=self.ui_font)
        cb.pack(side="left", padx=(2, 10))
        cb.bind("<<ComboboxSelected>>", lambda e: self.on_engine_change())
        ttk.Label(r3, text="開始/終了ページ:").pack(side="left")
        self.rstart_var = tk.StringVar()
        ttk.Entry(r3, textvariable=self.rstart_var, width=5,
                  font=self.ui_font).pack(side="left", padx=2)
        self.rend_var = tk.StringVar()
        ttk.Entry(r3, textvariable=self.rend_var, width=5,
                  font=self.ui_font).pack(side="left", padx=2)

        # YomiToku用（無料・オフライン）
        self.yomi_frame = ttk.Frame(self.reocr_frame)
        y1 = ttk.Frame(self.yomi_frame)
        y1.pack(anchor="w", pady=2)
        self.yomi_var = tk.StringVar()
        ttk.Button(y1, text="インストール手順",
                   command=self.show_yomitoku_help).pack(side="left")
        ttk.Button(y1, text="Pythonを選ぶ",
                   command=self.pick_python).pack(side="left", padx=4)
        ttk.Label(y1, textvariable=self.yomi_var,
                  wraplength=420).pack(side="left", padx=6)
        ttk.Label(
            self.yomi_frame,
            text="※ YomiTokuは非商用ライセンス（CC BY-NC-SA 4.0）です。"
                 "個人の自炊・研究は無償、業務利用は不可。\n"
                 "　 時代小説・古典は稀用漢字に弱いため Vision / Document AI を"
                 "おすすめします。",
            wraplength=640, foreground="#a05000").pack(anchor="w")

        # NDLOCR用（無料・オフライン・商用可）。**初期設定が要ることを明示する**
        self.ndl_frame = ttk.Frame(self.reocr_frame)
        n1 = ttk.Frame(self.ndl_frame)
        n1.pack(anchor="w", pady=2)
        self.ndl_var = tk.StringVar()
        ttk.Button(n1, text="初期設定の手順",
                   command=self.show_ndlocr_help).pack(side="left")
        ttk.Button(n1, text="NDLOCR-Liteフォルダを選ぶ",
                   command=self.pick_ndlocr_dir).pack(side="left", padx=4)
        ttk.Button(n1, text="Pythonを選ぶ",
                   command=self.pick_ndlocr_python).pack(side="left")
        ttk.Label(n1, textvariable=self.ndl_var,
                  wraplength=380).pack(side="left", padx=6)
        ttk.Label(
            self.ndl_frame,
            text="※ 使う前に初期設定が必要です（ライブラリのインストールと、"
                 "NDLOCR-Lite本体のダウンロード）。\n"
                 "　 手順は同梱の README_NDLOCR.md、または上の"
                 "「初期設定の手順」ボタンをご覧ください。\n"
                 "　 NDLOCR-Liteを置くフォルダのパスに日本語を含めないでください"
                 "（含むと起動しません）。",
            wraplength=640, foreground="#0a5aa0").pack(anchor="w")
        ttk.Label(
            self.ndl_frame,
            text="※ 無料・オフラインで商用利用も可（CC BY 4.0）ですが、"
                 "ルビはまだ苦手です（正解率65〜80%）。\n"
                 "　 ルビの多い本は Vision を、時代小説・古典は "
                 "Vision / Document AI をおすすめします。",
            wraplength=640, foreground="#a05000").pack(anchor="w")

        # Vision / DocAI用（Google Cloud認証）
        self.gcp_frame = ttk.Frame(self.reocr_frame)
        r4 = ttk.Frame(self.gcp_frame)
        r4.pack(anchor="w", pady=2)
        self.gcp_var = tk.StringVar()
        self._update_gcp_label()
        ttk.Button(r4, text="認証JSONを選ぶ",
                   command=self.pick_gcp).pack(side="left")
        ttk.Label(r4, textvariable=self.gcp_var,
                  wraplength=480).pack(side="left", padx=6)
        ttk.Label(
            self.gcp_frame,
            text="※ Google Cloudのアカウント作成・課金の有効化（クレジットカード登録）"
                 "が必要です。\n　 Visionは月1000ページまで無料、"
                 "Document AIは$1.50/1000ページの従量課金です。",
            wraplength=640, foreground="#666666").pack(anchor="w")
        self.on_engine_change()

    def toggle_adv(self):
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.adv_btn.configure(text="▾ 詳細設定")
            self.adv.pack(fill="x", after=self.adv_btn)
        else:
            self.adv_btn.configure(text="▸ 詳細設定（ふだんは触らなくてよい）")
            self.adv.forget()
        self.on_type_change()

    def on_type_change(self):
        btype = self.type_var.get()
        # 再OCRは小説/横書きのみ
        state = "disabled" if btype == "manga" else "normal"
        self.reocr_chk.configure(state=state)
        # ルビ既定: 横書き=付けない（誤検出対策）、縦書き=付ける
        self.ruby_var.set(bool(self.settings.get("ruby_drop_horizontal", True))
                          if btype == "horizontal" else False)
        for fr in (self.novel_frame, self.manga_frame, self.reocr_frame):
            fr.forget()
        if not self.adv_open:
            return
        if btype == "manga":
            self.manga_frame.pack(anchor="w", fill="x")
        else:
            self.novel_frame.pack(anchor="w", fill="x")
            if self.reocr_var.get():
                self.reocr_frame.pack(anchor="w", fill="x")

    def on_reocr_toggle(self):
        if (self.reocr_var.get()
                and self.engine_var.get() not in ("yomitoku", "ndlocr")):
            gcp = self.settings.get("gcp_json", "")
            if not gcp or not Path(gcp).exists():
                self.pick_gcp()
        self.on_type_change()

    def on_engine_change(self):
        """エンジンに応じて、下に出す設定行を切り替える。"""
        engine = self.engine_var.get()
        for fr in (self.gcp_frame, self.yomi_frame, self.ndl_frame):
            fr.pack_forget()
        if engine == "yomitoku":
            self.yomi_frame.pack(anchor="w", fill="x")
            self._update_yomi_label()
        elif engine == "ndlocr":
            self.ndl_frame.pack(anchor="w", fill="x")
            self._update_ndl_label()
        else:
            self.gcp_frame.pack(anchor="w", fill="x")

    def _update_yomi_label(self):
        """yomitokuが入ったPythonが見つかるかを表示する（時間がかかるので
        ワーカースレッドで調べ、結果はTk側の変数にだけ書き戻す）。"""
        self.yomi_var.set("YomiTokuを確認中...")

        def work():
            py = find_yomitoku_python(self.settings)
            self._yomi_result = (
                f"準備OK（{os.path.basename(py)}）" if py
                else "未インストール → 「インストール手順」を押してください"
            )

        self._yomi_result = None
        threading.Thread(target=work, daemon=True).start()

    def show_yomitoku_help(self):
        self._show_help_dialog("YomiTokuのインストール手順",
                               YOMITOKU_INSTALL_HELP, YOMITOKU_PIP_COMMANDS)

    def show_ndlocr_help(self):
        self._show_help_dialog("NDLOCRの初期設定の手順",
                               NDLOCR_INSTALL_HELP, NDLOCR_PIP_COMMANDS)

    def _show_help_dialog(self, title, help_text, pip_text):
        """インストール手順のダイアログ。

        messagebox.showinfo だと本文を選択できず、実機で「全文をコピーして
        メモ帳に貼り、行を選び直す」手間が発生した。テキストを選択可能にし、
        pipコマンドはボタン一発でクリップボードに入るようにする。
        """
        import tkinter as tk
        from tkinter import ttk

        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)

        body = tk.Text(win, wrap="word", width=78, height=26,
                       font=self.ui_font, borderwidth=8, relief="flat")
        body.pack(fill="both", expand=True, padx=6, pady=6)
        body.insert("1.0", help_text)
        # 読み取り専用にしつつ選択・コピーは許す（state="disabled" だと
        # 選択もできなくなるので、編集キーだけ無効化する）
        body.bind("<Key>", lambda e: (
            None if e.state & 0x4 else "break"))  # Ctrl系（コピー等）は通す

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=6, pady=(0, 8))
        status = tk.StringVar()

        def copy_pip():
            self.root.clipboard_clear()
            self.root.clipboard_append(pip_text)
            status.set("コピーしました。PowerShellに貼り付けてください")

        ttk.Button(bar, text="pipコマンドをコピー",
                   command=copy_pip).pack(side="left")
        ttk.Button(bar, text="閉じる", command=win.destroy).pack(side="right")
        ttk.Label(bar, textvariable=status,
                  foreground="#0a7").pack(side="left", padx=8)

    def _update_ndl_label(self):
        """NDLOCRの初期設定が済んでいるかを表示する。

        ライブラリの確認は別プロセス起動を伴って遅いのでワーカースレッドで行い、
        結果は _ndl_result 経由で poll()（メインスレッド）が拾う。
        **Tkinterを別スレッドから触らないため。**
        """
        self.ndl_var.set("確認中...")

        def work():
            lack = missing_ndlocr_scripts()
            if lack:
                self._ndl_result = "不足: " + " / ".join(lack)
                return
            py = find_ndlocr_python(self.settings)
            if not py:
                self._ndl_result = "ライブラリ未導入 →「初期設定の手順」へ"
                return
            err = check_ndlocr_dir(self.settings.get("ndlocr_dir", ""))
            if err:
                self._ndl_result = "NDLOCR-Liteフォルダ未設定"
                return
            self._ndl_result = f"準備OK（{os.path.basename(py)}）"

        self._ndl_result = None
        threading.Thread(target=work, daemon=True).start()

    def pick_ndlocr_dir(self):
        """NDLOCR-Lite の clone 先を選ぶ。選んだ時点で使えるか検査する。"""
        from tkinter import filedialog, messagebox

        path = filedialog.askdirectory(title="NDLOCR-Lite のフォルダを選ぶ")
        if not path:
            return
        err = check_ndlocr_dir(path)
        if err:
            messagebox.showwarning("このフォルダは使えません", err)
            return
        self.settings["ndlocr_dir"] = path
        save_settings(self.settings)
        self._update_ndl_label()

    def pick_ndlocr_python(self):
        """NDLOCRの依存を入れたPythonを手動で指定する（PATHにない場合用）。"""
        from tkinter import filedialog, messagebox

        ftypes = [("Python", "python.exe")] if IS_WIN else [("すべて", "*")]
        path = filedialog.askopenfilename(title="python.exe を選ぶ",
                                          filetypes=ftypes)
        if not path:
            return
        if not _has_ndlocr_deps(path):
            messagebox.showwarning(
                "必要なライブラリが入っていません",
                "選んだPythonには NDLOCR に必要なライブラリ"
                "（onnxruntime / opencv / PyYAML / PyMuPDF）が入っていません。"
                f"\n\n{NDLOCR_INSTALL_HELP}")
            return
        self.settings["ndlocr_python"] = path
        save_settings(self.settings)
        self._update_ndl_label()

    def pick_python(self):
        """yomitokuを入れたPythonを手動で指定する（PATHにない場合用）。"""
        from tkinter import filedialog, messagebox

        ftypes = [("Python", "python.exe")] if IS_WIN else [("すべて", "*")]
        path = filedialog.askopenfilename(title="python.exe を選ぶ",
                                          filetypes=ftypes)
        if not path:
            return
        if not _has_yomitoku(path):
            messagebox.showwarning(
                "YomiTokuが見つかりません",
                f"選んだPythonにはyomitokuが入っていません。\n\n{YOMITOKU_INSTALL_HELP}")
            return
        self.settings["python_path"] = path
        save_settings(self.settings)
        self._update_yomi_label()

    # ── ファイル・認証の選択 ──
    def _setup_dnd(self):
        """ドラッグ&ドロップの有効化。

        Windows: ctypes＋Win32 API（WM_DROPFILES）で実装。追加ライブラリ
        不要でexe化しても動く。ウィンドウ全体がドロップ先になる。
        それ以外（WSLg等の開発環境）: tkinterdnd2 があれば使う
        （rootを TkinterDnD.Tk で作った場合のみ有効）。
        どちらも使えなければクリック選択のみに退化する。
        """
        if IS_WIN and self._setup_dnd_win32():
            return
        try:
            from tkinterdnd2 import DND_FILES  # 任意依存
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>",
                               lambda e: self._on_drop_paths(
                                   [e.data.strip("{}").strip()]))
        except Exception:
            pass  # 未導入ならクリック選択のみ

    def _setup_dnd_win32(self):
        """Win32のWM_DROPFILESでD&Dを受け付ける（依存なし・Windows専用）。"""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            self.root.update_idletasks()
            hwnd = user32.GetParent(self.root.winfo_id()) or \
                self.root.winfo_id()

            LONG_PTR = ctypes.c_ssize_t
            WM_DROPFILES = 0x0233
            GWL_WNDPROC = -4
            WNDPROC = ctypes.WINFUNCTYPE(
                LONG_PTR, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM)

            SetWindowLongPtr = getattr(user32, "SetWindowLongPtrW",
                                       user32.SetWindowLongW)
            SetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int,
                                         LONG_PTR]
            SetWindowLongPtr.restype = LONG_PTR
            CallWindowProc = user32.CallWindowProcW
            CallWindowProc.argtypes = [LONG_PTR, wintypes.HWND,
                                       wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM]
            CallWindowProc.restype = LONG_PTR
            DragQueryFile = shell32.DragQueryFileW
            DragQueryFile.argtypes = [wintypes.WPARAM, wintypes.UINT,
                                      ctypes.c_wchar_p, wintypes.UINT]
            DragQueryFile.restype = wintypes.UINT
            # argtypes必須: 宣言しないとHDROP（64bitポインタ）が既定の
            # 32bit int変換でOverflowErrorになる（実測: Windows実機で
            # DragFinishだけ未宣言でドロップが無反応になった）
            DragFinish = shell32.DragFinish
            DragFinish.argtypes = [wintypes.WPARAM]
            DragFinish.restype = None

            def wnd_proc(h, msg, wp, lp):
                if msg == WM_DROPFILES:
                    paths = []
                    try:
                        n = DragQueryFile(wp, 0xFFFFFFFF, None, 0)
                        for i in range(n):
                            ln = DragQueryFile(wp, i, None, 0)
                            buf = ctypes.create_unicode_buffer(ln + 1)
                            DragQueryFile(wp, i, buf, ln + 1)
                            paths.append(buf.value)
                    except Exception:
                        pass   # 取得失敗してもウィンドウ処理は継続する
                    finally:
                        try:
                            DragFinish(wp)
                        except Exception:
                            pass
                    if paths:
                        # ここで root.after 等の Tk API を呼んではならない。
                        # ウィンドウプロシージャはTclのイベントディスパッチの
                        # 最中に呼ばれるため、tkinterへ再入するとGIL状態管理と
                        # 衝突し Fatal Python error (PyEval_RestoreThread) で
                        # 落ちる（Windows実機で実測）。素のPythonリストに
                        # 積むだけにし、受け渡しは poll() 側で行う
                        self._dropped_paths.append(paths)
                    return 0
                return CallWindowProc(self._dnd_old_proc, h, msg, wp, lp)

            # コールバックはGC防止のためインスタンスに保持する
            self._dnd_cb = WNDPROC(wnd_proc)
            self._dnd_old_proc = SetWindowLongPtr(
                hwnd, GWL_WNDPROC,
                ctypes.cast(self._dnd_cb, ctypes.c_void_p).value)
            if not self._dnd_old_proc:
                return False
            shell32.DragAcceptFiles(hwnd, True)
            # 管理者実行時にUIPIがWM_DROPFILESを遮断するのを許可
            # （WM_DROPFILES / WM_COPYDATA / WM_COPYGLOBALDATA、MSGFLT_ALLOW=1）
            try:
                for m in (0x0233, 0x004A, 0x0049):
                    user32.ChangeWindowMessageFilterEx(hwnd, m, 1, None)
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _on_drop_paths(self, paths):
        from tkinter import messagebox
        pdfs = [p for p in paths if p.lower().endswith(".pdf")]
        if not pdfs:
            messagebox.showinfo(APP_NAME, "PDFファイルをドロップしてください")
            return
        self.set_file(pdfs[0])

    def pick_file(self):
        from tkinter import filedialog
        initial = self.settings.get("last_dir") or str(Path.home())
        path = filedialog.askopenfilename(
            title="自炊PDFを選ぶ", initialdir=initial,
            filetypes=[("PDF", "*.pdf"), ("すべて", "*.*")])
        if path:
            self.set_file(path)

    def set_file(self, path):
        if not path:
            return
        self.pdf_path = path
        self.settings["last_dir"] = str(Path(path).parent)
        self.file_var.set(f"ファイル: {os.path.basename(path)}")
        t, a = parse_meta_from_filename(path)
        self.title_var.set(t)
        self.author_var.set(a)
        self.btn_folder.configure(state="normal")

    def pick_gcp(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Google Cloud サービスアカウントJSONを選ぶ",
            filetypes=[("JSON", "*.json"), ("すべて", "*.*")])
        if path:
            self.settings["gcp_json"] = path
            save_settings(self.settings)
        self._update_gcp_label()

    def _update_gcp_label(self):
        gcp = self.settings.get("gcp_json", "")
        if gcp and Path(gcp).exists():
            self.gcp_var.set(f"認証: {os.path.basename(gcp)}")
        else:
            self.gcp_var.set("認証: 未設定")

    # ── 実行 ──
    def _cfg(self):
        def _int(v):
            v = v.strip()
            return int(v) if v.isdigit() else None
        return {
            "pdf": self.pdf_path,
            "book_type": self.type_var.get(),
            "title": self.title_var.get(),
            "author": self.author_var.get(),
            "reocr": self.reocr_var.get() and self.type_var.get() != "manga",
            "reocr_engine": self.engine_var.get(),
            "reocr_start": _int(self.rstart_var.get()),
            "reocr_end": _int(self.rend_var.get()),
            "ruby_drop": self.ruby_var.get(),
            "pages": self.pages_var.get().strip(),
            "cover_page": _int(self.cover_var.get()),
            "direction": self.dir_var.get(),
            "quality": _int(self.quality_var.get()),
        }

    def on_run(self):
        from tkinter import messagebox
        if self.runner is not None:      # 実行中 → 中止
            self.runner.cancel()
            return
        if not self.pdf_path:
            messagebox.showinfo(APP_NAME, "先にPDFを選んでください")
            return
        jobs, err = build_jobs(self._cfg(), self.settings)
        if err:
            messagebox.showerror(APP_NAME, err)
            return
        self._start(jobs, remember=True)

    def on_regen(self):
        from tkinter import messagebox
        if self.runner is not None:
            return
        job = build_regen_job(self.settings.get("last_job"), self.settings)
        if job is None:
            messagebox.showinfo(APP_NAME,
                                "再生成できる変換履歴がありません（先に変換してください）")
            return
        self._start([job], remember=False)

    def _start(self, jobs, remember):
        self._remember_ctx = remember
        self._clear_log()
        self.result_txt = ""
        self.result_epub = ""
        for b in (self.btn_epub, self.btn_txt, self.btn_regen):
            b.configure(state="disabled")
        self.run_btn.configure(text="⏹ 中止")
        self.prog.start(80)
        self.runner = JobRunner(jobs, self.q)
        self.runner.start()

    def poll(self):
        # YomiTokuの導入確認（ワーカースレッド）の結果を反映する。
        # Tkinterはメインスレッド以外から触らないための受け渡し
        if getattr(self, "_yomi_result", None):
            self.yomi_var.set(self._yomi_result)
            self._yomi_result = None
        if getattr(self, "_ndl_result", None):
            self.ndl_var.set(self._ndl_result)
            self._ndl_result = None
        # Win32 D&D（wnd_proc）からのドロップをTk側スレッド文脈で処理する
        while self._dropped_paths:
            self._on_drop_paths(self._dropped_paths.pop(0))
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "stage":
                    self.stage_var.set(f"実行中: {payload} …")
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def _finish(self, results):
        self.runner = None
        self.prog.stop()
        self.run_btn.configure(text="▶ 変換開始")
        rc = results.get("rc", 0)
        self.result_txt = results.get("txt", "")
        self.result_epub = results.get("epub", "")
        if rc == 0:
            self.stage_var.set("✅ 完了しました")
            if self.result_epub:
                self.btn_epub.configure(state="normal")
            if self.result_txt:
                self.btn_txt.configure(state="normal")
            if self._remember_ctx and self.result_txt:
                self.settings["last_job"] = {
                    "pdf": self.pdf_path, "txt": self.result_txt,
                    "epub": self.result_epub,
                    "horizontal": self.type_var.get() == "horizontal"}
            if self.settings.get("last_job"):
                self.btn_regen.configure(state="normal")
            save_settings(self._collect_settings())
        elif rc == -2:
            self.stage_var.set("⏹ 中止しました")
        else:
            self.stage_var.set("✖ エラーで終了しました（ログを確認してください）")

    # ── 補助 ──
    def open_folder(self):
        target = self.result_epub or self.result_txt or self.pdf_path
        if target:
            open_path(Path(target).parent)

    def change_font(self, delta):
        size = max(9, min(28, int(self.settings.get("font_size", 14)) + delta))
        self.settings["font_size"] = size
        from tkinter import font as tkfont
        self.ui_font.configure(size=size)
        self.big_font.configure(size=size + 4)
        self.log_font.configure(size=max(size - 3, 9))
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except Exception:
                pass

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _restore_last_job(self):
        lj = self.settings.get("last_job")
        if lj and Path(lj.get("txt", "")).exists():
            self.btn_regen.configure(state="normal")

    def _collect_settings(self):
        s = self.settings
        s["book_type"] = self.type_var.get()
        s["reocr"] = bool(self.reocr_var.get())
        s["reocr_engine"] = self.engine_var.get()
        if self.type_var.get() == "horizontal":
            s["ruby_drop_horizontal"] = bool(self.ruby_var.get())
        s["geometry"] = self.root.geometry()
        return s

    def on_close(self):
        if self.runner is not None:
            self.runner.cancel()
        save_settings(self._collect_settings())
        self.root.destroy()


# ── エントリポイント ──────────────────────────────────

def run_gui(smoke=False):
    import tkinter as tk
    root = None
    if not IS_WIN:
        # 開発環境（WSLg等）では tkinterdnd2 があればD&D対応rootを作る。
        # WindowsはWin32 API実装（_setup_dnd_win32）を使うため不要
        try:
            from tkinterdnd2 import TkinterDnD
            root = TkinterDnD.Tk()
        except Exception:
            root = None
    if root is None:
        root = tk.Tk()
    root.minsize(660, 640)
    app = App(root)
    if smoke:
        root.update_idletasks()
        root.update()
        # 主要ウィジェットの存在確認
        assert app.run_btn.winfo_exists()
        assert app.log.winfo_exists()
        app.on_close()
        print("SMOKE OK")
        return
    root.mainloop()


def print_jobs(argv):
    """--print-jobs: コマンド組み立てを表示（実行しない。動作確認用）。"""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--type", default="novel",
                    choices=[k for k, _ in BOOK_TYPES])
    ap.add_argument("--reocr", action="store_true")
    ap.add_argument("--engine", default="yomitoku",
                    choices=list(ENGINE_CHOICES))
    args = ap.parse_args(argv)
    settings = load_settings()
    t, a = parse_meta_from_filename(args.pdf)
    cfg = {"pdf": args.pdf, "book_type": args.type, "title": t, "author": a,
           "reocr": args.reocr, "reocr_engine": args.engine,
           "ruby_drop": args.type == "horizontal"}
    jobs, err = build_jobs(cfg, settings)
    if err:
        print(f"エラー: {err}")
        return 1
    for j in jobs:
        print(j)
    return 0


def main():
    args = sys.argv[1:]
    if args and args[0] == "--print-jobs":
        sys.exit(print_jobs(args[1:]))
    run_gui(smoke=(args[:1] == ["--smoke"]))


if __name__ == "__main__":
    main()
