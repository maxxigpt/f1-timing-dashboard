"""
F1 Live Timing client — conecta al feed oficial de F1 via SignalR.
Usa FastF1's SignalRClient para grabar en archivo, luego procesa
en tiempo real con un hilo de tail. Estado compartido en memoria.
"""
import os, json, time, threading, copy

LIVE_FILE = os.path.join(os.path.dirname(__file__), "_live_feed.txt")

# ── Estado compartido ─────────────────────────────────────────────────────────
_state = {
    "drivers": {},       # "drv_num" -> dict con todos los datos
    "driver_list": {},   # "drv_num" -> {tla, full_name, team, team_color}
    "session_status": "",
    "connected": False,
    "last_update": 0.0,
}
_lock = threading.Lock()

# Códigos de estado de microsectores → color CSS
SEG_COLOR = {
    0:    "#374151",   # sin dato - gris
    256:  "#374151",   # out lap - gris
    2048: "#FFD700",   # amarillo (normal)
    2049: "#00D21E",   # verde (mejor personal)
    2051: "#B15DFF",   # púrpura (mejor sesión)
}
def seg_color(status): return SEG_COLOR.get(int(status), "#374151")

COMPOUND_ABBR   = {"SOFT": "S", "MEDIUM": "M", "HARD": "H", "INTERMEDIATE": "I", "WET": "W", "UNKNOWN": "?"}
COMPOUND_COLOR  = {"SOFT": "#FF3333", "MEDIUM": "#FFD700", "HARD": "#FFFFFF", "INTERMEDIATE": "#39B54A", "WET": "#0067FF"}

# ── Parsers ───────────────────────────────────────────────────────────────────
def parse_time(s):
    """'1:23.456' o '34.123' → float segundos, None si falla."""
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
    """float segundos → '1:23.456' o '34.123'"""
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
            0: {"value": "", "secs": None, "pb": False, "ob": False, "segments": {}},
            1: {"value": "", "secs": None, "pb": False, "ob": False, "segments": {}},
            2: {"value": "", "secs": None, "pb": False, "ob": False, "segments": {}},
        },
        "compound": "?", "tyre_laps": 0, "tyre_new": True,
    }

def _proc_timing_data(msg):
    lines = msg.get("Lines", {})
    for drv, data in lines.items():
        d = _state["drivers"].setdefault(drv, _drv_default())

        if "Position" in data:
            d["position"] = str(data["Position"])

        gap = data.get("GapToLeader", "")
        if gap != "":
            d["gap"] = str(gap)

        itv = data.get("IntervalToPositionAhead")
        if isinstance(itv, dict):
            d["interval"] = str(itv.get("Value", ""))

        ll = data.get("LastLapTime")
        if isinstance(ll, dict) and ll.get("Value"):
            d["last_lap_str"] = ll["Value"]
            d["last_lap"]    = parse_time(ll["Value"])
            d["last_lap_pb"] = bool(ll.get("PersonalFastest"))
            d["last_lap_ob"] = bool(ll.get("OverallFastest"))

        bl = data.get("BestLapTime")
        if isinstance(bl, dict) and bl.get("Value"):
            d["best_lap_str"] = bl["Value"]
            d["best_lap"]     = parse_time(bl["Value"])

        lnum = data.get("NumberOfLaps")
        if lnum is not None:
            try: d["lap_number"] = int(lnum)
            except Exception: pass

        for si_str, sec in data.get("Sectors", {}).items():
            si = int(si_str)
            s  = d["sectors"].setdefault(si, {"value": "", "secs": None, "pb": False, "ob": False, "segments": {}})
            if not isinstance(sec, dict):
                continue
            if sec.get("Value"):
                s["value"] = sec["Value"]
                s["secs"]  = parse_time(sec["Value"])
                s["pb"]    = bool(sec.get("PersonalFastest"))
                s["ob"]    = bool(sec.get("OverallFastest"))
            for seg_k, seg_v in sec.get("Segments", {}).items():
                status = seg_v.get("Status", 0) if isinstance(seg_v, dict) else int(seg_v or 0)
                s["segments"][int(seg_k)] = status

    _state["last_update"] = time.time()

