import json
d = json.load(open("campaigns.json", encoding="utf-8"))
c = d["campaigns"]
def links(x): return x.get("source_links") or []
nf = [x for x in c if not links(x) and not x.get("disqualified")]
nf.sort(key=lambda y: -(y.get("composite_score") or 0))
print(f"{len(nf)} rankable campaigns have NO source_links\n")
for x in nf[:15]:
    print(f'{x.get("name","?")[:42]:44} | comp {(x.get("composite_score") or 0):.3f} | {str(x.get("category","?"))[:12]}')
