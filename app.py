from flask import Flask, render_template, jsonify, request, redirect, session
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import sqlite3
import atexit
import threading
import json
import os

app = Flask(__name__)
app.secret_key = "chiqindixona_secret_key"

# =========================
# SOZLAMALAR
# =========================
TELEGRAM_BOT_TOKEN = "8311885440:AAE0nePAS6UxD2A3op7T1-lT0BCVk2SwGpc"
TELEGRAM_CHAT_ID = "5377837814"

DB_FILE = "bin_stats.db"
ONLINE_TIMEOUT_MINUTES = 10
MONITOR_INTERVAL_MINUTES = 1
HOURLY_REPORT_EVERY_HOURS = 1
DAILY_REPORT_HOUR = 21
DAILY_REPORT_MINUTE = 0

DEFAULT_BINS = [
    {"id": 1, "name": "Plastic", "bin_height_cm": 22.4},
    {"id": 2, "name": "Metal",   "bin_height_cm": 22.4},
    {"id": 3, "name": "Glass",   "bin_height_cm": 22.4},
    {"id": 4, "name": "Mixed",   "bin_height_cm": 22.4},
]

scheduler = BackgroundScheduler(timezone="Asia/Tashkent")

# Yangi quti qo'shish jarayonidagi user holatlari
# {chat_id: {"step": "waiting_name"/"waiting_height", "name": "..."}}
user_states = {}


