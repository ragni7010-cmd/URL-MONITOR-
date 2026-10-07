# Dependencies: pip install pyTelegramBotAPI requests Flask

import html
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import telebot
from flask import Flask, jsonify
from telebot.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    Message, CallbackQuery,
)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8967168772:AAEXiEj2ckiT1LPsaojijXeiSRU6u194HNw")
OWNER_ID = int(os.getenv("OWNER_ID", "6863389453"))
SELF_URL = os.getenv("SELF_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DATA_FILE = Path("data.json")
REQUEST_TIMEOUT = 15
LOG_BUFFER_SIZE = 300
SELF_PING_INTERVAL = 240

DEFAULT_INTERVAL_SEC = 300
MIN_INTERVAL_MIN = 1
MAX_INTERVAL_MIN = 1440

# ============================================================
# LOGGING
# ============================================================
LOG_BUFFER: deque = deque(maxlen=LOG_BUFFER_SIZE)


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler()])
for _h in logging.getLogger().handlers:
    _h.setFormatter(_fmt)
_ring = RingHandler()
_ring.setFormatter(_fmt)
logging.getLogger().addHandler(_ring)
logger = logging.getLogger("keepalive")

# ============================================================
# PERSISTENCE
# ============================================================
DEFAULT_DATA: Dict[str, Any] = {
    "users": {},
    "banned": [],
    "stats": {"total_pings": 0, "success": 0, "failed": 0, "started_at": int(time.time())},
    "settings": {"interval": DEFAULT_INTERVAL_SEC, "maintenance": False},
}

DATA_LOCK = threading.RLock()


def load_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        return json.loads(json.dumps(DEFAULT_DATA))
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("root not dict")
        for k, v in DEFAULT_DATA.items():
            d.setdefault(k, json.loads(json.dumps(v)))
        return d
    except Exception as exc:
        logger.error("load_data failed: %s", exc)
        return json.loads(json.dumps(DEFAULT_DATA))


DATA: Dict[str, Any] = load_data()


def save_data() -> None:
    with DATA_LOCK:
        tmp = DATA_FILE.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(DATA, f, indent=2, ensure_ascii=False)
            tmp.replace(DATA_FILE)
        except Exception as exc:
            logger.error("save_data failed: %s", exc)


# ============================================================
# HELPERS
# ============================================================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


def esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


