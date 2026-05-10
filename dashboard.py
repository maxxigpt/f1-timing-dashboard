import os, json, warnings, time, requests, threading, copy
import dash
from dash import dcc, html, Input, Output, State, callback_context
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import fastf1
import pandas as pd
from datetime import datetime
import live_timing

warnings.filterwarnings("ignore")

CACHE_DIR = "cache_f1"
os.makedirs(CACHE_DIR, exist_ok=True)
fastf1.Cache.enable_cache(CACHE_DIR)

# ── MEMORY CACHE & TTL ──────────────────────────────────────────────────────
_session_mem = {}
_session_mem_time = {}
SESSION_CACHE_TTL = 120   # 2 min para sesiones recientes

# Controla qué claves están siendo cargadas en background para no lanzar hilos duplicados
_loading_in_progress = set()
_loading_lock = threading.Lock()

def _ensure_session_loading(year, gp, stype):
    """Lanza un hilo para cargar la sesión en background si no está ya en progreso."""
    key = f"{year}-{gp}-{stype}-False"
    with _loading_lock:
        if key in _loading_in_progress or key in _session_mem:
            return
        _loading_in_progress.add(key)

    def _load():
        try:
            print(f"[BG Load] {year} {gp} {stype}…")
            get_session_data(year, gp, stype, with_telemetry=False)
            print(f"[BG Load] Listo: {year} {gp} {stype}")
        except Exception as e:
            print(f"[BG Load] Error: {e}")
        finally:
            with _loading_lock:
                _loading_in_progress.discard(key)

    threading.Thread(target=_load, daemon=True, name=f"bgload-{gp}").start()

def get_session_data(year, gp, session_type, with_telemetry=False):
    key = f"{year}-{gp}-{session_type}-{with_telemetry}"
    now = time.time()
    is_recent = (year == datetime.now().year)
    ttl = SESSION_CACHE_TTL if is_recent else 86400
    if key not in _session_mem or (now - _session_mem_time.get(key, 0)) > ttl:
        try:
            s = fastf1.get_session(year, gp, session_type)
            s.load(telemetry=with_telemetry, weather=False, messages=False)
            _session_mem[key] = s
            _session_mem_time[key] = now
        except Exception as e:
            print(f"[ERROR] {year} {gp} {session_type}: {e}")
            return _session_mem.get(key)
    return _session_mem[key]

# ── SESSION AUTO-DETECTION ──────────────────────────────────────────────────
_active_session_cache  = {"data": None, "ts": 0}
_detected_session_cache = {"data": None, "ts": 0}   # cache 5 min para detección de sesión actual

def get_current_session_info():
    """Detecta la sesión actual con cache de 5 minutos. No llama FF1 en cada tick."""
    now_ts = time.time()
    if now_ts - _detected_session_cache["ts"] < 300 and _detected_session_cache["data"]:
        return _detected_session_cache["data"]
    try:
        sessions = get_ff1_latest_session()
        if sessions:
            s = sessions[0]
            info = {
                "year":  s.get("_ff1_year", pd.Timestamp.now().year),
                "gp":    s.get("meeting_name", ""),
                "type":  s.get("session_name", ""),
                "flag":  s.get("country_code", "us"),
                "_ff1_gp":    s.get("_ff1_gp", ""),
                "_ff1_stype": s.get("_ff1_stype", ""),
            }
            _detected_session_cache["data"] = info
            _detected_session_cache["ts"]   = now_ts
            return info
    except Exception as e:
        print(f"[SessionDetect] {e}")
    return _detected_session_cache.get("data") or {}

def get_current_active_session():
    now_ts = time.time()
    if now_ts - _active_session_cache["ts"] < 60 and _active_session_cache["data"]:
        return _active_session_cache["data"]
    try:
        now = pd.Timestamp.now(tz="UTC")
        year = now.year
        sched = fastf1.get_event_schedule(year, include_testing=False)
        sched = sched[sched["EventFormat"] != "testing"].sort_values("RoundNumber")
        best = (year, "Japan", "Race")
        for _, ev in sched.iterrows():
            for i in range(5, 0, -1):
                s_name = ev.get(f"Session{i}")
                s_dt_val = ev.get(f"Session{i}DateUtc")
                if pd.isna(s_name) or pd.isna(s_dt_val): continue
                try:
                    s_dt = pd.to_datetime(s_dt_val).tz_localize("UTC")
                    if s_dt + pd.Timedelta(hours=3) < now:
                        best = (year, ev["EventName"], s_name)
                        _active_session_cache["data"] = best
                        _active_session_cache["ts"] = now_ts
                        return best
                except: continue
    except Exception as e:
        print(f"[WARN] {e}")
    return best

# ── MAPA ESTÁTICO DE PILOTOS 2026 ───────────────────────────────────────────
# Grilla completa 2026: 11 equipos × 2 pilotos = 22 pilotos
# Norris es campeón y lleva el #1; Verstappen eligió el #3.
# Nuevos equipos: Audi (ex-Kick Sauber) y Cadillac.
DRIVER_STATIC = {
    "1":  {"tla":"NOR","full_name":"Lando Norris",          "team":"McLaren"},
    "3":  {"tla":"VER","full_name":"Max Verstappen",         "team":"Red Bull Racing"},
    "5":  {"tla":"BOR","full_name":"Gabriel Bortoleto",      "team":"Audi"},
    "6":  {"tla":"HAD","full_name":"Isack Hadjar",           "team":"Red Bull Racing"},
    "10": {"tla":"GAS","full_name":"Pierre Gasly",           "team":"Alpine"},
    "11": {"tla":"PER","full_name":"Sergio Pérez",           "team":"Cadillac"},
    "12": {"tla":"ANT","full_name":"Andrea Kimi Antonelli",  "team":"Mercedes"},
    "14": {"tla":"ALO","full_name":"Fernando Alonso",        "team":"Aston Martin"},
    "16": {"tla":"LEC","full_name":"Charles Leclerc",        "team":"Ferrari"},
    "18": {"tla":"STR","full_name":"Lance Stroll",           "team":"Aston Martin"},
    "23": {"tla":"ALB","full_name":"Alexander Albon",        "team":"Williams"},
    "27": {"tla":"HUL","full_name":"Nico Hülkenberg",        "team":"Audi"},
    "30": {"tla":"LAW","full_name":"Liam Lawson",            "team":"Racing Bulls"},
    "31": {"tla":"OCO","full_name":"Esteban Ocon",           "team":"Haas F1 Team"},
    "41": {"tla":"LIN","full_name":"Arvid Lindblad",         "team":"Racing Bulls"},
    "43": {"tla":"COL","full_name":"Franco Colapinto",       "team":"Alpine"},
    "44": {"tla":"HAM","full_name":"Lewis Hamilton",         "team":"Ferrari"},
    "55": {"tla":"SAI","full_name":"Carlos Sainz",           "team":"Williams"},
    "63": {"tla":"RUS","full_name":"George Russell",         "team":"Mercedes"},
    "77": {"tla":"BOT","full_name":"Valtteri Bottas",        "team":"Cadillac"},
    "81": {"tla":"PIA","full_name":"Oscar Piastri",          "team":"McLaren"},
    "87": {"tla":"BEA","full_name":"Oliver Bearman",         "team":"Haas F1 Team"},
}

def get_driver_info(drv_num, dlist):
    """Devuelve info del piloto: primero del feed, luego del mapa estático."""
    if drv_num in dlist and dlist[drv_num].get("tla"):
        return dlist[drv_num]
    static = DRIVER_STATIC.get(str(drv_num), {})
    if static:
        team = static.get("team","")
        return {
            "tla":        static["tla"],
            "full_name":  static["full_name"],
            "team":       team,
            "team_color": TEAM_COLORS.get(team, "#888888"),
            "number":     str(drv_num),
        }
    return {"tla": str(drv_num), "full_name": "", "team": "", "team_color": "#888888", "number": str(drv_num)}

# ── COLORS & STYLE ──────────────────────────────────────────────────────────
BG_COLOR = "#0b0e11"
TABLE_HEADER_BG = "#1e2235"
ROW_BG = "#151922"
BORDER_COLOR = "#2a2f45"
TEXT_COLOR = "#d1d5db"
TEXT_MUTED = "#8e98a8"

COLOR_PURPLE = "#B15DFF"
COLOR_GREEN = "#00D21E"
COLOR_YELLOW = "#FFFB00"

TEAM_COLORS = {
    "Red Bull Racing": "#3671C6", "Ferrari": "#E8002D", "Mercedes": "#27F4D2",
    "McLaren": "#FF8000", "Aston Martin": "#229971", "Alpine": "#FF87BC",
    "Williams": "#64C4FF",
    "RB": "#6692FF", "Racing Bulls": "#6692FF", "Visa Cash App RB": "#6692FF", "AlphaTauri": "#6692FF",
    "Kick Sauber": "#52E252", "Sauber": "#52E252", "Alfa Romeo": "#900000",
    "Haas F1 Team": "#B6BABD", "Haas": "#B6BABD",
    "Audi": "#C0BFBF",       # Audi (ex-Kick Sauber) — silver/grey Audi brand color
    "Cadillac": "#CF102D",   # Cadillac — American red
}

# ── OPENF1 & FF1 CONSTANTS ──────────────────────────────────────────────────
MUTED  = "#8e98a8"
PURPLE = "#B15DFF"
GREEN  = "#00D21E"
YELLOW = "#FFFB00"
TEAM_COLORS_FF1 = TEAM_COLORS

# ── OPENF1 API CLIENT ───────────────────────────────────────────────────────
_of1_cache = {}
def of1_get(endpoint, params="", ttl=60):
    cache_key = f"{endpoint}?{params}"
    now = time.time()
    if cache_key in _of1_cache:
        data, ts = _of1_cache[cache_key]
        if now - ts < ttl: return data
    try:
        url = f"https://api.openf1.org/v1/{endpoint}?{params}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            _of1_cache[cache_key] = (data, now)
            return data
    except: pass
    return []

def get_ff1_session(year, gp, stype, with_telemetry=False):
    return get_session_data(year, gp, stype, with_telemetry)

def get_latest_session():
    """
    Busca la sesión más reciente disponible en OpenF1.
    Primero intenta el año actual, si no hay datos cae al año anterior.
    """
    now = pd.Timestamp.now(tz="UTC")
    year = now.year

    # Intentar año actual primero
    data = of1_get("sessions", f"year={year}", ttl=60)
    if data:
        # Filtrar sesiones que ya empezaron
        started = [s for s in data if s.get("date_start") and
                   pd.to_datetime(s["date_start"], utc=True) <= now]
        if started:
            # Ordenar por date_start descendente, tomar la más reciente
            started.sort(key=lambda x: x.get("date_start",""), reverse=True)
            return [started[0]]

    # Si no hay datos del año actual (requiere auth), usar año anterior
    data_prev = of1_get("sessions", f"year={year-1}", ttl=3600)
    if data_prev:
        started = [s for s in data_prev if s.get("date_start") and
                   pd.to_datetime(s["date_start"], utc=True) <= now]
        if started:
            started.sort(key=lambda x: x.get("date_start",""), reverse=True)
            return [started[0]]

    return []

def get_ff1_latest_session():
    """
    Detecta la sesión más reciente via FastF1 cuando OpenF1 no tiene 
    datos del año actual (requiere suscripción para 2026+).
    Retorna dict compatible con el formato de OpenF1 sessions.
    """
    try:
        now = pd.Timestamp.now(tz="UTC")
        year = now.year
        sched = fastf1.get_event_schedule(year, include_testing=False)
        sched = sched[sched["EventFormat"] != "testing"].sort_values("RoundNumber")

        best_ev = None
        best_stype = None
        best_dt = None

        session_order = [
            ("Session5", "Race"),
            ("Session4", "Qualifying"),
            ("Session4", "Sprint Qualifying"),
            ("Session3", "Sprint"),
            ("Session3", "Practice 3"),
            ("Session2", "Practice 2"),
            ("Session1", "Practice 1"),
        ]

        for _, ev in sched.iterrows():
            for col_prefix, sname in session_order:
                # Buscar la columna DateUtc correcta
                dt_col = None
                for i in range(1, 6):
                    if ev.get(f"Session{i}") == sname:
                        dt_col = f"Session{i}DateUtc"
                        break
                if not dt_col:
                    continue
                try:
                    sdt = pd.to_datetime(ev[dt_col]).tz_localize("UTC")
                    # Sesión que ya empezó
                    if sdt <= now:
                        if best_dt is None or sdt > best_dt:
                            best_dt = sdt
                            best_ev = ev
                            best_stype = sname
                except:
                    continue

        if best_ev is not None:
            country_code_map = {
                "Japan":"JP","Bahrain":"BH","Saudi Arabia":"SA","Australia":"AU",
                "China":"CN","United States":"US","Italy":"IT","Monaco":"MC",
                "Canada":"CA","Spain":"ES","Austria":"AT","Great Britain":"GB",
                "Belgium":"BE","Hungary":"HU","Netherlands":"NL","Singapore":"SG",
                "Mexico":"MX","Brazil":"BR","Qatar":"QA","United Arab Emirates":"AE",
            }
            cc = country_code_map.get(best_ev.get("Country",""), "XX").lower()
            return [{
                "session_key": f"ff1_{best_ev['EventName']}_{best_stype}",
                "session_name": best_stype,
                "meeting_name": best_ev["EventName"],
                "country_code": cc,
                "year": year,
                "date_end": best_dt.isoformat() if best_dt else None,
                "date_start": best_dt.isoformat() if best_dt else None,
                "_source": "fastf1",
                "_ff1_year": year,
                "_ff1_gp": best_ev["EventName"],
                "_ff1_stype": best_stype,
            }]
    except Exception as e:
        print(f"[FF1 detect] {e}")
    return []