def _proc_timing_app(msg):
    for drv, data in msg.get("Lines", {}).items():
        d = _state["drivers"].setdefault(drv, _drv_default())
        stints = data.get("Stints", {})
        if not stints:
            continue
        last_k = max(stints.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)
        st = stints[last_k]
        if not isinstance(st, dict):
            continue
        if "Compound" in st:
            d["compound"] = str(st["Compound"]).upper()
        try:
            d["tyre_laps"] = int(st.get("TotalLaps") or 0)
        except Exception:
            pass
        d["tyre_new"] = st.get("New", "TRUE") in ("TRUE", True)

def _proc_driver_list(msg):
    for drv, info in msg.items():
        if isinstance(info, dict):
            _state["driver_list"][drv] = {
                "tla":        info.get("Tla", drv),
                "full_name":  info.get("FullName", ""),
                "team":       info.get("TeamName", ""),
                "team_color": "#" + str(info.get("TeamColour", "888888")),
                "number":     str(info.get("RacingNumber", drv)),
            }

PROCESSORS = {
    "TimingData":    _proc_timing_data,
    "TimingAppData": _proc_timing_app,
    "DriverList":    _proc_driver_list,
    "SessionStatus": lambda m: _state.update({"session_status": m.get("Status", "")}),
}

# ── Hilo 1: graba el feed al archivo ─────────────────────────────────────────
def _recording_thread():
    from fastf1.livetiming.client import SignalRClient
    # Limpiar archivo previo solo al inicio
    try:
        open(LIVE_FILE, "w").close()
    except Exception:
        pass
    while True:
        try:
            print("[LiveTiming] Conectando al feed de F1...")
            # filemode="a" para NO truncar en cada reconexión
            client = SignalRClient(filename=LIVE_FILE, filemode="a", timeout=60)
            client.start()   # bloquea hasta que se desconecte
        except Exception as e:
            print(f"[LiveTiming] Reconectando en 5s: {e}")
        with _lock:
            _state["connected"] = False
        time.sleep(5)

# ── Hilo 2: lee el archivo en tiempo real (tail) ──────────────────────────────
def _processing_thread():
    while not os.path.exists(LIVE_FILE):
        time.sleep(0.2)

    with open(LIVE_FILE, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)   # ir al final del archivo
        while True:
            line = f.readline()
            if line:
                try:
                    parts = json.loads(line.strip())
                    topic, msg = parts[0], parts[1]
                    proc = PROCESSORS.get(topic)
                    if proc:
                        with _lock:
                            proc(msg)
                            _state["connected"] = True
                except Exception:
                    pass
            else:
                time.sleep(0.05)

# ── API pública ───────────────────────────────────────────────────────────────
def start():
    """Arranca ambos hilos (daemon). Llamar una sola vez al inicio."""
    for fn, name in [(_recording_thread, "f1-lt-rec"), (_processing_thread, "f1-lt-proc")]:
        t = threading.Thread(target=fn, daemon=True, name=name)
        t.start()
    print("[LiveTiming] Hilos iniciados.")

def get_state():
    with _lock:
        return copy.deepcopy(_state)

def is_fresh(max_age=15):
    """True si recibimos datos en los últimos max_age segundos."""
    with _lock:
        return _state["connected"] and (time.time() - _state["last_update"]) < max_age

def get_overall_best(drivers):
    """Calcula el mejor tiempo de sesión por sector y vuelta."""
    best = {"lap": None, "s0": None, "s1": None, "s2": None}
    for d in drivers.values():
        bl = d.get("best_lap")
        if bl and (best["lap"] is None or bl < best["lap"]):
            best["lap"] = bl
        for si in range(3):
            sv = d.get("sectors", {}).get(si, {}).get("secs")
            if sv and (best[f"s{si}"] is None or sv < best[f"s{si}"]):
                best[f"s{si}"] = sv
    return best
