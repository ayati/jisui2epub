"""柱ラン方式 + 被覆率ゲート + 章頭ページ算出。既存の見出し検出との突き合わせ付き。"""
import sys, os, re, difflib
from collections import Counter, defaultdict
sys.path.insert(0, '/home/ayati/jisui2epub')
import fitz
import jisui2epub as J

SIM = 0.66
MIN_PAGES = 3
MIN_SPAN = 3
MIN_DENS = 0.30
MIN_RUNS = 3
MIN_COVER = 0.60    # 選ばれたランが自身の全体レンジをどれだけ敷き詰めるか


def margin_strings(pages, drop):
    out = defaultdict(list)
    for pg in pages:
        for v in pg.vlines + pg.hlines:
            if (pg.num, id(v)) not in drop:
                continue
            t = v.text.strip()
            if not t or J.NOMBRE_RE.match(re.sub(r'\s+', '', t)):
                continue
            k = J.norm_hashira(t)
            if not k or not re.search(r'[ぁ-ゖァ-ヺー㐀-鿿]', k):
                continue
            out[pg.num].append(k)
    return out


def cluster(strings):
    freq = Counter(strings)
    reps, mapping = [], {}
    for s, _ in freq.most_common():
        hit = next((r for r in reps
                    if difflib.SequenceMatcher(None, s, r).ratio() >= SIM), None)
        if hit is None:
            reps.append(s)
            hit = s
        mapping[s] = hit
    return mapping


def pillar_runs(pages, drop, npages):
    per = margin_strings(pages, drop)
    mapping = cluster([s for v in per.values() for s in v])
    pageset = defaultdict(set)
    for p, ss in per.items():
        for s in ss:
            pageset[mapping[s]].add(p)
    cands = []
    for rep, ps in pageset.items():
        ps = sorted(ps)
        span = ps[-1] - ps[0] + 1
        if len(ps) < MIN_PAGES or span < MIN_SPAN:
            continue
        if len(ps) / span < MIN_DENS or span > npages * 0.6:
            continue
        cands.append((len(ps), ps[0], ps[-1], rep))
    cands.sort(key=lambda c: (-c[0], c[1]))
    chosen = []
    for cnt, lo, hi, rep in cands:
        if any(not (hi < l or lo > h) for _, l, h, _ in chosen):
            continue
        chosen.append((cnt, lo, hi, rep))
    chosen.sort(key=lambda c: c[1])
    return chosen


def main(pdf):
    doc = fitz.open(pdf)
    nums = list(range(len(doc)))
    body = J.detect_body_size(doc, nums)
    pages = [J.analyze_page(doc[i], i, body) for i in nums]
    drop, headings, btop, bbot, hkeys = J.classify_marginals(pages, body)
    img = J.classify_image_pages(doc, pages, drop)
    runs = pillar_runs(pages, drop, len(doc))

    name = os.path.basename(pdf)
    if not runs:
        print(f"\n### {name} ({len(doc)}p) runs=0 -> 不発火")
        return
    total = runs[-1][2] - runs[0][1] + 1
    cover = sum(hi - lo + 1 for _, lo, hi, _ in runs) / max(total, 1)
    fire = len(runs) >= MIN_RUNS and cover >= MIN_COVER
    print(f"\n### {name} ({len(doc)}p) runs={len(runs)} cover={cover:.2f} "
          f"-> {'発火' if fire else '不発火'}")
    if not fire:
        for cnt, lo, hi, rep in runs:
            print(f"      (p{lo+1}-{hi+1} {rep!r})")
        return
    prev_end = None
    for cnt, lo, hi, rep in runs:
        # 章頭ページ = [前ランの終端+1, ラン先頭] のうち最初の画像ページ、
        # 無ければ範囲の先頭
        rlo = prev_end + 1 if prev_end is not None else max(0, lo - 3)
        cand = [p for p in range(rlo, lo + 1) if p in img]
        start = cand[0] if cand else rlo
        exist = headings.get((start, None))
        print(f"    章頭 p{start+1:4d}  (ラン p{lo+1}-{hi+1}, n={cnt})  {rep!r}"
              + ("  [画像章扉]" if start in img else ""))
        prev_end = hi


if __name__ == "__main__":
    for pdf in sys.argv[1:]:
        try:
            main(pdf)
        except Exception as e:
            import traceback; traceback.print_exc()
