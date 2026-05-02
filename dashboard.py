import os, json, warnings, time, requests, threading
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
_active_session_cache = {"data": None, "ts": 0}

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
    "Haas F1 Team": "#B6BABD", "Haas": "#B6BABD"
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

        rows = []
        for i, row in r.iterrows():
            drv = str(row.get("Abbreviation", ""))
            pos = str(row.get("Position", ""))
            if pos.endswith(".0"): pos = pos[:-2]
            team = row.get("TeamName", "")
            tcolor = TEAM_COLORS.get(team, "#ffffff")
            logo = TEAM_LOGOS.get(team, "")

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

            # Mini-sector bars colored by last-lap performance
            sc1 = seg_color(last_s1, best_s1, overall_best_s1)
            sc2 = seg_color(last_s2, best_s2, overall_best_s2)
            sc3 = seg_color(last_s3, best_s3, overall_best_s3)
            mini_sectors = html.Div(style={"display": "flex", "gap": "2px", "height": "16px", "alignItems": "center", "justifyContent": "center"}, children=[
                html.Div(style={"width": "3px", "height": "16px", "backgroundColor": sc1}),
                html.Div(style={"width": "3px", "height": "16px", "backgroundColor": sc1}),
                html.Div(style={"width": "3px", "height": "16px", "backgroundColor": sc2}),
                html.Div(style={"width": "3px", "height": "16px", "backgroundColor": sc2}),
                html.Div(style={"width": "3px", "height": "16px", "backgroundColor": sc3}),
                html.Div(style={"width": "3px", "height": "16px", "backgroundColor": sc3}),
            ])

            interval = str(row.get("Time", "")).split()[0] if not pd.isna(row.get("Time")) else ""
            if interval and not interval.startswith("+") and interval != "Interval": interval = "+" + interval
            if pos == "1": interval = "Interval"

            int_bg    = COLOR_GREEN if pos != "1" else "transparent"
            int_color = "#000"      if pos != "1" else "#fff"
            int_style = {"backgroundColor": int_bg, "color": int_color, "fontWeight": "700", "padding": "2px 4px", "borderRadius": "2px"} if pos != "1" else {"color": "#fff"}

            last_lap_color = COLOR_GREEN if (not pd.isna(last_lap) and not pd.isna(best_lap) and last_lap == best_lap) else "#d1d5db"

            rows.append(html.Tr(id={'type': 'driver-row', 'index': drv}, style={"backgroundColor": row_bg, "transition": "background-color 0.1s"}, children=[
                html.Td(pos, style={"backgroundColor": tcolor, "color": "#000", "fontWeight": "800", "textAlign": "center", "width": "25px", "borderBottom": "1px solid #1a1e2b"}),
                html.Td(html.Div(style={"display": "flex", "alignItems": "center"}, children=[
                    html.Img(src=logo, style={"height": "10px", "marginRight": "8px", "filter": "brightness(0) invert(1) opacity(0.8)"}) if logo else html.Span(style={"width":"18px"}),
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
                        html.Th("⋮⋮", style={**th_center, "width": "25px"}),
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

# ── LIVE TABLE (feed oficial F1) ─────────────────────────────────────────────
def create_live_table(selected_list):
    """Tabla construida 100% desde el feed de live timing — microsectores reales."""
    state   = live_timing.get_state()
    drivers = state["drivers"]
    dlist   = state["driver_list"]

    if not drivers:
        return html.Div(
            "⏳ Conectando al feed en vivo de F1…",
            style={"color": MUTED, "padding": "60px", "textAlign": "center", "fontSize": "1.2rem"}
        )

    best = live_timing.get_overall_best(drivers)
    ob_lap = best["lap"]
    ob_s   = [best["s0"], best["s1"], best["s2"]]

    th_s      = {"backgroundColor": TABLE_HEADER_BG, "color": "#a0aab8", "fontSize": "0.75rem",
                 "fontWeight": "600", "padding": "8px 4px", "borderBottom": "2px solid #2a2f45",
                 "borderRight": "1px solid #1a1e2b", "fontFamily": "'Fira Code', monospace", "textAlign": "left"}
    th_center = {**th_s, "textAlign": "center"}
    td_s      = {"padding": "4px 4px", "borderBottom": "1px solid #1a1e2b", "borderRight": "1px solid #1a1e2b",
                 "fontSize": "0.85rem", "fontWeight": "500", "color": "#fff",
                 "fontFamily": "'Fira Code', monospace", "textAlign": "left",
                 "verticalAlign": "middle", "whiteSpace": "nowrap", "cursor": "pointer"}
    td_center = {**td_s, "textAlign": "center"}

    selected_list = selected_list or []

    # Ordenar por posición → mejor vuelta → número de piloto
    def sort_key(item):
        drv_num, d = item
        pos = d.get("position", "")
        try: pos_n = int(pos)
        except Exception: pos_n = 99
        bl = d.get("best_lap") or 9999
        return (pos_n, bl, drv_num)

    sorted_drivers = sorted(drivers.items(), key=sort_key)

    rows = []
    for drv_num, d in sorted_drivers:
        info  = dlist.get(drv_num, {})
        tla   = info.get("tla") or drv_num
        team  = info.get("team", "")
        raw_color = info.get("team_color", "#888888")
        tcolor = TEAM_COLORS.get(team, raw_color)
        logo   = TEAM_LOGOS.get(team, "")
        pos    = d.get("position", "")

        row_bg = ROW_BG
        if len(selected_list) > 0 and tla == selected_list[0]:
            row_bg = "rgba(0, 210, 30, 0.25)"
        elif len(selected_list) > 1 and tla == selected_list[1]:
            row_bg = "rgba(255, 0, 0, 0.25)"

        # Neumático
        raw_c   = d.get("compound", "?")
        comp_abbr  = live_timing.COMPOUND_ABBR.get(raw_c, raw_c[:1] if raw_c and raw_c != "?" else "?")
        comp_color = live_timing.COMPOUND_COLOR.get(raw_c, "#d1d5db")
        tyre_laps  = d.get("tyre_laps", 0)
        tyre_txt   = f"{tyre_laps} {comp_abbr}" if comp_abbr != "?" else "?"

        # Tiempos
        bl     = d.get("best_lap")
        ll     = d.get("last_lap")
        ll_pb  = d.get("last_lap_pb", False)
        ll_ob  = d.get("last_lap_ob", False)

        bl_color = COLOR_PURPLE if (bl and ob_lap and abs(bl - ob_lap) < 0.001) else "#fff"
        bl_bg    = "rgba(177,93,255,0.15)" if bl_color == COLOR_PURPLE else "transparent"
        ll_color = COLOR_PURPLE if ll_ob else (COLOR_GREEN if ll_pb else "#d1d5db")

        def sec_color_live(secs, si, is_pb, is_ob):
            if secs is None: return TEXT_COLOR
            if is_ob: return COLOR_PURPLE
            if is_pb: return COLOR_GREEN
            if ob_s[si] and abs(secs - ob_s[si]) < 0.001: return COLOR_PURPLE
            return COLOR_YELLOW

        sectors = d.get("sectors", {})

        # Últimos sectores
        ls_cells = []
        for si in range(3):
            sec  = sectors.get(si, {})
            sval = sec.get("value", "")
            secs = sec.get("secs")
            is_pb = sec.get("pb", False)
            is_ob = sec.get("ob", False)
            sc   = sec_color_live(secs, si, is_pb, is_ob)
            br   = "none" if si < 2 else None
            style = {**td_center, "color": sc}
            if br: style["borderRight"] = br
            if is_ob: style["backgroundColor"] = "rgba(177,93,255,0.15)"
            ls_cells.append(html.Td(sval, style=style))

        # Microsectores reales (todos los segmentos del feed)
        mini_bars = []
        for si in range(3):
            segs = sectors.get(si, {}).get("segments", {})
            if segs:
                for seg_idx in sorted(segs.keys()):
                    status = segs[seg_idx]
                    color  = live_timing.seg_color(status)
                    mini_bars.append(html.Div(style={
                        "width": "3px", "height": "16px",
                        "backgroundColor": color, "flexShrink": "0"
                    }))
            else:
                # Sin datos aún — 2 barras grises por sector
                for _ in range(2):
                    mini_bars.append(html.Div(style={
                        "width": "3px", "height": "16px",
                        "backgroundColor": "#374151", "flexShrink": "0"
                    }))

        mini_sectors = html.Div(
            style={"display": "flex", "gap": "1px", "height": "16px",
                   "alignItems": "center", "justifyContent": "center", "flexWrap": "nowrap"},
            children=mini_bars
        )

        # Gap / interval
        gap = d.get("gap", "")
        if gap and not gap.startswith("+") and pos != "1": gap = f"+{gap}"
        if pos == "1": gap = "Leader"
        int_bg    = COLOR_GREEN if pos not in ("1", "") else "transparent"
        int_color = "#000"      if pos not in ("1", "") else "#fff"
        int_style = {"backgroundColor": int_bg, "color": int_color, "fontWeight": "700",
                     "padding": "2px 4px", "borderRadius": "2px"} if pos not in ("1", "") else {"color": "#fff"}

        rows.append(html.Tr(
            id={'type': 'driver-row', 'index': tla},
            style={"backgroundColor": row_bg, "transition": "background-color 0.1s"},
            children=[
                html.Td(pos, style={"backgroundColor": tcolor, "color": "#000", "fontWeight": "800",
                                    "textAlign": "center", "width": "25px", "borderBottom": "1px solid #1a1e2b"}),
                html.Td(html.Div(style={"display": "flex", "alignItems": "center"}, children=[
                    html.Img(src=logo, style={"height": "10px", "marginRight": "8px",
                             "filter": "brightness(0) invert(1) opacity(0.8)"}) if logo else html.Span(style={"width": "18px"}),
                    html.Span(tla, style={"color": tcolor, "fontWeight": "700", "fontSize": "0.9rem"})
                ]), style=td_s),
                html.Td(html.Span(gap, style=int_style), style=td_center),
                html.Td(html.Span(tyre_txt, style={"color": comp_color, "fontWeight": "700"}), style=td_center),
                html.Td(live_timing.fmt_time(bl), style={**td_center, "color": bl_color, "backgroundColor": bl_bg}),
                html.Td(gap, style={**td_center, "color": "#d1d5db"}),
                html.Td(live_timing.fmt_time(ll), style={**td_center, "color": ll_color}),
                html.Td(mini_sectors, style={**td_center, "overflow": "hidden", "maxWidth": "200px"}),
                *ls_cells,
                # Best sectors — tomados del best_lap, no disponibles individualmente en live feed
                html.Td("", style={**td_center, "borderRight": "none"}),
                html.Td("", style={**td_center, "borderRight": "none"}),
                html.Td("", style=td_center),
            ]
        ))

    connected_badge = html.Span(
        "🔴 LIVE",
        style={"color": "#FF3333", "fontWeight": "700", "fontSize": "0.75rem",
               "marginLeft": "10px", "animation": "pulse 1s infinite"}
    )

    return html.Div([
        html.Div(connected_badge, style={"padding": "4px 10px", "textAlign": "right"}),
        html.Table(
            style={"width": "100%", "borderCollapse": "collapse", "borderRight": f"1px solid {BORDER_COLOR}"},
            children=[
                html.Thead([html.Tr([
                    html.Th("P",                                   style={**th_center, "width": "25px"}),
                    html.Th("DRIVER",                              style=th_s),
                    html.Th("GAP",                                 style=th_center),
                    html.Th("TYRE",                                style=th_center),
                    html.Th("BEST LAP",                            style=th_center),
                    html.Th("LEADER",                              style=th_center),
                    html.Th("LAST LAP",                            style=th_center),
                    html.Th("MINI SECTORS",                        style=th_center),
                    html.Th("S1",                                  style={**th_center, "borderRight": "none"}),
                    html.Th("S2",                                  style={**th_center, "borderRight": "none"}),
                    html.Th("S3",                                  style=th_center),
                    html.Th("BEST S1",                             style={**th_center, "borderRight": "none"}),
                    html.Th("BEST S2",                             style={**th_center, "borderRight": "none"}),
                    html.Th("BEST S3",                             style=th_center),
                ])]),
                html.Tbody(rows)
            ]
        )
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
    # ── Si el feed live está activo, úsalo directamente ──────────────────────
    if live_timing.is_fresh(max_age=30):
        state      = live_timing.get_state()
        stype      = state.get("session_status", "Live")
        drv_sample = next(iter(state["driver_list"].values()), {}) if state["driver_list"] else {}
        title      = f"Live Timing · Sprint Qualifying"
        flag_src   = f"https://flagcdn.com/us.svg"   # se actualiza abajo si hay sesión
        # Intentar obtener nombre de sesión del store anterior
        if sess_old:
            gp_name  = sess_old.get("gp", "")
            flag_src = f"https://flagcdn.com/{sess_old.get('flag','us')}.svg"
            title    = f"{gp_name} · Live Timing {pd.Timestamp.now().year}" if gp_name else title

        return (
            create_live_table(selected_list),
            html.Div(),   # mapa desactivado en live para no bloquear
            sess_old or {"year": 2026, "gp": "Live", "type": "Sprint Qualifying"},
            title,
            flag_src
        )

    # ── Fallback: FastF1 histórico ────────────────────────────────────────────
    sessions = get_latest_session()
    if not sessions or sessions[0].get("year", 0) < pd.Timestamp.now().year:
        sessions = get_ff1_latest_session()

    if not sessions:
        return (html.Div("Sin conexión. Verificá tu internet.",
                         style={"color": MUTED, "padding": "60px", "textAlign": "center"}),
                html.Div(), sess_old, "Offline", "")

    sess      = sessions[-1]
    year      = sess.get("year", 2026)
    gp        = (sess.get("meeting_name") or sess.get("location") or sess.get("country_name") or "Unknown GP")
    stype     = sess.get("session_name", "Race")
    title     = f"{gp} · {stype} {year}"
    flag_code = sess.get("country_code", "un")
    flag_src  = f"https://flagcdn.com/{flag_code}.svg"

    # Verificar si ya está en caché antes de intentar renderizar
    cache_key = f"{year}-{gp}-{stype}-False"
    if cache_key in _session_mem:
        table = create_table(year, gp, stype, selected_list)
    else:
        _ensure_session_loading(year, gp, stype)
        table = html.Div([
            html.Div("Cargando datos de sesión…",
                     style={"color": "#fff", "fontSize": "15px", "marginBottom": "6px"}),
            html.Div(f"{gp} · {stype} {year} — actualizando cada 5 s",
                     style={"color": MUTED, "fontSize": "12px"}),
        ], style={"padding": "60px", "textAlign": "center"})

    return (
        table,
        html.Div(),   # mapa desactivado para no bloquear
        {"year": year, "gp": gp, "type": stype, "flag": flag_code},
        title,
        flag_src
    )

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