def fmt_uptime(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts: List[str] = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def is_owner(m: Message) -> bool:
    return bool(m.from_user) and m.from_user.id == OWNER_ID


def is_banned(chat_id: int) -> bool:
    return int(chat_id) in DATA["banned"]


def notify_owner(text: str) -> None:
    if not OWNER_ID:
        return
    try:
        bot.send_message(OWNER_ID, text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("notify_owner failed: %s", exc)


def get_user(chat_id: int) -> Optional[Dict[str, Any]]:
    with DATA_LOCK:
        return DATA["users"].get(str(chat_id))


def ensure_user(m: Message) -> Tuple[str, Dict[str, Any], bool]:
    key = str(m.chat.id)
    with DATA_LOCK:
        u = DATA["users"].get(key)
        is_new = False
        if u is None:
            u = {
                "username": m.from_user.username or "",
                "first_name": m.from_user.first_name or "",
                "joined_at": int(time.time()),
                "urls": [], "ping_count": 0,
                "last_seen": int(time.time()),
            }
            DATA["users"][key] = u
            is_new = True
            save_data()
        else:
            u["last_seen"] = int(time.time())
            if m.from_user.username:
                u["username"] = m.from_user.username
        return key, u, is_new


def user_label(chat_id: int) -> str:
    u = get_user(chat_id)
    if not u:
        return f"<code>{chat_id}</code>"
    name = u.get("first_name") or "-"
    uname = u.get("username")
    tag = f"@{esc(uname)}" if uname else "—"
    return f"{esc(name)} ({tag}) <code>{chat_id}</code>"


def owner_only(m: Message) -> bool:
    if not is_owner(m):
        bot.reply_to(m, "⛔ <b>Owner only.</b>")
        return False
    return True


def valid_url(url: str) -> bool:
    return url.startswith(("http://", "https://")) and len(url) > 10 and " " not in url


def interval_minutes() -> int:
    return max(1, int(DATA["settings"].get("interval", DEFAULT_INTERVAL_SEC)) // 60)


def set_interval_minutes(mins: int) -> None:
    with DATA_LOCK:
        DATA["settings"]["interval"] = int(mins) * 60
        save_data()


# ============================================================
# PING ENGINE
# ============================================================
STOP_EVENT = threading.Event()


def ping_url(url: str) -> Optional[int]:
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT,
                         headers={"User-Agent": "KeepAliveBot/3.4"},
                         allow_redirects=True)
        return r.status_code
    except Exception as exc:
        logger.warning("Ping FAIL %s | %s", url, exc.__class__.__name__)
        return None


def ping_loop() -> None:
    logger.info("🚀 User-URL ping loop started")
    while not STOP_EVENT.is_set():
        interval = int(DATA["settings"].get("interval", DEFAULT_INTERVAL_SEC))
        maintenance = bool(DATA["settings"].get("maintenance", False))
        if maintenance:
            logger.info("🛠 Maintenance ON — skipping cycle")
        else:
            targets: List[Tuple[int, str]] = []
            with DATA_LOCK:
                for cid, u in DATA["users"].items():
                    for url in u.get("urls", []):
                        targets.append((int(cid), url))
            if targets:
                logger.info("🎯 Pinging %d user URL(s)", len(targets))
                for cid, url in targets:
                    status = ping_url(url)
                    with DATA_LOCK:
                        DATA["stats"]["total_pings"] += 1
                        uu = DATA["users"].get(str(cid))
                        if uu is not None:
                            uu["ping_count"] = uu.get("ping_count", 0) + 1
                        if status:
                            DATA["stats"]["success"] += 1
                        else:
                            DATA["stats"]["failed"] += 1
                    if status:
                        logger.info("✅ OK %s | %s", status, url)
                    else:
                        logger.warning("❌ FAIL | %s", url)
                save_data()
            else:
                logger.info("💤 No URLs registered")
        slept = 0.0
        while slept < interval and not STOP_EVENT.is_set():
            if int(DATA["settings"].get("interval", DEFAULT_INTERVAL_SEC)) != interval:
                break
            time.sleep(1)
            slept += 1


def self_ping_loop() -> None:
    if not SELF_URL:
        logger.info("ℹ️ SELF_URL not set — self-ping disabled")
        return
    logger.info("🛡 Self-ping loop started → %s", SELF_URL)
    while not STOP_EVENT.is_set():
        try:
            r = requests.get(f"{SELF_URL.rstrip('/')}/health",
                             timeout=15,
                             headers={"User-Agent": "SelfPing/1.0"})
            logger.info("🛡 Self-ping OK %s", r.status_code)
        except Exception as exc:
            logger.warning("🛡 Self-ping FAIL: %s", exc.__class__.__name__)
        slept = 0
        while slept < SELF_PING_INTERVAL and not STOP_EVENT.is_set():
            time.sleep(1)
            slept += 1


def start_ping_thread() -> threading.Thread:
    t = threading.Thread(target=ping_loop, name="ping-loop", daemon=True)
    t.start()
    return t


def start_self_ping_thread() -> threading.Thread:
    t = threading.Thread(target=self_ping_loop, name="self-ping", daemon=True)
    t.start()
    return t


# ============================================================
# FLASK HEALTH SERVER
# ============================================================
flask_app = Flask(__name__)


@flask_app.route("/")
def root() -> Any:
    st = DATA["stats"]
    return jsonify({
        "status": "alive",
        "service": "KeepAlive Bot",
        "uptime": fmt_uptime(time.time() - st.get("started_at", time.time())),
        "users": len(DATA["users"]),
        "urls": sum(len(u.get("urls", [])) for u in DATA["users"].values()),
        "pings": st.get("total_pings", 0),
        "interval_min": interval_minutes(),
        "maintenance": DATA["settings"].get("maintenance", False),
    })


@flask_app.route("/health")
def health() -> Any:
    return jsonify({"ok": True, "ts": int(time.time())})


def start_flask() -> threading.Thread:
    def _run() -> None:
        try:
            flask_app.run(host="0.0.0.0", port=PORT,
                          debug=False, use_reloader=False, threaded=True)
        except Exception as exc:
            logger.error("Flask server crashed: %s", exc)
    t = threading.Thread(target=_run, name="flask", daemon=True)
    t.start()
    logger.info("🌐 Flask health server on 0.0.0.0:%s", PORT)
    return t


# ============================================================
# MESSAGE TEMPLATES
# ============================================================
WELCOME = (
    "╔═══════════════════════════╗\n"
    "║   ⚡  <b>K E E P - A L I V E</b>  ⚡   ║\n"
    "║      🚀  <b>U P T I M E</b>  🚀      ║\n"
    "╚═══════════════════════════╝\n\n"
    "👋 ʜᴇʏ <b>{name}</b>!\n"
    "🌸 sᴡᴀɢᴀᴛ ʜᴀɪ ᴀᴀᴘᴋᴀ ᴏᴡɴ ᴜᴘᴛɪᴍᴇ ʙᴏᴛ ᴍᴇɪɴ ✨\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "🎯  <b>MISSION</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "Aapke URLs ko har <b>{mins} min</b> me auto-ping\n"
    "karke <i>Render / Replit</i> ko sᴏɴᴇ sᴇ ʙᴀᴄʜᴀɴᴀ 💤❌\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "📜  <b>QUICK COMMANDS</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    " ➕ <code>/add</code>       ➥  URL add karo\n"
    " ➖ <code>/remove</code>    ➥  URL hatao\n"
    " 📃 <code>/list</code>      ➥  apne URLs dekho\n"
    " 🚀 <code>/ping</code>      ➥  turant ping\n"
    " 📊 <code>/mystats</code>   ➥  apna stats\n"
    " 🆔 <code>/id</code>        ➥  apni ID\n"
    " ❓ <code>/help</code>      ➥  full menu\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "💡  <b>EXAMPLE</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "<code>/add https://security-store-2.onrender.com</code>\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⚡  <b>UPTIME GUARANTEED</b>  •  🔒  •  🛡  •  💎\n"
    "━━━━━━━━━━━━━━━━━━━━━"
)


HELP = (
    "╔═══════════════════════════╗\n"
    "║    📖  <b>H E L P  M E N U</b>    ║\n"
    "╚═══════════════════════════╝\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "🔗  <b>URL MANAGEMENT</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "➕ <code>/add &lt;url&gt;</code>\n"
    "     ➥ Naya URL register karo\n"
    "     ➥ Auto-ping turant start\n\n"
    "➖ <code>/remove &lt;url&gt;</code>\n"
    "     ➥ Registered URL hatao\n\n"
    "📃 <code>/list</code>\n"
    "     ➥ Apne saare URLs dekho\n\n"
    "🗑 <code>/clear</code>\n"
    "     ➥ Saare URLs ek saath delete\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "📡  <b>MONITORING</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "🚀 <code>/ping</code>\n"
    "     ➥ Turant manual ping karo\n"
    "     ➥ Har URL ka HTTP status code\n\n"
    "📊 <code>/mystats</code>\n"
    "     ➥ Total pings, URLs, join date\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⚙️  <b>INFO</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "🆔 <code>/id</code>\n"
    "     ➥ Apni user ID + chat ID\n\n"
    "❓ <code>/help</code>\n"
    "     ➥ Ye menu\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "💡  <b>EXAMPLE</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "<code>/add https://security-store-2.onrender.com</code>\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⏱  Ping interval: <b>{mins} min</b>\n"
    "⚡  Server: <b>24/7 Online</b>\n"
    "🛡  Status: <b>Fully Protected</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━"
)


# ============================================================
# USER COMMANDS
# ============================================================
@bot.message_handler(commands=["start"])
def cmd_start(m: Message) -> None:
    if is_banned(m.chat.id):
        bot.reply_to(m, "🚫 <b>You are banned.</b>")
        return
    key, u, is_new = ensure_user(m)
    if is_new:
        uname = m.from_user.username or "—"
        notify_owner(
            "🆕 <b>NEW USER STARTED BOT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 Name: <b>{esc(m.from_user.first_name)}</b>\n"
            f"🔗 Username: @{esc(uname) if uname != '—' else '—'}\n"
            f"🆔 ID: <code>{m.from_user.id}</code>\n"
            f"💬 Chat: <code>{m.chat.id}</code>\n"
            f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    bot.reply_to(m, WELCOME.format(
        name=esc(m.from_user.first_name or "Chief"),
        mins=interval_minutes(),
    ))


@bot.message_handler(commands=["help"])
def cmd_help(m: Message) -> None:
    if is_banned(m.chat.id):
        bot.reply_to(m, "🚫 <b>You are banned.</b>")
        return
    ensure_user(m)
    bot.reply_to(m, HELP.format(mins=interval_minutes()))


@bot.message_handler(commands=["id"])
def cmd_id(m: Message) -> None:
    bot.reply_to(
        m,
        "🆔 <b>YOUR IDENTITY</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 User ID:  <code>{m.from_user.id}</code>\n"
        f"💬 Chat ID:  <code>{m.chat.id}</code>",
    )


@bot.message_handler(commands=["add"])
def cmd_add(m: Message) -> None:
    if is_banned(m.chat.id):
        bot.reply_to(m, "🚫 Banned.")
        return
    key, u, _ = ensure_user(m)
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            m,
            "📝 <b>USAGE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<code>/add https://your-app.onrender.com</code>",
        )
        return
    url = parts[1].strip()
    if not valid_url(url):
        bot.reply_to(
            m,
            "⚠️ <b>INVALID URL</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "URL must start with <code>http://</code> or <code>https://</code>",
        )
        return
    with DATA_LOCK:
        if url in u["urls"]:
            bot.reply_to(m, f"⚠️ <b>ALREADY REGISTERED</b>\n<code>{esc(url)}</code>")
            return
        u["urls"].append(url)
        total = len(u["urls"])
        save_data()
    bot.reply_to(
        m,
        "✅ <b>URL ADDED SUCCESSFULLY</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <code>{esc(url)}</code>\n\n"
        f"⏱ Pinging every <b>{interval_minutes()} min</b>\n"
        f"📦 Your total URLs: <b>{total}</b>\n\n"
        "🎯 <i>Bot ab tumhare URL ko jagta rakhega 24/7</i>",
    )
    if not is_owner(m):
        notify_owner(
            "➕ <b>URL ADDED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 {user_label(m.chat.id)}\n"
            f"🔗 <code>{esc(url)}</code>\n"
            f"📦 User total: <b>{total}</b>\n"
            f"🕒 {datetime.now().strftime('%H:%M:%S')}"
        )
    logger.info("URL added by %s: %s", m.chat.id, url)


@bot.message_handler(commands=["remove"])
def cmd_remove(m: Message) -> None:
    if is_banned(m.chat.id):
        return
    key, u, _ = ensure_user(m)
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(
            m,
            "📝 <b>USAGE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<code>/remove https://your-app.onrender.com</code>",
        )
        return
    url = parts[1].strip()
    with DATA_LOCK:
        if url not in u["urls"]:
            bot.reply_to(m, f"❌ <b>NOT FOUND</b>\n<code>{esc(url)}</code>")
            return
        u["urls"].remove(url)
        remaining = len(u["urls"])
        save_data()
    bot.reply_to(
        m,
        "🗑 <b>URL REMOVED</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <code>{esc(url)}</code>\n\n"
        f"📦 Remaining URLs: <b>{remaining}</b>",
    )
    logger.info("URL removed by %s: %s", m.chat.id, url)


@bot.message_handler(commands=["list"])
def cmd_list(m: Message) -> None:
    if is_banned(m.chat.id):
        return
    key, u, _ = ensure_user(m)
    urls = u.get("urls", [])
    if not urls:
        bot.reply_to(
            m,
            "📭 <b>NO URLs YET</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Use <code>/add &lt;url&gt;</code> to register one.",
        )
        return
    lines = [f"┃ {i}. <code>{esc(x)}</code>" for i, x in enumerate(urls, 1)]
    bot.reply_to(
        m,
        f"📃 <b>YOUR URLs ({len(urls)})</b>\n"
        "┏━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + "\n┗━━━━━━━━━━━━━━━━━━\n\n"
        f"⏱ Ping interval: <b>{interval_minutes()} min</b>",
    )


@bot.message_handler(commands=["ping"])
def cmd_ping(m: Message) -> None:
    if is_banned(m.chat.id):
        return
    key, u, _ = ensure_user(m)
    urls = u.get("urls", [])
    if not urls:
        bot.reply_to(m, "📭 No URLs registered.")
        return
    bot.reply_to(m, f"🚀 <b>Pinging {len(urls)} URL(s)...</b>")
    results = []
    for url in urls:
        st = ping_url(url)
        results.append(f"{'✅' if st else '❌'} <code>{st or 'FAIL'}</code> — <code>{esc(url)}</code>")
    bot.send_message(
        m.chat.id,
        "📡 <b>PING RESULTS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n" + "\n".join(results),
    )


@bot.message_handler(commands=["mystats"])
def cmd_mystats(m: Message) -> None:
    if is_banned(m.chat.id):
        return
    key, u, _ = ensure_user(m)
    joined = datetime.fromtimestamp(u.get("joined_at", time.time())).strftime("%Y-%m-%d")
    bot.reply_to(
        m,
        "📊 <b>YOUR STATISTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: <b>{esc(u.get('first_name', '-'))}</b>\n"
        f"🆔 ID: <code>{m.chat.id}</code>\n"
        f"📅 Joined: <b>{joined}</b>\n"
        f"🔗 URLs: <b>{len(u.get('urls', []))}</b>\n"
        f"📡 Pings received: <b>{u.get('ping_count', 0)}</b>\n"
        f"⏱ Interval: <b>{interval_minutes()} min</b>",
    )


@bot.message_handler(commands=["clear"])
def cmd_clear(m: Message) -> None:
    if is_banned(m.chat.id):
        return
    key, u, _ = ensure_user(m)
    with DATA_LOCK:
        n = len(u.get("urls", []))
        u["urls"] = []
        save_data()
    bot.reply_to(
        m,
        "🗑 <b>CLEARED</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Removed <b>{n}</b> URL(s).",
    )


# ============================================================
# OWNER PANEL
# ============================================================
def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_btn("🟢 📊 Live Stats", "own:stats"),
           _btn("🔵 👥 Users", "own:users"))
    kb.add(_btn("🟣 🌐 All URLs", "own:allurls"),
           _btn("🟠 🚀 Force Ping All", "own:pingall"))
    kb.add(_btn("🟡 ⏱ Set Interval", "own:interval"),
           _btn("🔴 🛠 Maintenance", "own:maint"))
    kb.add(_btn("🟢 📢 Broadcast", "own:broadcast"),
           _btn("🔴 🚫 Ban Manager", "own:banmenu"))
    kb.add(_btn("🔵 📜 Live Logs", "own:logs"),
           _btn("🟣 🗄 Backup", "own:backup"))
    kb.add(_btn("♻️ Refresh Panel", "own:menu"))
    return kb


def interval_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=3)
    presets = [1, 2, 5, 10, 15, 30, 60]
    row: List[InlineKeyboardButton] = []
    for p in presets:
        badge = "🟢" if p == interval_minutes() else "⚪"
        row.append(_btn(f"{badge} {p} min", f"own:iset:{p}"))
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.add(_btn("🟡 ✏️ Custom: /setmin N", "own:menu"))
    kb.add(_btn("🔙 Back", "own:menu"))
    return kb


