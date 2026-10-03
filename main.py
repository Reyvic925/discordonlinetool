import time
import json
import random
import hashlib
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock
from datetime import datetime

from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosedError


# --------------------------------------------------------------------------
# Health endpoint (Render free tier requires an inbound-traffic web service)
# --------------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # Silence logs


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


# --------------------------------------------------------------------------
# Global state used for "don't repeat last activity" persistence
# --------------------------------------------------------------------------
STATE_FILE = "state.json"
STATE_LOCK = Lock()


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    """Caller must hold STATE_LOCK."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def token_key(token: str) -> str:
    """Stable non-reversible key so raw tokens never hit disk."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Status / Presence helpers
# --------------------------------------------------------------------------
class Status:
    """Some enum-like variables to easily choose a status."""
    ONLINE    = "online"
    DND       = "dnd"
    IDLE      = "idle"
    INVISIBLE = "invisible"
    OFFLINE   = "offline"
    all_types        = [ONLINE, DND, IDLE, INVISIBLE, OFFLINE]
    all_online_types = [ONLINE, DND, IDLE]

    class Activity:
        Game      = 0
        Streaming = 1
        Listening = 2
        Watching  = 3
        Custom    = 4
        Competing = 5
        all_types = [Game, Streaming, Listening, Watching, Custom, Competing]

        class Games:
            all_games = [
                'Minecraft', 'Rust', 'VRChat', 'Fortnite', 'Apex Legends',
                'Escape from Tarkov', 'Rainbow Six Siege',
                'Counter-Strike: Global Offensive', 'Hollow Knight',
                'Subnautica', 'Celeste', 'Dead Cells', 'Risk of Rain 2',
                'Destiny 2', 'INSIDE', 'LIMBO', 'Super Meat Boy',
            ]


class Presence:
    """Presence class for better token management."""
    def __init__(self, online_status) -> None:
        self.online_status = online_status
        self.activities = []

    def addActivity(self, name, activity_type, url=None):
        self.activities.append({
            "name": name,
            "type": activity_type,
            "url":  url if activity_type == Status.Activity.Streaming else None,
        })
        return len(self.activities) - 1

    def removeActivity(self, index):
        self.activities.pop(index)
        return True


# --------------------------------------------------------------------------
# Gateway
# --------------------------------------------------------------------------
class DiscordWebSocket:
    def __init__(self) -> None:
        self.websocket_instance = connect("wss://gateway.discord.gg/?v=10&encoding=json")
        self.heartbeat_counter = 0
        self.last_heartbeat = time.time()
        self.heartbeat_interval = 41250  # sane default until we read the real one
        self.username = "-"
        self.required_action = None

    def get_heatbeat_interval(self):
        resp = json.loads(self.websocket_instance.recv())
        self.heartbeat_interval = resp["d"]["heartbeat_interval"]

    def authenticate(self, token, rich: Presence):
        """Send the IDENTIFY payload with an initial presence."""
        self.websocket_instance.send(json.dumps({
            "op": 2,
            "d": {
                "token": token,
                "intents": 513,
                "properties": {
                    "os": "linux",
                    "browser": "Brave",
                    "device": "Desktop",
                },
                "presence": {
                    "activities": [activity for activity in rich.activities],
                    "status": rich.online_status,
                    # NOTE: 'since' is epoch *milliseconds* for presence payloads
                    "since": 0 if rich.online_status in ("offline", "invisible")
                             else int(time.time() * 1000),
                    "afk": rich.online_status == "idle",
                },
            },
        }))
        try:
            resp = json.loads(self.websocket_instance.recv())
            self.username = resp['d']['user']['username']
            self.required_action = resp['d'].get('required_action')
            self.heartbeat_counter += 1
            self.last_heartbeat = time.time()
            return resp
        except ConnectionClosedError:
            return False

    def send_heartbeat(self):
        """You need to send a heartbeat at least once in an interval to stay connected."""
        self.websocket_instance.send(json.dumps({
            "op": 1,
            "d": None,
        }))
        self.heartbeat_counter += 1
        self.last_heartbeat = time.time()
        resp = self.websocket_instance.recv()
        return resp

    def update_presence(self, rich: Presence):
        """Change status/activity in-place without reconnecting (opcode 3)."""
        self.websocket_instance.send(json.dumps({
            "op": 3,
            "d": {
                "activities": rich.activities,
                "status": rich.online_status,
                "since": 0 if rich.online_status in ("offline", "invisible")
                         else int(time.time() * 1000),
                "afk": rich.online_status == "idle",
            },
        }))


# --------------------------------------------------------------------------
# Activity building (with no-repeat persistence)
# --------------------------------------------------------------------------
_ACTIVITY_LOOKUP = {
    "game": 0, "streaming": 1, "listening": 2,
    "watching": 3, "custom": 4, "competing": 5,
}


