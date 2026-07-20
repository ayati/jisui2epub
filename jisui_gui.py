#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自炊変換GUI — 自炊PDFをePubに変換するWindows向けランチャー。

jisui2epub / vision_reocr / docai_reocr / manga_p2epub を子プロセスとして
起動する薄いGUI（設計は DESIGN_WindowsGUI.md）。ツール本体には一切手を
入れず、公開CLIオプションの組み立てだけを行う。

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

TOOL_NAMES = ("jisui2epub", "vision_reocr", "docai_reocr", "manga_p2epub")

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
    "reocr_engine": "vision",     # vision / docai
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


def resolve_tool(name, settings):
    """ツール名 → 起動コマンド（リスト）を解決する。見つからなければ None。

    優先順: 設定の手動パス → GUIと同じフォルダのexe → 同フォルダの.py →
    開発時の既知の場所（mangaは隣のリポジトリ）。
    """
    manual = settings.get("tool_paths", {}).get(name, "")
    if manual and Path(manual).exists():
        p = Path(manual)
        return [str(p)] if p.suffix.lower() == ".exe" else [_script_python(p), str(p)]

    here = app_dir()
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
        engine = cfg.get("reocr_engine") or "vision"
        tool = resolve_tool(f"{engine}_reocr", settings)
        if tool is None:
            return [], f"{engine}_reocr が見つかりません（設定でパスを指定してください）"
        gcp = settings.get("gcp_json", "")
        if not gcp or not Path(gcp).exists():
            return [], ("再OCRにはGoogle Cloudの認証JSONが必要です。"
                        "「認証JSONを選ぶ」で設定してください")
        argv = tool + [str(pdf)]
        if cfg.get("reocr_start"):
            argv += ["--start", str(cfg["reocr_start"])]
        if cfg.get("reocr_end"):
            argv += ["--end", str(cfg["reocr_end"])]
        out_pdf = pdf.with_name(f"{pdf.stem}_{engine}.pdf")
        jobs.append(Job(f"再OCR（{engine}）", argv,
                        env_extra={"GOOGLE_APPLICATION_CREDENTIALS": gcp}))
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
            outer, text="先に再OCRで文字を読み直す（Vision API・高精度化）",
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
        self.engine_var = tk.StringVar(value=s.get("reocr_engine", "vision"))
        ttk.Combobox(r3, textvariable=self.engine_var, width=20,
                     values=("vision", "docai"),
                     state="readonly", font=self.ui_font).pack(
            side="left", padx=(2, 10))
        ttk.Label(r3, text="開始/終了ページ:").pack(side="left")
        self.rstart_var = tk.StringVar()
        ttk.Entry(r3, textvariable=self.rstart_var, width=5,
                  font=self.ui_font).pack(side="left", padx=2)
        self.rend_var = tk.StringVar()
        ttk.Entry(r3, textvariable=self.rend_var, width=5,
                  font=self.ui_font).pack(side="left", padx=2)
        r4 = ttk.Frame(self.reocr_frame)
        r4.pack(anchor="w", pady=2)
        self.gcp_var = tk.StringVar()
        self._update_gcp_label()
        ttk.Button(r4, text="認証JSONを選ぶ",
                   command=self.pick_gcp).pack(side="left")
        ttk.Label(r4, textvariable=self.gcp_var,
                  wraplength=480).pack(side="left", padx=6)

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
        if self.reocr_var.get():
            gcp = self.settings.get("gcp_json", "")
            if not gcp or not Path(gcp).exists():
                self.pick_gcp()
        self.on_type_change()

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
    ap.add_argument("--engine", default="vision", choices=["vision", "docai"])
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