def maint_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    state = "ON" if DATA["settings"]["maintenance"] else "OFF"
    kb.add(_btn(f"🔴 Toggle Maintenance (now: {state})", "own:mtoggle"))
    kb.add(_btn("🔙 Back", "own:menu"))
    return kb


def banmenu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(_btn("🟢 📃 Show Banned List", "own:banlist"))
    kb.add(_btn("🔴 🚫 Ban: /ban <id>", "own:menu"))
    kb.add(_btn("🟡 ✅ Unban: /unban <id>", "own:menu"))
    kb.add(_btn("🔙 Back", "own:menu"))
    return kb


def panel_text() -> str:
    users_count = len(DATA["users"])
    total_urls = sum(len(u.get("urls", [])) for u in DATA["users"].values())
    st = DATA["stats"]
    uptime = fmt_uptime(time.time() - st.get("started_at", time.time()))
    settings = DATA["settings"]
    maint = "🟢 ON" if settings["maintenance"] else "🔴 OFF"
    self_ping = "🟢 ON" if SELF_URL else "🔴 OFF"
    return (
        "╔═══════════════════════════╗\n"
        "║   👑 <b>OWNER CONTROL PANEL</b>   ║\n"
        "╚═══════════════════════════╝\n\n"
        f"👥 Users: <b>{users_count}</b>       🔗 URLs: <b>{total_urls}</b>\n"
        f"🚫 Banned: <b>{len(DATA['banned'])}</b>      ⏱ Interval: <b>{interval_minutes()} min</b>\n"
        f"🛠 Maintenance: <b>{maint}</b>\n"
        f"🛡 Self-Ping: <b>{self_ping}</b>\n"
        f"⏳ Uptime: <b>{uptime}</b>\n\n"
        "┌─── <b>📜 OWNER COMMANDS</b> ───\n"
        "│ <code>/add &lt;url&gt;</code>     ➥ apna URL add\n"
        "│ <code>/remove &lt;url&gt;</code>  ➥ apna URL hatao\n"
        "│ <code>/list</code>            ➥ apne URLs\n"
        "│ <code>/stats</code>           ➥ global stats\n"
        "│ <code>/users</code>           ➥ user list\n"
        "│ <code>/user &lt;id&gt;</code>      ➥ user details\n"
        "│ <code>/allurls</code>         ➥ sab URLs\n"
        "│ <code>/setmin &lt;min&gt;</code>   ➥ interval set\n"
        "│ <code>/maintenance on|off</code>\n"
        "│ <code>/broadcast &lt;msg&gt;</code>\n"
        "│ <code>/ban &lt;id&gt;</code>        ➥ ban user\n"
        "│ <code>/unban &lt;id&gt;</code>      ➥ unban user\n"
        "│ <code>/banned</code>          ➥ banned list\n"
        "│ <code>/logs [n]</code>        ➥ live logs\n"
        "│ <code>/backup</code>          ➥ data download\n"
        "└────────────────────\n\n"
        "👇 <i>Tap a button below</i>"
    )


