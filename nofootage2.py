import json
d = json.load(open("campaigns.json", encoding="utf-8"))
c = d["campaigns"]
def links(x): return x.get("source_links") or []
nf = [x for x in c if not links(x) and not x.get("disqualified")]
nf.sort(key=lambda y: -(y.get("composite_score") or 0))
for x in nf[:8]:
    print(x.get("name","?")[:40])
    print("  ", x.get("url","NO URL"))
    print("  scraped:", x.get("scraped_at","?"), "| status:", x.get("status","?"))
    print()