TEAM_LOGOS = {
    "Red Bull Racing": "https://upload.wikimedia.org/wikipedia/en/thumb/0/0c/Red_Bull_Racing_logo.svg/100px-Red_Bull_Racing_logo.svg.png",
    "Ferrari": "https://upload.wikimedia.org/wikipedia/en/thumb/d/d0/Scuderia_Ferrari_Logo.svg/100px-Scuderia_Ferrari_Logo.svg.png",
    "Mercedes": "https://upload.wikimedia.org/wikipedia/commons/thumb/f/fb/Mercedes_AMG_Petronas_F1_Logo.svg/100px-Mercedes_AMG_Petronas_F1_Logo.svg.png",
    "McLaren": "https://upload.wikimedia.org/wikipedia/en/thumb/6/6b/McLaren_Racing_logo.svg/100px-McLaren_Racing_logo.svg.png",
    "Aston Martin": "https://upload.wikimedia.org/wikipedia/en/thumb/b/b3/Aston_Martin_F1_Logo.svg/100px-Aston_Martin_F1_Logo.svg.png",
    "Alpine": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b5/Alpine_F1_Team_Logo.svg/100px-Alpine_F1_Team_Logo.svg.png",
    "Williams": "https://upload.wikimedia.org/wikipedia/commons/thumb/5/59/Williams_Racing_logo.svg/100px-Williams_Racing_logo.svg.png",
    "RB": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/60/Scuderia_AlphaTauri_Logo.svg/100px-Scuderia_AlphaTauri_Logo.svg.png",
    "Racing Bulls": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/60/Scuderia_AlphaTauri_Logo.svg/100px-Scuderia_AlphaTauri_Logo.svg.png",
    "Visa Cash App RB": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/60/Scuderia_AlphaTauri_Logo.svg/100px-Scuderia_AlphaTauri_Logo.svg.png",
    "AlphaTauri": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/60/Scuderia_AlphaTauri_Logo.svg/100px-Scuderia_AlphaTauri_Logo.svg.png",
    "Kick Sauber": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9e/Stake_F1_Team_Kick_Sauber_logo.svg/100px-Stake_F1_Team_Kick_Sauber_logo.svg.png",
    "Sauber": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9e/Stake_F1_Team_Kick_Sauber_logo.svg/100px-Stake_F1_Team_Kick_Sauber_logo.svg.png",
    "Alfa Romeo": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9e/Stake_F1_Team_Kick_Sauber_logo.svg/100px-Stake_F1_Team_Kick_Sauber_logo.svg.png",
    "Haas F1 Team": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7a/Haas_F1_team_logo.svg/100px-Haas_F1_team_logo.svg.png",
    "Haas": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7a/Haas_F1_team_logo.svg/100px-Haas_F1_team_logo.svg.png",
}

# ── LOGOS LOCALES (assets/logos/processed/) — fondo eliminado, alta calidad ──
# "logo-screen" = usar mix-blend-mode:screen para logos con fondo oscuro residual
TEAM_LOGO_LOCAL = {
    "Red Bull Racing": ("/assets/logos/processed/red_bull.png",    "logo-screen"),
    "Ferrari":         ("/assets/logos/processed/ferrari.png",     ""),
    "McLaren":         ("/assets/logos/processed/mclaren.png",     ""),
    "Mercedes":        ("/assets/logos/processed/mercedes.png",    ""),
    "Aston Martin":    ("/assets/logos/processed/aston_martin.png",""),
    "Alpine":          ("/assets/logos/processed/alpine.png",      ""),
    "Williams":        ("/assets/logos/processed/williams.png",    ""),
    "Racing Bulls":    ("/assets/logos/processed/racing_bulls.png",""),
    "Audi":            ("/assets/logos/processed/audi.png",        "logo-audi"),
    "Cadillac":        ("/assets/logos/processed/cadillac.png",    ""),
    "Haas F1 Team":    ("/assets/logos/processed/haas.png",        ""),
}