def stats_text() -> str:
    st = DATA["stats"]
    total = st.get("total_pings", 0)
    ok = st.get("success", 0)
    fail = st.get("failed", 0)
    rate = (ok / total * 100) if total else 0.0
    uptime = fmt_uptime(time.time() - st.get("started_at", time.time()))
    total_urls = sum(len(u.get("urls", [])) for u in DATA["users"].values())
    return (
        "🟢 <b>LIVE STATISTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total users: <b>{len(DATA['users'])}</b>\n"
        f"🔗 Active URLs: <b>{total_urls}</b>\n"
        f"📡 Total pings: <b>{total}</b>\n"
        f"✅ Success: <b>{ok}</b>\n"
        f"❌ Failed: <b>{fail}</b>\n"
        f"📈 Success rate: <b>{rate:.2f}%</b>\n"
        f"⏱ Interval: <b>{interval_minutes()} min</b>\n"
        f"🛡 Self-Ping: <b>{'🟢 ON' if SELF_URL else '🔴 OFF'}</b>\n"
        f"⏳ Uptime: <b>{uptime}</b>"
    )


def users_text() -> str:
    users = DATA["users"]
    if not users:
        return "📭 <b>No users yet.</b>"
    sorted_u = sorted(users.items(),
                      key=lambda kv: kv[1].get("joined_at", 0), reverse=True)[:30]
    lines = []
    for cid, u in sorted_u:
        uname = f"@{u['username']}" if u.get("username") else "—"
        lines.append(
            f"┃ <b>{esc(u.get('first_name', '-'))}</b> {esc(uname)}\n"
            f"┃ 🆔 <code>{cid}</code> • 🔗 {len(u.get('urls', []))} URLs"
        )
    return (
        f"🔵 <b>USERS (showing {len(sorted_u)} of {len(users)})</b>\n"
        "┏━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + "\n┗━━━━━━━━━━━━━━━━━━"
    )


