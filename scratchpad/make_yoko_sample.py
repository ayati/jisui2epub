#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""横書き対応（DESIGN_横書き対応.md §8.2-1）検証用の合成PDFを生成する。

単段組・横書きの小冊子を模したテキスト層のみのPDF:
  - 本文10pt・1字下げ段落・行送り18pt
  - 柱（上マージン・7pt・毎ページ同文）
  - ノンブル（下マージン・7pt）
  - 章見出し（14pt・章頭ページ）
  - 横ルビ（親文字の上・5pt）
  - 章末ページは途中で終わる（意図的改ページの検出対象）
"""
import fitz

W, H = 420, 595            # A5相当
X0, X1 = 50, 372           # 本文左右マージン
Y_TOP, Y_BOT = 70, 545     # 本文上下
FS = 10.0                  # 本文フォント
PITCH = 18.0               # 行送り
RUBY_FS = 5.0
CPL = int((X1 - X0) / FS)  # 1行の文字数

HASHIRA = "こころの散歩道"

# (段落テキスト, {文字index: ルビ}) 。indexは段落文字列内の位置
CH1 = [
    ("　吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。"
     "何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは記憶している。"
     "吾輩はここで始めて人間というものを見た。", {1: ("吾輩", "わがはい")}),
    ("　しかもあとで聞くとそれは書生という人間中で一番獰悪な種族であったそうだ。"
     "この書生というのは時々我々を捕えて煮て食うという話である。"
     "しかしその当時は何という考もなかったから別段恐しいとも思わなかった。", {}),
    ("　ただ彼の掌に載せられてスーと持ち上げられた時何だかフワフワした感じが"
     "あったばかりである。掌の上で少し落ちついて書生の顔を見たのがいわゆる"
     "人間というものの見始であろう。この時妙なものだと思った感じが今でも"
     "残っている。第一毛をもって装飾されべきはずの顔がつるつるして"
     "まるで薬缶だ。", {5: ("掌", "てのひら")}),
    ("　その後猫にもだいぶ逢ったがこんな片輪には一度も出会わした事がない。"
     "のみならず顔の真中があまりに突起している。そうしてその穴の中から時々"
     "ぷうぷうと煙を吹く。どうも咽せぽくて実に弱った。これが人間の飲む"
     "煙草というものである事はようやくこの頃知った。", {}),
]
CH2 = [
    ("　この書生の掌の裏でしばらくはよい心持に坐っておったが、しばらくすると"
     "非常な速力で運転し始めた。書生が動くのか自分だけが動くのか分らないが"
     "無暗に眼が廻る。胸が悪くなる。到底助からないと思っていると、どさりと"
     "音がして眼から火が出た。それまでは記憶しているがあとは何の事やら"
     "いくら考え出そうとしても分らない。", {6: ("掌", "てのひら")}),
    ("　ふと気が付いて見ると書生はいない。たくさんおった兄弟が一疋も見えぬ。"
     "肝心の母親さえ姿を隠してしまった。その上今までの所とは違って無暗に"
     "明るい。眼を明いていられぬくらいだ。はてな何でも容子がおかしいと、"
     "のそのそ這い出して見ると非常に痛い。吾輩は藁の上から急に笹原の中へ"
     "棄てられたのである。", {}),
]


def wrap(para):
    """段落を1行CPL文字で折り返す。返り値 [(行文字列, {行内index: ルビ})]"""
    text, rubies = para
    lines = []
    i = 0
    while i < len(text):
        chunk = text[i:i + CPL]
        rmap = {k - i: v for k, v in rubies.items() if i <= k < i + CPL}
        lines.append((chunk, rmap))
        i += CPL
    return lines


def draw_line(page, x, y, text, rmap, fontsize=FS):
    # 実スキャンのOCR層は字下げ空白を字形として持たない（bboxは可視文字から
    # 始まる）。空白を字形ごと描くとスパンbboxに空白幅が含まれ、collect_spans
    # の空白剥がしでセル境界が1文字ドリフトする偽の症状が出るため、
    # 空白分はペン位置のオフセットで表現する
    nsp = len(text) - len(text.lstrip("　"))
    page.insert_text((x + nsp * fontsize, y), text.lstrip("　"),
                     fontsize=fontsize, fontname="japan")
    for idx, (base, yomi) in rmap.items():
        bx = x + idx * fontsize          # 全角文字の送り=フォントサイズ
        bw = len(base) * fontsize
        rx = bx + (bw - len(yomi) * RUBY_FS) / 2
        page.insert_text((rx, y - fontsize - 1.0), yomi,
                         fontsize=RUBY_FS, fontname="japan")


def main(out="scratchpad/yoko_sample.pdf"):
    doc = fitz.open()
    pno = 0

    def new_page(with_hashira=True):
        nonlocal pno
        page = doc.new_page(width=W, height=H)
        pno += 1
        if with_hashira:
            page.insert_text((X0, 40), HASHIRA, fontsize=7, fontname="japan")
        page.insert_text((W / 2 - 5, 575), str(pno + 8), fontsize=7,
                         fontname="japan")
        return page

    def render_chapter(title, paras, first_page):
        page = first_page
        y = Y_TOP + PITCH
        page.insert_text((X0 + FS * 2, y), title, fontsize=14,
                         fontname="japan")
        y += PITCH * 2
        for para in paras:
            for text, rmap in wrap(para):
                if y > Y_BOT:
                    page = new_page()
                    y = Y_TOP + PITCH
                draw_line(page, X0, y, text, rmap)
                y += PITCH

    render_chapter("第一章　猫の目", CH1, new_page())
    render_chapter("第二章　書生の掌", CH2, new_page())
    doc.save(out, garbage=4)
    print(f"生成: {out} ({len(doc)}ページ, {CPL}字/行)")


if __name__ == "__main__":
    main()