def format_time(delta):
    if pd.isna(delta): return ""
    secs = delta.total_seconds()
    m = int(secs // 60)
    s = secs % 60
    if m > 0: return f"{m}:{s:06.3f}"
    return f"{s:06.3f}"

# ── APP INITIALIZATION ──────────────────────────────────────────────────────
EXTERNAL_STYLESHEETS = [
    dbc.themes.DARKLY,
    "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap"
]

app = dash.Dash(__name__, external_stylesheets=EXTERNAL_STYLESHEETS, suppress_callback_exceptions=True)
server = app.server  # Expone el servidor Flask para gunicorn
app.title = "F1 Timing Dashboard 2.0"
live_timing.start()

# ── OPENF1 LIVE POLLING (fallback cuando SignalR no funciona en Render) ────────
_of1_live = {"drivers": {}, "driver_list": {}, "ts": 0, "session_key": None}
_of1_live_lock = threading.Lock()

def _fetch_of1_live():
    """Obtiene datos en vivo de OpenF1 API y actualiza _of1_live."""
    try:
        # Sesión activa
        sessions = of1_get("sessions", "session_key=latest", ttl=30)
        if not sessions: return
        sk = sessions[-1].get("session_key")
        if not sk: return

        drivers_raw  = of1_get("drivers",   f"session_key={sk}", ttl=300)
        positions_raw= of1_get("position",  f"session_key={sk}", ttl=5)
        intervals_raw= of1_get("intervals", f"session_key={sk}", ttl=5)
        laps_raw     = of1_get("laps",      f"session_key={sk}", ttl=5)
        stints_raw   = of1_get("stints",    f"session_key={sk}", ttl=10)

        if not drivers_raw or not positions_raw: return

        pos_latest = {}
        for p in positions_raw:
            pos_latest[str(p.get("driver_number",""))] = p

        itv_latest = {}
        for iv in intervals_raw:
            itv_latest[str(iv.get("driver_number",""))] = iv

        laps_by_drv = {}
        for lap in laps_raw:
            laps_by_drv.setdefault(str(lap.get("driver_number","")), []).append(lap)

        stints_by_drv = {}
        for s in stints_raw:
            stints_by_drv.setdefault(str(s.get("driver_number","")), []).append(s)

        driver_list = {}
        for d in drivers_raw:
            num = str(d.get("driver_number",""))
            color = d.get("team_colour","888888") or "888888"
            if not color.startswith("#"): color = "#" + color
            driver_list[num] = {
                "tla":       d.get("name_acronym", num),
                "full_name": d.get("full_name", ""),
                "team":      d.get("team_name", ""),
                "team_color": color,
                "number":    num,
            }

        drivers = {}
        for d in drivers_raw:
            num = str(d.get("driver_number",""))
            pos_data   = pos_latest.get(num, {})
            itv_data   = itv_latest.get(num, {})
            drv_laps   = sorted(laps_by_drv.get(num, []), key=lambda x: x.get("lap_number",0))
            drv_stints = stints_by_drv.get(num, [])

            last_lap, best_lap = None, None
            if drv_laps:
                try: last_lap = float(drv_laps[-1].get("lap_duration") or 0) or None
                except: pass
                for lap in drv_laps:
                    try:
                        lt = float(lap.get("lap_duration") or 0)
                        if lt and (best_lap is None or lt < best_lap): best_lap = lt
                    except: pass

            sectors = {i: {"value":"","secs":None,"pb":False,"ob":False,"segments":{}} for i in range(3)}
            if drv_laps:
                for si, key in enumerate(["duration_sector_1","duration_sector_2","duration_sector_3"]):
                    try:
                        sv = float(drv_laps[-1].get(key) or 0)
                        if sv: sectors[si] = {"value":f"{sv:.3f}","secs":sv,"pb":False,"ob":False,"segments":{}}
                    except: pass

            def _fmt(v):
                if not v: return ""
                try:
                    v = float(v)
                    return f"+{v:.3f}" if v >= 0 else f"{v:.3f}"
                except: return str(v)

            gap = _fmt(itv_data.get("gap_to_leader"))
            itv = _fmt(itv_data.get("interval"))

            compound, tyre_laps = "?", 0
            if drv_stints:
                st = drv_stints[-1]
                compound = str(st.get("compound","?")).upper()
                ls = st.get("lap_start") or 0
                le = st.get("lap_end") or 0
                tyre_laps = max(0, (le - ls + 1) if le > ls else 0)

            drivers[num] = {
                "position":     str(pos_data.get("position","")),
                "gap":          gap,
                "interval":     itv,
                "last_lap":     last_lap,
                "last_lap_str": live_timing.fmt_time(last_lap) if last_lap else "",
                "last_lap_pb":  False,
                "last_lap_ob":  False,
                "best_lap":     best_lap,
                "best_lap_str":"",
                "lap_number":   len(drv_laps),
                "sectors":      sectors,
                "compound":     compound,
                "tyre_laps":    tyre_laps,
                "tyre_new":     False,
                "in_pit":       False,
                "pit_out":      False,
            }

        if drivers:
            with _of1_live_lock:
                _of1_live["drivers"]     = drivers
                _of1_live["driver_list"] = driver_list
                _of1_live["session_key"] = sk
                _of1_live["ts"]          = time.time()
            print(f"[OF1 Poll] {len(drivers)} pilotos actualizados, session_key={sk}")
    except Exception as e:
        print(f"[OF1 Poll] Error: {e}")

def _of1_poll_thread():
    while True:
        _fetch_of1_live()
        time.sleep(10)

def get_of1_live_state():
    """Retorna el estado OpenF1 si tiene datos de los últimos 60s."""
    with _of1_live_lock:
        if _of1_live["drivers"] and (time.time() - _of1_live["ts"]) < 60:
            return copy.deepcopy(_of1_live)
    return None

threading.Thread(target=_of1_poll_thread, daemon=True, name="of1-poll").start()
print("[OF1 Poll] Hilo iniciado.")

def _prewarm_session():
    """Pre-carga la sesión actual en background para que la primera vista sea rápida."""
    def _run():
        time.sleep(2)  # dejar que el servidor arranque primero
        try:
            sessions = get_ff1_latest_session()
            if sessions:
                s = sessions[0]
                year  = s.get("_ff1_year", 2026)
                gp    = s.get("_ff1_gp", "")
                stype = s.get("_ff1_stype", "Race")
                if gp:
                    print(f"[Prewarm] Cargando {year} {gp} {stype}...")
                    get_session_data(year, gp, stype, with_telemetry=False)
                    print(f"[Prewarm] Listo.")
        except Exception as e:
            print(f"[Prewarm] {e}")
    threading.Thread(target=_run, daemon=True, name="prewarm").start()

_prewarm_session()

# ── HEADER & NAVIGATION ─────────────────────────────────────────────────────
menu_btn_style = {
    "backgroundColor": "transparent", "color": "#d1d5db", "border": "1px solid transparent",
    "padding": "6px 14px", "fontWeight": "600", "fontSize": "0.9rem",
    "cursor": "pointer", "fontFamily": "'Inter', sans-serif", "borderRadius": "4px"
}

header_bar = html.Div(
    style={"backgroundColor": "#13171f", "borderBottom": f"1px solid {BORDER_COLOR}"},
    children=[
        html.Div(
            style={"display": "flex", "alignItems": "center", "padding": "8px 20px"},
            children=[
                html.Img(src="https://upload.wikimedia.org/wikipedia/commons/thumb/3/33/F1.svg/100px-F1.svg.png", style={"height": "20px"}),
                html.Div(style={"flex": "1"}),
                html.Div(style={"display": "flex", "gap": "5px"}, children=[
                    html.Button("Live Timing", id="btn-nav-live", style={**menu_btn_style, "backgroundColor": "#1e2532"}),
                    html.Button("Replay", id="btn-nav-replay", style=menu_btn_style),
                    html.Button("Calendar", id="btn-nav-calendar", style=menu_btn_style),
                ]),
                html.Div(style={"flex": "1", "display": "flex", "justifyContent": "flex-end", "gap": "15px", "color": "#fff", "fontSize": "1.2rem"}, children=["⏸", "0", "⚙", "👤"])
            ]
        ),
        html.Div(
            style={"display": "flex", "alignItems": "center", "padding": "8px 20px", "borderTop": f"1px solid {BORDER_COLOR}"},
            children=[
                html.Img(id="session-flag-img", src="", style={"height":"20px","borderRadius":"2px","border":"1px solid #333","marginRight":"10px"}),
                html.H2(id="session-title", children="Cargando...", style={"color":"#fff","margin":0,"fontSize":"1.1rem","fontWeight":"700","fontFamily":"'Inter', sans-serif"}),
                html.Span(" 🏁", style={"fontSize": "1.2rem", "marginLeft": "5px"}),
                html.Div(style={"flex": "1"}),
                html.Div("53 / 53 ", style={"color": "#fff", "fontWeight": "700", "fontSize": "1rem", "fontFamily": "'Inter', sans-serif"}),
                html.Span("LAP", style={"color":TEXT_MUTED,"fontSize":"0.6rem", "marginRight": "20px", "marginTop": "4px"}),
                html.Div("TRACK CLEAR", style={"backgroundColor": "#00a651", "color": "#fff", "padding": "4px 12px", "borderRadius": "4px", "fontWeight": "700", "fontSize": "0.9rem", "fontFamily": "'Inter', sans-serif"})
            ]
        )
    ]
)

app.layout = html.Div(
    style={"backgroundColor": BG_COLOR, "minHeight": "100vh", "margin": 0, "overflowX": "hidden"},
    children=[
        header_bar,
        html.Div(id="tab-content", style={"padding": "0"}),
        dcc.Store(id="selected-drivers", data=[]),
        dcc.Store(id="current-session", data={"year": 2026, "gp": "Japan", "type": "Race"}),
        dcc.Store(id="selected-calendar-event", data=None),
        dcc.Store(id="active-tab", data="live"),
        dcc.Interval(id="data-refresh-interval", interval=5000, n_intervals=0, disabled=True)
    ]
)

# Tell Dash to ignore missing components on initial render by providing a validation layout
app.validation_layout = html.Div([
    app.layout,
    html.Div(id="live-table-content"),
    html.Div(id="live-map-content"),
    html.Div(id="replay-table-content"),
    html.Div(id="replay-map-content"),
    html.Img(id="session-flag-img"),
    html.Button(id={"type": "btn-back", "index": "calendar"}),
    html.Div(id={"type": "calendar-card", "index": "dummy"}),
    html.Div(id="calendar-main-content"),
    html.Div(id="countdown-container"),
    dcc.Interval(id="clock-interval"),
    dcc.Store(id="next-race-times"),
])



# ── REPLAY VIEW COMPONENTS ──────────────────────────────────────────────────
dropdown_style = {"color": "black", "backgroundColor": "#fff", "borderRadius": "4px", "fontFamily": "'Inter', sans-serif"}

def render_replay_view():
    return html.Div(id="replay-main-container", style={"padding": "20px"}, children=[
        html.Div(id="replay-form-container", style={
            "display": "flex", "flexDirection": "column", "justifyContent": "center", "alignItems": "center",
            "height": "80vh", "transition": "all 0.5s ease"
        }, children=[
            html.Div(style={"backgroundColor": ROW_BG, "padding": "40px", "borderRadius": "12px", "border": f"1px solid {BORDER_COLOR}", "width": "100%", "maxWidth": "700px"}, children=[
                html.H3("F1 Historical Replay", style={"color": "#fff", "fontFamily": "'Inter', sans-serif", "textAlign": "center", "marginBottom": "30px"}),
                dbc.Row([
                    dbc.Col([html.Label("Year", style={"color": "#aaa"}), dcc.Dropdown(id="replay-year", options=[{"label": str(y), "value": y} for y in range(2016, 2027)], value=2026, clearable=False, style=dropdown_style)], width=3),
                    dbc.Col([html.Label("Grand Prix", style={"color": "#aaa"}), dcc.Dropdown(id="replay-gp", options=[{"label": "Japan", "value": "Japan"}], value="Japan", clearable=False, style=dropdown_style)], width=5),
                    dbc.Col([html.Label("Session", style={"color": "#aaa"}), dcc.Dropdown(id="replay-session", options=[{"label": "Race", "value": "Race"}, {"label": "Qualifying", "value": "Qualifying"}], value="Race", clearable=False, style=dropdown_style)], width=4)
                ]),
                html.Div(style={"textAlign": "center", "marginTop": "40px"}, children=[
                    html.Button("BUSCAR SESIÓN", id="btn-search-replay", style={
                        "backgroundColor": "#e10600", "color": "#fff", "padding": "12px 30px", "borderRadius": "4px",
                        "fontWeight": "700", "fontSize": "1.1rem", "border": "none", "cursor": "pointer", "boxShadow": "0 4px 6px rgba(0,0,0,0.3)"
                    })
                ])
            ])
        ]),
        html.Div(id="replay-results-container")
    ])

# ── MAIN LAYOUT FOR LIVE/REPLAY ─────────────────────────────────────────────
def generate_dashboard_layout(target_table_id, target_map_id):
    return dbc.Row(
        style={"margin": "0", "width": "100%"},
        children=[
            dbc.Col(
                width=8,
                style={"padding": "0"},
                children=[html.Div(id=target_table_id)]
            ),
            dbc.Col(
                width=4,
                style={"padding": "0"},
                children=[html.Div(id=target_map_id, style={"height": "100%", "minHeight": "800px"})]
            )
        ]
    )

# ── ROUTING ─────────────────────────────────────────────────────────────────
@app.callback(
    Output("tab-content", "children"),
    Output("active-tab", "data"),
    Output("data-refresh-interval", "disabled"),
    Input("btn-nav-live", "n_clicks"),
    Input("btn-nav-replay", "n_clicks"),
    Input("btn-nav-calendar", "n_clicks")
)
def route_tabs(btn_l, btn_r, btn_cal):
    ctx = callback_context
    tab = "live"
    if ctx.triggered:
        prop = ctx.triggered[0]["prop_id"].split(".")[0]
        if prop == "btn-nav-replay": tab = "replay"
        elif prop == "btn-nav-calendar": tab = "calendar"

    interval_disabled = (tab != "live")

    if tab == "live":
        return html.Div(style={"padding": "0"}, children=[generate_dashboard_layout("live-table-content", "live-map-content")]), "live", False
    elif tab == "replay":
        return render_replay_view(), "replay", True
    elif tab == "calendar":
        return render_calendar_view(), "calendar", True
    else:
        return html.Div(f"Section {tab.capitalize()} coming soon.", style={"color": "#fff", "padding": "40px", "textAlign": "center"}), tab, True

# ── DUEL STATE LOGIC (STRICT) ───────────────────────────────────────────────
@app.callback(
    Output("selected-drivers", "data"),
    Input({'type': 'driver-row', 'index': dash.ALL}, 'n_clicks'),
    State("selected-drivers", "data"),
    prevent_initial_call=True
)
def update_selection(n_clicks_list, current_list):
    ctx = callback_context
    if not ctx.triggered: raise dash.exceptions.PreventUpdate
    try:
        trigger_id = json.loads(ctx.triggered[0]['prop_id'].split('.')[0])
        clicked_drv = trigger_id['index']
        val = ctx.triggered[0]['value']
        if val is None: raise dash.exceptions.PreventUpdate
    except: raise dash.exceptions.PreventUpdate
    
    current_list = current_list or []
    
    if len(current_list) == 0:
        return [clicked_drv]
    elif len(current_list) == 1:
        if clicked_drv == current_list[0]:
            return [] # 2nd click on same clears
        return current_list + [clicked_drv]
    else:
        # 3rd click or more -> clears everything
        return []

# ── REPLAY GP LIST UPDATE ───────────────────────────────────────────────────
@app.callback(
    Output("replay-gp", "options"),
    Output("replay-gp", "value"),
    Input("replay-year", "value")
)
def update_gp_list(year):
    if not year: raise dash.exceptions.PreventUpdate
    try:
        sched = fastf1.get_event_schedule(year)
        # Filter out testing if we want, but let's keep it simple
        opts = [{"label": r["EventName"], "value": r["EventName"]} for _, r in sched.iterrows() if r["EventFormat"] != 'testing']
        val = opts[0]["value"] if opts else None
        return opts, val
    except Exception as e:
        return [], None

# ── TABLE GENERATION ALGORITHM ──────────────────────────────────────────────
def create_table(year, gp, session_type, selected_list):
    try:
        s = get_session_data(year, gp, session_type, with_telemetry=False)
        if s is None:
            return html.Div(
                f"Esperando datos de sesión para {gp} – {session_type}…",
                style={"color": MUTED, "padding": "60px", "textAlign": "center", "fontSize": "1.1rem"}
            )

        try:
            r = s.results
        except Exception:
            r = pd.DataFrame()

        try:
            laps = s.laps
        except Exception:
            laps = pd.DataFrame()

        if r is None or (hasattr(r, "empty") and r.empty):
            return html.Div([
                html.Div("Datos no disponibles aún.", style={"color": "#fff", "fontSize": "16px", "marginBottom": "8px"}),
                html.Div("FastF1 publica los datos ~30–60 min después del final de sesión. Actualizando automáticamente…",
                         style={"color": MUTED, "fontSize": "13px"}),
            ], style={"padding": "60px", "textAlign": "center"})

        laps = laps if (laps is not None and not laps.empty) else pd.DataFrame()

        def safe_min(series):
            try:
                v = series.dropna().min()
                return v if not pd.isna(v) else pd.NaT
            except Exception:
                return pd.NaT

        overall_best_lap = safe_min(laps['LapTime'])     if not laps.empty else pd.NaT
        overall_best_s1  = safe_min(laps['Sector1Time']) if not laps.empty else pd.NaT
        overall_best_s2  = safe_min(laps['Sector2Time']) if not laps.empty else pd.NaT
        overall_best_s3  = safe_min(laps['Sector3Time']) if not laps.empty else pd.NaT

        COMPOUND_COLORS = {
            "SOFT": "#FF3333", "MEDIUM": "#FFD700", "HARD": "#FFFFFF",
            "INTERMEDIATE": "#39B54A", "WET": "#0067FF",
        }
        COMPOUND_ABBR = {
            "SOFT": "S", "MEDIUM": "M", "HARD": "H", "INTERMEDIATE": "I", "WET": "W",
        }

        th_s = {"backgroundColor": TABLE_HEADER_BG, "color": "#a0aab8", "fontSize": "0.75rem", "fontWeight": "600", "padding": "8px 4px", "borderBottom": "2px solid #2a2f45", "borderRight": "1px solid #1a1e2b", "fontFamily": "'Fira Code', monospace", "textAlign": "left"}
        th_center = {**th_s, "textAlign": "center"}

        td_s = {"padding": "4px 4px", "borderBottom": "1px solid #1a1e2b", "borderRight": "1px solid #1a1e2b", "fontSize": "0.85rem", "fontWeight": "500", "color": "#fff", "fontFamily": "'Fira Code', monospace", "textAlign": "left", "verticalAlign": "middle", "whiteSpace": "nowrap", "cursor": "pointer"}
        td_center = {**td_s, "textAlign": "center"}

        selected_list = selected_list or []

        # ── Re-numerar posiciones secuencialmente para evitar duplicados del feed ──
        # FastF1 a veces asigna el mismo número a dos pilotos y Time=NaT para todos.
        # Solución: usar el mejor tiempo real de laps como clave de ordenación.
        r = r.copy()
        r["Position"] = pd.to_numeric(r["Position"], errors="coerce")

        if not laps.empty:
            # Mejor vuelta de cada piloto (en segundos) → desempata duplicados
            bl = (
                laps.groupby("Driver")["LapTime"]
                .min()
                .dropna()
                .apply(lambda x: x.total_seconds())
                .rename("_best_secs")
                .reset_index()
                .rename(columns={"Driver": "Abbreviation"})
            )
            r = r.merge(bl, on="Abbreviation", how="left")
            r["_best_secs"] = r["_best_secs"].fillna(float("inf"))
            r = r.sort_values(["Position", "_best_secs"]).reset_index(drop=True)
            r = r.drop(columns=["_best_secs"])
        else:
            r = r.sort_values("Position").reset_index(drop=True)

        r["Position"] = range(1, len(r) + 1)

        rows = []
        for i, row in r.iterrows():
            drv = str(row.get("Abbreviation", ""))
            pos = str(int(row["Position"]))
            team = row.get("TeamName", "")
            tcolor = TEAM_COLORS.get(team, "#ffffff")
            logo = TEAM_LOGOS.get(team, "")
            # Logos locales procesados (fondo eliminado, alta calidad)
            _ld      = TEAM_LOGO_LOCAL.get(team)
            logo_src = _ld[0] if _ld else ""
            logo_blend = _ld[1] if _ld else ""

            row_bg = ROW_BG
            if len(selected_list) > 0 and drv == selected_list[0]:
                row_bg = "rgba(0, 210, 30, 0.25)"
            elif len(selected_list) > 1 and drv == selected_list[1]:
                row_bg = "rgba(255, 0, 0, 0.25)"

            drv_laps = laps[laps['Driver'] == drv] if not laps.empty else pd.DataFrame()
            best_lap = safe_min(drv_laps['LapTime'])     if not drv_laps.empty else pd.NaT
            best_s1  = safe_min(drv_laps['Sector1Time']) if not drv_laps.empty else pd.NaT
            best_s2  = safe_min(drv_laps['Sector2Time']) if not drv_laps.empty else pd.NaT
            best_s3  = safe_min(drv_laps['Sector3Time']) if not drv_laps.empty else pd.NaT

            last_row = drv_laps.iloc[-1] if not drv_laps.empty else None
            last_lap = last_row['LapTime']     if last_row is not None else pd.NaT
            last_s1  = last_row['Sector1Time'] if last_row is not None else pd.NaT
            last_s2  = last_row['Sector2Time'] if last_row is not None else pd.NaT
            last_s3  = last_row['Sector3Time'] if last_row is not None else pd.NaT

            # ── Tire info from last completed lap ──────────────────────────
            compound           = "?"
            tyre_life          = 0
            compound_color_val = "#d1d5db"
            if last_row is not None:
                raw_c = str(last_row.get('Compound') or '').upper().strip()
                compound = COMPOUND_ABBR.get(raw_c, raw_c[:1] if raw_c and raw_c != 'NAN' else '?')
                compound_color_val = COMPOUND_COLORS.get(raw_c, "#d1d5db")
                try:
                    tl = last_row.get('TyreLife')
                    tyre_life = int(tl) if (tl is not None and not pd.isna(tl)) else len(drv_laps[drv_laps['Compound'] == last_row.get('Compound')])
                except Exception:
                    tyre_life = len(drv_laps[drv_laps['Compound'] == last_row.get('Compound', None)])

            tyre_txt = f"{tyre_life} {compound}" if compound != "?" else "?"

            bl_color = COLOR_PURPLE if (not pd.isna(best_lap) and not pd.isna(overall_best_lap) and best_lap == overall_best_lap) else "#fff"
            bl_bg    = "transparent" if bl_color == "#fff" else "rgba(177, 93, 255, 0.15)"

            def sec_format(val, pb, overall):
                if pd.isna(val): return "", TEXT_COLOR, "transparent"
                if not pd.isna(overall) and val == overall: return f"{val.total_seconds():.3f}", COLOR_PURPLE, "rgba(177, 93, 255, 0.15)"
                if not pd.isna(pb) and val == pb:          return f"{val.total_seconds():.3f}", COLOR_GREEN,  "transparent"
                return f"{val.total_seconds():.3f}", COLOR_YELLOW, "transparent"

            def seg_color(val, pb, overall):
                if pd.isna(val): return "#374151"
                if not pd.isna(overall) and val == overall: return COLOR_PURPLE
                if not pd.isna(pb)      and val == pb:      return COLOR_GREEN
                return COLOR_YELLOW

            # Best-sector columns
            s1_txt, s1_c, s1_bg = sec_format(best_s1, best_s1, overall_best_s1)
            s2_txt, s2_c, s2_bg = sec_format(best_s2, best_s2, overall_best_s2)
            s3_txt, s3_c, s3_bg = sec_format(best_s3, best_s3, overall_best_s3)

            # Last-lap sector columns
            ls1_txt, ls1_c, _ = sec_format(last_s1, best_s1, overall_best_s1)
            ls2_txt, ls2_c, _ = sec_format(last_s2, best_s2, overall_best_s2)
            ls3_txt, ls3_c, _ = sec_format(last_s3, best_s3, overall_best_s3)

            # Mini-sector bars: 8 barras por sector (24 total), separadas por sector
            sc1 = seg_color(last_s1, best_s1, overall_best_s1)
            sc2 = seg_color(last_s2, best_s2, overall_best_s2)
            sc3 = seg_color(last_s3, best_s3, overall_best_s3)
            _gap = html.Div(style={"width": "5px", "height": "18px", "backgroundColor": "transparent", "flexShrink": "0"})
            _seg = lambda c: html.Div(style={"width": "7px", "height": "18px", "backgroundColor": c, "flexShrink": "0", "borderRadius": "2px"})
            mini_sectors = html.Div(
                style={"display": "flex", "gap": "2px", "height": "18px", "alignItems": "center", "justifyContent": "center", "flexWrap": "nowrap"},
                children=(
                    [_seg(sc1) for _ in range(9)]   # S1 → 9 segmentos
                    + [_gap]
                    + [_seg(sc2) for _ in range(9)]  # S2 → 9 segmentos
                    + [_gap]
                    + [_seg(sc3) for _ in range(8)]  # S3 → 8 segmentos
                )
            )

            interval = str(row.get("Time", "")).split()[0] if not pd.isna(row.get("Time")) else ""
            if interval and not interval.startswith("+") and interval != "Interval": interval = "+" + interval
            if pos == "1": interval = "Interval"

            int_bg    = COLOR_GREEN if pos != "1" else "transparent"
            int_color = "#000"      if pos != "1" else "#fff"
            int_style = {"backgroundColor": int_bg, "color": int_color, "fontWeight": "700", "padding": "2px 4px", "borderRadius": "2px"} if pos != "1" else {"color": "#fff"}

            last_lap_color = COLOR_GREEN if (not pd.isna(last_lap) and not pd.isna(best_lap) and last_lap == best_lap) else "#d1d5db"

            rows.append(html.Tr(id={'type': 'driver-row', 'index': drv}, style={"backgroundColor": row_bg, "transition": "background-color 0.1s"}, children=[
                html.Td(None, className="f1-pit-cell"),
                html.Td(pos, style={"backgroundColor": tcolor, "color": "#000", "fontWeight": "800", "textAlign": "center", "width": "25px", "borderBottom": "1px solid #1a1e2b"}),
                html.Td(html.Div(style={"display": "flex", "alignItems": "center", "gap": "7px"}, children=[
                    html.Img(
                        src=logo_src,
                        style={
                            "height": "18px", "width": "auto", "maxWidth": "36px",
                            "objectFit": "contain", "flexShrink": "0",
                            "mixBlendMode": "screen" if logo_blend == "logo-screen" else "normal",
                            "opacity": "0.9",
                            "filter": "drop-shadow(0 1px 2px rgba(0,0,0,0.5))",
                        }
                    ) if logo_src else html.Span(style={"width": "20px", "display": "inline-block"}),
                    html.Span(drv, style={"color": tcolor, "fontWeight": "700", "fontSize": "0.9rem"})
                ]), style=td_s),
                html.Td(html.Span(interval, style=int_style), style=td_center),
                html.Td(html.Span(tyre_txt, style={"color": compound_color_val, "fontWeight": "700"}), style=td_center),
                html.Td(format_time(best_lap), style={**td_center, "color": bl_color, "backgroundColor": bl_bg}),
                html.Td(interval, style={**td_center, "color": "#d1d5db"}),
                html.Td(format_time(last_lap), style={**td_center, "color": last_lap_color}),
                html.Td(mini_sectors, style=td_center),
                html.Td(ls1_txt, style={**td_center, "color": ls1_c, "borderRight": "none"}),
                html.Td(ls2_txt, style={**td_center, "color": ls2_c, "borderRight": "none"}),
                html.Td(ls3_txt, style={**td_center, "color": ls3_c}),
                html.Td(s1_txt, style={**td_center, "color": s1_c, "backgroundColor": s1_bg, "borderRight": "none"}),
                html.Td(s2_txt, style={**td_center, "color": s2_c, "backgroundColor": s2_bg, "borderRight": "none"}),
                html.Td(s3_txt, style={**td_center, "color": s3_c, "backgroundColor": s3_bg})
            ]))

        return html.Table(
            style={"width": "100%", "borderCollapse": "collapse", "borderRight": f"1px solid {BORDER_COLOR}"},
            children=[
                html.Thead([
                    html.Tr([
                        html.Th("PIT", style={**th_center, "width": "30px"}),
                        html.Th("⋮⋮",  style={**th_center, "width": "25px"}),
                        html.Th("⋮⋮ DRIVER ↑", style=th_s),
                        html.Th("⋮⋮ INTERVAL ↑", style=th_center),
                        html.Th("⋮⋮ TYRE ↑", style=th_center),
                        html.Th("⋮⋮ BEST LAP ↑", style=th_center),
                        html.Th("⋮⋮ LEADER ↑", style=th_center),
                        html.Th("⋮⋮ LAST LAP ↑", style=th_center),
                        html.Th("⋮⋮ MINI SECTORS", style=th_center),
                        html.Th("⋮⋮ LAST SECTORS ↑", colSpan=3, style=th_center),
                        html.Th("⋮⋮ BEST SECTORS ↑", colSpan=3, style=th_center),
                    ])
                ]),
                html.Tbody(rows)
            ]
        )
    except Exception as e:
        return html.Div(f"Error: {e}", style={"color": "red"})

def create_map(year, gp, session_type, selected_list):
    try:
        s = get_ff1_session(year, gp, session_type, with_telemetry=True)
        if s.laps is None or len(s.laps) == 0:
            raise Exception("Sin laps cargados")
        lap = s.laps.pick_fastest()
        if lap is None:
            raise Exception("Sin vuelta rápida")
        tel = lap.get_telemetry()
        x, y = tel["X"].values, tel["Y"].values
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=x, y=y, mode="lines", line=dict(color="#4682B4", width=4), showlegend=False))
        
        try:
            ci = s.get_circuit_info()
            if hasattr(ci, "drs_zones"):
                for z in ci.drs_zones:
                    st = int(z["start"] / tel["Distance"].iloc[-1] * len(x))
                    en = int(z["end"] / tel["Distance"].iloc[-1] * len(x))
                    if st < len(x) and en < len(x):
                        fig.add_trace(go.Scatter(x=x[st:en], y=y[st:en], mode="lines", line=dict(color="#FF4500", width=5), showlegend=False))
        except: pass
        
        fig.update_layout(
            template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=100, b=0),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False, scaleanchor="y"),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False)
        )
        return dcc.Graph(figure=fig, config={"displayModeBar": False}, style={"height": "100%"})
    except Exception as e:
        return html.Div()