def allurls_text() -> str:
    lines: List[str] = []
    total = 0
    for cid, u in DATA["users"].items():
        urls = u.get("urls", [])
        if not urls:
            continue
        lines.append(f"┃ 👤 <code>{cid}</code> ({esc(u.get('first_name', '-'))})")
        for x in urls:
            lines.append(f"┃   • <code>{esc(x)}</code>")
            total += 1
    if not lines:
        return "📭 <b>No URLs registered anywhere.</b>"
    return (
        f"🟣 <b>ALL URLs ({total})</b>\n"
        "┏━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + "\n┗━━━━━━━━━━━━━━━━━━"
    )


def logs_text(n: int = 30) -> str:
    lines = list(LOG_BUFFER)[-n:]
    if not lines:
        return "📭 <b>No logs buffered.</b>"
    body = "\n".join(lines)
    if len(body) > 3500:
        body = body[-3500:]
    return (
        f"📜 <b>LIVE LOGS (last {len(lines)})</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<pre>{html.escape(body)}</pre>"
    )


def banned_text() -> str:
    if not DATA["banned"]:
        return "✅ <b>No banned users.</b>"
    lines = [f"┃ 🚫 <code>{cid}</code>" for cid in DATA["banned"]]
    return (
        f"🔴 <b>BANNED ({len(DATA['banned'])})</b>\n"
        "┏━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + "\n┗━━━━━━━━━━━━━━━━━━"
    )


