import json, random
d = json.load(open('campaigns.json', encoding='utf-8'))
c = [x for x in d['campaigns'] if x.get('category')]
s = random.sample(c, min(20, len(c)))
for x in s:
    print(f"{x.get('category',''):16} {x.get('category_confidence','?'):5} {x.get('category_source','?'):8} {x.get('name','')[:40]}")
