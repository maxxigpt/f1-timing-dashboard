"""
F1 Live Timing — conexión directa al feed SignalR oficial de F1.
No requiere suscripción F1TV.
"""
import os, json, time, threading, copy, requests
import websocket   # websocket-client (ya instalado como dep de signalrcore)
import urllib.parse

LIVE_FILE = os.path.join(os.path.dirname(__file__), "_live_feed.txt")

F1_HUB  = "https://livetiming.formula1.com/signalr"
F1_WSS  = "wss://livetiming.formula1.com/signalr"
TOPICS  = [
    "TimingData", "TimingAppData", "TimingStats",
    "DriverList", "SessionStatus", "LapCount",
    "ExtrapolatedClock", "WeatherData",
]
HEADERS = {
    "User-Agent":      "BestHTTP",
    "Accept-Encoding": "gzip, identity",
    "Connection":      "Upgrade",
}

# ── Estado compartido ─────────────────────────────────────────────────────────
_state = {
    "drivers": {},
    "driver_list": {},
    "session_status": "",
    "connected": False,
    "last_update": 0.0,
}
_lock = threading.Lock()
_live_file_lock = threading.Lock()

SEG_COLOR = {
    0:    "#374151",
    256:  "#374151",
    2048: "#FFD700",
    2049: "#00D21E",
    2051: "#B15DFF",
}
def seg_color(status):
    return SEG_COLOR.get(int(status), "#374151")

COMPOUND_ABBR  = {"SOFT":"S","MEDIUM":"M","HARD":"H","INTERMEDIATE":"I","WET":"W","UNKNOWN":"?"}
COMPOUND_COLOR = {"SOFT":"#FF3333","MEDIUM":"#FFD700","HARD":"#FFFFFF","INTERMEDIATE":"#39B54A","WET":"#0067FF"}

# ── Parsers ───────────────────────────────────────────────────────────────────
def parse_time(s):
    if not s: return None
    try:
        s = str(s).strip()
        if ":" in s:
            m, r = s.split(":", 1)
            return int(m) * 60 + float(r)
        return float(s)
    except Exception:
        return None