# ── ROUTING UPDATES ─────────────────────────────────────────────────────────

def build_timing_table_ff1(year, gp, stype):
    """
    Construye timing table usando FastF1 cuando OpenF1 no tiene datos.
    Retorna el mismo formato que build_timing_table.
    """
    s = get_ff1_session(year, gp, stype, with_telemetry=False)
    if s is None:
        return [], {}

    laps = s.laps
    results = s.results.copy()
    results["Position"] = pd.to_numeric(results["Position"], errors="coerce")
    results = results.sort_values("Position").reset_index(drop=True)

    def ts(delta):
        if pd.isna(delta): return None
        try: return delta.total_seconds()
        except: return None

    obb  = ts(laps["LapTime"].min())
    obs1 = ts(laps["Sector1Time"].min())
    obs2 = ts(laps["Sector2Time"].min())
    obs3 = ts(laps["Sector3Time"].min())

    def sec_color(val, pb, overall):
        if val is None: return "gray"
        if overall and abs(val - overall) < 0.001: return "purple"
        if pb and abs(val - pb) < 0.001: return "green"
        return "yellow"

    rows = []
    for _, r in results.iterrows():
        drv  = str(r.get("Abbreviation", ""))
        tc   = TEAM_COLORS_FF1.get(r.get("TeamName", ""), "#888")
        pos  = r.get("Position")
        pos  = int(pos) if not pd.isna(pos) else None
        dl   = laps[laps["Driver"] == drv]
        bl   = ts(dl["LapTime"].min()) if not dl.empty else None
        bs1  = ts(dl["Sector1Time"].min()) if not dl.empty else None
        bs2  = ts(dl["Sector2Time"].min()) if not dl.empty else None
        bs3  = ts(dl["Sector3Time"].min()) if not dl.empty else None
        ll   = dl.iloc[-1] if not dl.empty else None
        llt  = ts(ll["LapTime"])     if ll is not None else None
        ls1  = ts(ll["Sector1Time"]) if ll is not None else None
        ls2  = ts(ll["Sector2Time"]) if ll is not None else None
        ls3  = ts(ll["Sector3Time"]) if ll is not None else None
        lnum = int(dl.iloc[-1]["LapNumber"]) if not dl.empty else 0

        # Neumático del último stint
        compound = "?"
        stint_laps = 0
        if not dl.empty:
            last_c = dl.iloc[-1].get("Compound", "?")
            compound = str(last_c).upper() if not pd.isna(last_c) else "?"
            same_compound = dl[dl["Compound"] == last_c]
            stint_laps = len(same_compound)

        # Mini sectores (3 barras, colores reales)
        def mc(v, pb, ov):
            if v is None: return "#374151"
            if ov and abs(v-ov)<0.001: return PURPLE
            if pb and abs(v-pb)<0.001: return GREEN
            return YELLOW

        seg_colors = [mc(ls1,bs1,obs1), mc(ls2,bs2,obs2), mc(ls3,bs3,obs3)]

        # Gap al líder
        gap = str(r.get("Time","")).split()[0] if not pd.isna(r.get("Time")) else None
        if gap and not gap.startswith("+") and pos != 1: gap = f"+{gap}"
        if pos == 1: gap = None

        rows.append({
            "driver_number": drv,
            "abbreviation":  drv,
            "full_name":     str(r.get("FullName","")),
            "team_name":     str(r.get("TeamName","")),
            "team_color":    tc,
            "headshot":      "",
            "position":      pos,
            "lap_number":    lnum,
            "compound":      compound,
            "stint_laps":    stint_laps,
            "gap":           gap,
            "best_lap":      bl,
            "best_s1":       bs1,
            "best_s2":       bs2,
            "best_s3":       bs3,
            "last_laptime":  llt,
            "last_s1": ls1, "last_s2": ls2, "last_s3": ls3,
            "seg1":[], "seg2":[], "seg3":[],
            "s1_color":  sec_color(ls1, bs1, obs1),
            "s2_color":  sec_color(ls2, bs2, obs2),
            "s3_color":  sec_color(ls3, bs3, obs3),
            "lap_color": sec_color(llt, bl,  obb),
            "best_lap_color": "purple" if (bl and obb and abs(bl-obb)<0.001) else "white",
            "seg_colors": seg_colors,
            "pits": 0,
        })

    rows.sort(key=lambda x: (x["position"] is None, x["position"] or 999))
    return rows, {"best_lap":obb,"best_s1":obs1,"best_s2":obs2,"best_s3":obs3}