@bot.message_handler(commands=["owner", "panel"])
def cmd_owner(m: Message) -> None:
    if not owner_only(m):
        return
    bot.reply_to(m, panel_text(), reply_markup=main_menu_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("own:"))
def on_owner_cb(cb: CallbackQuery) -> None:
    if cb.from_user.id != OWNER_ID:
        bot.answer_callback_query(cb.id, "⛔ Owner only.", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    try:
        if action == "menu":
            bot.edit_message_text(panel_text(), cb.message.chat.id,
                                  cb.message.message_id, reply_markup=main_menu_kb())
        elif action == "stats":
            bot.edit_message_text(stats_text(), cb.message.chat.id,
                                  cb.message.message_id, reply_markup=main_menu_kb())
        elif action == "users":
            bot.edit_message_text(users_text(), cb.message.chat.id,
                                  cb.message.message_id, reply_markup=main_menu_kb())
        elif action == "allurls":
            bot.edit_message_text(allurls_text(), cb.message.chat.id,
                                  cb.message.message_id, reply_markup=main_menu_kb())
        elif action == "logs":
            bot.edit_message_text(logs_text(40), cb.message.chat.id,
                                  cb.message.message_id, reply_markup=main_menu_kb())
        elif action == "interval":
            bot.edit_message_text(
                "🟡 <b>SET PING INTERVAL</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Current: <b>{interval_minutes()} min</b>\n"
                f"Range: <b>{MIN_INTERVAL_MIN}–{MAX_INTERVAL_MIN} min</b>\n\n"
                "<i>Tap a preset below, or type</i> <code>/setmin N</code>",
                cb.message.chat.id, cb.message.message_id,
                reply_markup=interval_kb(),
            )
        elif action == "iset":
            mins = int(parts[2])
            if not (MIN_INTERVAL_MIN <= mins <= MAX_INTERVAL_MIN):
                bot.answer_callback_query(cb.id, "❌ Out of range", show_alert=True)
                return
            set_interval_minutes(mins)
            logger.info("Owner set interval = %s min", mins)
            bot.answer_callback_query(cb.id, f"✅ Set to {mins} min")
            bot.edit_message_text(
                "🟡 <b>SET PING INTERVAL</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Current: <b>{interval_minutes()} min</b> ✅\n"
                f"Range: <b>{MIN_INTERVAL_MIN}–{MAX_INTERVAL_MIN} min</b>\n\n"
                "<i>Tap a preset below, or type</i> <code>/setmin N</code>",
                cb.message.chat.id, cb.message.message_id,
                reply_markup=interval_kb(),
            )
        elif action == "maint":
            state = DATA["settings"]["maintenance"]
            bot.edit_message_text(
                "🔴 <b>MAINTENANCE MODE</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "When ON, ping loop pauses but bot stays live.\n\n"
                f"Current: <b>{'🟢 ON' if state else '🔴 OFF'}</b>",
                cb.message.chat.id, cb.message.message_id,
                reply_markup=maint_kb(),
            )
        elif action == "mtoggle":
            with DATA_LOCK:
                DATA["settings"]["maintenance"] = not DATA["settings"]["maintenance"]
                state = DATA["settings"]["maintenance"]
                save_data()
            bot.answer_callback_query(cb.id, f"🛠 {'ON' if state else 'OFF'}")
            bot.edit_message_text(
                "🔴 <b>MAINTENANCE MODE</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "When ON, ping loop pauses but bot stays live.\n\n"
                f"Current: <b>{'🟢 ON' if state else '🔴 OFF'}</b>",
                cb.message.chat.id, cb.message.message_id,
                reply_markup=maint_kb(),
            )
        elif action == "pingall":
            bot.answer_callback_query(cb.id, "🚀 Pinging all...")
            targets: List[Tuple[int, str]] = []
            with DATA_LOCK:
                for cid, u in DATA["users"].items():
                    for url in u.get("urls", []):
                        targets.append((int(cid), url))
            if not targets:
                bot.send_message(cb.message.chat.id, "📭 No URLs to ping.")
                return
            ok, fail = 0, 0
            for _, url in targets:
                st = ping_url(url)
                if st:
                    ok += 1
                else:
                    fail += 1
            bot.send_message(
                cb.message.chat.id,
                "📡 <b>FORCE PING DONE</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"✅ OK: <b>{ok}</b>\n"
                f"❌ FAIL: <b>{fail}</b>",
            )
        elif action == "broadcast":
            bot.edit_message_text(
                "📢 <b>BROADCAST</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Send: <code>/broadcast &lt;message&gt;</code>\n\n"
                f"Recipients: <b>{len(DATA['users'])}</b>",
                cb.message.chat.id, cb.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(_btn("🔙 Back", "own:menu")),
            )
        elif action == "banmenu":
            bot.edit_message_text(
                "🔴 <b>BAN MANAGER</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Banned: <b>{len(DATA['banned'])}</b>\n\n"
                "Use <code>/ban &lt;id&gt;</code> / <code>/unban &lt;id&gt;</code>",
                cb.message.chat.id, cb.message.message_id,
                reply_markup=banmenu_kb(),
            )
        elif action == "banlist":
            bot.edit_message_text(banned_text(), cb.message.chat.id,
                                  cb.message.message_id, reply_markup=banmenu_kb())
        elif action == "backup":
            save_data()
            try:
                with DATA_FILE.open("rb") as f:
                    bot.send_document(cb.message.chat.id, f, caption="🗄 <b>Data backup</b>")
            except Exception as exc:
                bot.send_message(cb.message.chat.id, f"❌ Backup failed: <code>{esc(exc)}</code>")
            bot.answer_callback_query(cb.id, "🗄 Sent")
        else:
            bot.answer_callback_query(cb.id, "❓ Unknown action")
    except Exception as exc:
        logger.error("callback error: %s", exc)
        bot.answer_callback_query(cb.id, "❌ Error, try /owner again")


# ============================================================
# OWNER — TEXT COMMANDS
# ============================================================
@bot.message_handler(commands=["stats"])
def cmd_stats(m: Message) -> None:
    if not owner_only(m):
        return
    bot.reply_to(m, stats_text(), reply_markup=main_menu_kb())


@bot.message_handler(commands=["users"])
def cmd_users(m: Message) -> None:
    if not owner_only(m):
        return
    bot.reply_to(m, users_text())


@bot.message_handler(commands=["user"])
def cmd_user(m: Message) -> None:
    if not owner_only(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(m, "📝 Usage: <code>/user &lt;chat_id&gt;</code>")
        return
    cid = parts[1].strip()
    u = DATA["users"].get(cid)
    if not u:
        bot.reply_to(m, f"❌ No user with id <code>{esc(cid)}</code>")
        return
    joined = datetime.fromtimestamp(u.get("joined_at", time.time())).strftime("%Y-%m-%d %H:%M")
    last = datetime.fromtimestamp(u.get("last_seen", time.time())).strftime("%Y-%m-%d %H:%M")
    urls = u.get("urls", [])
    url_lines = "\n".join(f"  • <code>{esc(x)}</code>" for x in urls) or "  — none —"
    banned = "🚫 YES" if int(cid) in DATA["banned"] else "✅ NO"
    bot.reply_to(
        m,
        "👤 <b>USER DETAILS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <code>{cid}</code>\n"
        f"👤 Name: <b>{esc(u.get('first_name', '-'))}</b>\n"
        f"🔗 Username: @{esc(u.get('username') or '—')}\n"
        f"📅 Joined: {joined}\n"
        f"👀 Last seen: {last}\n"
        f"🚫 Banned: {banned}\n"
        f"📡 Pings: <b>{u.get('ping_count', 0)}</b>\n"
        f"🔗 URLs ({len(urls)}):\n{url_lines}",
    )


@bot.message_handler(commands=["allurls"])
def cmd_allurls(m: Message) -> None:
    if not owner_only(m):
        return
    bot.reply_to(m, allurls_text())


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m: Message) -> None:
    if not owner_only(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, "📝 Usage: <code>/broadcast &lt;message&gt;</code>")
        return
    msg = parts[1].strip()
    ok, fail = 0, 0
    for cid in list(DATA["users"].keys()):
        try:
            bot.send_message(int(cid), f"📢 <b>BROADCAST</b>\n\n{esc(msg)}")
            ok += 1
            time.sleep(0.05)
        except Exception:
            fail += 1
    bot.reply_to(
        m,
        "📢 <b>BROADCAST DONE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"✅ Sent: <b>{ok}</b>\n"
        f"❌ Failed: <b>{fail}</b>",
    )


@bot.message_handler(commands=["ban"])
def cmd_ban(m: Message) -> None:
    if not owner_only(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        bot.reply_to(m, "📝 Usage: <code>/ban &lt;chat_id&gt;</code>")
        return
    cid = int(parts[1].strip())
    with DATA_LOCK:
        if cid in DATA["banned"]:
            bot.reply_to(m, f"⚠️ Already banned: <code>{cid}</code>")
            return
        DATA["banned"].append(cid)
        save_data()
    bot.reply_to(m, f"🚫 Banned: <code>{cid}</code>")
    try:
        bot.send_message(cid, "🚫 You have been banned by owner.")
    except Exception:
        pass


@bot.message_handler(commands=["unban"])
def cmd_unban(m: Message) -> None:
    if not owner_only(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        bot.reply_to(m, "📝 Usage: <code>/unban &lt;chat_id&gt;</code>")
        return
    cid = int(parts[1].strip())
    with DATA_LOCK:
        if cid not in DATA["banned"]:
            bot.reply_to(m, f"⚠️ Not banned: <code>{cid}</code>")
            return
        DATA["banned"].remove(cid)
        save_data()
    bot.reply_to(m, f"✅ Unbanned: <code>{cid}</code>")


@bot.message_handler(commands=["banned"])
def cmd_banned(m: Message) -> None:
    if not owner_only(m):
        return
    bot.reply_to(m, banned_text())


@bot.message_handler(commands=["setmin"])
def cmd_setmin(m: Message) -> None:
    if not owner_only(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(
            m,
            "📝 <b>USAGE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<code>/setmin &lt;minutes&gt;</code>\n"
            f"Range: <b>{MIN_INTERVAL_MIN}–{MAX_INTERVAL_MIN} min</b>",
        )
        return
    mins = int(parts[1].strip())
    if not (MIN_INTERVAL_MIN <= mins <= MAX_INTERVAL_MIN):
        bot.reply_to(m, f"⚠️ Out of range. Must be <b>{MIN_INTERVAL_MIN}–{MAX_INTERVAL_MIN} min</b>")
        return
    set_interval_minutes(mins)
    logger.info("Owner set interval = %s min", mins)
    bot.reply_to(
        m,
        "✅ <b>INTERVAL UPDATED</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Now pinging every <b>{mins} min</b>\n"
        "🔄 Takes effect next cycle.",
        reply_markup=main_menu_kb(),
    )


@bot.message_handler(commands=["interval"])
def cmd_interval(m: Message) -> None:
    if not owner_only(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(
            m,
            f"📝 Usage: <code>/interval &lt;minutes&gt;</code> "
            f"({MIN_INTERVAL_MIN}–{MAX_INTERVAL_MIN})\n"
            "Tip: <code>/owner</code> → ⏱ Set Interval",
        )
        return
    mins = int(parts[1].strip())
    if not (MIN_INTERVAL_MIN <= mins <= MAX_INTERVAL_MIN):
        bot.reply_to(m, f"⚠️ Range: {MIN_INTERVAL_MIN}–{MAX_INTERVAL_MIN} min")
        return
    set_interval_minutes(mins)
    bot.reply_to(m, f"⏱ Interval = <b>{mins} min</b> ✅")


@bot.message_handler(commands=["maintenance"])
def cmd_maintenance(m: Message) -> None:
    if not owner_only(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip().lower() not in ("on", "off"):
        bot.reply_to(m, "📝 Usage: <code>/maintenance on|off</code>")
        return
    val = parts[1].strip().lower() == "on"
    with DATA_LOCK:
        DATA["settings"]["maintenance"] = val
        save_data()
    bot.reply_to(m, f"🛠 Maintenance: <b>{'🟢 ON' if val else '🔴 OFF'}</b>")


@bot.message_handler(commands=["logs"])
def cmd_logs(m: Message) -> None:
    if not owner_only(m):
        return
    parts = m.text.split(maxsplit=1)
    n = 30
    if len(parts) >= 2 and parts[1].strip().isdigit():
        n = max(1, min(int(parts[1].strip()), LOG_BUFFER_SIZE))
    bot.reply_to(m, logs_text(n))


@bot.message_handler(commands=["backup"])
def cmd_backup(m: Message) -> None:
    if not owner_only(m):
        return
    save_data()
    try:
        with DATA_FILE.open("rb") as f:
            bot.send_document(m.chat.id, f, caption="🗄 <b>Data backup</b>")
    except Exception as exc:
        bot.reply_to(m, f"❌ Backup failed: <code>{esc(exc)}</code>")


# ============================================================
# FALLBACK
# ============================================================
@bot.message_handler(func=lambda msg: True)
def fallback(m: Message) -> None:
    if is_banned(m.chat.id):
        return
    bot.reply_to(
        m,
        "❓ <b>Unknown command.</b>\n"
        "Send /help for the full menu.",
    )


# ============================================================
# ENTRYPOINT
# ============================================================
def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
        raise SystemExit("❌ Set BOT_TOKEN env var.")
    if not OWNER_ID:
        raise SystemExit("❌ Set OWNER_ID env var.")

    logger.info("🚀 Starting KeepAlive Bot v3.4 (Render) — owner=%s", OWNER_ID)
    if SELF_URL:
        logger.info("🛡 SELF_URL = %s", SELF_URL)
    else:
        logger.info("⚠️ SELF_URL not set — bot may sleep on Render free tier!")

    start_flask()
    start_ping_thread()
    start_self_ping_thread()

    notify_owner(
        "🟢 <b>Bot started on Render.</b>\n"
        "Owner panel: /owner"
    )

    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=20)
    except Exception as exc:
        logger.error("Polling crashed: %s", exc)
        STOP_EVENT.set()


if __name__ == "__main__":
    main()