import os
import json
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = json.load(open(os.path.join(BASE_DIR, 'data', '50k', 'gsj_50k_catalog.json'), 'r', encoding='utf-8'))
sheets = d['sheets']

# Show region 16-19 details
r16plus = [s for s in sheets if int(s['id'][:2]) >= 16]
print(f"Region 16-19 sheets: {len(r16plus)}")
for s in r16plus:
    print(f"  {s['id']} {s['code']} {s['name']} {s['year']}")

print()

# Show duplicates
from collections import Counter
id_counter = Counter(s['id'] for s in sheets)
dups = {k: v for k, v in id_counter.items() if v > 1}
if dups:
    print(f"Duplicate IDs: {dups}")
    for dup_id in dups:
        entries = [s for s in sheets if s['id'] == dup_id]
        for e in entries:
            print(f"  {e}")

# Final unique count
unique_ids = set(s['id'] for s in sheets)
print(f"\nTotal entries: {len(sheets)}")
print(f"Unique sheet IDs: {len(unique_ids)}")
print(f"  Hokkaido (01-04): {len([i for i in unique_ids if int(i[:2]) <= 4])}")
print(f"  Honshu+ (05-19):  {len([i for i in unique_ids if int(i[:2]) >= 5])}")
