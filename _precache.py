"""
_precache.py — Genera cache_f1/track_records.json con los records
historicos de qualy y carrera para cada circuito del calendario 2026.
Correr UNA sola vez antes de arrancar la app:
    python _precache.py
"""
import os, json, warnings
import fastf1
import pandas as pd

warnings.filterwarnings("ignore")

CACHE_DIR = "cache_f1"
os.makedirs(CACHE_DIR, exist_ok=True)
fastf1.Cache.enable_cache(CACHE_DIR)

RECORDS_FILE = os.path.join(CACHE_DIR, "track_records.json")

if os.path.exists(RECORDS_FILE):
    with open(RECORDS_FILE, "r", encoding="utf-8") as f:
        all_records = json.load(f)
    print(f"Cache existente cargado: {len(all_records)} circuitos ya procesados.")
else:
    all_records = {}

def fmt_laptime(td):
    if pd.isna(td): return None
    secs = td.total_seconds()
    m = int(secs // 60)
    s = secs % 60
    return f"{m}:{s:06.3f}"

def abbr_name(full_name):
    parts = str(full_name).split()
    if len(parts) >= 2:
        return f"{parts[0][0]}. {' '.join(parts[1:])}"
    return full_name

sched_2026 = fastf1.get_event_schedule(2026, include_testing=False)
events = [(row["EventName"], row["Location"]) for _, row in sched_2026.iterrows()]

print(f"\nProcesando {len(events)} circuitos...\n")

for ev_name, location in events:
    if ev_name in all_records:
        print(f"  SKIP  {ev_name} (ya en cache)")
        continue

    print(f"  Procesando: {ev_name} ...")
    best_qualy = None
    best_race  = None

    for yr in range(2018, 2026):
        try:
            yr_sched = fastf1.get_event_schedule(yr, include_testing=False)
            ev_match = yr_sched[yr_sched["EventName"] == ev_name]
            if ev_match.empty:
                ev_match = yr_sched[yr_sched["Location"] == location]
            if ev_match.empty:
                continue
            ev_yr_name = ev_match.iloc[0]["EventName"]

            try:
                q = fastf1.get_session(yr, ev_yr_name, "Q")
                q.load(telemetry=False, weather=False, messages=False)
                ql = q.laps[["Driver", "LapTime"]].dropna(subset=["LapTime"])
                if not ql.empty:
                    best_idx = ql["LapTime"].idxmin()
                    lt = ql.loc[best_idx, "LapTime"]
                    drv = ql.loc[best_idx, "Driver"]
                    try: full = q.get_driver(drv).get("FullName", drv)
                    except: full = drv
                    if best_qualy is None or lt < best_qualy[0]:
                        best_qualy = (lt, full, yr)
                    print(f"    Q {yr}: {abbr_name(full)} {fmt_laptime(lt)}")
            except Exception as e:
                print(f"    Q {yr}: skip ({e})")

            try:
                r = fastf1.get_session(yr, ev_yr_name, "R")
                r.load(telemetry=False, weather=False, messages=False)
                rl = r.laps[["Driver", "LapTime"]].dropna(subset=["LapTime"])
                if not rl.empty:
                    best_idx = rl["LapTime"].idxmin()
                    lt = rl.loc[best_idx, "LapTime"]
                    drv = rl.loc[best_idx, "Driver"]
                    try: full = r.get_driver(drv).get("FullName", drv)
                    except: full = drv
                    if best_race is None or lt < best_race[0]:
                        best_race = (lt, full, yr)
                    print(f"    R {yr}: {abbr_name(full)} {fmt_laptime(lt)}")
            except Exception as e:
                print(f"    R {yr}: skip ({e})")

        except Exception as e:
            print(f"    {yr}: skip ({e})")
            continue

    qualy_str = f"{abbr_name(best_qualy[1])} - {fmt_laptime(best_qualy[0])} - {best_qualy[2]}" if best_qualy else "No data"
    race_str  = f"{abbr_name(best_race[1])} - {fmt_laptime(best_race[0])} - {best_race[2]}" if best_race else "No data"

    all_records[ev_name] = {"qualy": qualy_str, "race": race_str}
    print(f"    [OK] Qualy: {qualy_str}")
    print(f"    [OK] Race:  {race_str}\n")

    with open(RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

print(f"\n[DONE] Records guardados en {RECORDS_FILE}")
