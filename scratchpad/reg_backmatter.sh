#!/usr/bin/env bash
# 巻末カット・目次隣接の締め・図キャプション見出し除外・ルビ組の妥当性判定の回帰。
#
#   bash scratchpad/reg_backmatter.sh baseline   # HEAD の jisui2epub.py で採取
#   bash scratchpad/reg_backmatter.sh new        # 作業ツリーで採取
#   bash scratchpad/reg_backmatter.sh diff       # 2つを比較
#
# 受け入れ基準:
#   縦書き = 出力が **1バイト一致**（変更はすべて横書き経路のはず）
#   横書き = 本文が減らない・章見出しが減らない（減る場合は中身を確認）
set -u
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
MODE="${1:-new}"
S=temp_sample

if [ "$MODE" = "diff" ]; then
  for f in scratchpad/reg_bm_new/*.txt; do
    n=$(basename "$f" .txt); b="scratchpad/reg_bm_base/$n.txt"
    [ -f "$b" ] || { printf "%-12s | baseline なし\n" "$n"; continue; }
    d=$(diff "$b" "$f" | grep -c '^[<>]')
    printf "%-12s | diff=%-5s | 見出し %s→%s | ルビ %s→%s | 字数 %s→%s\n" \
      "$n" "$d" \
      "$(grep -c '見出し］' "$b")" "$(grep -c '見出し］' "$f")" \
      "$(grep -o '《[^》]*》' "$b" | wc -l)" "$(grep -o '《[^》]*》' "$f" | wc -l)" \
      "$(wc -c <"$b")" "$(wc -c <"$f")"
  done
  exit 0
fi

SRC="jisui2epub.py"; OUT=scratchpad/reg_bm_new
if [ "$MODE" = "baseline" ]; then
  git show HEAD:jisui2epub.py > scratchpad/base_jisui2epub.py
  SRC=scratchpad/base_jisui2epub.py; OUT=scratchpad/reg_bm_base
fi
mkdir -p "$OUT"
run() { local n="$1"; shift
  timeout 2400 $PY "$SRC" "$@" --no-epub -o "$OUT/$n.txt" >"$OUT/$n.log" 2>&1 \
    || echo "FAIL $n"; echo "done $n"; }

# ── 横書き（今回の変更が効く経路） ──────────────────────
run kisho    "$S/図解気象学入門改訂版_古川武彦・大木勇人.pdf"
run ds30     "$S/30秒でわかる！データサイエンスで重要な50の理論_Ｌ．ヴィッタート編.pdf" --horizontal
run fantasy  "$S/シナリオのためのファンタジー事典_山北篤.pdf"
run angou    "$S/現代暗号入門_神永正博_ndlocr.pdf"
run angou_s  "$S/現代暗号入門_神永正博.pdf"
run gakki    "$S/楽器の科学_フランソワ・デュボラ_ndlocr.pdf"
run gakki_s  "$S/楽器の科学_フランソワ・デュボラ.pdf"
run net      "$S/ネットオーディオのすすめ　高音質定額配信を楽しもう_山之内正_ndlocr.pdf"
run net_s    "$S/ネットオーディオのすすめ　高音質定額配信を楽しもう_山之内正.pdf"

# ── 縦書き（1バイト一致であること） ────────────────────
run guri     "$S/start_jisui_scaned_グリックの冒険.pdf"
run guri_n   "$S/グリックの冒険_斎藤敦夫_ndlocr.pdf"
run honmono  "$S/ぼんものの魔法使_ポール・ギャリコ_scansnap_START.pdf"
run hakushaku "$S/伯爵夫人は超能力_ドロシー・ギルマン.pdf"
run timeleap "$S/START_タイム・リープ上_高畑京一郎.pdf"
run kiri     "$S/霧のむこうのふしぎな町_柏葉幸子_scansnap_START.pdf"
run chika    "$S/地下室からのふしぎな旅_vision.pdf"
run soga     "$S/蘇我氏_vision.pdf"
run kokuro   "$S/黒牢城_米澤穂信_vision.pdf"
run sofro    "$S/ソフロニア嬢、倫敦で恋に陥落する_ゲイル・キャリガー_START.pdf"
run dokoyori "$S/どこよりも遠い場所にいる君へ_阿部暁子_ndlocr.pdf"
run kaze     "$S/風の万里黎明の空上_小野不由美_vision.pdf"
run kazeshita "$S/風の万里黎明の空下_小野不由美.pdf"
run anron    "$S/赤毛のアン論－八つの扉_松本侑子_ndlocr.pdf"
run sangoku  "$S/三国志（五）_吉川英治.pdf"
run hyakuoku "$S/百億の昼と千億の夜_光瀬龍.pdf"
run boku     "$S/ぼくがぼくであること_山仲恒.pdf"
run karte    "$S/天久鷹央の推理カルテ_知念実希人.pdf"
run jinrui   "$S/人類の起源_篠田謙一.pdf"
run hoshi    "$S/星空をつくる機械　プラネタリム100年史　増補版_井上毅.pdf"
run pizan    "$S/fixed_連帯惑星ピザンの危機_高千穂遥_ndlocr.pdf"
run anne     "$S/赤毛のアン_モンゴメリ・村岡花子訳_ndlocr.pdf"
run rail     "$S/RAIL＿WARS！_豊田巧_ndlocr.pdf"
run tosho    "$S/図書館戦争_有川浩.pdf"
run seirei   "$S/精霊の守り人_上橋菜穂子.pdf"
