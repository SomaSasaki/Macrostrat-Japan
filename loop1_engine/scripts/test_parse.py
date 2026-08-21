# -*- coding: utf-8 -*-
"""
test_parse.py
"""
import io
import json
import os
import re
import sys
import urllib.request
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

url = 'https://www.gsj.jp/Map/JP/geology2-1.html'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=10) as res:
    html = res.read().decode('utf-8')

soup = BeautifulSoup(html, 'html.parser')
for tr in soup.find_all('tr'):
    text = tr.get_text()
    code_m = re.search(r'([Nn][A-Za-z]-\d{2}-\d{1,2})', text)
    if code_m:
        code = code_m.group(1).upper()
        # Look at the html structure of tr
        # Let's inspect all child elements
        th = tr.find('th')
        tds = tr.find_all('td')
        print(f"CODE: {code}")
        if th:
            print(f"  TH: {th.prettify().strip()}")
        for i, td in enumerate(tds):
            print(f"  TD[{i}]: {td.get_text().strip()[:40]}")
        print("-" * 40)
        break
