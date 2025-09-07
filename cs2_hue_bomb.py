from flask import Flask, request, jsonify
import requests, time, threading


import queue

# Connexions persistantes
HUE_S = requests.Session()
NANO_S = requests.Session()
DEFAULT_TIMEOUT = 3  # au lieu de 2

# File d’attente pour les commandes Nanoleaf (non bloquant)
NANO_CMD_Q = queue.Queue(maxsize=50)
NANO_ONLINE = True  # indicatif

# ===== CONFIG =====
BRIDGE_IP = "10.0.0.215"          # IP de ton pont Hue
HUE_USERNAME = "CTBJRBJgzd5A4mRnzNZr8vXomqM1mGQY7Sa23gwz"  # username créé avec le bouton du pont
TARGET_LIGHT_IDS = [2, 3, 4, 5, 10, 11, 12, 15]           # IDs des lumières Hue à contrôler

AUTH_TOKEN_GSI = "CHANGEMOI"       # doit correspondre au .cfg de CS2
EXPLOSION_HOLD_SECONDS = 17        # temps rouge après explosion
RESTORE_DELAY_SECONDS = 3         # délai avant retour à l’état de base après le noir

# --- Nanoleaf (NL22 / Shapes) ---
# Laisse vide si tu veux désactiver Nanoleaf
NANO_DEVICES = [
    {"ip": "10.0.0.162", "token": "SwKz585ymzfN83PImu0XnUmlchjduh5N"},
]
# ==================

# ---------- SCENES/EFFECTS CONFIG ----------


# Nanoleaf: noms des effets
NANO_EFFECTS = {
    "pulse": "CS2 Pulse",
    "green": "CS2 Green",
    "red":   "CS2 Red",
}
# -------------------------------------------


# ===== OPTIONS DE RESTAURATION =====
# Hue : si tu mets un nom de scène ici, on rappellera cette scène au lieu de la baseline capturée.
HUE_DEFAULT_SCENE_NAME = ""   # mets "" pour désactiver et garder la baseline par état
HUE_DEFAULT_SCENE_GROUP_ID = "0"          # "0" = toutes les lumières, sinon l'ID de ta pièce/groupe

# Nanoleaf : idem, si tu mets un nom d'effet on sélectionne cet effet à la restauration.
NANO_DEFAULT_EFFECT_NAME = "Setup2023" # mets "" pour désactiver et garder la baseline par état
# ===================================


app = Flask(__name__)
current_state = {"bomb": None, "round_phase": None}

_restore_timer = None

def schedule_restore_baseline_once():
    global _restore_timer
    if _restore_timer and _restore_timer.is_alive():
        return  # déjà programmé
    def _job():
        time.sleep(RESTORE_DELAY_SECONDS)
        apply_baseline(transition_time_ds=15)
    _restore_timer = threading.Thread(target=_job, daemon=True)
    _restore_timer.start()

def beep_interval(t_elapsed):
    """
    Calcule l'intervalle entre les bips selon le temps écoulé (en secondes).
    Début ~0.95s, fin ~0.22s sur 40s.
    """
    start, end = 0.95, 0.22
    total = 40.0
    x = min(max(t_elapsed / total, 0.0), 1.0)
    return end + (start - end) * (1 - x)**0.8

_beeper_thread = None
_beeper_stop = threading.Event()