# ── TABLA PLACEHOLDER (nombres estáticos mientras carga FastF1) ──────────────
def _create_placeholder_table(selected_list):
    """
    Tabla de espera con las MISMAS clases CSS que create_live_table().
    Cuando el feed en vivo arranque, React solo rellena los valores vacíos;
    el DOM structure es idéntico → sin salto visual.
    """
    def _empty_segs():
        """Crea segmentos vacíos únicos por fila — evita compartir el mismo objeto entre filas."""
        return (
            [html.Div(className="f1-seg") for _ in range(9)]
            + [html.Div(className="f1-seg seg-gap")]
            + [html.Div(className="f1-seg") for _ in range(9)]
            + [html.Div(className="f1-seg seg-gap")]
            + [html.Div(className="f1-seg") for _ in range(8)]
        )

    rows = []
    # Ordenar por número de piloto (sin posición aún, orden fijo)
    for num in sorted(DRIVER_STATIC.keys(), key=lambda x: int(x)):
        info      = DRIVER_STATIC[num]
        tla       = info["tla"]
        full_name = info["full_name"]
        team      = info["team"]
        tcolor    = TEAM_COLORS.get(team, "#555")
        last_name = full_name.split()[-1] if full_name else ""

        is_cadillac  = (team == "Cadillac")
        cadillac_cls = " cadillac" if is_cadillac else ""

        logo_data    = TEAM_LOGO_LOCAL.get(team)
        logo_src     = logo_data[0] if logo_data else None
        logo_cls     = ("f1-team-logo " + logo_data[1]).strip() if logo_data else "f1-team-logo"

        driver_cell = html.Div(
            style={"display":"flex","alignItems":"center","gap":"0","padding":"0","height":"100%"},
            children=[
                html.Div("–", className="f1-pos-embed",
                         style={"backgroundColor": "#1a1f2b"}),
                html.Img(src=logo_src, className=logo_cls) if logo_src
                    else html.Div(style={"width":"26px","flexShrink":"0"}),
                html.Span(tla, className=f"f1-tla{cadillac_cls}",
                          style={"color": tcolor, "marginLeft":"6px"}),
            ]
        )

        rows.append(html.Tr(
            id={'type':'driver-row','index':tla},
            className="f1-table-row",
            children=[
                html.Td(None, className="f1-pit-cell"),
                html.Td(driver_cell, style={"padding":"0"}),
                html.Td(html.Span("–", className="f1-interval-val")),
                html.Td(html.Div(className="f1-tyre-cell", children=[
                    html.Span("(?)", className="f1-tyre-letter", style={"color":"#555"}),
                ])),
                html.Td(html.Span("–", className="f1-time t-muted")),
                html.Td(html.Span("–", className="f1-gap-val")),
                html.Td(html.Span("–", className="f1-time t-muted")),
                html.Td(html.Div(_empty_segs(), className="f1-minisectors")),
                html.Td(html.Span("", className="f1-sector t-muted"), style={"borderRight":"none"}),
                html.Td(html.Span("", className="f1-sector t-muted"), style={"borderRight":"none"}),
                html.Td(html.Span("", className="f1-sector t-muted")),
                html.Td(html.Span("", className="f1-sector t-muted"), style={"borderRight":"none"}),
                html.Td(html.Span("", className="f1-sector t-muted"), style={"borderRight":"none"}),
                html.Td(html.Span("", className="f1-sector t-muted")),
            ]
        ))

    return html.Div(className="f1-table-wrapper", children=[
        html.Div(className="f1-live-badge", style={"opacity":"0.4"}, children=[
            html.Div(className="f1-live-dot"),
            html.Span("EN ESPERA"),
        ]),
        html.Table(className="f1-table", children=[
            html.Thead(html.Tr([
                html.Th("PIT",          style={"width":"34px",  "minWidth":"34px"}),
                html.Th("DRIVER",       className="th-left", style={"width":"190px","minWidth":"190px"}),
                html.Th("INTERVAL",     style={"width":"80px",  "minWidth":"80px"}),
                html.Th("TYRE",         style={"width":"56px",  "minWidth":"56px"}),
                html.Th("BEST LAP",     style={"width":"82px",  "minWidth":"82px"}),
                html.Th("LEADER",       style={"width":"80px",  "minWidth":"80px"}),
                html.Th("LAST LAP",     style={"width":"82px",  "minWidth":"82px"}),
                html.Th("MINI SECTORS", style={"width":"200px", "minWidth":"200px"}),
                html.Th("LAST S",       colSpan=3, style={"width":"174px","minWidth":"174px"}),
                html.Th("BEST S",       colSpan=3, style={"width":"174px","minWidth":"174px"}),
            ])),
            html.Tbody(rows),
        ])
    ])

# ── SANITIZACIÓN DE POSICIÓN ─────────────────────────────────────────────────
# Buffer module-level: mantiene el último entero válido por número de piloto.
# Nunca retorna NaN, None-string ni vacío al DOM.
_pos_buffer: dict = {}

def _safe_pos(drv_num: str, raw) -> int | None:
    """
    Convierte el campo posición a int limpio.
    - Si es válido (> 0): actualiza el buffer y retorna el int.
    - Si no (NaN, "", None, "NaN", float): retorna el último conocido o None.
    Garantiza que sort_key y el DOM nunca reciban un valor inválido.
    """
    try:
        n = int(float(str(raw)))     # float() primero absorbe "NaN", "1.0", etc.
        if n > 0:
            _pos_buffer[drv_num] = n
            return n
    except (ValueError, TypeError):
        pass
    return _pos_buffer.get(drv_num)  # None si no se ha visto aún

