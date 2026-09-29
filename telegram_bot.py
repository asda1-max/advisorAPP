"""
advisorAPP Telegram bot.

Capture expenses/income by chatting, plus daily "belum catat" reminders.
Bridges to the same SQLite databases used by app.py.

Run:
    python telegram_bot.py
Config (env or .env):
    TELEGRAM_BOT_TOKEN     required
    TELEGRAM_CHAT_ID       optional whitelist / target chat
    ADVISOR_USER_ID        default 1 -> advisor_{id}.db
    TELEGRAM_REMINDER_TIME default 21:00 (local)
    TELEGRAM_REMINDER      default on
"""

import json
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime

import bcrypt
import requests

_STARTED = False
_START_LOCK = threading.Lock()
_PRESET_LOCK = threading.Lock()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTH_DATABASE = os.path.join(BASE_DIR, "auth.db")
PRESETS_FILE = os.path.join(BASE_DIR, "presets.json")
API = "https://api.telegram.org/bot{token}/{method}"

EXPENSE_PREFIXES = ("-",)
INCOME_PREFIXES = ("+",)
INCOME_WORDS = ("income", "masuk", "terima", "gaji", "dividen", "bonus", "refund")
STRIP_WORDS = ("income", "expense", "pengeluaran", "pemasukan", "masuk", "keluar")

DEFAULT_PRESETS = {
    "sangu": {"kind": "income", "amount": 100000, "description": "Uang saku", "category": "Cash"},
}

SUFFIX_MULT = {
    "jt": 1_000_000, "juta": 1_000_000,
    "rb": 1_000, "ribu": 1_000, "k": 1_000,
}

NUM_RE = re.compile(r"(\d[\d.,]*)\s*(jt|juta|rb|ribu|k)?", re.IGNORECASE)


def load_env(path=None):
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _to_float(raw, suffix):
    raw = raw.strip()
    if suffix:
        value = raw.replace(",", ".")
        try:
            number = float(value)
        except ValueError:
            number = float(re.sub(r"[^\d.]", "", value) or 0)
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        number = float(re.sub(r"[.,]", "", raw))
    else:
        number = float(raw.replace(",", "."))
    return number * SUFFIX_MULT.get((suffix or "").lower(), 1)


def parse_amount(text):
    best = None
    for match in NUM_RE.finditer(text):
        raw, suffix = match.group(1), match.group(2)
        if not raw or raw in (".", ","):
            continue
        try:
            value = _to_float(raw, suffix)
        except ValueError:
            continue
        if suffix:
            return value
        if best is None:
            best = value
    return best


