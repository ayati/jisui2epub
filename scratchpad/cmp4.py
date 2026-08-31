"""検出結果を GOAL の書籍ページ番号と突き合わせる。
物理→書籍の変換は「前後6ページの既知ノンブルから求めたオフセットの最頻値」で行い、
近傍に既知ノンブルが無いページは採点から外す（オフセットが本の途中で
12〜15 と変わる本があるため、遠方からの外挿は当てにならない）。"""
import re, sys, collections
rows=[]
for line in open(sys.argv[1],encoding='utf-8'):
    m=re.match(r'p(\d+)\tnombre=(\S+)\t(\S+)\tink=([\d.]+)\tarea=([\d.]+)\tnv=(\d+)',line)
    if m: rows.append((int(m.group(1)), None if m.group(2)=='None' else int(m.group(2)),
                       m.group(3), float(m.group(4)), float(m.group(5)), int(m.group(6))))
raw={p:n for p,n,_,_,_,_ in rows if n is not None}
good={p:n for p,n in raw.items() if any(raw.get(q)==n+(q-p) for q in (p-2,p-1,p+1,p+2) if q in raw)}
def book(p):
    if p in good: return good[p]
    c=collections.Counter(q-good[q] for q in range(p-6,p+7) if q in good)
    if not c: return None
    return p - c.most_common(1)[0][0]
goal=set(int(x) for x in sys.argv[3].split(','))
thr=float(sys.argv[2]); lo,hi=min(goal),max(goal)
det, unknown = set(), 0
for p,n,k,ik,a,nv in rows:
    if ik < thr: continue
    b=book(p)
    if b is None: unknown+=1; continue
    if lo-3<=b<=hi+3: det.add(b)
print(f"thr={thr} 判定不能={unknown}")
print(" hit  ",len(det&goal)); print(" miss ",sorted(goal-det)); print(" extra",sorted(det-goal))
print(f" recall {len(det&goal)}/{len(goal)}  precision {len(det&goal)}/{len(det)}")