# =========================
# BAZA
# =========================
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bin_state (
            bin_id INTEGER PRIMARY KEY,
            bin_name TEXT NOT NULL,
            distance_cm REAL,
            bin_height_cm REAL NOT NULL,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            bin_id INTEGER NOT NULL,
            bin_name TEXT NOT NULL,
            fill_percent INTEGER NOT NULL,
            distance_cm REAL,
            bin_height_cm REAL NOT NULL,
            online INTEGER NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alert_state (
            bin_id INTEGER PRIMARY KEY,
            last_level TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def seed_bins():
    conn = get_db()
    cur = conn.cursor()
    for b in DEFAULT_BINS:
        cur.execute("""
            INSERT OR IGNORE INTO bin_state (bin_id, bin_name, distance_cm, bin_height_cm, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (b["id"], b["name"], None, b["bin_height_cm"], None))
    conn.commit()
    conn.close()


# =========================
# YORDAMCHI FUNKSIYALAR
# =========================
def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))

def calculate_fill_percent(distance_cm, bin_height_cm):
    if distance_cm is None:
        return 0

    percent = (EMPTY_DISTANCE - distance_cm) / (EMPTY_DISTANCE - FULL_DISTANCE) * 100

    return max(0, min(100, int(percent)))

def get_status_text(percent, online):
    if not online: return "Offline"
    if percent >= 90: return "To'la"
    if percent >= 80: return "Ogohlantirish"
    if percent >= 50: return "O'rtacha"
    return "Normal"

def get_status_emoji(percent, online):
    if not online: return "🔴"
    if percent >= 90: return "🚨"
    if percent >= 80: return "⚠️"
    if percent >= 50: return "🟡"
    return "🟢"

def format_bin_line(bin_item):
    if bin_item["device_online"]:
        return (
            f"{get_status_emoji(bin_item['fill_percent'], True)} "
            f"{bin_item['name']} — {bin_item['fill_percent']}% | "
            f"{bin_item['distance_cm']:.1f} cm | {bin_item['status_text']}"
        )
    return f"🔴 {bin_item['name']} — Offline"

def is_online(updated_at):
    if not updated_at:
        return False
    try:
        last_time = datetime.fromisoformat(updated_at)
    except Exception:
        return False
    return (datetime.now() - last_time) <= timedelta(minutes=ONLINE_TIMEOUT_MINUTES)


# =========================
# TELEGRAM FUNKSIYALARI
# =========================
def send_telegram_message(text, chat_id=None, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "BOT_TOKENINGIZNI_YOZING":
        return
    target = chat_id if chat_id else TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=payload, timeout=10)
        print("Telegram:", r.status_code)
        return r.json()
    except Exception as e:
        print("Telegram xato:", e)


def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.json()
    except Exception as e:
        print("Edit xato:", e)


def answer_callback(callback_query_id, text=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, data={"callback_query_id": callback_query_id, "text": text}, timeout=10)
    except Exception as e:
        print("Callback xato:", e)


# =========================
# KNOPKALAR
# =========================
def build_main_menu():
    """Barcha qutilar knopkasi + yangi quti qo'shish"""
    bins = get_current_bins()
    keyboard = []

    # Har 2 ta qutini bir qatorga joylashtirish
    row = []
    for b in bins:
        emoji = get_status_emoji(b["fill_percent"], b["device_online"])
        btn = {
            "text": f"{emoji} {b['name']} ({b['fill_percent']}%)",
            "callback_data": f"bin_{b['id']}"
        }
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Yangi quti qo'shish va hisobot knopkalari
    keyboard.append([
        {"text": "➕ Yangi quti qo'shish", "callback_data": "add_bin"},
        {"text": "📊 Kunlik hisobot",      "callback_data": "daily_report"}
    ])
    keyboard.append([
        {"text": "🔄 Yangilash", "callback_data": "refresh"},
        {"text": "⏰ Soatlik hisobot", "callback_data": "hourly_report"}
    ])

    return {"inline_keyboard": keyboard}


def build_bin_detail_menu(bin_id):
    """Bitta quti detail sahifasidagi knopkalar"""
    return {
        "inline_keyboard": [
            [
                {"text": "✏️ Nomini o'zgartirish", "callback_data": f"rename_{bin_id}"},
                {"text": "🗑 Qutini o'chirish",    "callback_data": f"delete_{bin_id}"}
            ],
            [
                {"text": "◀️ Orqaga", "callback_data": "back_main"}
            ]
        ]
    }


# =========================
# QUTILAR HOLATI
# =========================
def get_current_bins():
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT bin_id, bin_name, distance_cm, bin_height_cm, updated_at
        FROM bin_state ORDER BY bin_id ASC
    """).fetchall()
    conn.close()

    bins = []
    for row in rows:
        online = is_online(row["updated_at"]) and row["distance_cm"] is not None
        fill_percent = calculate_fill_percent(row["distance_cm"], row["bin_height_cm"]) if online else 0
        bins.append({
            "id":           row["bin_id"],
            "name":         row["bin_name"],
            "distance_cm":  float(row["distance_cm"]) if row["distance_cm"] is not None else 0,
            "bin_height_cm": float(row["bin_height_cm"]),
            "device_online": online,
            "updated_at":   row["updated_at"],
            "fill_percent": fill_percent,
            "status_text":  get_status_text(fill_percent, online)
        })
    return bins


def update_bin_state(bin_id, distance_cm, bin_height_cm=None, name=None):
    conn = get_db()
    cur = conn.cursor()
    existing = cur.execute(
        "SELECT bin_id, bin_name, bin_height_cm FROM bin_state WHERE bin_id=?", (bin_id,)
    ).fetchone()

    if existing:
        final_name   = name if name else existing["bin_name"]
        final_height = float(bin_height_cm) if bin_height_cm is not None else float(existing["bin_height_cm"])
        cur.execute("""
            UPDATE bin_state SET bin_name=?, distance_cm=?, bin_height_cm=?, updated_at=?
            WHERE bin_id=?
        """, (final_name, float(distance_cm), final_height, now_iso(), bin_id))
    else:
        final_name   = name if name else f"Bin {bin_id}"
        final_height = float(bin_height_cm) if bin_height_cm is not None else 22.4
        cur.execute("""
            INSERT INTO bin_state (bin_id, bin_name, distance_cm, bin_height_cm, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (bin_id, final_name, float(distance_cm), final_height, now_iso()))

    conn.commit()
    conn.close()


def get_next_bin_id():
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute("SELECT MAX(bin_id) as max_id FROM bin_state").fetchone()
    conn.close()
    return (row["max_id"] or 0) + 1


def delete_bin(bin_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM bin_state WHERE bin_id=?", (bin_id,))
    cur.execute("DELETE FROM alert_state WHERE bin_id=?", (bin_id,))
    conn.commit()
    conn.close()


def rename_bin(bin_id, new_name):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE bin_state SET bin_name=? WHERE bin_id=?", (new_name, bin_id))
    conn.commit()
    conn.close()


# =========================
# SNAPSHOT
# =========================
def save_snapshot(bins):
    conn = get_db()
    cur = conn.cursor()
    created_at = now_iso()
    for b in bins:
        cur.execute("""
            INSERT INTO snapshots (created_at, bin_id, bin_name, fill_percent, distance_cm, bin_height_cm, online)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            created_at, b["id"], b["name"], b["fill_percent"],
            b["distance_cm"] if b["device_online"] else None,
            b["bin_height_cm"], 1 if b["device_online"] else 0
        ))
    conn.commit()
    conn.close()


# =========================
# ALERTLAR
# =========================
def check_alerts_and_send(bins):
    conn = get_db()
    cur = conn.cursor()
    current_time = now_iso()

    for b in bins:
        bin_id   = b["id"]
        bin_name = b["name"]
        online   = b["device_online"]
        percent  = b["fill_percent"]

        # 🔥 YANGI THRESHOLD
        if not online:
            current_level = "offline"
        elif percent >= 90:
            current_level = "critical"
        elif percent >= 60:   # 🔥 OLDIN 80 EDI
            current_level = "warning"
        else:
            current_level = "normal"

        row = cur.execute(
            "SELECT last_level FROM alert_state WHERE bin_id=?",
            (bin_id,)
        ).fetchone()

        last_level = row["last_level"] if row else None

        # 🔥 FAqat o‘zgarganda yuboradi (spam yo‘q)
        if current_level != last_level:

            if current_level == "warning":
                send_telegram_message(
                    f"⚠️ <b>Quti 60% dan oshdi</b>\n"
                    f"Bo‘lim: {bin_name}\n"
                    f"To‘lish: {percent}%"
                )

            elif current_level == "critical":
                send_telegram_message(
                    f"🚨 <b>Quti deyarli to‘ldi</b>\n"
                    f"Bo‘lim: {bin_name}\n"
                    f"To‘lish: {percent}%"
                )

            elif current_level == "offline" and last_level in ["normal", "warning", "critical"]:
                send_telegram_message(
                    f"🔴 <b>Aloqa uzildi</b>\nBo‘lim: {bin_name}"
                )

            elif current_level == "normal" and last_level == "offline":
                send_telegram_message(
                    f"🟢 <b>Aloqa tiklandi</b>\n"
                    f"Bo‘lim: {bin_name}\n"
                    f"Daraja: {percent}%"
                )

            elif current_level == "normal" and last_level in ["warning", "critical"]:
                send_telegram_message(
                    f"✅ <b>Quti bo‘shatildi</b>\n"
                    f"Bo‘lim: {bin_name}\n"
                    f"Daraja: {percent}%"
                )

            # 🔥 STATE SAQLASH
            if row:
                cur.execute("""
                    UPDATE alert_state
                    SET last_level=?, updated_at=?
                    WHERE bin_id=?
                """, (current_level, current_time, bin_id))
            else:
                cur.execute("""
                    INSERT INTO alert_state (bin_id, last_level, updated_at)
                    VALUES (?, ?, ?)
                """, (bin_id, current_level, current_time))

    conn.commit()
    conn.close()

# =========================
# HISOBOTLAR
# =========================
def send_hourly_report(chat_id=None):
    bins = get_current_bins()
    online_bins  = [b["name"] for b in bins if b["device_online"]]
    offline_bins = [b["name"] for b in bins if not b["device_online"]]
    warning_bins = [b["name"] for b in bins if b["device_online"] and b["fill_percent"] >= 80]

    lines = [
        "♻️ <b>Har soatlik qutilar holati</b>",
        datetime.now().strftime("%Y-%m-%d %H:%M"), ""
    ]
    for b in bins:
        lines.append(format_bin_line(b))
    lines += [
        "",
        f"Jami: {len(bins)} | Online: {len(online_bins)} | Offline: {len(offline_bins)}",
        f"Ogohlantirish: {len(warning_bins)}"
    ]
    send_telegram_message("\n".join(lines), chat_id)


def send_daily_report(chat_id=None):
    conn = get_db()
    cur = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")

    rows = cur.execute("""
        SELECT * FROM snapshots WHERE substr(created_at, 1, 10)=? ORDER BY created_at ASC
    """, (today,)).fetchall()
    bin_rows = cur.execute("SELECT bin_id, bin_name FROM bin_state ORDER BY bin_id ASC").fetchall()
    conn.close()

    if not bin_rows:
        send_telegram_message("📊 Kunlik hisobot\nQutilar topilmadi.", chat_id)
        return

    grouped = {}
    for row in rows:
        n = row["bin_name"]
        grouped.setdefault(n, []).append(row)

    lines = [f"📊 <b>Kunlik hisobot</b>", today, ""]
    total_warn = 0

    for br in bin_rows:
        bn = br["bin_name"]
        items = grouped.get(bn, [])
        online_items = [x for x in items if x["online"] == 1]

        if online_items:
            values = [x["fill_percent"] for x in online_items]
            warn_count = len([v for v in values if v >= 80])
            total_warn += warn_count
            lines.append(
                f"♻️ <b>{bn}</b>\n"
                f"   Oxirgi: {online_items[-1]['fill_percent']}% | "
                f"Ortacha: {round(sum(values)/len(values))}%\n"
                f"   Maksimum: {max(values)}% | Minimum: {min(values)}%\n"
                f"   Ogohlantirish: {warn_count} marta\n"
            )
        else:
            lines.append(f"♻️ <b>{bn}</b>\n   Bugun offline\n")

    lines.append(f"Jami ogohlantirish: {total_warn}")
    send_telegram_message("\n".join(lines), chat_id)


# =========================
# BOT: XABAR HANDLER
# =========================
def handle_message(chat_id, text):
    state = user_states.get(chat_id, {})

    # --- Yangi quti: nom kutilmoqda ---
    if state.get("step") == "waiting_name":
        user_states[chat_id] = {"step": "waiting_height", "name": text.strip()}
        send_telegram_message(
            f"✅ Nom: <b>{text.strip()}</b>\n\n"
            f"Endi quti balandligini cm da yozing:\n"
            f"(Misol: 22.4)",
            chat_id
        )
        return

    # --- Yangi quti: balandlik kutilmoqda ---
    if state.get("step") == "waiting_height":
        try:
            height = float(text.strip().replace(",", "."))
        except ValueError:
            send_telegram_message("Iltimos raqam kiriting. Misol: 22.4", chat_id)
            return

        new_name = state["name"]
        new_id   = get_next_bin_id()

        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO bin_state (bin_id, bin_name, distance_cm, bin_height_cm, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (new_id, new_name, None, height, None))
        conn.commit()
        conn.close()

        user_states.pop(chat_id, None)

        send_telegram_message(
            f"✅ <b>{new_name}</b> quti qo'shildi!\n"
            f"ID: {new_id} | Balandlik: {height} cm\n\n"
            f"ESP32 da BIN_ID = {new_id} qilib sozlang.",
            chat_id
        )
        # Asosiy menyuni yangilash
        send_main_menu(chat_id)
        return

    # --- Nomini o'zgartirish: nom kutilmoqda ---
    if state.get("step") == "waiting_rename":
        bin_id   = state["bin_id"]
        new_name = text.strip()
        rename_bin(bin_id, new_name)
        user_states.pop(chat_id, None)
        send_telegram_message(
            f"✅ Quti nomi <b>{new_name}</b> ga o'zgartirildi!", chat_id
        )
        send_main_menu(chat_id)
        return

    # --- Oddiy komandalar ---
    if text == "/start" or text == "/menu":
        send_main_menu(chat_id)
    elif text == "/hisobot":
        send_daily_report(chat_id)
    elif text == "/soatlik":
        send_hourly_report(chat_id)
    else:
        send_telegram_message(
            "Qutilarni ko'rish uchun /start yozing.", chat_id
        )


def send_main_menu(chat_id):
    bins = get_current_bins()
    online  = sum(1 for b in bins if b["device_online"])
    offline = sum(1 for b in bins if not b["device_online"])
    warning = sum(1 for b in bins if b["device_online"] and b["fill_percent"] >= 80)

    text = (
        f"🗑 <b>Chiqindixona Monitoring</b>\n"
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Jami: {len(bins)} quti\n"
        f"🟢 Online: {online} | 🔴 Offline: {offline}"
    )
    if warning:
        text += f"\n⚠️ Ogohlantirish: {warning} quti"

    send_telegram_message(text, chat_id, reply_markup=build_main_menu())


# =========================
# BOT: CALLBACK HANDLER
# =========================
def handle_callback(callback_query):
    cb_id     = callback_query["id"]
    chat_id   = str(callback_query["message"]["chat"]["id"])
    message_id = callback_query["message"]["message_id"]
    data      = callback_query.get("data", "")

    answer_callback(cb_id)

    # --- Asosiy menyu ---
    if data == "back_main" or data == "refresh":
        bins    = get_current_bins()
        online  = sum(1 for b in bins if b["device_online"])
        offline = sum(1 for b in bins if not b["device_online"])
        warning = sum(1 for b in bins if b["device_online"] and b["fill_percent"] >= 80)

        text = (
            f"🗑 <b>Chiqindixona Monitoring</b>\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"Jami: {len(bins)} quti\n"
            f"🟢 Online: {online} | 🔴 Offline: {offline}"
        )
        if warning:
            text += f"\n⚠️ Ogohlantirish: {warning} quti"

        edit_telegram_message(chat_id, message_id, text, build_main_menu())

    # --- Bitta quti detail ---
    elif data.startswith("bin_"):
        bin_id = int(data.split("_")[1])
        bins   = get_current_bins()
        b      = next((x for x in bins if x["id"] == bin_id), None)

        if not b:
            answer_callback(cb_id, "Quti topilmadi")
            return

        emoji   = get_status_emoji(b["fill_percent"], b["device_online"])
        status  = "🟢 Online" if b["device_online"] else "🔴 Offline"
        updated = b["updated_at"] or "Ma'lumot yo'q"

        # To'lganlik progress bar
        filled  = round(b["fill_percent"] / 10)
        bar     = "█" * filled + "░" * (10 - filled)

        text = (
            f"{emoji} <b>{b['name']}</b>\n\n"
            f"Holat: {status}\n"
            f"To'lganlik: [{bar}] {b['fill_percent']}%\n"
            f"Masofa: {b['distance_cm']:.1f} cm\n"
            f"Quti balandligi: {b['bin_height_cm']} cm\n"
            f"Oxirgi yangilanish:\n{updated}"
        )
        edit_telegram_message(chat_id, message_id, text, build_bin_detail_menu(bin_id))

    # --- Yangi quti qo'shish ---
    elif data == "add_bin":
        user_states[chat_id] = {"step": "waiting_name"}
        edit_telegram_message(
            chat_id, message_id,
            "➕ <b>Yangi quti qo'shish</b>\n\nQuti nomini yozing:\n(Misol: Maktab, Ofis, 5-qavat)"
        )

    # --- Nomini o'zgartirish ---
    elif data.startswith("rename_"):
        bin_id = int(data.split("_")[1])
        bins   = get_current_bins()
        b      = next((x for x in bins if x["id"] == bin_id), None)
        if not b:
            return
        user_states[chat_id] = {"step": "waiting_rename", "bin_id": bin_id}
        edit_telegram_message(
            chat_id, message_id,
            f"✏️ <b>{b['name']}</b> uchun yangi nom yozing:"
        )

    # --- Qutini o'chirish ---
    elif data.startswith("delete_"):
        bin_id   = int(data.split("_")[1])
        bins     = get_current_bins()
        b        = next((x for x in bins if x["id"] == bin_id), None)
        bin_name = b["name"] if b else f"ID {bin_id}"
        delete_bin(bin_id)

        bins    = get_current_bins()
        online  = sum(1 for x in bins if x["device_online"])
        offline = sum(1 for x in bins if not x["device_online"])

        text = (
            f"🗑 <b>Chiqindixona Monitoring</b>\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"✅ <b>{bin_name}</b> o'chirildi!\n\n"
            f"Jami: {len(bins)} quti\n"
            f"🟢 Online: {online} | 🔴 Offline: {offline}"
        )
        edit_telegram_message(chat_id, message_id, text, build_main_menu())

    # --- Kunlik hisobot ---
    elif data == "daily_report":
        send_daily_report(chat_id)

    # --- Soatlik hisobot ---
    elif data == "hourly_report":
        send_hourly_report(chat_id)


# =========================
# WEBHOOK HANDLER
# =========================
def handle_telegram_update(update):
    try:
        # Callback (knopka bosildi)
        if "callback_query" in update:
            threading.Thread(
                target=handle_callback,
                args=(update["callback_query"],)
            ).start()
            return

        # Oddiy xabar
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text    = message.get("text", "").strip()

        if chat_id and text:
            threading.Thread(
                target=handle_message,
                args=(chat_id, text)
            ).start()

    except Exception as e:
        print("Update xato:", e)


def set_telegram_webhook(server_url):
    webhook_url = f"{server_url}/telegram-webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    try:
        resp = requests.post(url, data={"url": webhook_url}, timeout=10)
        print("Webhook:", resp.json())
    except Exception as e:
        print("Webhook xato:", e)


# =========================
# SCHEDULER
# =========================
def monitor_job():
    bins = get_current_bins()
    save_snapshot(bins)
    check_alerts_and_send(bins)
    print("Monitor job:", datetime.now())

def hourly_report_job():
    send_hourly_report()

def daily_report_job():
    send_daily_report()

def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(monitor_job, "interval", minutes=MONITOR_INTERVAL_MINUTES,
                          id="monitor_job", replace_existing=True)
        scheduler.add_job(hourly_report_job, "interval", hours=HOURLY_REPORT_EVERY_HOURS,
                          id="hourly_report_job", replace_existing=True)
        scheduler.add_job(daily_report_job, "cron", hour=DAILY_REPORT_HOUR,
                          minute=DAILY_REPORT_MINUTE, id="daily_report_job", replace_existing=True)
        scheduler.start()


# =========================
# FLASK ROUTELAR
# =========================
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/bins")
def api_bins():
    return jsonify({"bins": get_current_bins()})


@app.route("/api/update-bin", methods=["GET", "POST"])
def api_update_bin():
    data = request.get_json(silent=True) or {}

    bin_id = data.get("id") or request.values.get("id")
    distance_cm = data.get("distance_cm") or request.values.get("distance_cm")
    bin_height_cm = data.get("bin_height_cm") or request.values.get("bin_height_cm")
    name = data.get("name") or request.values.get("name")

    if bin_id is None or distance_cm is None:
        return jsonify({"success": False}), 400

    bin_id = int(bin_id)
    distance_cm = float(distance_cm)

    update_bin_state(bin_id, distance_cm, bin_height_cm, name)

    # 🔥 REAL-TIME ALERT (ENG MUHIM)
    bins = get_current_bins()
    check_alerts_and_send(bins)

    updated_bin = next((b for b in bins if b["id"] == bin_id), None)

    return jsonify({
        "success": True,
        "bin": updated_bin
    })


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True)
    if update:
        handle_telegram_update(update)
    return jsonify({"ok": True})


@app.route("/send-hourly-now")
def send_hourly_now():
    send_hourly_report()
    return "Yuborildi"


@app.route("/send-daily-now")
def send_daily_now():
    send_daily_report()
    return "Yuborildi"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    
    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    # oddiy javob
    requests.post(URL, json={
        "chat_id": chat_id,
        "text": f"Siz yozdingiz: {text}"
    })

    return "ok"

@app.route("/run-monitor-now")
def run_monitor_now():
    monitor_job()
    return "Ishladi"


@app.route("/details/<bin_code>")
def details(bin_code):
    if "user" not in session:
        return redirect("/login")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT bin_code, name, location_name, lat, lng,
               last_distance, fill_percent, status, is_online, updated_at
        FROM bins WHERE bin_code=?
    """, (bin_code,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return "Quti topilmadi"
    bin_info = {
        "bin_code": row[0], "name": row[1], "location_name": row[2],
        "lat": row[3], "lng": row[4], "distance": row[5],
        "fill": row[6], "status": row[7], "is_online": row[8], "updated_at": row[9]
    }
    return render_template("details.html", bin_info=bin_info)


@app.route("/map")
def map_page():
    if "user" not in session:
        return redirect("/login")
    return render_template("map.html")


@app.route("/fill-all-test")
def fill_all_test():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE bin_state SET distance_cm=0, updated_at=?", (now_iso(),))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "100% qilindi"})


# =========================
# START
# =========================
init_db()
seed_bins()
start_scheduler()

# Domeningizni yozing va qatorni oching:
set_telegram_webhook("https://smart-waste-l3cv.onrender.com/")

atexit.register(lambda: scheduler.shutdown(wait=False) if scheduler.running else None)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))