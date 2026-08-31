#!/usr/bin/env bash
# 部分図（DESIGN_部分図.md）の回帰テスト。
#
#   bash scratchpad/reg_figure.sh [出力ディレクトリ]
#
# 各サンプルを `--inline-figure off` と既定の2通りで変換し、
#   - 図タグ・キャプション行を除いた本文が一致するか（0行であること）
#   - 章見出しの数が変わっていないか
#   - 部分図・見開きが何枚出たか
# を並べる。**受け入れ基準は「本文diff=0行」と「章見出しが減らないこと」**
# （減る場合は、図版ページのOCRジャンクが作っていた偽見出しかどうかを必ず確認する）。
set -u
OUT="${1:-/tmp/reg_figure}"
mkdir -p "$OUT"
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python

strip() { grep -v "の図（\|はキャプション］" "$1"; }

run() {
  local n="$1"; shift
  "$PY" jisui2epub.py "$@" --no-epub --inline-figure off -o "$OUT/${n}_off.txt" >"$OUT/${n}_off.log" 2>&1
  "$PY" jisui2epub.py "$@" --no-epub                     -o "$OUT/${n}_on.txt"  >"$OUT/${n}_on.log"  2>&1
  local fig sp
  fig=$(grep -oP "本文中の図: .*" "$OUT/${n}_on.log")
  sp=$(grep -oP "見開き連結: .*" "$OUT/${n}_on.log")
  printf "%-10s | %-42s | %-22s | 本文diff=%s行 | 見出し %s→%s\n" \
    "$n" "${fig:-図0枚}" "${sp:-見開きなし}" \
    "$(diff <(strip "$OUT/${n}_off.txt") <(strip "$OUT/${n}_on.txt") | wc -l)" \
    "$(grep -c '中見出し］' "$OUT/${n}_off.txt")" \
    "$(grep -c '中見出し］' "$OUT/${n}_on.txt")"
}

S=temp_sample
run jinrui    "$S/人類の起源_篠田謙一.pdf"
run hoshi     "$S/星空をつくる機械　プラネタリム100年史　増補版_井上毅.pdf"
run soga      "$S/蘇我氏一古代豪族の興亡_倉本一宏.pdf"
run tl        "$S/START_タイム・リープ上_高畑京一郎.pdf"
run sofro     "$S/ソフロニア嬢、倫敦で恋に陥落する_ゲイル・キャリガー_START.pdf"
run glick     "$S/start_jisui_scaned_グリックの冒険.pdf"
run kiri      "$S/START_霧のむこうのふしぎな町_柏葉幸子.pdf"
run honmono   "$S/ぼんものの魔法使_ポール・ギャリコ_scansnap_START.pdf"
run boku      "$S/ぼくがぼくであること_山仲恒.pdf"
run hakushaku "$S/伯爵夫人は超能力_ドロシー・ギルマン.pdf"
# 横書き（転置座標）
run fant "$S/シナリオのためのファンタジー事典_山北篤.pdf" --horizontal --ruby drop
run ds   "$S/30秒でわかる！データサイエンスで重要な50の理論_Ｌ．ヴィッタート編.pdf" --horizontal --ruby drop