# ── LIVE TABLE — Premium F1 Dark Mode ────────────────────────────────────────
def create_live_table(selected_list):
    state   = live_timing.get_state()
    drivers = state["drivers"]
    dlist   = state["driver_list"]

    # Construir lista completa: feed live + todos los conocidos del static map
    # Así siempre se muestran todos los pilotos aunque el feed solo tenga algunos
    all_nums = set(drivers.keys()) | set(DRIVER_STATIC.keys())
    # Si el feed no tiene NINGÚN piloto, mostrar mensaje
    if not drivers and not dlist:
        return html.Div("⏳ Conectando al feed en vivo de F1…",
            style={"color": MUTED, "padding": "60px", "textAlign": "center", "fontSize": "1.1rem"})

    best   = live_timing.get_overall_best(drivers)
    ob_lap = best["lap"]
    ob_s   = [best["s0"], best["s1"], best["s2"]]
    selected_list = selected_list or []

    def sort_key(num):
        d     = drivers.get(num, {})
        pos_n = _safe_pos(num, d.get("position", "")) or 99
        has_time = 0 if d.get("best_lap") else 1
        return (has_time, pos_n, d.get("best_lap") or 9999, num)

    SEG_CSS = {
        0:"f1-seg", 256:"f1-seg",
        2048:"f1-seg seg-yellow", 2049:"f1-seg seg-green", 2051:"f1-seg seg-purple"
    }

    rows = []
    for drv_num in sorted(all_nums, key=sort_key):
        d    = drivers.get(drv_num, {})
        info = get_driver_info(drv_num, dlist)
        tla       = info.get("tla") or drv_num
        full_name = info.get("full_name", "")
        team      = info.get("team", "")
        tcolor    = TEAM_COLORS.get(team, info.get("team_color","#555"))
        pos_int   = _safe_pos(drv_num, d.get("position", ""))
        pos_disp  = str(pos_int) if pos_int else "–"
        last_name = full_name.split()[-1] if full_name else ""
        # Cadillac: clase CSS extra para identidad blanca
        is_cadillac  = (team == "Cadillac")
        cadillac_cls = " cadillac" if is_cadillac else ""

        # Row highlight
        row_style = {}
        if selected_list and tla == selected_list[0]:
            row_style = {"backgroundColor":"rgba(0,210,30,0.10)"}
        elif len(selected_list) > 1 and tla == selected_list[1]:
            row_style = {"backgroundColor":"rgba(255,60,60,0.10)"}

        # ── Tyre: formato "31 (H)" ──────────────────────────────────────────
        raw_c      = d.get("compound","?")
        comp_abbr  = live_timing.COMPOUND_ABBR.get(raw_c, "?")
        comp_color = live_timing.COMPOUND_COLOR.get(raw_c, "#888")
        tyre_laps  = d.get("tyre_laps", 0)
        tyre_cell  = html.Div(className="f1-tyre-cell", children=[
            html.Span(f"{tyre_laps} " if tyre_laps else "",
                      style={"color":"#9aa0b0","fontFamily":"var(--font-mono)","fontSize":"0.78rem"}),
            html.Span(f"({comp_abbr})", className="f1-tyre-letter", style={"color": comp_color}),
        ])

        # ── Tiempos ──────────────────────────────────────────────────────────
        bl    = d.get("best_lap")
        ll    = d.get("last_lap")
        ll_ob = d.get("last_lap_ob", False)
        ll_pb = d.get("last_lap_pb", False)
        bl_is_ob = bl and ob_lap and abs(bl - ob_lap) < 0.001

        bl_cls = "f1-time " + ("t-purple" if bl_is_ob else "t-white" if bl else "t-muted")
        ll_cls = "f1-time " + ("t-purple" if ll_ob else "t-pb-bg" if ll_pb else "t-muted" if not ll else "t-white")

        def sec_cls(secs, si, is_pb, is_ob):
            if not secs: return "f1-sector t-muted"
            if is_ob or (ob_s[si] and abs(secs - ob_s[si]) < 0.001): return "f1-sector t-purple"
            if is_pb: return "f1-sector t-green"
            return "f1-sector t-yellow"

        sectors = d.get("sectors", {})

        # ── Last sector cells ────────────────────────────────────────────────
        ls_cells = []
        for si in range(3):
            sec  = sectors.get(si, {})
            sval = sec.get("value","")
            sc   = sec_cls(sec.get("secs"), si, sec.get("pb",False), sec.get("ob",False))
            br   = {"borderRight":"none"} if si < 2 else {}
            bg   = {"backgroundColor":"rgba(177,93,255,0.08)"} if sec.get("ob") else {}
            ls_cells.append(html.Td(html.Span(sval, className=sc), style={**br, **bg}))

        # ── Best sector cells (TimingStats) ──────────────────────────────────
        bs_data = d.get("best_sectors", {})
        bs_cells = []
        for si in range(3):
            bs   = bs_data.get(si, {})
            bval = bs.get("value", "")
            bob  = bs.get("ob", False)
            bsecs = bs.get("secs")
            is_ob_overall = bob or (bsecs and ob_s[si] and abs(bsecs - ob_s[si]) < 0.001)
            sc = "f1-sector t-purple" if (is_ob_overall and bval) else ("f1-sector t-white" if bval else "f1-sector t-muted")
            bg = {"backgroundColor":"rgba(177,93,255,0.08)"} if is_ob_overall and bval else {}
            br = {"borderRight":"none"} if si < 2 else {}
            bs_cells.append(html.Td(html.Span(bval, className=sc), style={**br, **bg}))

        # ── Mini sectores ────────────────────────────────────────────────────
        SEG_COUNT = [9, 9, 8]
        mini_segs = []
        for si in range(3):
            if si > 0:
                mini_segs.append(html.Div(className="f1-seg seg-gap"))
            segs = sectors.get(si,{}).get("segments",{})
            if segs:
                for idx in sorted(segs.keys()):
                    st = segs[idx]
                    mini_segs.append(html.Div(className=SEG_CSS.get(int(st),"f1-seg")))
            else:
                mini_segs.extend([html.Div(className="f1-seg") for _ in range(SEG_COUNT[si])])

        # ── PIT status ───────────────────────────────────────────────────────
        in_pit  = d.get("in_pit",  False)
        pit_out = d.get("pit_out", False)
        if in_pit:
            pit_el = html.Span("PIT", className="f1-pit-badge pit-in")
        elif pit_out:
            pit_el = html.Span("OUT", className="f1-pit-badge pit-out")
        else:
            pit_el = None

        # ── LEADER (gap al primero) ───────────────────────────────────────
        gap = str(d.get("gap","")).strip()
        is_lapped_gap = gap.upper().endswith("L")
        if gap and not is_lapped_gap and not gap.startswith("+") and pos_int != 1:
            gap = f"+{gap}"
        gap_el = html.Span("Leader", className="f1-gap-leader") if pos_int == 1 \
                 else html.Span(gap or "–", className="f1-gap-val")

        # ── INTERVAL (gap al piloto de delante) ──────────────────────────
        if pos_int == 1:
            itv_el = html.Span("Interval", className="f1-itv-label")
        else:
            itv_raw = d.get("interval", "")
            itv_str = str(itv_raw).strip()
            is_lapped_itv = itv_str.upper().endswith("L")
            if is_lapped_itv:
                # Lapped: show "1L", "2L" with muted style, no green bg
                itv_el = html.Span(itv_str, className="f1-interval-val")
            elif itv_str and itv_str[0].isdigit():
                itv_str = f"+{itv_str}"
                itv_el = html.Span(itv_str, className="f1-itv-gap")
            elif itv_str and itv_str.startswith("+"):
                itv_el = html.Span(itv_str, className="f1-itv-gap")
            else:
                itv_el = html.Span("–", className="f1-interval-val")

        # ── Logo + driver cell (pos embebida, logo, TLA) ─────────────────
        logo_data = TEAM_LOGO_LOCAL.get(team)
        logo_src  = logo_data[0] if logo_data else None
        logo_cls  = ("f1-team-logo " + logo_data[1]).strip() if logo_data else "f1-team-logo"

        driver_cell = html.Div(
            style={"display":"flex","alignItems":"center","gap":"0","padding":"0","height":"100%"},
            children=[
                html.Div(pos_disp, className="f1-pos-embed",
                         style={"backgroundColor": tcolor if pos_int else "#1a1f2b"}),
                html.Img(src=logo_src, className=logo_cls) if logo_src
                    else html.Div(style={"width":"26px","flexShrink":"0"}),
                html.Span(tla, className=f"f1-tla{cadillac_cls}",
                          style={"color": tcolor, "marginLeft":"6px"}),
            ]
        )

        rows.append(html.Tr(
            id={'type':'driver-row','index':tla},
            className="f1-table-row",
            style=row_style,
            children=[
                html.Td(pit_el, className="f1-pit-cell"),
                html.Td(driver_cell, style={"padding":"0"}),
                html.Td(itv_el),
                html.Td(tyre_cell),
                html.Td(html.Span(live_timing.fmt_time(bl) or "–", className=bl_cls,
                                  style={"backgroundColor":"rgba(177,93,255,0.10)"} if bl_is_ob else {})),
                html.Td(gap_el),
                html.Td(html.Span(live_timing.fmt_time(ll) or "–", className=ll_cls)),
                html.Td(html.Div(mini_segs, className="f1-minisectors")),
                *ls_cells,
                *bs_cells,
            ]
        ))

    return html.Div(className="f1-table-wrapper", children=[
        html.Div(className="f1-live-badge", children=[
            html.Div(className="f1-live-dot"),
            html.Span("LIVE"),
        ]),
        html.Table(className="f1-table", children=[
            html.Thead(html.Tr([
                html.Th("PIT",          style={"width":"34px",  "minWidth":"34px"}),
                html.Th("DRIVER",       className="th-left", style={"width":"190px","minWidth":"190px"}),
                html.Th("INTERVAL",     style={"width":"80px",  "minWidth":"80px"}),
                html.Th("TYRE",         style={"width":"56px",  "minWidth":"56px"}),
                html.Th("BEST LAP",     style={"width":"82px",  "minWidth":"82px"}),
                html.Th("LEADER",       style={"width":"80px",  "minWidth":"80px"}),
                html.Th("LAST LAP",     style={"width":"82px",  "minWidth":"82px"}),
                html.Th("MINI SECTORS", style={"width":"200px", "minWidth":"200px"}),
                html.Th("LAST S",       colSpan=3, style={"width":"174px","minWidth":"174px"}),
                html.Th("BEST S",       colSpan=3, style={"width":"174px","minWidth":"174px"}),
            ])),
            html.Tbody(rows),
        ])
    ])

@app.callback(
    Output("live-table-content", "children"),
    Output("live-map-content", "children"),
    Output("current-session", "data"),
    Output("session-title", "children"),
    Output("session-flag-img", "src"),
    Input("selected-drivers", "data"),
    Input("data-refresh-interval", "n_intervals"),
    State("current-session", "data"),
    State("active-tab", "data"),
    prevent_initial_call=True
)
def update_live(selected_list, n_intervals, sess_old, active_tab):
    if not active_tab or active_tab != "live":
        raise dash.exceptions.PreventUpdate

    # ── PRIORIDAD 1: SignalR WebSocket (feed nativo F1) ─────────────────────────
    live_state = live_timing.get_state()
    has_live_data = bool(live_state.get("drivers"))
    session_status = live_state.get("session_status", "")
    is_session_active = session_status in ("Active", "Started", "AbortedStart")
    if live_timing.is_fresh(max_age=30) and has_live_data and is_session_active:
        now_year = pd.Timestamp.now().year
        # Detectar sesión si no la tenemos o es del año incorrecto
        sess_info = sess_old
        if not sess_info or sess_info.get("year", 0) != now_year or sess_info.get("gp","") == "Japan":
            try:
                sessions = get_ff1_latest_session()
                if sessions:
                    s = sessions[0]
                    sess_info = {
                        "year":  s.get("_ff1_year", now_year),
                        "gp":    s.get("meeting_name", ""),
                        "type":  s.get("session_name", ""),
                        "flag":  s.get("country_code", "us"),
                    }
            except Exception:
                pass
        gp_name  = (sess_info or {}).get("gp", "") or "Live"
        stype    = (sess_info or {}).get("type", "") or "Timing"
        flag_src = f"https://flagcdn.com/{(sess_info or {}).get('flag','us')}.svg"
        title    = f"{gp_name} · {stype} {now_year}"
        return (
            create_live_table(selected_list),
            html.Div(),
            sess_info or sess_old or {"year": now_year, "gp": gp_name, "type": stype},
            title,
            flag_src
        )

    # ── PRIORIDAD 1.5: OpenF1 API polling (fallback si SignalR no funciona) ─────
    of1_state = get_of1_live_state()
    if of1_state and of1_state.get("drivers"):
        # Inyectar datos OF1 en live_timing para reutilizar create_live_table
        with live_timing._lock:
            live_timing._state["drivers"]     = of1_state["drivers"]
            live_timing._state["driver_list"] = of1_state["driver_list"]
            live_timing._state["connected"]   = True
            live_timing._state["last_update"] = of1_state["ts"]
        now_year = pd.Timestamp.now().year
        sess_info = sess_old
        try:
            sessions_of1 = of1_get("sessions", "session_key=latest", ttl=60)
            if sessions_of1:
                s = sessions_of1[-1]
                gp_name = s.get("meeting_name","") or s.get("circuit_short_name","") or "Live"
                stype   = s.get("session_name","") or "Race"
                flag_src = f"https://flagcdn.com/us.svg"
                title    = f"{gp_name} · {stype} {now_year}"
                sess_info = {"year": now_year, "gp": gp_name, "type": stype, "flag": "us"}
        except Exception as e:
            print(f"[OF1 sess] {e}")
            gp_name  = (sess_old or {}).get("gp","Live")
            stype    = (sess_old or {}).get("type","Race")
            flag_src = "https://flagcdn.com/us.svg"
            title    = f"{gp_name} · {stype} {now_year}"
        return (
            create_live_table(selected_list),
            html.Div(),
            sess_info or sess_old or {},
            title,
            flag_src
        )

    # ── PRIORIDAD 2: detectar sesión actual con cache (no spamear FF1 API) ──────
    now_year = pd.Timestamp.now().year
    sess_info = get_current_session_info()   # cache 5 min

    year      = sess_info.get("year", now_year)
    gp        = sess_info.get("gp", "") or sess_info.get("_ff1_gp", "")
    stype     = sess_info.get("type", "") or sess_info.get("_ff1_stype", "")
    flag_code = sess_info.get("flag", "us")
    title     = f"{gp} · {stype} {year}" if gp else f"Live Timing {year}"
    flag_src  = f"https://flagcdn.com/{flag_code}.svg"

    # ── PRIORIDAD 3: datos históricos en caché (SOLO sesiones completadas) ──────
    cache_key = f"{year}-{gp}-{stype}-False"
    if cache_key in _session_mem:
        s = _session_mem[cache_key]
        try:
            has_results = s.results is not None and not s.results.empty
        except Exception:
            has_results = False
        if has_results:
            # Sesión terminada con datos reales → tabla histórica FastF1
            return (
                create_table(year, gp, stype, selected_list),
                html.Div(),
                sess_info,
                title,
                flag_src
            )
        # Sin resultados → sesión en curso o muy reciente → caer al placeholder

    # ── PRIORIDAD 4: disparar carga en background + mostrar tabla estática ──────
    if gp and stype:
        _ensure_session_loading(year, gp, stype)

    # Mostrar tabla con nombres del mapa estático mientras llegan los datos reales
    table = _create_placeholder_table(selected_list)
    return (table, html.Div(), sess_info or sess_old, title, flag_src)

# ── REPLAY LOGIC ────────────────────────────────────────────────────────────
@app.callback(
    Output("replay-form-container", "style"),
    Output("replay-results-container", "children"),
    Input("btn-search-replay", "n_clicks"),
    State("replay-year", "value"), State("replay-gp", "value"), State("replay-session", "value"),
    prevent_initial_call=True
)
def trigger_replay_search(n_clicks, year, gp, session_type):
    new_style = {
        "display": "flex", "flexDirection": "row", "justifyContent": "center", "alignItems": "center",
        "height": "auto", "padding": "20px", "transition": "all 0.5s ease", "gap": "20px"
    }
    
    results = dcc.Loading(
        type="circle", color=COLOR_PURPLE,
        children=[
            html.Div(style={"marginTop": "20px", "textAlign": "center", "color": "#aaa", "marginBottom": "20px"}, children="Cargando Telemetría Histórica..."),
            generate_dashboard_layout("replay-table-content", "replay-map-content")
        ]
    )
    return new_style, results

@app.callback(
    Output("replay-table-content", "children"),
    Output("replay-map-content", "children"),
    Input("replay-results-container", "children"),
    State("replay-year", "value"), State("replay-gp", "value"), State("replay-session", "value"),
    State("selected-drivers", "data")
)
def populate_replay_data(dummy, year, gp, session_type, selected_list):
    if not dummy: raise dash.exceptions.PreventUpdate
    return create_table(year, gp, session_type, selected_list), create_map(year, gp, session_type, selected_list)





# ── CALENDAR VIEW COMPONENTS ────────────────────────────────────────────────
from datetime import datetime
import pytz

@app.callback(
    Output("selected-calendar-event", "data"),
    Input({"type": "calendar-card", "index": dash.ALL}, "n_clicks"),
    Input({"type": "btn-back", "index": dash.ALL}, "n_clicks"),
    prevent_initial_call=True
)
def select_calendar_event(card_clicks, btn_back):
    ctx = dash.callback_context
    if not ctx.triggered: raise dash.exceptions.PreventUpdate
    trigger = ctx.triggered[0]['prop_id']
    
    # Handle back button click
    if "btn-back" in trigger:
        # btn_back is a list of all n_clicks for all btn-back components
        # We need to check if any of them was clicked (i.e. has a value > 0)
        if not any(btn_back): raise dash.exceptions.PreventUpdate
        return None
        
    try:
        trigger_id = json.loads(trigger.split('.')[0])
        clicked_ev = trigger_id['index']
        val = ctx.triggered[0]['value']
        if not val: raise dash.exceptions.PreventUpdate
        return clicked_ev
    except: raise dash.exceptions.PreventUpdate

