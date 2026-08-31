import re,sys,bisect
rows=[]
for line in open(sys.argv[1],encoding='utf-8'):
    m=re.match(r'p(\d+)\tnombre=(\S+)\t(\S+)\tink=([\d.]+)',line)
    if m: rows.append((int(m.group(1)), None if m.group(2)=='None' else int(m.group(2)), m.group(3), float(m.group(4))))
raw={p:n for p,n,_,_ in rows if n is not None}
# 局所連番のノンブルだけ信用する
good={p:n for p,n in raw.items() if any(raw.get(q)==n+(q-p) for q in (p-2,p-1,p+1,p+2) if q in raw)}
ks=sorted(good)
def book(p):
    if p in good: return good[p]
    if not ks: return None
    i=bisect.bisect_left(ks,p); cand=[ks[j] for j in (i-1,i) if 0<=j<len(ks)]
    q=min(cand,key=lambda k:abs(k-p)); return good[q]+(p-q)
goal=set(int(x) for x in sys.argv[3].split(','))
thr=float(sys.argv[2])
det={}
for p,n,k,ik in rows:
    if ik>=thr:
        b=book(p)
        if b: det.setdefault(b,[]).append((p,k,round(ik,3)))
lo,hi=min(goal),max(goal)
D=set(det); Db=set(x for x in D if lo-3<=x<=hi+3)
print("miss ",sorted(goal-D))
print("extra(本文域)",sorted(Db-goal))
print(f"recall {len(D&goal)}/{len(goal)} precision(本文域) {len(Db&goal)}/{len(Db)}")
print("miss詳細:", {m:[r for r in rows if book(r[0])==m] for m in sorted(goal-D)})