def build_activity(config, state, tkey):
    """Pick a random activity, avoiding the previous one for this token."""
    with STATE_LOCK:
        last = state.get(tkey, {})
    last_name = last.get("name")
    last_type = last.get("type")

    types_available = config["choose_random_activity_type_from"]
    name = "Discord"
    chosen_type = Status.Activity.Custom
    url = None

    # Try a few times to avoid repeating the exact same (name, type)
    for _ in range(10):
        raw_type = random.choice(types_available)
        chosen_type = _ACTIVITY_LOOKUP[raw_type]
        url = None

        match chosen_type:
            case Status.Activity.Game:
                name = random.choice(config["game"]["choose_random_game_from"])
            case Status.Activity.Streaming:
                name = random.choice(config["streaming"]["choose_random_name_from"])
                url  = random.choice(config["streaming"]["choose_random_url_from"])
            case Status.Activity.Listening:
                name = random.choice(config["listening"]["choose_random_name_from"])
            case Status.Activity.Watching:
                name = random.choice(config["watching"]["choose_random_name_from"])
            case Status.Activity.Custom:
                name = random.choice(config["custom"]["choose_random_name_from"])
            case Status.Activity.Competing:
                name = random.choice(config["competing"]["choose_random_name_from"])

        if not (name == last_name and chosen_type == last_type):
            break

    with STATE_LOCK:
        state[tkey] = {"name": name, "type": chosen_type}
        save_state(state)

    pres = Presence("online")
    pres.addActivity(name=name, activity_type=chosen_type, url=url)
    return pres


# --------------------------------------------------------------------------
# Status / timing helpers
# --------------------------------------------------------------------------
def pick_status(config, hour):
    """Weighted random status, biased toward 'invisible'/'idle' at night."""
    night_hours = config.get("night_hours", [])
    if hour in night_hours:
        pool = config.get("night_pool", ["invisible", "invisible", "idle", "online"])
    else:
        pool = config["choose_random_online_status_from"]
    return random.choice(pool)


def next_interval(config, status):
    """Return seconds until the next status change, based on the new status."""
    table = config.get("status_change_interval_seconds", {})
    spec = table.get(status) or table.get("default") or {"min": 900, "max": 5400}
    return random.randint(spec["min"], spec["max"])


# --------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------
def intro(tokens):
    try:
        from colorama import Fore, Style
    except ImportError:
        class _Noop:
            def __getattr__(self, _): return ""
        Fore = Style = _Noop()

    print(Fore.GREEN + "Piggy's Onliner "
          + Fore.MAGENTA + "Epic "
          + Fore.CYAN + "[Multiple Accounts] "
          + Fore.RED + f"Total Accounts: {len(tokens)}"
          + Style.RESET_ALL)
    print(Fore.GREEN + "                    Created By PiggyAwesome"
          + Style.RESET_ALL)


def plog(symbol, text, username, extra):
    print(f"[{symbol}] {f'{text} |':>25} {username + ' |':>32} {extra}")


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
def main(token, activity: Presence, config, state):
    tkey = token_key(token)

    socket = DiscordWebSocket()
    socket.get_heatbeat_interval()

    auth_resp = socket.authenticate(token, activity)
    if not auth_resp:
        plog("X", "Failed to Authenticate", "-", "TOKEN INVALID")
        return

    plog("OK", "Authenticated", socket.username, socket.required_action or "-")
    plog("IN", "Initial Status", socket.username, activity.online_status)

    next_change = time.time() + next_interval(config, activity.online_status)
    swap_chance = config.get("activity_swap_chance", 0.4)

    while True:
        try:
            # --- heartbeat on schedule -------------------------------------
            if time.time() - socket.last_heartbeat >= (socket.heartbeat_interval / 1000) - 5:
                plog("HB", f"Sending Heartbeat {socket.heartbeat_counter:04}",
                     socket.username, f"{socket.heartbeat_interval}ms")
                socket.send_heartbeat()

            # --- scheduled status rotation ---------------------------------
            if time.time() >= next_change:
                new_status = pick_status(config, datetime.now().hour)
                activity.online_status = new_status

                # Occasionally rotate the activity too — looks more human
                if random.random() < swap_chance:
                    fresh = build_activity(config, state, tkey)
                    activity.activities = fresh.activities

                socket.update_presence(activity)

                detail = new_status
                if activity.activities:
                    a = activity.activities[0]
                    detail = f"{new_status} / {a.get('name')}"

                plog("ST", "Status Changed", socket.username, detail)

                next_change = time.time() + next_interval(config, new_status)

            time.sleep(0.5)

        except TypeError:
            # transient recv() returning nothing; keep the loop alive
            time.sleep(1)

        except ConnectionClosedError:
            plog("!", "Socket Closed", socket.username, "will not reconnect")
            return

        except Exception as e:
            # Never let one account take down the whole process
            plog("!", "Error", socket.username, repr(e))
            time.sleep(2)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
if __name__ == "__main__":
    # Tokens: prefer env var (Render-safe, keeps secrets out of the repo),
    # fall back to tokens.txt for local runs.
    raw = os.environ.get("DISCORD_TOKENS", "")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]

    if not tokens:
        try:
            with open("tokens.txt", "r") as token_file:
                tokens = [t.strip() for t in token_file.read().splitlines() if t.strip()]
        except FileNotFoundError:
            raise SystemExit(
                "No Discord tokens configured. Set DISCORD_TOKENS in the deployment "
                "environment, or create tokens.txt for local runs."
            )

    if not tokens:
        raise SystemExit(
            "No Discord tokens configured. Set DISCORD_TOKENS in the deployment "
            "environment, or add tokens to tokens.txt for local runs."
        )

    with open("config.json", "r") as config_file:
        config = json.loads(config_file.read())

    state = load_state()

    intro(tokens)

    # Health server so Render's web service stays awake on inbound traffic.
    Thread(target=start_health_server, daemon=True).start()

    for token in tokens:
        # Build a unique initial activity + status for each account
        activity = build_activity(config, state, token_key(token))
        activity.online_status = pick_status(config, datetime.now().hour)

        Thread(
            target=main,
            args=(token, activity, config, state),
            daemon=True,
        ).start()

    # Keep the main thread alive so the daemon threads keep running
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nShutting down...")