def render_calendar_view():
    return html.Div([
        html.Div(id="calendar-main-content"),
        dcc.Interval(id="clock-interval", interval=1000, n_intervals=0),
        dcc.Store(id="next-race-times", data={"p1": 0, "race": 0})
    ])

@app.callback(
    Output("calendar-main-content", "children"),
    Output("next-race-times", "data"),
    Input("tab-content", "children"),
    Input("selected-calendar-event", "data")
)
def update_calendar_view(dummy, selected_event):
    year = 2026
    sched = fastf1.get_event_schedule(year)
    now_utc = pd.to_datetime('now', utc=True)
    
    if selected_event is None:
        finished_cards = []
        future_cards = []
        next_card = None
        next_race_times = {"p1": 0, "race": 0}
        
        # Sort schedule chronologically
        sched = sched.sort_values(by="RoundNumber")
        
        for idx, row in sched.iterrows():
            if row["EventFormat"] == 'testing': continue
            
            ev_name = row["EventName"]
            round_num = row["RoundNumber"]
            country = row["Country"]
            loc = row["Location"]
            
            try: race_dt = pd.to_datetime(row["Session5DateUtc"]).tz_localize('UTC')
            except: race_dt = pd.to_datetime(row["Session5Date"]).tz_localize('UTC')
            
            is_past = race_dt < now_utc
            is_next = not is_past and next_card is None
            
            sessions = []
            for i in range(1, 6):
                s_name = row.get(f"Session{i}")
                s_dt_val = row.get(f"Session{i}DateUtc")
                if pd.isna(s_name) or pd.isna(s_dt_val): continue
                try: s_dt = pd.to_datetime(s_dt_val).tz_localize('UTC')
                except: s_dt = pd.to_datetime(s_dt_val)
                sessions.append({"name": s_name, "dt": s_dt})
            
            if is_next:
                next_race_times = {"p1": sessions[0]["dt"].timestamp() if sessions else 0, "race": sessions[-1]["dt"].timestamp() if sessions else 0}
                
                session_divs = []
                for s in sessions:
                    local_dt = s["dt"].tz_convert(datetime.now().astimezone().tzinfo) if s["dt"] else None
                    dt_str = local_dt.strftime('%d/%m, %H:%M') if local_dt else ""
                    session_divs.append(html.Div(
                        style={
                            "display": "flex", "justifyContent": "space-between", 
                            "padding": "12px 15px", "backgroundColor": "#23263a", 
                            "marginBottom": "5px", "borderRadius": "8px"
                        }, 
                        children=[
                            html.Span(s["name"], style={"color": "#fff", "fontWeight": "600", "fontSize": "0.9rem"}),
                            html.Span(dt_str, style={"color": "#a0aab8", "fontSize": "0.85rem"})
                        ]
                    ))
                
                next_card = html.Div(
                    id="next-race-anchor",
                    style={
                        "backgroundColor": "#1e2235", "borderRadius": "10px", "overflow": "hidden",
                        "boxShadow": "0 8px 30px rgba(0,0,0,0.5)", "marginBottom": "40px", "marginTop": "40px"
                    },
                    children=[
                        html.Div(style={"backgroundColor": "#FF1801", "padding": "15px 20px", "display": "flex", "justifyContent": "space-between", "alignItems": "center"}, children=[
                            html.Div("Next Race", style={"color": "#fff", "fontWeight": "800", "fontSize": "1.3rem"}),
                            html.Div(f"Round {round_num}/22", style={"color": "rgba(255,255,255,0.9)", "fontSize": "0.9rem"})
                        ]),
                        html.Div(style={"padding": "20px", "display": "flex", "gap": "30px", "flexWrap": "wrap"}, children=[
                            html.Div(style={"flex": "1", "minWidth": "300px"}, children=[
                                html.Div(style={"display": "flex", "alignItems": "center", "gap": "15px", "marginBottom": "20px"}, children=[
                                    html.Div(style={"width": "40px", "height": "25px", "backgroundColor": "#fff", "borderRadius": "2px", "display": "flex", "justifyContent": "center", "alignItems": "center", "color": "#000", "fontWeight": "bold", "fontSize": "0.8rem"}, children=country[:2].upper()),
                                    html.Div(children=[
                                        html.Div(ev_name, style={"color": "#fff", "fontWeight": "800", "fontSize": "1.3rem", "lineHeight": "1.2"}),
                                        html.Div(country, style={"color": "rgba(255,255,255,0.6)", "fontSize": "0.9rem"})
                                    ])
                                ]),
                                html.Div(id="countdown-container", style={"backgroundColor": "#2f272a", "borderRadius": "10px", "padding": "25px", "minHeight": "200px"})
                            ]),
                            html.Div(style={"flex": "1", "minWidth": "300px"}, children=[
                                html.Div("Schedule", style={"color": "#fff", "fontWeight": "800", "textAlign": "center", "marginBottom": "15px", "fontSize": "1.1rem"}),
                                html.Div(children=session_divs)
                            ])
                        ])
                    ]
                )
                # Wrap next_card so it can be clicked
                next_card = html.Div(id={"type": "calendar-card", "index": ev_name}, n_clicks=0, style={"cursor": "pointer"}, children=[next_card])
                
            elif is_past:
                # Usar caché si ya está disponible, si no mostrar placeholder sin bloquear
                cache_key = f"{year}-{ev_name}-Race-False"
                top_list = []
                if cache_key in _session_mem:
                    try:
                        s_past = _session_mem[cache_key]
                        for _, r in s_past.results.head(5).iterrows():
                            pos = str(r.get('Position', ''))
                            if pos.endswith('.0'): pos = pos[:-2]
                            top_list.append(html.Div(
                                style={"display": "flex", "justifyContent": "space-between",
                                       "padding": "12px 15px", "backgroundColor": "#1e2235",
                                       "marginBottom": "5px", "borderRadius": "8px"},
                                children=[
                                    html.Span(f"{pos}. {r.get('Abbreviation', '')}", style={"color": "#fff", "fontWeight": "700", "fontSize": "0.9rem"}),
                                    html.Span(r.get('TeamName', ''), style={"color": "#a0aab8", "fontSize": "0.85rem"})
                                ]
                            ))
                    except Exception:
                        pass
                if not top_list:
                    top_list = [html.Div("Click para ver resultados", style={"color": "#a0aab8", "padding": "15px", "textAlign": "center"})]

                # Finished Card based on image
                card = html.Div(
                    id={"type": "calendar-card", "index": ev_name},
                    n_clicks=0,
                    style={
                        "backgroundColor": "#151922", "borderRadius": "10px", "overflow": "hidden",
                        "boxShadow": "0 4px 15px rgba(0,0,0,0.2)", "cursor": "pointer",
                        "border": "1px solid #2a2f45", "display": "flex", "flexDirection": "column",
                        "transition": "transform 0.2s"
                    },
                    children=[
                        # GREY HEADER
                        html.Div(style={"backgroundColor": "#2a2b2f", "padding": "15px 20px", "display": "flex", "justifyContent": "space-between", "alignItems": "center"}, children=[
                            html.Div(children=[
                                html.Div(country, style={"color": "rgba(255,255,255,0.8)", "fontSize": "0.85rem", "marginBottom": "2px"}),
                                html.Div("FINISHED", style={"color": "#fff", "fontWeight": "800", "fontSize": "1.2rem", "lineHeight": "1"})
                            ]),
                            html.Div(style={"textAlign": "right", "lineHeight": "1"}, children=[
                                html.Div(str(round_num), style={"color": "#fff", "fontWeight": "800", "fontSize": "1.5rem"}),
                                html.Div("/22", style={"color": "rgba(255,255,255,0.8)", "fontSize": "0.8rem", "fontWeight": "600"})
                            ])
                        ]),
                        # BODY
                        html.Div(style={"padding": "15px"}, children=top_list)
                    ]
                )
                finished_cards.append((round_num, card))
                
            else:
                # Future Card (Red Header, Grid of sessions)
                session_divs = []
                for s in sessions:
                    local_dt = s["dt"].tz_convert(datetime.now().astimezone().tzinfo) if s["dt"] else None
                    dt_str = local_dt.strftime('%d/%m, %H:%M') if local_dt else ""
                    session_divs.append(html.Div(
                        style={
                            "display": "flex", "justifyContent": "space-between", 
                            "padding": "12px 15px", "backgroundColor": "#1e2235", 
                            "marginBottom": "5px", "borderRadius": "8px"
                        }, 
                        children=[
                            html.Span(s["name"], style={"color": "#fff", "fontWeight": "500", "fontSize": "0.9rem"}),
                            html.Span(dt_str, style={"color": "#a0aab8", "fontSize": "0.85rem"})
                        ]
                    ))
                
                card = html.Div(
                    id={"type": "calendar-card", "index": ev_name},
                    n_clicks=0,
                    style={
                        "backgroundColor": "#151922", "borderRadius": "10px", "overflow": "hidden",
                        "boxShadow": "0 4px 15px rgba(0,0,0,0.2)", "cursor": "pointer",
                        "border": "1px solid #2a2f45", "display": "flex", "flexDirection": "column",
                        "transition": "transform 0.2s"
                    },
                    children=[
                        html.Div(style={"backgroundColor": "#FF1801", "padding": "15px 20px", "display": "flex", "justifyContent": "space-between", "alignItems": "center"}, children=[
                            html.Div(style={"display": "flex", "alignItems": "center", "gap": "15px"}, children=[
                                html.Div(style={"width": "30px", "height": "20px", "backgroundColor": "#fff", "borderRadius": "2px", "display": "flex", "justifyContent": "center", "alignItems": "center", "color": "#000", "fontWeight": "bold", "fontSize": "0.7rem"}, children=country[:2].upper()),
                                html.Div(children=[
                                    html.Div(ev_name, style={"color": "#fff", "fontWeight": "800", "fontSize": "1.1rem", "lineHeight": "1.2"}),
                                    html.Div(country, style={"color": "rgba(255,255,255,0.8)", "fontSize": "0.85rem"})
                                ])
                            ]),
                            html.Div(style={"textAlign": "right", "lineHeight": "1"}, children=[
                                html.Div(str(round_num), style={"color": "#fff", "fontWeight": "800", "fontSize": "1.5rem"}),
                                html.Div("/22", style={"color": "rgba(255,255,255,0.8)", "fontSize": "0.8rem", "fontWeight": "600"})
                            ])
                        ]),
                        html.Div(style={"padding": "15px"}, children=session_divs)
                    ]
                )
                future_cards.append((round_num, card))
                
        # Sort and Extract
        finished_cards.sort(key=lambda x: x[0])
        future_cards.sort(key=lambda x: x[0])
        
        grid_finished = html.Div(style={"display": "grid", "gridTemplateColumns": "repeat(auto-fill, minmax(350px, 1fr))", "gap": "20px"}, children=[c for r, c in finished_cards])
        grid_future = html.Div(style={"display": "grid", "gridTemplateColumns": "repeat(auto-fill, minmax(350px, 1fr))", "gap": "20px"}, children=[c for r, c in future_cards])
        
        layout = html.Div(style={"padding": "20px", "maxWidth": "1200px", "margin": "0 auto"}, children=[
            html.H1("Formula 1 Calendar 2026", style={"color": "#fff", "fontFamily": "'Inter', sans-serif", "fontWeight": "800", "textAlign": "center", "marginBottom": "40px", "marginTop": "10px", "fontSize": "2.5rem"}),
            grid_finished,
            next_card if next_card else html.Div(),
            grid_future
        ])
        
        return html.Div(id="calendar-scroll-wrapper", style={"animation": "fadeIn 0.5s"}, children=layout), next_race_times
    else:
        # DETAIL VIEW
        ev_row = sched[sched["EventName"] == selected_event]
        if ev_row.empty: return html.Div(style={"animation": "fadeIn 0.5s"}, children=[html.Button("← BACK", id={"type": "btn-back", "index": "calendar"}, n_clicks=0)]), {"p1": 0, "race": 0}
        ev_row = ev_row.iloc[0]
        
        country = ev_row.get("Country", "")
        ev_name = ev_row.get("EventName", "")
        location = ev_row.get("Location", "")
        
        sessions = []
        for i in range(1, 6):
            s_name = ev_row.get(f"Session{i}")
            s_dt_val = ev_row.get(f"Session{i}DateUtc")
            if pd.isna(s_name) or pd.isna(s_dt_val): continue
            try: s_dt = pd.to_datetime(s_dt_val).tz_localize('UTC')
            except: s_dt = pd.to_datetime(s_dt_val)
            sessions.append({"name": s_name, "dt": s_dt})
            
        session_list = []
        colors = ["#3671C6", "#229971", "#FF8000", "#B15DFF", "#FF1801"]
        icons = ["🏎️", "🏎️", "🏎️", "⏱️", "🏁"]
        
        for idx, s in enumerate(sessions):
            local_dt = s["dt"].tz_convert(datetime.now().astimezone().tzinfo) if s["dt"] else None
            date_str = local_dt.strftime('%d %b') if local_dt else ""
            time_str = local_dt.strftime('%H:%M') if local_dt else ""
            
            c = colors[idx % len(colors)]
            icon = icons[idx % len(icons)]
            
            session_list.append(html.Div(
                style={
                    "display": "flex", "justifyContent": "space-between", "alignItems": "center",
                    "padding": "20px 25px", "backgroundColor": "#151922", "marginBottom": "15px", 
                    "borderRadius": "20px", "borderLeft": f"5px solid {c}",
                    "boxShadow": "0 4px 15px rgba(0,0,0,0.3)"
                }, 
                children=[
                    html.Div(style={"display": "flex", "alignItems": "center", "gap": "15px"}, children=[
                        html.Div(icon, style={"fontSize": "1.5rem", "backgroundColor": "#1e2235", "padding": "10px", "borderRadius": "12px"}),
                        html.Span(s["name"], style={"color": "#fff", "fontWeight": "800", "fontSize": "1.2rem"})
                    ]),
                    html.Div(style={"textAlign": "right"}, children=[
                        html.Div(date_str, style={"color": "#a0aab8", "fontWeight": "600", "fontSize": "0.9rem"}),
                        html.Div(time_str, style={"color": c, "fontWeight": "800", "fontSize": "1.4rem"})
                    ])
                ]
            ))
            
        # USER REQUESTED FASTF1 TRACK RECORDS LOGIC
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

        best_qualy = None  # (LapTime timedelta, driver_full, year)
        best_race  = None  # (LapTime timedelta, driver_full, year)

        for yr in range(2018, 2026):
            try:
                yr_sched = fastf1.get_event_schedule(yr, include_testing=False)
                ev_match = yr_sched[yr_sched["EventName"] == selected_event]
                if ev_match.empty:
                    ev_match = yr_sched[yr_sched["Location"] == location]
                if ev_match.empty:
                    continue
                ev_yr_name = ev_match.iloc[0]["EventName"]

                # Qualifying
                try:
                    q = fastf1.get_session(yr, ev_yr_name, "Q")
                    q.load(telemetry=False, weather=False, messages=False)
                    ql = q.laps[["Driver", "LapTime"]].dropna(subset=["LapTime"])
                    if not ql.empty:
                        best_idx = ql["LapTime"].idxmin()
                        lt = ql.loc[best_idx, "LapTime"]
                        drv = ql.loc[best_idx, "Driver"]
                        try:
                            full = q.get_driver(drv).get("FullName", drv)
                        except:
                            full = drv
                        if best_qualy is None or lt < best_qualy[0]:
                            best_qualy = (lt, full, yr)
                except:
                    pass

                # Race fastest lap
                try:
                    r = fastf1.get_session(yr, ev_yr_name, "R")
                    r.load(telemetry=False, weather=False, messages=False)
                    rl = r.laps[["Driver", "LapTime"]].dropna(subset=["LapTime"])
                    if not rl.empty:
                        best_idx = rl["LapTime"].idxmin()
                        lt = rl.loc[best_idx, "LapTime"]
                        drv = rl.loc[best_idx, "Driver"]
                        try:
                            full = r.get_driver(drv).get("FullName", drv)
                        except:
                            full = drv
                        if best_race is None or lt < best_race[0]:
                            best_race = (lt, full, yr)
                except:
                    pass

            except:
                continue

        qualy_record = f"{abbr_name(best_qualy[1])} - {fmt_laptime(best_qualy[0])} - {best_qualy[2]}" if best_qualy else "No data"
        race_record  = f"{abbr_name(best_race[1])} - {fmt_laptime(best_race[0])} - {best_race[2]}" if best_race else "No data"
        
        return html.Div(style={"animation": "fadeIn 0.5s", "padding": "30px", "maxWidth": "1300px", "margin": "0 auto", "position": "relative"}, children=[
            # Floating Back Button
            html.Button(
                "← ALL RACES", 
                id={"type": "btn-back", "index": "calendar"}, n_clicks=0,
                style={
                    "position": "absolute", "top": "10px", "left": "30px",
                    "backgroundColor": "#1e2235", "color": "#fff", "border": "1px solid #2a2f45", 
                    "padding": "12px 25px", "borderRadius": "30px", "cursor": "pointer", 
                    "fontWeight": "800", "boxShadow": "0 8px 20px rgba(0,0,0,0.4)",
                    "zIndex": "100", "display": "flex", "alignItems": "center", "gap": "10px",
                    "transition": "all 0.3s ease"
                }
            ),
            
            # Header
            html.Div(style={"textAlign": "center", "marginBottom": "50px", "marginTop": "60px"}, children=[
                html.H1(ev_name, style={"color": "#fff", "fontFamily": "'Inter', sans-serif", "fontWeight": "900", "fontSize": "3.5rem", "marginBottom": "10px"}),
                html.Span(f"{location}, {country}", style={"color": "#a0aab8", "fontSize": "1.5rem", "fontWeight": "600", "letterSpacing": "2px", "textTransform": "uppercase"})
            ]),
            
            dbc.Row(style={"marginTop": "20px"}, children=[
                # Left Column - Schedule
                dbc.Col(width=12, lg=5, style={"marginBottom": "30px"}, children=[
                    html.Div(style={"backgroundColor": "#1e2235", "borderRadius": "20px", "padding": "35px", "boxShadow": "0 10px 40px rgba(0,0,0,0.5)"}, children=[
                        html.H3("Weekend Schedule", style={"color": "#fff", "marginBottom": "30px", "fontWeight": "800", "fontSize": "1.8rem", "borderBottom": "2px solid #2a2f45", "paddingBottom": "15px"}),
                        html.Div(children=session_list)
                    ])
                ]),
                
                # Right Column - Map & Records
                dbc.Col(width=12, lg=7, children=[
                    html.Div(style={"display": "flex", "flexDirection": "column", "gap": "30px", "height": "100%"}, children=[
                        # Map Container
                        html.Div(style={
                            "backgroundColor": "#1e2235", "borderRadius": "20px", "padding": "35px", 
                            "boxShadow": "0 10px 40px rgba(0,0,0,0.5)", "flex": "1", 
                            "display": "flex", "flexDirection": "column"
                        }, children=[
                            html.H3("Circuit Map", style={"color": "#fff", "marginBottom": "20px", "fontWeight": "800", "fontSize": "1.8rem"}),
                            html.Div(style={
                                "width": "100%", "flex": "1", "minHeight": "400px", "backgroundColor": "#151922", 
                                "border": "2px solid #2a2f45", "borderRadius": "15px", 
                                "display": "flex", "alignItems": "center", "justifyContent": "center",
                                "position": "relative", "overflow": "hidden",
                                "backgroundImage": "radial-gradient(#2a2f45 1px, transparent 1px)", "backgroundSize": "20px 20px"
                            }, children=[
                                html.Span("High-Res Track Layout", style={"color": "#a0aab8", "fontWeight": "700", "fontSize": "1.2rem", "position": "absolute"}),
                                html.Div(style={"width": "60%", "height": "50%", "border": "4px solid #fff", "borderRadius": "100px 30px 100px 30px", "position": "absolute", "opacity": "0.1"}),
                                html.Div(style={"position": "absolute", "bottom": "30px", "right": "30px", "backgroundColor": "rgba(0, 255, 0, 0.1)", "border": "1px solid #00ff00", "color": "#00ff00", "padding": "8px 15px", "borderRadius": "8px", "fontWeight": "800", "fontSize": "0.9rem"}, children="DRS ZONE ACTIVE")
                            ])
                        ]),
                        
                        # Records Container
                        html.Div(style={
                            "backgroundColor": "#1e2235", "borderRadius": "20px", "padding": "35px", 
                            "boxShadow": "0 10px 40px rgba(0,0,0,0.5)"
                        }, children=[
                            html.H3("Track Records", style={"color": "#fff", "marginBottom": "25px", "fontWeight": "800", "fontSize": "1.8rem"}),
                            html.Div(style={"display": "flex", "gap": "20px"}, children=[
                                html.Div(style={"flex": "1", "backgroundColor": "#151922", "padding": "25px", "borderRadius": "15px", "borderLeft": "5px solid #B15DFF", "boxShadow": "inset 0 0 20px rgba(177,93,255,0.05)"}, children=[
                                    html.Div("All-Time Qualy Record", style={"color": "#B15DFF", "fontSize": "0.9rem", "fontWeight": "800", "textTransform": "uppercase", "letterSpacing": "1px", "marginBottom": "10px"}),
                                    html.Div(qualy_record, style={"color": "#fff", "fontWeight": "700", "fontSize": "1.1rem"}),
                                ]),
                                html.Div(style={"flex": "1", "backgroundColor": "#151922", "padding": "25px", "borderRadius": "15px", "borderLeft": "5px solid #00D21E", "boxShadow": "inset 0 0 20px rgba(0,210,30,0.05)"}, children=[
                                    html.Div("Race Lap Record", style={"color": "#00D21E", "fontSize": "0.9rem", "fontWeight": "800", "textTransform": "uppercase", "letterSpacing": "1px", "marginBottom": "10px"}),
                                    html.Div(race_record, style={"color": "#fff", "fontWeight": "700", "fontSize": "1.1rem"}),
                                ])
                            ])
                        ])
                    ])
                ])
            ])
        ]), {"p1": 0, "race": 0}

