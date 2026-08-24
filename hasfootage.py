import json
d = json.load(open("campaigns.json", encoding="utf-8"))
c = d["campaigns"]
def links(x): return x.get("source_links") or []
has = [x for x in c if links(x) and not x.get("disqualified")]
has.sort(key=lambda y: -(y.get("composite_score") or 0))
print(f"{len(has)} campaigns have source_links\n")
for x in has[:15]:
    ls = links(x)
    cat = x.get("category","?")
    ota = x.get("open_to_all")
    join = "OPEN" if ota else "join?"
    print(f'{x.get("name","?")[:38]:40} | {len(ls)} link(s) | {(x.get("composite_score") or 0):.3f} | {str(cat)[:12]:12} | {join}')
    for l in ls[:2]:
        u = l if isinstance(l,str) else (l.get("url") or l.get("link") or l)
        print(f'      {str(u)[:78]}')
