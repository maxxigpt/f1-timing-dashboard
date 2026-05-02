import requests, json, pandas as pd

try:
    print('=== SESSIONS 2026 ===')
    r = requests.get('https://api.openf1.org/v1/sessions?year=2026', timeout=10)
    print('Status:', r.status_code)
    data = r.json()
    print('Total sesiones:', len(data))
    for s in data[-5:]:
        print(f'  {s.get("session_key")} | {s.get("meeting_name")} | {s.get("session_name")} | start: {s.get("date_start")} | end: {s.get("date_end")}')

    print()
    print('=== SESSION LATEST ===')
    r2 = requests.get('https://api.openf1.org/v1/sessions?session_key=latest', timeout=10)
    print('Status:', r2.status_code)
    data2 = r2.json()
    if data2:
        s = data2[-1]
        print(f'  key: {s.get("session_key")} | {s.get("meeting_name")} | {s.get("session_name")} | year: {s.get("year")}')

    print()
    print('=== MEETINGS 2026 ===')
    r3 = requests.get('https://api.openf1.org/v1/meetings?year=2026', timeout=10)
    print('Status:', r3.status_code)
    data3 = r3.json()
    print('Total meetings:', len(data3))
    for m in data3[-3:]:
        print(f'  {m.get("meeting_key")} | {m.get("meeting_name")} | {m.get("date_start")}')
except Exception as e:
    print(f"Error: {e}")