# ======== HUE HELPERS ========
def hue_put(light_id, payload):
    url = f"http://{BRIDGE_IP}/api/{HUE_USERNAME}/lights/{light_id}/state"
    try:
        HUE_S.put(url, json=payload, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        print(f"Erreur Hue: {e}")

def set_all_lights(payload):
    for lid in TARGET_LIGHT_IDS:
        hue_put(lid, payload)

# ======== NANOLEAF HELPERS ========
def _nano_url(dev, path):
    return f"http://{dev['ip']}:16021/api/v1/{dev['token']}{path}"

def nano_put(dev, path, payload):
    # On file la commande au worker, sans bloquer
    try:
        NANO_CMD_Q.put_nowait((_nano_put_now, (dev, path, payload)))
    except queue.Full:
        # Si la file est pleine, on jette silencieusement (évite le lag)
        pass

def nano_get_state(dev):
    try:
        r = requests.get(_nano_url(dev, "/state"), timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Erreur lecture state Nano ({dev['ip']}): {e}")
        return {}

def nano_set_on(value: bool):
    if not NANO_DEVICES: return
    for dev in NANO_DEVICES:
        nano_put(dev, "/state", {"on": {"value": bool(value)}})

def nano_set_hsb(hue_deg: int, sat_pct: int, bri_pct: int):
    if not NANO_DEVICES: return
    payload = {
        "on": {"value": True},
        "hue": {"value": int(hue_deg)},
        "sat": {"value": int(sat_pct)},
        "brightness": {"value": int(bri_pct)},
    }
    for dev in NANO_DEVICES:
        nano_put(dev, "/state", payload)


def nano_select_effect(effect_key):
    name = NANO_EFFECTS.get(effect_key)
    if not name or not NANO_DEVICES:
        return
    for dev in NANO_DEVICES:
        nano_put(dev, "/effects", {"select": name})


def nano_select_effect_name(name):
    if not name or not NANO_DEVICES:
        return False
    ok = False
    for dev in NANO_DEVICES:
        try:
            NANO_CMD_Q.put_nowait((_nano_put_now, (dev, "/effects", {"select": name})))
            ok = True
        except queue.Full:
            pass
    return ok

# --- envoi immédiat (utilisé par le worker) ---
def _nano_put_now(dev, path, payload):
    NANO_S.put(_nano_url(dev, path), json=payload, timeout=DEFAULT_TIMEOUT)

# --- worker en arrière-plan avec retries & backoff ---
def _nano_worker():
    global NANO_ONLINE
    while True:
        item = NANO_CMD_Q.get()
        if item is None:
            break
        fn, args = item
        ok = False
        for attempt in range(3):  # 3 tentatives
            try:
                fn(*args)
                ok = True
                NANO_ONLINE = True
                break
            except requests.RequestException as e:
                # backoff court (0.2s, 0.4s, 0.6s)
                time.sleep(0.2 * (attempt + 1))
        if not ok:
            NANO_ONLINE = False
            # on log mais on NE bloque PAS le reste
            print("⚠️  Nanoleaf offline temporairement (commande ignorée).")
        NANO_CMD_Q.task_done() 


# ======== BASELINE (Hue + Nano) ========
BASELINE = {}        # light_id -> dict(state baseline)
NANO_BASELINE = {}   # dev_ip  -> dict(state baseline Nano)

def get_light_state(light_id):
    url = f"http://{BRIDGE_IP}/api/{HUE_USERNAME}/lights/{light_id}"
    r = requests.get(url, timeout=3)
    r.raise_for_status()
    return r.json().get("state", {}), r.json().get("name", f"Light {light_id}")

def capture_baseline():
    """Lit l’état initial. Hue = synchrone ; Nanoleaf = en arrière-plan pour ne pas bloquer."""
    print("==> Capture baseline des lampes Hue:")
    for lid in TARGET_LIGHT_IDS:
        try:
            st, name = get_light_state(lid)
            BASELINE[lid] = {
                "on": st.get("on", False),
                "bri": st.get("bri", 254),
                "hue": st.get("hue"),
                "sat": st.get("sat"),
                "ct":  st.get("ct"),
                "colormode": st.get("colormode"),
            }
            print(f"  - Hue {lid} ({name}) -> {BASELINE[lid]}")
        except Exception as e:
            print(f"  ! baseline Hue échouée pour {lid}: {e}")

    # Nanoleaf : capture non bloquante
    def _cap_nano():
        if NANO_DEVICES:
            print("==> Capture baseline Nanoleaf (async):")
        for dev in NANO_DEVICES:
            try:
                r = NANO_S.get(_nano_url(dev, "/state"), timeout=DEFAULT_TIMEOUT)
                r.raise_for_status()
                st = r.json()
                snap = {
                    "on":  st.get("on", {}).get("value", False),
                    "bri": st.get("brightness", {}).get("value", 100),
                    "hue": st.get("hue", {}).get("value", None),
                    "sat": st.get("sat", {}).get("value", None),
                    "ct":  st.get("ct", {}).get("value", None),
                }
                NANO_BASELINE[dev["ip"]] = snap
                print(f"  - Nano {dev['ip']} -> {snap}")
            except requests.RequestException as e:
                print(f"  ! baseline Nano échouée pour {dev['ip']}: {e}")
    threading.Thread(target=_cap_nano, daemon=True).start()


def apply_baseline(transition_time_ds=10):
    """Réapplique l’état mémorisé OU une scène/effet par défaut si spécifiés."""
    print("==> Restauration (scène/effet si configurés, sinon baseline état)")

    used_scene = False
    # N'appelle PAS de fonction Hue si on n'a pas activé le nom de scène
    if HUE_DEFAULT_SCENE_NAME:
        try:
            used_scene = hue_recall_scene_by_name(HUE_DEFAULT_SCENE_NAME, HUE_DEFAULT_SCENE_GROUP_ID)
        except NameError:
            # Les helpers Hue scènes sont commentés -> on ignore et on retombera sur la baseline
            used_scene = False

    used_effect = False
    if NANO_DEFAULT_EFFECT_NAME:
        used_effect = nano_select_effect_name(NANO_DEFAULT_EFFECT_NAME)

    # Si on n'a PAS utilisé de scène/effet (non configuré ou introuvable), on retombe sur la baseline "état"
    if not used_scene:
        # --- Hue baseline état (ton code actuel) ---
        for lid, st in BASELINE.items():
            if not st.get("on", False):
                hue_put(lid, {"on": False, "transitiontime": transition_time_ds})
                continue
            payload = {"on": True, "bri": st.get("bri", 200), "transitiontime": transition_time_ds}
            cm = st.get("colormode")
            if cm == "ct" and st.get("ct") is not None:
                payload["ct"] = st["ct"]
            else:
                if st.get("hue") is not None: payload["hue"] = st["hue"]
                if st.get("sat") is not None: payload["sat"] = st["sat"]
            hue_put(lid, payload)

    if not used_effect:
        # --- Nanoleaf baseline état (ton code actuel) ---
        for dev in NANO_DEVICES:
            st = NANO_BASELINE.get(dev["ip"])
            if not st:
                continue
            if not st.get("on", False):
                nano_set_on(False); continue
            if st.get("hue") is not None and st.get("sat") is not None:
                nano_set_hsb(hue_deg=int(st["hue"]), sat_pct=int(st["sat"]), bri_pct=int(st.get("bri", 100)))
            else:
                nano_put(dev, "/state", {"on": {"value": True}, "brightness": {"value": int(st.get("bri", 100))}})


def schedule_restore_baseline():
    """Attends RESTORE_DELAY_SECONDS puis remet la baseline (version multi-appels possible)."""
    def _job():
        time.sleep(RESTORE_DELAY_SECONDS)
        apply_baseline(transition_time_ds=15)
    threading.Thread(target=_job, daemon=True).start()

# ======== Effets (Hue + Nano, non-invasif à ta logique) ========
def red_flash():
    # Hue: alert clignote; Nano: rouge fort ON (flash court géré par beeper)
    set_all_lights({"on": True, "hue": 0, "sat": 254, "bri": 254, "alert": "lselect"})
    nano_set_hsb(hue_deg=0, sat_pct=100, bri_pct=100)

def green_fade():
    # Vert puis fondu vers noir (Hue + Nano)
    set_all_lights({"on": True, "hue": 25500, "sat": 254, "bri": 200, "transitiontime": 10})
    nano_set_hsb(hue_deg=120, sat_pct=100, bri_pct=80)
    time.sleep(5)
    set_all_lights({"bri": 50, "transitiontime": 20})
    nano_set_hsb(hue_deg=120, sat_pct=100, bri_pct=30)
    time.sleep(3)
    # OFF
    set_all_lights({"on": False, "transitiontime": 10})
    nano_set_on(False)
    # Après 10 s de noir, on remet la baseline
    schedule_restore_baseline()

def red_hold(seconds):
    # Rouge fixe (Hue + Nano), puis OFF, puis baseline planifiée
    set_all_lights({"on": True, "hue": 0, "sat": 254, "bri": 254})
    nano_set_hsb(hue_deg=0, sat_pct=100, bri_pct=100)
    time.sleep(seconds)
    set_all_lights({"on": False})
    nano_set_on(False)
    schedule_restore_baseline()

def bomb_beeper():
    start_time = time.time()
    while not _beeper_stop.is_set() and current_state.get("bomb") == "planted":
        t_elapsed = time.time() - start_time
        interval = beep_interval(t_elapsed)
        # HUE seulement
        set_all_lights({"on": True, "hue": 0, "sat": 254, "bri": 254})
        time.sleep(0.07)
        set_all_lights({"on": False})
        time.sleep(max(0.05, interval - 0.07))

# ======== HTTP (GSI) ========
@app.route("/gsi", methods=["POST"])
def gsi():
    global _beeper_thread  # indispensable si on assigne _beeper_thread ici

    if AUTH_TOKEN_GSI and request.headers.get("Authorization") != AUTH_TOKEN_GSI:
        pass

    data = request.get_json(silent=True) or {}
    print("=== DONNÉES REÇUES DE CS2 ===")
    print(data)
    print("=============================")

    round_info = data.get("round", {})
    bomb_state = round_info.get("bomb")
    round_phase = round_info.get("phase")

    # --- Gestion des états bombe (edge sur changement) ---
    changed = bomb_state and bomb_state != current_state.get("bomb")
    if changed:
        current_state["bomb"] = bomb_state
        print(f"Bomb state changé : {bomb_state}")

        if bomb_state == "planted":
            # Nanoleaf : 1 seul changement d'état
            nano_select_effect("pulse")
            # Hue : on garde TON comportement (beeper)
            if _beeper_thread is None or not _beeper_thread.is_alive():
                _beeper_stop.clear()
                _beeper_thread = threading.Thread(target=bomb_beeper, daemon=True)
                _beeper_thread.start()

        elif bomb_state == "defused":
            nano_select_effect("green")
            _beeper_stop.set()
            threading.Thread(target=green_fade, daemon=True).start()

        elif bomb_state == "exploded":
            _beeper_stop.set()
            nano_select_effect("red")
            threading.Thread(target=lambda: red_hold(EXPLOSION_HOLD_SECONDS), daemon=True).start()

    # --- Gestion de phase de manche (edge-trigger) ---
    new_phase = round_phase
    old_phase = current_state.get("round_phase")
    if new_phase != old_phase:
        current_state["round_phase"] = new_phase
        if new_phase == "freezetime":
            _beeper_stop.set()                 # au cas où le beeper tourne encore
            # OFF (Hue + Nano)
            set_all_lights({"on": False})
            nano_set_on(False)
            current_state["bomb"] = None
            schedule_restore_baseline_once()   # une seule restauration planifiée

    return jsonify({"ok": True})

if __name__ == "__main__":
    # démarre le worker Nanoleaf
    threading.Thread(target=_nano_worker, daemon=True).start()

    capture_baseline()
    print("Serveur démarré sur http://127.0.0.1:3000/gsi")
    print(f"Restauration Nano: {'effet '+NANO_DEFAULT_EFFECT_NAME if 'NANO_DEFAULT_EFFECT_NAME' in globals() and NANO_DEFAULT_EFFECT_NAME else 'état baseline'}")
    app.run(host="127.0.0.1", port=3000)