@app.callback(
    Output("countdown-container", "children"),
    Input("clock-interval", "n_intervals"),
    State("next-race-times", "data"),
    prevent_initial_call=True
)
def update_countdown(n, times):
    try:
        now = datetime.now().timestamp()
        
        def format_diff(diff):
            if diff < 0: return None
            d = int(diff // 86400)
            h = int((diff % 86400) // 3600)
            m = int((diff % 3600) // 60)
            s = int(diff % 60)
            return d, h, m, s
            
        p1_diff = times.get("p1", 0) - now
        race_diff = times.get("race", 0) - now
        
        val_style = {"fontSize": "2.2rem", "fontWeight": "400", "color": "#fff", "fontFamily": "'Inter', sans-serif"}
        lbl_style = {"fontSize": "0.8rem", "color": "#8e98a8", "fontWeight": "500", "marginTop": "2px"}
        
        def make_row(title, diff):
            if diff < 0:
                return html.Div(style={"marginBottom": "20px"}, children=[
                    html.Div(f"{title} in:", style={"color": "#fff", "fontSize": "1.2rem", "marginBottom": "10px"}),
                    html.Span("STARTED", style={"color": "#00D21E", "fontWeight": "800", "fontSize": "1.5rem"})
                ])
            d, h, m, s = format_diff(diff)
            return html.Div(style={"marginBottom": "20px"}, children=[
                html.Div(f"{title} in:", style={"color": "#fff", "fontSize": "1.2rem", "marginBottom": "10px"}),
                html.Div(style={"display": "flex", "gap": "20px"}, children=[
                    html.Div(style={"display": "flex", "flexDirection": "column", "alignItems": "center", "minWidth": "50px"}, children=[html.Span(f"{d}", style=val_style), html.Span("days", style=lbl_style)]),
                    html.Div(style={"display": "flex", "flexDirection": "column", "alignItems": "center", "minWidth": "50px"}, children=[html.Span(f"{h}", style=val_style), html.Span("hours", style=lbl_style)]),
                    html.Div(style={"display": "flex", "flexDirection": "column", "alignItems": "center", "minWidth": "50px"}, children=[html.Span(f"{m}", style=val_style), html.Span("minutes", style=lbl_style)]),
                    html.Div(style={"display": "flex", "flexDirection": "column", "alignItems": "center", "minWidth": "50px"}, children=[html.Span(f"{s}", style=val_style), html.Span("seconds", style=lbl_style)])
                ])
            ])
            
        return [
            make_row("Practice 1", p1_diff),
            make_row("Race", race_diff)
        ]
    except:
        return ""

app.clientside_callback(
    """
    function(children) {
        if (children) {
            setTimeout(function() {
                var el = document.getElementById('next-race-anchor');
                if (el) {
                    el.scrollIntoView({behavior: 'smooth', block: 'center'});
                }
            }, 500);
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("tab-content", "data-scroll-dummy"),
    Input("tab-content", "children")
)


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8055))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