def fmt_time(secs):
    if secs is None: return ""
    try:
        m = int(secs // 60)
        s = secs % 60
        return f"{m}:{s:06.3f}" if m > 0 else f"{s:06.3f}"
    except Exception:
        return ""

# ── Procesadores de mensajes ──────────────────────────────────────────────────
def _drv_default():
    return {
        "position": "", "gap": "", "interval": "",
        "last_lap": None, "last_lap_str": "", "last_lap_pb": False, "last_lap_ob": False,
        "best_lap": None, "best_lap_str": "",
        "lap_number": 0,
        "sectors": {
            0: {"value":"","secs":None,"pb":False,"ob":False,"segments":{}},
            1: {"value":"","secs":None,"pb":False,"ob":False,"segments":{}},
            2: {"value":"","secs":None,"pb":False,"ob":False,"segments":{}},
        },
        "compound":"?","tyre_laps":0,"tyre_new":True,
        "in_pit": False, "pit_out": False,
    }

def _proc_timing_data(msg):
    for drv, data in msg.get("Lines", {}).items():
        d = _state["drivers"].setdefault(drv, _drv_default())
        if "Position" in data:
            d["position"] = str(data["Position"])
        gap = data.get("GapToLeader","")
        if gap != "":
            d["gap"] = str(gap)
        itv = data.get("IntervalToPositionAhead")
        if isinstance(itv, dict):
            d["interval"] = str(itv.get("Value",""))
        if "InPit"  in data: d["in_pit"]  = bool(data["InPit"])
        if "PitOut" in data: d["pit_out"] = bool(data["PitOut"])
        ll = data.get("LastLapTime")
        if isinstance(ll, dict) and ll.get("Value"):
            d["last_lap_str"] = ll["Value"]
            d["last_lap"]     = parse_time(ll["Value"])
            d["last_lap_pb"]  = bool(ll.get("PersonalFastest"))
            d["last_lap_ob"]  = bool(ll.get("OverallFastest"))
        bl = data.get("BestLapTime")
        if isinstance(bl, dict) and bl.get("Value"):
            d["best_lap_str"] = bl["Value"]
            d["best_lap"]     = parse_time(bl["Value"])
        lnum = data.get("NumberOfLaps")
        if lnum is not None:
            try: d["lap_number"] = int(lnum)
            except: pass
        for si_str, sec in data.get("Sectors", {}).items():
            si = int(si_str)
            s  = d["sectors"].setdefault(si, {"value":"","secs":None,"pb":False,"ob":False,"segments":{}})
            if not isinstance(sec, dict): continue
            if sec.get("Value"):
                s["value"] = sec["Value"]
                s["secs"]  = parse_time(sec["Value"])
                s["pb"]    = bool(sec.get("PersonalFastest"))
                s["ob"]    = bool(sec.get("OverallFastest"))
            for seg_k, seg_v in sec.get("Segments", {}).items():
                status = seg_v.get("Status",0) if isinstance(seg_v,dict) else int(seg_v or 0)
                s["segments"][int(seg_k)] = status
    _state["last_update"] = time.time()

def _proc_timing_app(msg):
    for drv, data in msg.get("Lines", {}).items():
        d = _state["drivers"].setdefault(drv, _drv_default())
        stints = data.get("Stints", {})
        if not stints: continue
        last_k = max(stints.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)
        st = stints[last_k]
        if not isinstance(st, dict): continue
        if "Compound" in st:
            d["compound"] = str(st["Compound"]).upper()
        try:
            d["tyre_laps"] = int(st.get("TotalLaps") or 0)
        except: pass
        d["tyre_new"] = st.get("New","TRUE") in ("TRUE", True)

def _proc_driver_list(msg):
    for drv, info in msg.items():
        if isinstance(info, dict):
            _state["driver_list"][drv] = {
                "tla":        info.get("Tla", drv),
                "full_name":  info.get("FullName",""),
                "team":       info.get("TeamName",""),
                "team_color": "#" + str(info.get("TeamColour","888888")),
                "number":     str(info.get("RacingNumber", drv)),
            }

PROCESSORS = {
    "TimingData":    _proc_timing_data,
    "TimingAppData": _proc_timing_app,
    "DriverList":    _proc_driver_list,
    "SessionStatus": lambda m: _state.update({"session_status": m.get("Status","")}),
}

def _write_line(topic, msg):
    """Guarda el mensaje al archivo de feed."""
    try:
        line = json.dumps([topic, msg]) + "\n"
        with _live_file_lock:
            with open(LIVE_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass

def _handle_signalr_message(raw):
    """Parsea el mensaje SignalR y despacha a procesadores."""
    try:
        data = json.loads(raw)
    except Exception:
        return

    # Mensajes de datos: {"M": [{"H":"Streaming","M":"feed","A":[topic, msg, ts]}]}
    for item in data.get("M", []):
        if item.get("H","").lower() == "streaming" and item.get("M","").lower() == "feed":
            args = item.get("A", [])
            if len(args) >= 2:
                topic, msg = args[0], args[1]
                _write_line(topic, msg)
                proc = PROCESSORS.get(topic)
                if proc:
                    with _lock:
                        proc(msg)
                        _state["connected"] = True
                        _state["last_update"] = time.time()

    # Respuesta inicial con snapshot
    if "R" in data and isinstance(data["R"], dict):
        for topic, msg in data["R"].items():
            _write_line(topic, msg)
            proc = PROCESSORS.get(topic)
            if proc:
                with _lock:
                    proc(msg)
                    _state["connected"] = True
                    _state["last_update"] = time.time()

# ── Conexión directa SignalR ──────────────────────────────────────────────────
def _connect_once():
    """Un intento de conexión completo. Retorna cuando se pierde la conexión."""
    conn_data = json.dumps([{"name": "Streaming"}])

    # 1. Negotiate
    neg_url = f"{F1_HUB}/negotiate"
    resp = requests.get(neg_url, params={
        "connectionData": conn_data,
        "clientProtocol": "1.5",
    }, headers={"User-Agent": "BestHTTP"}, timeout=10)
    resp.raise_for_status()
    token = resp.json()["ConnectionToken"]
    print(f"[LiveTiming] Token obtenido, conectando WebSocket...", flush=True)

    # 2. URL WebSocket
    ws_params = urllib.parse.urlencode({
        "transport":      "webSockets",
        "connectionData": conn_data,
        "clientProtocol": "1.5",
        "connectionToken": token,
    })
    ws_url = f"{F1_WSS}/connect?{ws_params}"

    connected_event = threading.Event()

    def on_open(ws):
        print("[LiveTiming] WebSocket conectado. Suscribiendo tópicos...", flush=True)
        sub = {"H": "Streaming", "M": "Subscribe", "A": [TOPICS], "I": 1}
        ws.send(json.dumps(sub))
        connected_event.set()

    def on_message(ws, message):
        _handle_signalr_message(message)

    def on_error(ws, error):
        print(f"[LiveTiming] Error WS: {error}", flush=True)

    def on_close(ws, code, msg):
        print(f"[LiveTiming] WebSocket cerrado: {code}", flush=True)

    ws = websocket.WebSocketApp(
        ws_url,
        header=HEADERS,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever(ping_interval=30, ping_timeout=10)

# ── Hilo de reconexión ────────────────────────────────────────────────────────
def _recording_thread():
    try:
        open(LIVE_FILE, "w").close()
    except Exception:
        pass

    while True:
        try:
            print("[LiveTiming] Conectando al feed de F1...", flush=True)
            _connect_once()
        except Exception as e:
            print(f"[LiveTiming] Error: {e} — reconectando en 5s", flush=True)
        with _lock:
            _state["connected"] = False
        time.sleep(5)

# ── API pública ───────────────────────────────────────────────────────────────
def start():
    t = threading.Thread(target=_recording_thread, daemon=True, name="f1-lt-rec")
    t.start()
    print("[LiveTiming] Hilo iniciado.", flush=True)

def get_state():
    with _lock:
        return copy.deepcopy(_state)

def is_fresh(max_age=15):
    with _lock:
        return _state["connected"] and (time.time() - _state["last_update"]) < max_age

def get_overall_best(drivers):
    best = {"lap":None,"s0":None,"s1":None,"s2":None}
    for d in drivers.values():
        bl = d.get("best_lap")
        if bl and (best["lap"] is None or bl < best["lap"]):
            best["lap"] = bl
        for si in range(3):
            sv = d.get("sectors",{}).get(si,{}).get("secs")
            if sv and (best[f"s{si}"] is None or sv < best[f"s{si}"]):
                best[f"s{si}"] = sv
    return best
