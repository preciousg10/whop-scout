import json
d = json.load(open("campaigns.json", encoding="utf-8"))
c = d["campaigns"]
def links(x): return x.get("source_links") or []
live = [x for x in c if x.get("status") != "not_listed_this_run" and not x.get("disqualified")]
nf = [x for x in live if not links(x)]
nf.sort(key=lambda y: -(y.get("composite_score") or 0))
print(f"{len(live)} live campaigns this run | {len(nf)} of them have NO footage links\n")
for x in nf[:15]:
    print(f'{x.get("name","?")[:42]:44} | comp {(x.get("composite_score") or 0):.3f} | {str(x.get("category","?"))[:12]} | {x.get("status","?")}')