def parse_message(text):
    text = (text or "").strip()
    if not text:
        return None
    lower = text.lower()

    kind = "expense"
    if text.startswith(INCOME_PREFIXES) or any(w in lower for w in INCOME_WORDS):
        kind = "income"
    elif text.startswith(EXPENSE_PREFIXES):
        kind = "expense"

    amount = parse_amount(text)
    if not amount:
        return None

    description = text
    for word in STRIP_WORDS:
        description = re.sub(rf"\b{word}\b", " ", description, flags=re.IGNORECASE)
    description = NUM_RE.sub(" ", description)
    description = description.strip(" +-:,\t")
    description = re.sub(r"\s+", " ", description).strip()

    return {"kind": kind, "amount": float(amount), "description": description}


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def load_presets():
    """Load presets, seeding defaults on first run."""
    with _PRESET_LOCK:
        presets = {}
        if os.path.exists(PRESETS_FILE):
            try:
                with open(PRESETS_FILE, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    presets = data
            except (OSError, ValueError):
                presets = {}
        if not presets:
            presets = dict(DEFAULT_PRESETS)
            _write_presets(presets)
        return presets


def _write_presets(presets):
    with open(PRESETS_FILE, "w", encoding="utf-8") as fh:
        json.dump(presets, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def get_preset(name):
    """Return (name, preset_dict) if `name` is a preset, else None."""
    key = (name or "").strip().lower()
    if not key:
        return None
    presets = load_presets()
    if key in presets:
        return key, presets[key]
    return None


def _category_id(db, kind, name):
    table = "categories" if kind == "expense" else "income_categories"
    row = db.execute(
        f"SELECT id FROM {table} WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row:
        return row["id"]
    cur = db.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    db.commit()
    return cur.lastrowid


def separate_category(db, kind, description, explicit_category=None):
    """Use an explicit category if given, else if the description ends with a
    known category name use it (and strip it). Returns (desc, category_id).
    """
    desc = (description or "").strip()
    if explicit_category:
        return desc, _category_id(db, kind, explicit_category)

    table = "categories" if kind == "expense" else "income_categories"
    rows = db.execute(
        f"SELECT id, name FROM {table} ORDER BY LENGTH(name) DESC"
    ).fetchall()
    lowered = desc.lower()
    for row in rows:
        name = row["name"].strip()
        nl = name.lower()
        if lowered == nl or lowered.endswith(" " + nl):
            clean = desc[: len(desc) - len(name)].strip()
            return clean, row["id"]
    if rows:
        return desc, rows[0]["id"]
    fallback = "Uncategorized" if kind == "expense" else "Other"
    cur = db.execute(f"INSERT INTO {table} (name) VALUES (?)", (fallback,))
    db.commit()
    return desc, cur.lastrowid


def add_entry(user_id, parsed):
    db = get_user_db(user_id)
    table = "expenses" if parsed["kind"] == "expense" else "income"
    description, category_id = separate_category(
        db, parsed["kind"], parsed["description"], parsed.get("category")
    )
    parsed["description"] = description
    cur = db.execute(
        f"INSERT INTO {table} (amount, description, date, category_id) VALUES (?, ?, ?, ?)",
        (parsed["amount"], description, date.today().isoformat(), category_id),
    )
    db.commit()
    entry_id = cur.lastrowid
    category = db.execute(
        f"SELECT name FROM {'categories' if parsed['kind'] == 'expense' else 'income_categories'} WHERE id = ?",
        (category_id,),
    ).fetchone()["name"]
    return entry_id, category, db, table


def auth_db():
    return connect(AUTH_DATABASE)


def init_links(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS telegram_links (
            chat_id   INTEGER PRIMARY KEY,
            user_id   INTEGER NOT NULL,
            linked_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    db.commit()


def authenticate(username, password):
    """Verify credentials against auth.db. Returns user row or None."""
    db = auth_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    row = db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    db.close()
    if not row:
        return None
    try:
        if bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
            return row
    except (ValueError, TypeError):
        return None
    return None


def link_chat(chat_id, user_id):
    db = auth_db()
    init_links(db)
    db.execute(
        "INSERT INTO telegram_links (chat_id, user_id) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET user_id = excluded.user_id, "
        "linked_at = datetime('now','localtime')",
        (chat_id, user_id),
    )
    db.commit()
    db.close()


def unlink_chat(chat_id):
    db = auth_db()
    init_links(db)
    cur = db.execute("DELETE FROM telegram_links WHERE chat_id = ?", (chat_id,))
    db.commit()
    db.close()
    return cur.rowcount > 0


def linked_user(chat_id):
    db = auth_db()
    init_links(db)
    row = db.execute(
        "SELECT users.id, users.username FROM telegram_links "
        "JOIN users ON users.id = telegram_links.user_id "
        "WHERE telegram_links.chat_id = ?",
        (chat_id,),
    ).fetchone()
    db.close()
    return row


def user_db_path(user_id):
    return os.path.join(BASE_DIR, f"advisor_{user_id}.db")


def get_user_db(user_id):
    from app import init_db
    path = user_db_path(user_id)
    init_db(path)
    return connect(path)


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def summary(user_id):
    db = get_user_db(user_id)
    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    def total(table, since):
        col = "amount"
        return db.execute(
            f"SELECT COALESCE(SUM({col}), 0) AS t FROM {table} WHERE date >= ?", (since,)
        ).fetchone()["t"]

    data = {
        "today_expense": total("expenses", today),
        "today_income": total("income", today),
        "month_expense": total("expenses", month_start),
        "month_income": total("income", month_start),
    }
    db.close()
    return data


def recent_entries(user_id, limit=10):
    db = get_user_db(user_id)
    rows = db.execute("""
        SELECT * FROM (
            SELECT e.id, e.amount, e.description, e.date, c.name AS category, 'expense' AS kind, e.created_at
            FROM expenses e JOIN categories c ON e.category_id = c.id
            UNION ALL
            SELECT i.id, i.amount, i.description, i.date, ic.name AS category, 'income' AS kind, i.created_at
            FROM income i JOIN income_categories ic ON i.category_id = ic.id
        ) ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    db.close()
    return rows


def undo_last(user_id):
    db = get_user_db(user_id)
    last = db.execute("""
        SELECT * FROM (
            SELECT id, amount, description, created_at, 'expenses' AS tbl FROM expenses
            UNION ALL
            SELECT id, amount, description, created_at, 'income' AS tbl FROM income
        ) ORDER BY created_at DESC LIMIT 1
    """).fetchone()
    if not last:
        db.close()
        return None
    db.execute(f"DELETE FROM {last['tbl']} WHERE id = ?", (last["id"],))
    db.commit()
    db.close()
    return last


def list_categories(user_id):
    db = get_user_db(user_id)
    exp = [r["name"] for r in db.execute("SELECT name FROM categories ORDER BY name")]
    inc = [r["name"] for r in db.execute("SELECT name FROM income_categories ORDER BY name")]
    db.close()
    return exp, inc


def rupiah(value):
    return "Rp " + f"{value:,.0f}".replace(",", ".")


class TelegramBot:
    def __init__(self, token):
        self.token = token
        self.offset = 0
        self.reminder_sent_on = None

    def call(self, method, **payload):
        url = API.format(token=self.token, method=method)
        try:
            resp = requests.post(url, json=payload, timeout=40)
            return resp.json()
        except requests.RequestException:
            return {"ok": False}

    def send(self, chat_id, text):
        self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")

    def handle(self, message):
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        if not text:
            return

        command = text.split()[0].lower().split("@")[0]

        if command == "/login":
            self.cmd_login(chat_id, text)
            return
        if command == "/start":
            self.send(chat_id, (
                "<b>advisorAPP bot</b>\n\n"
                "Login dulu ya:\n"
                "<code>/login username password</code>\n\n"
                "Belum punya akun? Daftar di aplikasi web dulu."
            ))
            return

        user = linked_user(chat_id)
        if user is None:
            self.send(chat_id, (
                "Kamu belum login. Kirim:\n"
                "<code>/login username password</code>"
            ))
            return

        user_id = user["id"]
        username = user["username"]

        if command in ("/help",):
            self.send(chat_id, (
                f"<b>advisorAPP bot</b> — login sebagai {username}\n\n"
                "Catat pengeluaran/pemasukan langsung dari chat:\n"
                "• <code>makan 25000</code>\n"
                "• <code>kopi 15rb</code>\n"
                "• <code>+gaji 5jt</code> (income)\n"
                "• <code>income dividen 250rb</code>\n\n"
                "<b>Preset</b>\n"
                "• <code>sangu</code> - catat uang saku 100rb (Cash)\n"
                "• <code>/preset</code> - lihat daftar preset\n"
                "/preset add nama income 100000 Cash Uang saku\n"
                "/preset del nama\n\n"
                "<b>Perintah</b>\n"
                "/saldo - ringkasan hari & bulan ini\n"
                "/lapor - 10 transaksi terakhir\n"
                "/undo - batalkan input terakhir\n"
                "/kategori - daftar kategori\n"
                "/id - chat id kamu\n"
                "/logout - keluar dari sesi bot"
            ))
        elif command == "/logout":
            unlink_chat(chat_id)
            self.send(chat_id, "Kamu sudah logout. Sesi bot diakhiri.")
        elif command == "/preset":
            self.cmd_preset(chat_id, text)
        elif command == "/saldo":
            s = summary(user_id)
            net_today = s["today_income"] - s["today_expense"]
            net_month = s["month_income"] - s["month_expense"]
            self.send(chat_id, (
                f"<b>Hari ini</b>\n"
                f"Keluar: {rupiah(s['today_expense'])}\n"
                f"Masuk: {rupiah(s['today_income'])}\n"
                f"Net: {rupiah(net_today)}\n\n"
                f"<b>Bulan ini</b>\n"
                f"Keluar: {rupiah(s['month_expense'])}\n"
                f"Masuk: {rupiah(s['month_income'])}\n"
                f"Net: {rupiah(net_month)}"
            ))
        elif command == "/lapor":
            rows = recent_entries(user_id, 10)
            if not rows:
                self.send(chat_id, "Belum ada transaksi.")
                return
            lines = []
            for r in rows:
                sign = "-" if r["kind"] == "expense" else "+"
                desc = r["description"] or r["category"]
                lines.append(f"{r['date']}  {sign}{rupiah(r['amount'])}  {desc} ({r['category']})")
            self.send(chat_id, "<b>10 transaksi terakhir</b>\n" + "\n".join(lines))
        elif command == "/undo":
            last = undo_last(user_id)
            if last:
                self.send(chat_id, f"Dibatalkan: {rupiah(last['amount'])} {last['description']}")
            else:
                self.send(chat_id, "Tidak ada transaksi untuk dibatalkan.")
        elif command == "/kategori":
            exp, inc = list_categories(user_id)
            self.send(chat_id, (
                "<b>Pengeluaran</b>\n" + ", ".join(exp) + "\n\n"
                "<b>Pemasukan</b>\n" + ", ".join(inc)
            ))
        elif command == "/id":
            self.send(chat_id, f"Chat ID: <code>{chat_id}</code>")
        elif text.startswith("/"):
            self.send(chat_id, "Perintah tidak dikenal. Ketik /help.")
        else:
            self.record(chat_id, user_id, text)

    def record(self, chat_id, user_id, text):
        match = get_preset(text)
        if match:
            name, preset = match
            parsed = {
                "kind": preset.get("kind", "expense"),
                "amount": float(preset.get("amount", 0)),
                "description": preset.get("description", name),
                "category": preset.get("category"),
            }
        else:
            parsed = parse_message(text)
        if not parsed or not parsed.get("amount"):
            self.send(chat_id, "Format: <code>makan 25000</code> atau <code>+gaji 5jt</code>.")
            return
        entry_id, category, db, table = add_entry(user_id, parsed)
        db.close()
        label = "Pengeluaran" if parsed["kind"] == "expense" else "Pemasukan"
        desc = parsed["description"] or category
        self.send(chat_id, (
            f"✅ {label} dicatat (#{entry_id})\n"
            f"{rupiah(parsed['amount'])} — {desc} [{category}]"
        ))

    def cmd_preset(self, chat_id, text):
        parts = text.split()
        if len(parts) == 1:
            presets = load_presets()
            if not presets:
                self.send(chat_id, "Belum ada preset. Tambah dengan:\n"
                                    "<code>/preset add sangu income 100000 Cash Uang saku</code>")
                return
            lines = []
            for name, p in presets.items():
                kind = p.get("kind", "expense")
                lines.append(
                    f"• <code>{name}</code> — {rupiah(float(p.get('amount', 0)))} "
                    f"[{p.get('category') or '-'}] ({kind})"
                )
            self.send(chat_id, (
                "<b>Daftar preset</b>\n" + "\n".join(lines) +
                "\n\nPakai: kirim nama preset.\n"
                "Hapus: <code>/preset del nama</code>"
            ))
            return

        action = parts[1].lower()
        if action in ("del", "delete", "hapus"):
            if len(parts) < 3:
                self.send(chat_id, "Format: <code>/preset del nama</code>")
                return
            name = parts[2].lower()
            presets = load_presets()
            if name in presets:
                del presets[name]
                with _PRESET_LOCK:
                    _write_presets(presets)
                self.send(chat_id, f"Preset '{name}' dihapus.")
            else:
                self.send(chat_id, f"Preset '{name}' tidak ditemukan.")
            return

        if action in ("add", "set", "tambah"):
            if len(parts) < 5:
                self.send(chat_id, (
                    "Format: <code>/preset add nama kind amount kategori deskripsi</code>\n"
                    "Contoh: <code>/preset add sangu income 100000 Cash Uang saku</code>"
                ))
                return
            name = parts[2].lower()
            kind = parts[3].lower()
            if kind not in ("income", "expense"):
                self.send(chat_id, "Kind harus <code>income</code> atau <code>expense</code>.")
                return
            amount = parse_amount(parts[4])
            if not amount:
                self.send(chat_id, "Amount tidak valid. Contoh: <code>100000</code> atau <code>100rb</code>.")
                return
            category = parts[5] if len(parts) >= 6 else None
            description = " ".join(parts[6:]) if len(parts) >= 7 else name
            presets = load_presets()
            presets[name] = {
                "kind": kind,
                "amount": float(amount),
                "description": description,
                "category": category,
            }
            with _PRESET_LOCK:
                _write_presets(presets)
            self.send(chat_id, (
                f"Preset '{name}' disimpan:\n"
                f"{rupiah(float(amount))} [{category or '-'}] ({kind}) — {description}"
            ))
            return

        self.send(chat_id, (
            "Perintah preset:\n"
            "/preset - lihat daftar\n"
            "/preset add nama kind amount kategori deskripsi\n"
            "/preset del nama"
        ))

    def cmd_login(self, chat_id, text):
        parts = text.split()
        if len(parts) < 3:
            self.send(chat_id, "Format: <code>/login username password</code>")
            return
        username, password = parts[1], " ".join(parts[2:])
        user = authenticate(username, password)
        if not user:
            self.send(chat_id, "Login gagal. Cek username/password.")
            return
        link_chat(chat_id, user["id"])
        self.send(chat_id, (
            f"✅ Login berhasil sebagai <b>{user['username']}</b>.\n"
            "Sekarang kirim <code>makan 25000</code> untuk mencatat."
        ))

    def poll(self):
        while True:
            result = self.call("getUpdates", offset=self.offset, timeout=30)
            if not result.get("ok"):
                time.sleep(3)
                continue
            for update in result.get("result", []):
                self.offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if message:
                    try:
                        self.handle(message)
                    except Exception as exc:
                        print(f"[bot] handler error: {exc}")

    def reminder_loop(self):
        if os.environ.get("TELEGRAM_REMINDER", "on").lower() in ("off", "0", "false"):
            return
        reminder_time = os.environ.get("TELEGRAM_REMINDER_TIME", "21:00")
        try:
            hh, mm = [int(x) for x in reminder_time.split(":")]
        except ValueError:
            hh, mm = 21, 0

        while True:
            now = datetime.now()
            if (now.hour, now.minute) >= (hh, mm) and self.reminder_sent_on != now.date():
                self.send_reminders()
                self.reminder_sent_on = now.date()
            time.sleep(30)

    def send_reminders(self):
        db = auth_db()
        init_links(db)
        links = db.execute("SELECT chat_id, user_id FROM telegram_links").fetchall()
        db.close()
        today = date.today().isoformat()
        for link in links:
            try:
                udb = get_user_db(link["user_id"])
                count = udb.execute(
                    "SELECT (SELECT COUNT(*) FROM expenses WHERE date = ?) + "
                    "(SELECT COUNT(*) FROM income WHERE date = ?) AS c",
                    (today, today),
                ).fetchone()["c"]
                udb.close()
                if count == 0:
                    self.send(link["chat_id"], (
                        "📝 Hari ini belum ada catatan nih.\n"
                        "Kirim misal: <code>makan 25000</code>"
                    ))
            except Exception as exc:
                print(f"[bot] reminder error: {exc}")


def start_bot_in_thread():
    """Start the bot poller + reminder as a daemon thread (idempotent)."""
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True

    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("[bot] TELEGRAM_BOT_TOKEN belum diset — bot tidak dijalankan.")
        return False

    bot = TelegramBot(token)
    threading.Thread(target=bot.reminder_loop, daemon=True).start()
    threading.Thread(target=bot.poll, daemon=True).start()
    print("[bot] Telegram bot aktif.")
    return True


def main():
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diset (isi file .env).")

    bot = TelegramBot(token)
    threading.Thread(target=bot.reminder_loop, daemon=True).start()
    print("[bot] running. Tekan Ctrl+C untuk berhenti.")
    bot.poll()


if __name__ == "__main__":
    main()
