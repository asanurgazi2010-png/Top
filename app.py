import os
import re
import secrets
import string
import time
import threading
import sqlite3
import html
import logging
import math
from contextlib import contextmanager
from datetime import datetime, timezone

import telebot
from telebot import types

# ============================================================
# TOPUP TELEGRAM BOT
# Install: pip install pyTelegramBotAPI
# Set TOPUP_BOT_TOKEN and ADMIN_ID as environment variables
# (or in a .env file). NEVER write the token inside this file.
# ============================================================

def _load_dotenv(path=".env"):
    """Tiny .env reader (KEY=VALUE per line). Real environment variables win."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()

BOT_TOKEN = os.getenv("TOPUP_BOT_TOKEN", "8717284897:AAH2XHvB94bLusag6FmEsVfTF14ayrLYzK4").strip()
DB_PATH = os.getenv("TOPUP_DB_PATH", "topup_bot.db")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")  # auto-filled at startup

if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
    raise SystemExit(
        "TOPUP_BOT_TOKEN missing or malformed.\n"
        "A token looks like  123456789:AAH...  (ONE colon only).\n"
        "Put it in the environment or in a .env file next to this script."
    )
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "8103866669").strip())
except ValueError:
    raise SystemExit("ADMIN_ID missing or not a number (your own Telegram user id).")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("topup-bot")


# ------------------------------------------------------------
# Colored buttons (Telegram Bot API 9.4+)
# style: "primary" = blue, "success" = green, "danger" = red
# Works with new and old pyTelegramBotAPI versions.
# ------------------------------------------------------------
def _apply_style(button, style):
    if not style:
        return button
    button.style = style
    original = button.to_dict

    def to_dict():
        data = original()
        data["style"] = style
        return data

    button.to_dict = to_dict
    return button


def ibtn(text, style=None, **kwargs):
    """Inline button with optional color."""
    try:
        return types.InlineKeyboardButton(text, style=style, **kwargs)
    except TypeError:
        return _apply_style(types.InlineKeyboardButton(text, **kwargs), style)


def rbtn(text, style=None):
    """Reply-keyboard button with optional color."""
    try:
        return types.KeyboardButton(text, style=style)
    except TypeError:
        return _apply_style(types.KeyboardButton(text), style)


@contextmanager
def db():
    """Commit on success, roll back on error, and ALWAYS close the connection."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def parse_amount(text, allow_negative=False, allow_zero=False, maximum=10_000_000):
    """float() that rejects nan/inf and out-of-range values (raises ValueError)."""
    value = float(text)
    if not math.isfinite(value) or abs(value) > maximum:
        raise ValueError("bad amount")
    if not allow_negative and (value < 0 or (value == 0 and not allow_zero)):
        raise ValueError("bad amount")
    return value


def init_db():
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS force_channels (
            channel TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT DEFAULT '',
            FOREIGN KEY(game_id) REFERENCES games(id)
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            package_id INTEGER NOT NULL,
            game_name TEXT NOT NULL,
            package_name TEXT NOT NULL,
            price REAL NOT NULL,
            player_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL,
            transaction_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            reply TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gift_codes (
            code TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1,
            used_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gift_redeems (
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (code, user_id)
        );
        CREATE TABLE IF NOT EXISTS referrals (
            referred_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            bonus REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        cols = [r["name"] for r in con.execute("PRAGMA table_info(deposits)")]
        if "screenshot" not in cols:
            con.execute("ALTER TABLE deposits ADD COLUMN screenshot TEXT DEFAULT ''")
        if "usd_amount" not in cols:
            con.execute("ALTER TABLE deposits ADD COLUMN usd_amount REAL DEFAULT 0")
        if "rate" not in cols:
            con.execute("ALTER TABLE deposits ADD COLUMN rate REAL DEFAULT 0")
        gcols = [r["name"] for r in con.execute("PRAGMA table_info(gift_codes)")]
        if "channel_chat" not in gcols:
            con.execute("ALTER TABLE gift_codes ADD COLUMN channel_chat TEXT DEFAULT ''")
        if "channel_msg_id" not in gcols:
            con.execute("ALTER TABLE gift_codes ADD COLUMN channel_msg_id INTEGER DEFAULT 0")
        if "last_user" not in gcols:
            con.execute("ALTER TABLE gift_codes ADD COLUMN last_user TEXT DEFAULT ''")
        defaults = {
            "ref_bonus": "2",
            "payment_bkash": "Not configured",
            "payment_nagad": "Not configured",
            "payment_usdt": "Not configured",
            "order_channel": "",
            "gift_channel": "",
            "usd_rate": "0",
            "min_deposit_nagad": "50",
            "min_deposit_usdt": "1",
            "auto_approve": "0",
        }
        for key, value in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (key, value),
            )


def setting(key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_float_setting(key, default=0.0):
    try:
        return float(setting(key, str(default)) or default)
    except ValueError:
        return default


def set_setting(key, value):
    with db() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

def get_user(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def register_user(user):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users(user_id,username,first_name,created_at) "
            "VALUES(?,?,?,?)",
            (
                user.id,
                user.username or "",
                user.first_name or "",
                now_iso(),
            ),
        )
        con.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (user.username or "", user.first_name or "", user.id),
        )


def main_menu(user_id=None):
    k = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    k.row(
        rbtn("👤 Pʀᴏғɪʟᴇ", "primary"),
        rbtn("💳 Dᴇᴘᴏsɪᴛ", "success"),
    )
    k.row(
        rbtn("🎮 Tᴏᴘᴜᴘ", "success"),
        rbtn("🛒 Mʏ Oʀᴅᴇʀs", "primary"),
    )
    k.row(
        rbtn("🎁 Rᴇғᴇʀ", "primary"),
        rbtn("🎟 Gɪғᴛ Cᴏᴅᴇ", "success"),
    )
    # Admin-only button: shown only when the user is the admin.
    if user_id is not None and is_admin(user_id):
        k.row(
            rbtn("🎫 Sᴜᴘᴘᴏʀᴛ", "danger"),
            rbtn("🛠 Aᴅᴍɪɴ Pᴀɴᴇʟ", "danger"),
        )
    else:
        k.row(rbtn("🎫 Sᴜᴘᴘᴏʀᴛ", "danger"))
    return k


def is_admin(user_id):
    return int(user_id) == ADMIN_ID


def is_joined(user_id):
    with db() as con:
        channels = [
            r["channel"]
            for r in con.execute("SELECT channel FROM force_channels")
        ]
    for channel in channels:
        try:
            member = bot.get_chat_member(channel, user_id)
            if member.status in ("left", "kicked"):
                return False
            if member.status == "restricted" and getattr(member, "is_member", True) is False:
                return False
        except Exception:
            log.exception("Membership check failed for %s", channel)
            return False
    return True


def join_prompt(chat_id):
    with db() as con:
        channels = [
            r["channel"]
            for r in con.execute("SELECT channel FROM force_channels")
        ]
    kb = types.InlineKeyboardMarkup()
    for channel in channels:
        clean = channel.lstrip("@")
        kb.add(
            ibtn(
                "📢 Join " + channel,
                "primary",
                url=f"https://t.me/{clean}",
            )
        )
    kb.add(
        ibtn(
            "✅ I Joined — Check",
            "success",
            callback_data="check_join",
        )
    )
    bot.send_message(
        chat_id,
        "🔒 <b>বট ব্যবহার করতে আগে নিচের চ্যানেলগুলোতে Join করুন।</b>",
        reply_markup=kb,
    )


def require_join(message):
    if is_admin(message.from_user.id):
        return True
    if not is_joined(message.from_user.id):
        join_prompt(message.chat.id)
        return False
    return True


def referral_link(user_id):
    username = BOT_USERNAME or bot.get_me().username
    return f"https://t.me/{username}?start=ref_{user_id}"


def credit_referral(new_user_id, referrer_id):
    if not referrer_id or referrer_id == new_user_id:
        return False
    if not get_user(referrer_id):
        return False

    bonus = float(setting("ref_bonus", "2") or 2)
    with db() as con:
        exists = con.execute(
            "SELECT referred_id FROM referrals WHERE referred_id=?",
            (new_user_id,),
        ).fetchone()
        if exists:
            return False
        con.execute(
            "INSERT INTO referrals(referred_id,referrer_id,bonus,created_at) "
            "VALUES(?,?,?,?)",
            (new_user_id, referrer_id, bonus, now_iso()),
        )
        con.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (bonus, referrer_id),
        )
    try:
        bot.send_message(
            referrer_id,
            f"🎉 নতুন referral join করেছে!\n"
            f"💵 Bonus: ৳{bonus:g}\n"
            f"👤 User ID: <code>{new_user_id}</code>",
        )
    except Exception:
        pass
    return True


def show_account(message):
    row = get_user(message.from_user.id)
    if not row:
        register_user(message.from_user)
        row = get_user(message.from_user.id)

    with db() as con:
        count = con.execute(
            "SELECT COUNT(*) n FROM referrals WHERE referrer_id=?",
            (message.from_user.id,),
        ).fetchone()["n"]
        earned = con.execute(
            "SELECT COALESCE(SUM(bonus),0) total FROM referrals WHERE referrer_id=?",
            (message.from_user.id,),
        ).fetchone()["total"]

    bot.send_message(
        message.chat.id,
        "╭━━━〔 👤 <b>Mʏ Pʀᴏғɪʟᴇ</b> 〕━━━╮\n\n"
        f"👤 <b>Name:</b> {html.escape(message.from_user.first_name or 'User')}\n"
        f"🆔 <b>User ID:</b> <code>{message.from_user.id}</code>\n"
        f"💰 <b>Balance:</b> ৳{float(row['balance']):g}\n"
        f"👥 <b>Referrals:</b> {count}\n"
        f"🎁 <b>Referral Earned:</b> ৳{float(earned):g}\n\n"
        "╰━━━━━━━━━━━━━━━━━━╯",
        reply_markup=main_menu(message.from_user.id),
    )


def admin_panel(chat_id):
    """Show the admin tools as inline buttons."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        ibtn("🎁 Referral Bonus", "primary", callback_data="adm:refbonus"),
        ibtn("💳 Payment Settings", "primary", callback_data="adm:payment"),
        ibtn("💲 Dollar Rate", "primary", callback_data="adm:usdrate"),
        ibtn("⬇️ Min Deposit", "primary", callback_data="adm:mindep"),
        ibtn("⚡ Auto Approve", "danger", callback_data="adm:autoapp"),
        ibtn("📢 Order Channel", "primary", callback_data="adm:orderchannel"),
        ibtn("➕ Add Channel", "success", callback_data="adm:addchannel"),
        ibtn("➖ Remove Channel", "danger", callback_data="adm:removechannel"),
        ibtn("🎮 Add Game", "success", callback_data="adm:addgame"),
        ibtn("📦 Add Package", "success", callback_data="adm:addpackage"),
        ibtn("📋 Games & Packages", "primary", callback_data="adm:games"),
        ibtn("📊 Referral Stats", "primary", callback_data="adm:refstats"),
        ibtn("⏳ Pending Requests", "primary", callback_data="adm:pending"),
        ibtn("✅ Approve Deposit", "success", callback_data="adm:approvedep"),
        ibtn("❌ Reject Deposit", "danger", callback_data="adm:rejectdep"),
        ibtn("✅ Complete Order", "success", callback_data="adm:approveorder"),
        ibtn("↩️ Reject & Refund", "danger", callback_data="adm:rejectorder"),
        ibtn("💰 Manage Balance", "primary", callback_data="adm:balance"),
        ibtn("📨 Broadcast", "primary", callback_data="adm:broadcast"),
        ibtn("🎟 Create Gift Code", "success", callback_data="adm:gc_create"),
        ibtn("📋 Gift Codes", "primary", callback_data="adm:gc_list"),
        ibtn("🚫 Disable Gift Code", "danger", callback_data="adm:gc_disable"),
        ibtn("📣 Gift Channel", "primary", callback_data="adm:giftchannel"),
    ]
    kb.add(*buttons)
    bot.send_message(
        chat_id,
        "🛠 <b>ADMIN PANEL</b>\n\nএকটি অপশন বেছে নিন। প্রয়োজন হলে বাটনে ক্লিক করলে কমান্ডের ফরম্যাট দেখাবে।",
        reply_markup=kb,
    )


# ------------------------------------------------------------
# ADMIN PANEL: every button asks for the needed info directly
# ------------------------------------------------------------
def parse_channel(text, allow_id=False):
    """Accepts @name, name, t.me/name or https://t.me/name. Returns '@name' or None."""
    t = text.strip()
    t = re.sub(r"^https?://", "", t, flags=re.I)
    if t.lower().startswith(("t.me/", "telegram.me/")):
        t = t.split("/", 1)[1]
    t = t.split("?")[0].strip("/").split("/")[0]
    if t.startswith("+") or t.lower() == "joinchat":
        return None
    if allow_id and re.fullmatch(r"-100\d+", t):
        return t
    t = t.lstrip("@")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", t):
        return "@" + t
    return None


def check_channel(channel):
    """Returns an error text, or None when the bot can use the channel."""
    try:
        bot.get_chat(channel)
        me = bot.get_me()
        member = bot.get_chat_member(channel, me.id)
        if member.status not in ("administrator", "creator"):
            return "❌ Bot ওই channel-এ admin নয়। আগে Bot-কে admin করুন।"
    except Exception:
        return "❌ Channel পাওয়া যায়নি, অথবা Bot ওই channel-এ admin নয়।"
    return None


def admin_back_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(ibtn("⬅️ Admin Panel", "primary", callback_data="adm:home"))
    return kb


def ask(chat_id, text, handler, *args):
    msg = bot.send_message(
        chat_id,
        text + "\n\n<i>বাতিল করতে /cancel লিখুন।</i>",
        reply_markup=admin_back_kb(),
    )
    bot.register_next_step_handler(msg, handler, *args)


def admin_input(message):
    """Returns the admin's text, or None if it was a cancel / not from the admin."""
    if not is_admin(message.from_user.id):
        return None
    if _is_cancel(message):
        bot.send_message(
            message.chat.id, "❌ বাতিল করা হয়েছে।", reply_markup=admin_back_kb()
        )
        return None
    return message.text.strip()


def pending_deposits_text():
    with db() as con:
        rows = con.execute(
            "SELECT * FROM deposits WHERE status='pending' ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return "⏳ কোনো pending deposit নেই।"
    return "⏳ <b>Pending deposits:</b>\n" + "\n".join(
        f"#{d['id']} | User {d['user_id']} | "
        f"{html.escape(d['method'])} | TX: {html.escape(d['transaction_id'])}"
        for d in rows
    )


def pending_orders_text():
    with db() as con:
        rows = con.execute(
            "SELECT * FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return "⏳ কোনো pending order নেই।"
    return "⏳ <b>Pending orders:</b>\n" + "\n".join(
        f"#{o['id']} | User {o['user_id']} | {html.escape(o['package_name'])} | "
        f"৳{float(o['price']):g} | Player: {html.escape(o['player_id'])}"
        for o in rows
    )


def process_ref_bonus(message):
    text = admin_input(message)
    if text is None:
        return
    try:
        bonus = parse_amount(text, allow_zero=True, maximum=100000)
    except ValueError:
        ask(message.chat.id, "❌ সঠিক amount দিন (যেমন: 5):", process_ref_bonus)
        return
    set_setting("ref_bonus", bonus)
    bot.send_message(
        message.chat.id,
        f"✅ Referral bonus এখন ৳{bonus:g}",
        reply_markup=admin_back_kb(),
    )


def process_payment_info(message, method):
    text = admin_input(message)
    if text is None:
        return
    set_setting("payment_" + method, text)
    bot.send_message(
        message.chat.id,
        f"✅ {method.upper()} payment info updated:\n<code>{html.escape(text)}</code>",
        reply_markup=admin_back_kb(),
    )


def process_usd_rate(message):
    text = admin_input(message)
    if text is None:
        return
    try:
        rate = parse_amount(_clean_number(text), maximum=100000)
    except ValueError:
        ask(message.chat.id, "❌ সঠিক rate লিখুন (যেমন: 125):", process_usd_rate)
        return
    set_setting("usd_rate", rate)
    bot.send_message(
        message.chat.id,
        f"✅ Dollar Rate সেট হয়েছে: $1 = ৳{rate:g}",
        reply_markup=admin_back_kb(),
    )


def process_min_deposit(message, method):
    text = admin_input(message)
    if text is None:
        return
    try:
        value = parse_amount(_clean_number(text), allow_zero=True, maximum=1_000_000)
    except ValueError:
        ask(message.chat.id, "❌ সঠিক amount লিখুন (যেমন: 50):", process_min_deposit, method)
        return
    set_setting("min_deposit_" + method, value)
    unit = "$" if method == "usdt" else "৳"
    bot.send_message(
        message.chat.id,
        f"✅ {DEPOSIT_METHODS[method]} Minimum Deposit: {unit}{value:g}",
        reply_markup=admin_back_kb(),
    )


def process_order_channel(message):
    text = admin_input(message)
    if text is None:
        return
    channel = parse_channel(text, allow_id=True)
    if not channel:
        ask(
            message.chat.id,
            "❌ সঠিক channel link বা @username দিন।\nযেমন: <code>@mychannel</code> বা <code>https://t.me/mychannel</code>",
            process_order_channel,
        )
        return
    error = check_channel(channel)
    if error:
        ask(message.chat.id, error + "\nঅন্য channel দিন:", process_order_channel)
        return
    set_setting("order_channel", channel)
    bot.send_message(
        message.chat.id,
        f"✅ Order notification channel set: {html.escape(channel)}",
        reply_markup=admin_back_kb(),
    )


def process_add_channel(message):
    text = admin_input(message)
    if text is None:
        return
    channel = parse_channel(text)
    if not channel:
        ask(
            message.chat.id,
            "❌ সঠিক public channel link বা @username দিন।\nযেমন: <code>@mychannel</code> বা <code>https://t.me/mychannel</code>",
            process_add_channel,
        )
        return
    error = check_channel(channel)
    if error:
        ask(message.chat.id, error + "\nআবার channel দিন:", process_add_channel)
        return
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO force_channels(channel) VALUES(?)", (channel,)
        )
    bot.send_message(
        message.chat.id,
        f"✅ Force-join channel added: {html.escape(channel)}",
        reply_markup=admin_back_kb(),
    )


def process_remove_channel(message):
    text = admin_input(message)
    if text is None:
        return
    channel = parse_channel(text)
    if not channel:
        ask(message.chat.id, "❌ সঠিক channel link বা @username দিন:", process_remove_channel)
        return
    with db() as con:
        cur = con.execute(
            "DELETE FROM force_channels WHERE lower(channel)=lower(?)", (channel,)
        )
    bot.send_message(
        message.chat.id,
        "✅ Channel removed." if cur.rowcount else "❌ এই channel তালিকায় নেই।",
        reply_markup=admin_back_kb(),
    )


def process_add_game(message):
    text = admin_input(message)
    if text is None:
        return
    try:
        with db() as con:
            con.execute("INSERT INTO games(name) VALUES(?)", (text,))
        bot.send_message(
            message.chat.id,
            f"✅ Game added: {html.escape(text)}",
            reply_markup=admin_back_kb(),
        )
    except sqlite3.IntegrityError:
        ask(message.chat.id, "❌ এই game আগে থেকেই আছে। অন্য নাম লিখুন:", process_add_game)


def process_pkg_name(message, game_id):
    text = admin_input(message)
    if text is None:
        return
    ask(message.chat.id, "💰 Package-এর <b>Price</b> লিখুন (যেমন: 80):", process_pkg_price, game_id, text)


def process_pkg_price(message, game_id, name):
    text = admin_input(message)
    if text is None:
        return
    try:
        price = parse_amount(text, allow_zero=True)
    except ValueError:
        ask(message.chat.id, "❌ Price অবশ্যই number হতে হবে। আবার লিখুন:", process_pkg_price, game_id, name)
        return
    ask(
        message.chat.id,
        "📝 Package-এর <b>Description</b> লিখুন।\nদিতে না চাইলে <code>-</code> লিখুন:",
        process_pkg_desc,
        game_id,
        name,
        price,
    )


def process_pkg_desc(message, game_id, name, price):
    text = admin_input(message)
    if text is None:
        return
    description = "" if text == "-" else text
    with db() as con:
        con.execute(
            "INSERT INTO packages(game_id,name,price,description) VALUES(?,?,?,?)",
            (game_id, name, price, description),
        )
    bot.send_message(
        message.chat.id,
        f"✅ Package added: {html.escape(name)} — ৳{price:g}",
        reply_markup=admin_back_kb(),
    )


def process_id_action(message, command, handler, label):
    text = admin_input(message)
    if text is None:
        return
    if not text.isdigit():
        ask(message.chat.id, f"❌ শুধু {label} (সংখ্যা) লিখুন:", process_id_action, command, handler, label)
        return
    message.text = f"/{command} {text}"
    handler(message)


def process_balance_user(message):
    text = admin_input(message)
    if text is None:
        return
    if not text.isdigit():
        ask(message.chat.id, "❌ শুধু User ID (সংখ্যা) লিখুন:", process_balance_user)
        return
    if not get_user(int(text)):
        ask(message.chat.id, "❌ এই User পাওয়া যায়নি। সঠিক User ID লিখুন:", process_balance_user)
        return
    ask(
        message.chat.id,
        "💰 Amount লিখুন।\nBalance যোগ করতে <code>10</code>, কমাতে <code>-10</code> লিখুন:",
        process_balance_amount,
        text,
    )


def process_balance_amount(message, user_id):
    text = admin_input(message)
    if text is None:
        return
    try:
        parse_amount(text, allow_negative=True)
    except ValueError:
        ask(message.chat.id, "❌ সঠিক amount লিখুন (যেমন: 10 বা -10):", process_balance_amount, user_id)
        return
    message.text = f"/balance {user_id} {text}"
    change_balance(message)


def process_broadcast(message):
    text = admin_input(message)
    if text is None:
        return
    message.text = "/broadcast " + text
    broadcast(message)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("adm:"))
def admin_panel_callback(call):
    # Only the configured admin can use these controls.
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Admin only.", show_alert=True)
        return

    action = call.data.split(":", 1)[1]
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(chat_id)

    if action == "home":
        admin_panel(chat_id)
        return

    # Existing command handlers check message.from_user, so use the real admin.
    msg = call.message
    msg.from_user = call.from_user

    if action == "refbonus":
        current = float(setting("ref_bonus", "2") or 2)
        ask(chat_id, f"🎁 <b>Referral Bonus</b>\nবর্তমান: ৳{current:g}\n\nনতুন bonus amount লিখুন (যেমন: 5):", process_ref_bonus)

    elif action == "payment":
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            ibtn("📱 Nagad", "success", callback_data="adm:pay:nagad"),
            ibtn("💵 USDT (BEP20)", "primary", callback_data="adm:pay:usdt"),
        )
        kb.add(ibtn("⬅️ Admin Panel", "primary", callback_data="adm:home"))
        bot.send_message(chat_id, "💳 <b>কোন Payment method সেট করবেন?</b>", reply_markup=kb)

    elif action.startswith("pay:"):
        method = action.split(":", 1)[1]
        if method not in DEPOSIT_METHODS:
            return
        label = "BEP20 wallet address" if method == "usdt" else "Nagad নম্বর"
        current = setting("payment_" + method, "Not configured")
        ask(
            chat_id,
            f"💳 <b>{DEPOSIT_METHODS[method]}</b>\nবর্তমান: <code>{html.escape(current)}</code>\n\nনতুন {label} লিখুন:",
            process_payment_info,
            method,
        )

    elif action == "usdrate":
        current = get_usd_rate()
        ask(
            chat_id,
            f"💲 <b>Dollar Rate</b>\nবর্তমান: $1 = ৳{current:g}\n\n"
            "১ ডলার = কত টাকা লিখুন (যেমন: 125):",
            process_usd_rate,
        )

    elif action == "mindep":
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            ibtn(f"📱 Nagad (৳{get_min_deposit('nagad'):g})", "success", callback_data="adm:mindep:nagad"),
            ibtn(f"💵 USDT (${get_min_deposit('usdt'):g})", "primary", callback_data="adm:mindep:usdt"),
        )
        kb.add(ibtn("⬅️ Admin Panel", "primary", callback_data="adm:home"))
        bot.send_message(chat_id, "⬇️ <b>কোন method-এর Minimum Deposit সেট করবেন?</b>", reply_markup=kb)

    elif action.startswith("mindep:"):
        method = action.split(":", 1)[1]
        if method not in DEPOSIT_METHODS:
            return
        unit = "ডলার ($)" if method == "usdt" else "টাকা (৳)"
        ask(
            chat_id,
            f"⬇️ <b>{DEPOSIT_METHODS[method]} Minimum Deposit</b>\n"
            f"বর্তমান: {get_min_deposit(method):g}\n\nনতুন minimum {unit} লিখুন:",
            process_min_deposit,
            method,
        )

    elif action == "autoapp":
        new_state = "0" if setting("auto_approve", "0") == "1" else "1"
        set_setting("auto_approve", new_state)
        if new_state == "1":
            txt = (
                "⚡ <b>Auto Approve: ON</b>\n\n"
                "⚠️ এখন user amount, Transaction ID ও screenshot পাঠালেই Admin verify ছাড়া "
                "balance যোগ হয়ে যাবে। ভুয়া TxID দিয়ে কেউ balance নিতে পারে — ঝুঁকি আপনার।"
            )
        else:
            txt = "🛑 <b>Auto Approve: OFF</b>\n\nএখন প্রতিটি deposit আপনি এক ট্যাপে Approve করবেন।"
        bot.send_message(chat_id, txt, reply_markup=admin_back_kb())

    elif action == "orderchannel":
        current = setting("order_channel", "") or "সেট করা নেই"
        ask(
            chat_id,
            f"📢 <b>Order Notification Channel</b>\nবর্তমান: {html.escape(current)}\n\n"
            "Channel-এর link বা @username দিন।\nBot-কে ওই channel-এ admin করা থাকতে হবে।",
            process_order_channel,
        )

    elif action == "addchannel":
        ask(
            chat_id,
            "➕ <b>Force-Join Channel যোগ করুন</b>\n\n"
            "Channel-এর link বা @username দিন।\nযেমন: <code>@mychannel</code> বা <code>https://t.me/mychannel</code>\n"
            "Bot-কে ওই channel-এ admin করা থাকতে হবে।",
            process_add_channel,
        )

    elif action == "removechannel":
        with db() as con:
            channels = [r["channel"] for r in con.execute("SELECT channel FROM force_channels")]
        if not channels:
            bot.send_message(chat_id, "❌ কোনো force-join channel নেই।", reply_markup=admin_back_kb())
            return
        listing = "\n".join(html.escape(c) for c in channels)
        ask(
            chat_id,
            f"➖ <b>Channel Remove করুন</b>\n\nবর্তমান channel:\n{listing}\n\nযে channel-এর link বা @username remove করবেন সেটা লিখুন:",
            process_remove_channel,
        )

    elif action == "addgame":
        ask(chat_id, "🎮 <b>Game-এর নাম লিখুন</b>\nযেমন: Free Fire", process_add_game)

    elif action == "addpackage":
        with db() as con:
            games = con.execute("SELECT * FROM games ORDER BY id").fetchall()
        if not games:
            bot.send_message(chat_id, "❌ আগে একটি Game যোগ করুন।", reply_markup=admin_back_kb())
            return
        kb = types.InlineKeyboardMarkup()
        for game in games:
            kb.add(ibtn(f"🎮 {game['name']}", "primary", callback_data=f"adm:pkg:{game['id']}"))
        kb.add(ibtn("⬅️ Admin Panel", "primary", callback_data="adm:home"))
        bot.send_message(chat_id, "📦 <b>কোন Game-এ Package যোগ করবেন?</b>", reply_markup=kb)

    elif action.startswith("pkg:"):
        try:
            game_id = int(action.split(":", 1)[1])
        except ValueError:
            return
        with db() as con:
            game = con.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        if not game:
            bot.send_message(chat_id, "❌ Game পাওয়া যায়নি।", reply_markup=admin_back_kb())
            return
        ask(
            chat_id,
            f"📦 <b>{html.escape(game['name'])}</b>\n\nPackage-এর <b>নাম</b> লিখুন (যেমন: 100 Diamonds):",
            process_pkg_name,
            game_id,
        )

    elif action == "games":
        list_games(msg)

    elif action == "refstats":
        ref_stats(msg)

    elif action == "pending":
        pending(msg)

    elif action == "approvedep":
        ask(chat_id, pending_deposits_text() + "\n\n✅ <b>Approve করতে Deposit ID লিখুন:</b>",
            process_dep_approve_id)

    elif action == "rejectdep":
        ask(chat_id, pending_deposits_text() + "\n\n❌ <b>Reject করতে Deposit ID লিখুন:</b>",
            process_id_action, "reject_deposit", reject_deposit, "Deposit ID")

    elif action == "approveorder":
        ask(chat_id, pending_orders_text() + "\n\n✅ <b>Complete করতে Order ID লিখুন:</b>",
            process_id_action, "approve_order", approve_order, "Order ID")

    elif action == "rejectorder":
        ask(chat_id, pending_orders_text() + "\n\n↩️ <b>Reject ও Refund করতে Order ID লিখুন:</b>",
            process_id_action, "reject_order", reject_order, "Order ID")

    elif action == "balance":
        ask(chat_id, "💰 <b>Manage Balance</b>\n\nUser-এর <b>User ID</b> লিখুন:", process_balance_user)

    elif action == "broadcast":
        ask(chat_id, "📨 <b>Broadcast</b>\n\nসব user-কে যে মেসেজ পাঠাতে চান সেটা লিখুন:", process_broadcast)

    elif action == "gc_create":
        ask(chat_id, "🎟 <b>Gift Code তৈরি</b>\n\nপ্রতি user কত <b>টাকা (৳)</b> পাবে লিখুন:", process_gc_amount)

    elif action == "giftchannel":
        current = setting("gift_channel", "") or "সেট করা নেই (Order channel ব্যবহার হবে)"
        ask(
            chat_id,
            f"📣 <b>Gift Notification Channel</b>\nবর্তমান: {html.escape(current)}\n\n"
            "যে channel-এ Gift Code ব্যবহারের পোস্ট যাবে তার link বা @username দিন।\n"
            "মুছে ফেলতে <code>-</code> লিখুন। Bot-কে ওই channel-এ admin করা থাকতে হবে।",
            process_gift_channel,
        )

    elif action == "gc_list":
        bot.send_message(chat_id, gift_codes_text(), reply_markup=admin_back_kb())

    elif action == "gc_disable":
        ask(chat_id, gift_codes_text() + "\n\n🚫 <b>যে Code বন্ধ করবেন সেটা লিখুন:</b>", process_gc_disable)

    else:
        bot.send_message(chat_id, "Unknown admin option.", reply_markup=admin_back_kb())


@bot.message_handler(commands=["start"])
def start(message):
    user = message.from_user
    is_new = get_user(user.id) is None
    register_user(user)

    parts = (message.text or "").split(maxsplit=1)
    if is_new and len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            credit_referral(user.id, int(parts[1][4:]))
        except (ValueError, TypeError):
            pass

    if not is_admin(user.id) and not is_joined(user.id):
        join_prompt(message.chat.id)
        return

    bot.send_message(
        message.chat.id,
        f"👋 Welcome, <b>{html.escape(user.first_name or 'User')}</b>!\n"
        "🎮 TopUp Bot-এ স্বাগতম। নিচের মেনু ব্যবহার করুন।",
        reply_markup=main_menu(message.from_user.id),
    )


@bot.callback_query_handler(func=lambda call: call.data == "check_join")
def check_join_callback(call):
    if is_joined(call.from_user.id) or is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Membership verified!")
        bot.send_message(
            call.message.chat.id,
            "✅ Verification successful.",
            reply_markup=main_menu(call.from_user.id),
        )
    else:
        bot.answer_callback_query(
            call.id,
            "❌ সব চ্যানেলে Join করে আবার চেষ্টা করুন।",
            show_alert=True,
        )


@bot.message_handler(commands=["admin"])
def admin_command(message):
    if is_admin(message.from_user.id):
        admin_panel(message.chat.id)


@bot.message_handler(commands=["setrefbonus"])
def set_ref_bonus(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: /setrefbonus 2")
        return
    try:
        bonus = parse_amount(parts[1], allow_zero=True, maximum=100000)
        set_setting("ref_bonus", bonus)
        bot.reply_to(message, f"✅ Referral bonus set to ৳{bonus:g}")
    except ValueError:
        bot.reply_to(message, "সঠিক amount দিন। যেমন: /setrefbonus 2")


@bot.message_handler(commands=["refstats"])
def ref_stats(message):
    if not is_admin(message.from_user.id):
        return
    with db() as con:
        total = con.execute("SELECT COUNT(*) n FROM referrals").fetchone()["n"]
        earned = con.execute(
            "SELECT COALESCE(SUM(bonus),0) total FROM referrals"
        ).fetchone()["total"]
        users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    bot.reply_to(
        message,
        f"📊 Referral stats\nUsers: {users}\nReferrals: {total}\n"
        f"Bonus paid: ৳{float(earned):g}",
    )


@bot.message_handler(commands=["setpayment"])
def set_payment(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or parts[1].lower() not in ("nagad", "usdt"):
        bot.reply_to(message, "Usage: /setpayment nagad NUMBER\n/setpayment usdt BEP20_WALLET")
        return
    method = parts[1].lower()
    set_setting("payment_" + method, parts[2].strip())
    bot.reply_to(message, f"✅ {method.upper()} payment info updated.")


@bot.message_handler(commands=["setorderchannel"])
def set_order_channel(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /setorderchannel @channel")
        return
    set_setting("order_channel", parts[1].strip())
    bot.reply_to(message, "✅ Order notification channel set.")


@bot.message_handler(commands=["addchannel"])
def add_channel(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("@"):
        bot.reply_to(message, "Usage: /addchannel @channel")
        return
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO force_channels(channel) VALUES(?)",
            (parts[1],),
        )
    bot.reply_to(
        message,
        "✅ Force-join channel added. Make sure the bot is admin there.",
    )


@bot.message_handler(commands=["removechannel"])
def remove_channel(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /removechannel @channel")
        return
    with db() as con:
        con.execute("DELETE FROM force_channels WHERE channel=?", (parts[1],))
    bot.reply_to(message, "✅ Channel removed.")


@bot.message_handler(commands=["addgame"])
def add_game(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /addgame Free Fire")
        return
    try:
        with db() as con:
            con.execute("INSERT INTO games(name) VALUES(?)", (parts[1].strip(),))
        bot.reply_to(message, "✅ Game added.")
    except sqlite3.IntegrityError:
        bot.reply_to(message, "এই game আগে থেকেই আছে।")


@bot.message_handler(commands=["addpackage"])
def add_package(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(
            message,
            "Usage: /addpackage GAME_ID | Package Name | Price | Description",
        )
        return
    fields = [x.strip() for x in parts[1].split("|")]
    if len(fields) < 3:
        bot.reply_to(message, "Format: GAME_ID | Package Name | Price | Description")
        return
    try:
        game_id = int(fields[0])
        price = parse_amount(fields[2], allow_zero=True)
        description = fields[3] if len(fields) > 3 else ""
        with db() as con:
            exists = con.execute(
                "SELECT id FROM games WHERE id=?", (game_id,)
            ).fetchone()
            if not exists:
                bot.reply_to(message, "Game ID পাওয়া যায়নি। /games লিখে ID দেখুন।")
                return
            con.execute(
                "INSERT INTO packages(game_id,name,price,description) VALUES(?,?,?,?)",
                (game_id, fields[1], price, description),
            )
        bot.reply_to(message, "✅ Package added.")
    except (ValueError, sqlite3.Error):
        bot.reply_to(message, "তথ্য ঠিক দিন। Price অবশ্যই number হতে হবে।")


@bot.message_handler(commands=["games"])
def list_games(message):
    with db() as con:
        games = con.execute("SELECT * FROM games ORDER BY id").fetchall()
        packages = con.execute(
            "SELECT p.*,g.name game_name FROM packages p "
            "JOIN games g ON g.id=p.game_id ORDER BY p.id"
        ).fetchall()
    lines = ["🎮 <b>Games & Packages</b>"]
    for game in games:
        lines.append(f"\n<b>Game ID {game['id']}: {html.escape(game['name'])}</b>")
        found = [p for p in packages if p["game_id"] == game["id"]]
        if not found:
            lines.append("  No packages yet.")
        for package in found:
            lines.append(
                f"  Package ID {package['id']}: {html.escape(package['name'])}"
                f" — ৳{float(package['price']):g}"
            )
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["pending"])
def pending(message):
    if not is_admin(message.from_user.id):
        return
    with db() as con:
        deposits = con.execute(
            "SELECT * FROM deposits WHERE status='pending' ORDER BY id DESC LIMIT 30"
        ).fetchall()
        orders = con.execute(
            "SELECT * FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 30"
        ).fetchall()
    lines = ["⏳ <b>Pending deposits</b>"]
    lines += [
        f"Deposit #{d['id']} | User {d['user_id']} | "
        f"{('৳' + format(float(d['amount']), 'g')) if float(d['amount']) > 0 else 'Amount: screenshot দেখুন'} | "
        f"{html.escape(d['method'])} | TX: {html.escape(d['transaction_id'])}"
        for d in deposits
    ]
    lines.append("\n⏳ <b>Pending orders</b>")
    lines += [
        f"Order #{o['id']} | User {o['user_id']} | "
        f"{html.escape(o['package_name'])} | ৳{float(o['price']):g} | "
        f"Player ID: {html.escape(o['player_id'])}"
        for o in orders
    ]
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["approve_deposit"])
def approve_deposit(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) not in (2, 3) or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /approve_deposit ID AMOUNT")
        return
    dep_id = int(parts[1])
    with db() as con:
        dep = con.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    if not dep or dep["status"] != "pending":
        bot.reply_to(message, "Pending deposit পাওয়া যায়নি।")
        return
    try:
        amount = parse_amount(parts[2]) if len(parts) == 3 else float(dep["amount"])
    except ValueError:
        amount = 0
    if amount <= 0:
        bot.reply_to(message, "Amount দিন: /approve_deposit ID AMOUNT")
        return
    if do_approve_deposit(dep_id, amount):
        bot.reply_to(message, f"✅ Deposit #{dep_id} approved. ৳{amount:g} যোগ হয়েছে।")
    else:
        bot.reply_to(message, "Pending deposit পাওয়া যায়নি।")


@bot.message_handler(commands=["reject_deposit"])
def reject_deposit(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /reject_deposit ID")
        return
    dep = do_reject_deposit(int(parts[1]))
    bot.reply_to(
        message,
        "❌ Deposit rejected." if dep else "Pending deposit পাওয়া যায়নি।",
    )


@bot.message_handler(commands=["approve_order"])
def approve_order(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /approve_order ID")
        return
    with db() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id=?", (int(parts[1]),)
        ).fetchone()
        if not order or order["status"] != "pending":
            bot.reply_to(message, "Pending order পাওয়া যায়নি।")
            return
        cur = con.execute(
            "UPDATE orders SET status='completed' WHERE id=? AND status='pending'",
            (int(parts[1]),),
        )
        if not cur.rowcount:
            bot.reply_to(message, "Pending order পাওয়া যায়নি।")
            return
    bot.reply_to(message, "✅ Order marked completed.")
    try:
        bot.send_message(
            order["user_id"],
            f"✅ আপনার Order #{order['id']} complete হয়েছে।",
        )
    except Exception:
        pass


@bot.message_handler(commands=["reject_order"])
def reject_order(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /reject_order ID")
        return
    order_id = int(parts[1])
    with db() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        if not order or order["status"] != "pending":
            bot.reply_to(message, "Pending order পাওয়া যায়নি।")
            return
        cur = con.execute(
            "UPDATE orders SET status='rejected' WHERE id=? AND status='pending'",
            (order_id,),
        )
        if not cur.rowcount:  # someone else already handled it -> never refund twice
            bot.reply_to(message, "Pending order পাওয়া যায়নি।")
            return
        con.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (order["price"], order["user_id"]),
        )
    bot.reply_to(message, f"❌ Order #{order_id} rejected; balance refunded.")
    try:
        bot.send_message(
            order["user_id"],
            f"❌ আপনার Order #{order_id} reject হয়েছে। ৳{float(order['price']):g} ফেরত দেওয়া হয়েছে।",
        )
    except Exception:
        pass


@bot.message_handler(commands=["balance"])
def change_balance(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(
            message,
            "Usage: /balance USER_ID AMOUNT\nExample: /balance 123456789 10",
        )
        return
    try:
        user_id, amount = int(parts[1]), parse_amount(parts[2], allow_negative=True)
        with db() as con:
            cur = con.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=? AND balance+?>=0",
                (amount, user_id, amount),
            )
        if not cur.rowcount:
            bot.reply_to(message, "User পাওয়া যায়নি, অথবা balance ঋণাত্মক হয়ে যাবে।")
            return
        bot.reply_to(message, f"✅ Balance updated by ৳{amount:g}.")
        try:
            bot.send_message(
                user_id,
                f"💰 Admin আপনার balance ৳{amount:g} পরিবর্তন করেছেন।",
            )
        except Exception:
            pass
    except ValueError:
        bot.reply_to(message, "User ID ও amount ঠিক দিন।")


@bot.message_handler(commands=["broadcast"])
def broadcast(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /broadcast Your message")
        return
    with db() as con:
        ids = [r["user_id"] for r in con.execute("SELECT user_id FROM users")]
    sent = 0
    for uid in ids:
        try:
            try:
                bot.send_message(uid, parts[1])
            except Exception:
                # Probably broken HTML tags in the text -> resend as plain text.
                bot.send_message(uid, html.escape(parts[1]))
            sent += 1
        except Exception:
            pass
        time.sleep(0.05)  # stay under Telegram's flood limit
    bot.reply_to(message, f"📣 Broadcast finished. Sent: {sent}/{len(ids)}")


# Main menu handlers use the new button labels.
@bot.message_handler(func=lambda m: m.text == "👤 Pʀᴏғɪʟᴇ")
def account_handler(message):
    if require_join(message):
        show_account(message)


@bot.message_handler(func=lambda m: m.text == "🎁 Rᴇғᴇʀ")
def refer_handler(message):
    if not require_join(message):
        return
    with db() as con:
        count = con.execute(
            "SELECT COUNT(*) n FROM referrals WHERE referrer_id=?",
            (message.from_user.id,),
        ).fetchone()["n"]
        earned = con.execute(
            "SELECT COALESCE(SUM(bonus),0) total FROM referrals WHERE referrer_id=?",
            (message.from_user.id,),
        ).fetchone()["total"]
    bonus = float(setting("ref_bonus", "2") or 2)
    bot.send_message(
        message.chat.id,
        "╭━━━〔 🎁 <b>Rᴇғᴇʀ & Eᴀʀɴ</b> 〕━━━╮\n\n"
        f"💵 <b>Referral Bonus:</b> ৳{bonus:g}\n"
        f"👥 <b>Total Referrals:</b> {count}\n"
        f"💰 <b>Total Earned:</b> ৳{float(earned):g}\n\n"
        f"🔗 <b>Your Referral Link:</b>\n"
        f"<code>{referral_link(message.from_user.id)}</code>\n\n"
        "বন্ধুদের link পাঠান। নতুন user প্রথমবার link দিয়ে bot start করলে bonus যোগ হবে।\n"
        "╰━━━━━━━━━━━━━━━━━━╯",
        reply_markup=main_menu(message.from_user.id),
    )


def user_balance(user_id):
    row = get_user(user_id)
    return float(row["balance"]) if row else 0.0


def games_view(user_id):
    """Pretty game list. Returns (text, keyboard) or None when nothing to show."""
    with db() as con:
        games = con.execute(
            "SELECT g.id, g.name, COUNT(p.id) n, MIN(p.price) minp "
            "FROM games g JOIN packages p ON p.game_id=g.id "
            "GROUP BY g.id ORDER BY g.id"
        ).fetchall()
    if not games:
        return None
    lines = []
    kb = types.InlineKeyboardMarkup()
    for game in games:
        lines.append(
            f"🎮 <b>{html.escape(game['name'])}</b>\n"
            f"   ┗ 📦 {game['n']} Packages  •  💸 শুরু ৳{float(game['minp']):g} থেকে"
        )
        kb.add(
            ibtn(
                f"🎮 {game['name']}  •  {game['n']} Packages",
                "primary",
                callback_data=f"game:{game['id']}",
            )
        )
    text = (
        "╭━━━〔 🎮 <b>Tᴏᴘᴜᴘ Gᴀᴍᴇs</b> 〕━━━╮\n\n"
        f"💰 <b>Your Balance:</b> ৳{user_balance(user_id):g}\n\n"
        + "\n\n".join(lines)
        + "\n\n╰━━━━━━━━━━━━━━━━━━╯\n"
        "👇 <i>নিচ থেকে আপনার Game বেছে নিন</i>"
    )
    return text, kb


def packages_view(game_id, user_id):
    """Pretty package list for one game. Returns (text, keyboard) or None."""
    with db() as con:
        game = con.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        packages = con.execute(
            "SELECT * FROM packages WHERE game_id=? ORDER BY id", (game_id,)
        ).fetchall()
    if not game or not packages:
        return None
    lines = []
    kb = types.InlineKeyboardMarkup()
    for package in packages:
        line = f"💎 <b>{html.escape(package['name'])}</b>  —  ৳{float(package['price']):g}"
        if package["description"]:
            line += f"\n   ┗ <i>{html.escape(package['description'])}</i>"
        lines.append(line)
        kb.add(
            ibtn(
                f"💎 {package['name']}  •  ৳{float(package['price']):g}",
                "success",
                callback_data=f"pkg:{package['id']}",
            )
        )
    kb.add(ibtn("⬅️ Back to Games", "primary", callback_data="topup:games"))
    text = (
        f"╭━━━〔 🎮 <b>{html.escape(game['name'])}</b> 〕━━━╮\n\n"
        f"💰 <b>Your Balance:</b> ৳{user_balance(user_id):g}\n"
        f"📦 <b>Available Packages:</b> {len(packages)}\n\n"
        + "\n\n".join(lines)
        + "\n\n╰━━━━━━━━━━━━━━━━━━╯\n"
        "👇 <i>নিচ থেকে Package বেছে নিন</i>"
    )
    return text, kb


def edit_or_send(call, text, kb):
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == "🎮 Tᴏᴘᴜᴘ")
def topup_handler(message):
    if not require_join(message):
        return
    view = games_view(message.from_user.id)
    if not view:
        bot.send_message(
            message.chat.id,
            "এখনো কোনো game/package যোগ করা হয়নি।",
            reply_markup=main_menu(message.from_user.id),
        )
        return
    bot.send_message(message.chat.id, view[0], reply_markup=view[1])


@bot.callback_query_handler(func=lambda c: c.data == "topup:games")
def back_to_games(call):
    view = games_view(call.from_user.id)
    bot.answer_callback_query(call.id)
    if view:
        edit_or_send(call, view[0], view[1])


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("game:"))
def choose_game(call):
    if not is_admin(call.from_user.id) and not is_joined(call.from_user.id):
        bot.answer_callback_query(call.id, "আগে channel join করুন।", show_alert=True)
        return
    try:
        game_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid game.")
        return
    view = packages_view(game_id, call.from_user.id)
    if not view:
        bot.answer_callback_query(call.id, "এই game-এর package নেই।", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    edit_or_send(call, view[0], view[1])


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pkg:"))
def choose_package(call):
    if not is_admin(call.from_user.id) and not is_joined(call.from_user.id):
        bot.answer_callback_query(call.id, "আগে channel join করুন।", show_alert=True)
        return
    try:
        package_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid package.")
        return
    with db() as con:
        row = con.execute(
            "SELECT p.*,g.name game_name FROM packages p "
            "JOIN games g ON g.id=p.game_id WHERE p.id=?",
            (package_id,),
        ).fetchone()
    if not row:
        bot.answer_callback_query(call.id, "Package পাওয়া যায়নি।", show_alert=True)
        return

    price = float(row["price"])
    balance = user_balance(call.from_user.id)
    text = (
        "╭━━━〔 📦 <b>Oʀᴅᴇʀ Dᴇᴛᴀɪʟs</b> 〕━━━╮\n\n"
        f"🎮 <b>Game:</b> {html.escape(row['game_name'])}\n"
        f"💎 <b>Package:</b> {html.escape(row['name'])}\n"
    )
    if row["description"]:
        text += f"📝 <b>Details:</b> {html.escape(row['description'])}\n"
    text += (
        f"💵 <b>Price:</b> ৳{price:g}\n"
        f"💰 <b>Your Balance:</b> ৳{balance:g}\n\n"
        "╰━━━━━━━━━━━━━━━━━━╯\n"
    )
    if balance < price:
        text += "⚠️ <i>আপনার balance কম। অর্ডারের আগে Deposit করুন।</i>\n\n"
    text += "🪪 <b>আপনার Player ID লিখুন:</b>"
    msg = bot.send_message(call.message.chat.id, text)
    bot.register_next_step_handler(msg, process_player_id, package_id)
    bot.answer_callback_query(call.id)


def process_player_id(message, package_id):
    if _is_cancel(message):  # /cancel, any /command, or a menu button
        bot.send_message(
            message.chat.id,
            "❌ Order বাতিল করা হয়েছে।",
            reply_markup=main_menu(message.from_user.id),
        )
        return
    player_id = message.text.strip()
    if not player_id or len(player_id) > 100:
        msg = bot.send_message(
            message.chat.id, "❌ সঠিক Player ID দিন (বাতিল: /cancel):"
        )
        bot.register_next_step_handler(msg, process_player_id, package_id)
        return

    order_id = None
    balance = None
    # "Check balance" and "deduct" are ONE conditional UPDATE, so two quick taps
    # can never spend the same money twice.
    with db() as con:
        row = con.execute(
            "SELECT p.*,g.name game_name FROM packages p "
            "JOIN games g ON g.id=p.game_id WHERE p.id=?",
            (package_id,),
        ).fetchone()
        if row:
            price = float(row["price"])
            cur = con.execute(
                "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                (price, message.from_user.id, price),
            )
            if cur.rowcount:
                cur = con.execute(
                    "INSERT INTO orders(user_id,package_id,game_name,package_name,price,player_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        message.from_user.id,
                        package_id,
                        row["game_name"],
                        row["name"],
                        price,
                        player_id,
                        now_iso(),
                    ),
                )
                order_id = cur.lastrowid
            else:
                u = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (message.from_user.id,),
                ).fetchone()
                balance = float(u["balance"]) if u else None

    if not row or (order_id is None and balance is None):
        bot.reply_to(message, "তথ্য পাওয়া যায়নি। /start দিন।")
        return
    price = float(row["price"])
    if order_id is None:
        bot.send_message(
            message.chat.id,
            f"❌ আপনার balance যথেষ্ট নয়।\n"
            f"Price: ৳{price:g}\n"
            f"Balance: ৳{balance:g}\n"
            "আগে Deposit করুন।",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    bot.send_message(
        message.chat.id,
        f"✅ <b>Order submitted!</b>\n"
        f"🆔 Order ID: <code>#{order_id}</code>\n"
        f"📦 Package: {html.escape(row['name'])}\n"
        f"🪪 Player ID: <code>{html.escape(player_id)}</code>\n"
        f"💰 Price: ৳{price:g}\n"
        "📌 Status: Pending",
        reply_markup=main_menu(message.from_user.id),
    )

    # Notify order channel with premium text and an inline bot button.
    channel = setting("order_channel", "").strip()
    if channel:
        try:
            order_text = (
                "╭━━━〔 🛒 <b>NEW TOPUP ORDER</b> 〕━━━╮\n\n"
                f"🆔 <b>Order ID:</b> <code>#{order_id}</code>\n"
                f"👤 <b>User ID:</b> <code>{message.from_user.id}</code>\n"
                f"🎮 <b>Game:</b> {html.escape(row['game_name'])}\n"
                f"📦 <b>Package:</b> {html.escape(row['name'])}\n"
                f"🪪 <b>Player ID:</b> <code>{html.escape(player_id)}</code>\n"
                f"💰 <b>Price:</b> ৳{price:g}\n\n"
                "╰━━━━━━━━━━━━━━━━━━╯\n"
                "⚡ <i>Order received successfully!</i>"
            )
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                ibtn(
                    "🤖 OPEN TOPUP BOT",
                    "primary",
                    url=f"https://t.me/{BOT_USERNAME}",
                )
            )
            bot.send_message(
                channel,
                order_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as exc:
            log.warning("Could not send order notification: %s", exc)


# ------------------------------------------------------------
# DEPOSIT: method -> pay -> Transaction ID -> Screenshot -> Admin verify
# ------------------------------------------------------------
DEPOSIT_METHODS = {
    "nagad": "Nagad",
    "usdt": "USDT (BEP20)",
}


@bot.message_handler(func=lambda m: m.text == "💳 Dᴇᴘᴏsɪᴛ")
def deposit_menu(message):
    if not require_join(message):
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(ibtn("📱 Nagad", "success", callback_data="deposit:nagad"))
    kb.add(ibtn("💵 USDT (BEP20)", "primary", callback_data="deposit:usdt"))
    bot.send_message(
        message.chat.id,
        "💰 <b>Deposit Method নির্বাচন করুন:</b>"
        + (f"\n\n💱 Dollar Rate: $1 = ৳{get_usd_rate():g}" if get_usd_rate() > 0 else ""),
        reply_markup=kb,
    )


def _clean_number(text):
    """'$ 1,200.50' / '৳500' -> '1200.50' / '500'. Also accepts Bangla digits."""
    t = (text or "").strip()
    t = t.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
    for ch in ("$", "৳", ",", " "):
        t = t.replace(ch, "")
    return t


def get_usd_rate():
    return get_float_setting("usd_rate", 0.0)


def get_min_deposit(method):
    default = 1.0 if method == "usdt" else 50.0
    return get_float_setting("min_deposit_" + method, default)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("deposit:"))
def choose_deposit_method(call):
    if not is_admin(call.from_user.id) and not is_joined(call.from_user.id):
        bot.answer_callback_query(call.id, "আগে channel join করুন।", show_alert=True)
        return
    method = call.data.split(":", 1)[1].lower()
    if method not in DEPOSIT_METHODS:
        bot.answer_callback_query(call.id, "Invalid method", show_alert=True)
        return
    info = setting("payment_" + method, "Not configured")
    if info == "Not configured":
        bot.answer_callback_query(
            call.id,
            "Admin এখনো payment setup করেনি।",
            show_alert=True,
        )
        return
    rate = get_usd_rate()
    if method == "usdt" and rate <= 0:
        bot.answer_callback_query(
            call.id,
            "Admin এখনো Dollar Rate সেট করেনি।",
            show_alert=True,
        )
        return

    minimum = get_min_deposit(method)
    if method == "nagad":
        text = (
            "📱 <b>Nagad Deposit</b>\n\n"
            "নিচের নাম্বারে <b>Send Money</b> করুন:\n"
            f"<code>{html.escape(info)}</code>\n\n"
            f"⬇️ <b>Minimum Deposit:</b> ৳{minimum:g}\n\n"
            "💰 আপনি কত <b>টাকা (৳)</b> পাঠাবেন বা পাঠিয়েছেন সেটা লিখুন:"
        )
    else:
        text = (
            "💵 <b>USDT (BEP20) Deposit</b>\n\n"
            "নিচের wallet address-এ পাঠান:\n"
            f"<code>{html.escape(info)}</code>\n\n"
            "⚠️ <b>শুধুমাত্র BEP20 (BSC) network</b> ব্যবহার করুন। ভুল network-এ পাঠালে টাকা হারিয়ে যাবে।\n\n"
            f"💱 <b>Dollar Rate:</b> $1 = ৳{rate:g}\n"
            f"⬇️ <b>Minimum Deposit:</b> ${minimum:g}\n\n"
            "💰 আপনি কত <b>ডলার ($)</b> পাঠাবেন বা পাঠিয়েছেন সেটা লিখুন:"
        )
    text += "\n\n<i>বাতিল করতে /cancel লিখুন।</i>"
    msg = bot.send_message(call.message.chat.id, text)
    bot.register_next_step_handler(msg, process_deposit_amount, method)
    bot.answer_callback_query(call.id)


def process_deposit_amount(message, method):
    if _is_cancel(message):
        bot.send_message(
            message.chat.id,
            "❌ Deposit বাতিল করা হয়েছে।",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    minimum = get_min_deposit(method)
    unit = "$" if method == "usdt" else "৳"
    try:
        value = parse_amount(_clean_number(message.text), maximum=1_000_000)
    except ValueError:
        msg = bot.send_message(
            message.chat.id,
            "❌ সঠিক amount লিখুন (যেমন: 10)। বাতিল: /cancel",
        )
        bot.register_next_step_handler(msg, process_deposit_amount, method)
        return
    if value < minimum:
        msg = bot.send_message(
            message.chat.id,
            f"❌ Minimum deposit {unit}{minimum:g}। এর বেশি amount লিখুন। বাতিল: /cancel",
        )
        bot.register_next_step_handler(msg, process_deposit_amount, method)
        return

    if method == "usdt":
        rate = get_usd_rate()
        if rate <= 0:
            bot.send_message(
                message.chat.id,
                "❌ Admin এখনো Dollar Rate সেট করেনি।",
                reply_markup=main_menu(message.from_user.id),
            )
            return
        usd, bdt = value, round(value * rate, 2)
        summary = f"💵 ${usd:g} × ৳{rate:g} = <b>৳{bdt:g}</b>"
    else:
        usd, rate, bdt = 0.0, 0.0, round(value, 2)
        summary = f"💰 Amount: <b>৳{bdt:g}</b>"

    if method == "usdt":
        ask_txt = "✅ এখন আপনার <b>Transaction ID (TxHash)</b> লিখে পাঠান:"
    else:
        ask_txt = "✅ এখন আপনার <b>Transaction ID</b> লিখে পাঠান:"
    msg = bot.send_message(
        message.chat.id,
        f"{summary}\n\nApprove হলে এই পরিমাণ আপনার balance-এ যোগ হবে।\n\n{ask_txt}"
        "\n\n<i>বাতিল করতে /cancel লিখুন।</i>",
    )
    bot.register_next_step_handler(msg, process_deposit_txid, method, usd, rate, bdt)


def process_deposit_txid(message, method, usd, rate, bdt):
    if _is_cancel(message):
        bot.send_message(
            message.chat.id,
            "❌ Deposit বাতিল করা হয়েছে।",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    txid = message.text.strip()
    if len(txid) < 4 or len(txid) > 120 or " " in txid:
        msg = bot.send_message(
            message.chat.id,
            "❌ সঠিক Transaction ID লিখুন (বাতিল: /cancel):",
        )
        bot.register_next_step_handler(msg, process_deposit_txid, method, usd, rate, bdt)
        return

    with db() as con:
        used = con.execute(
            "SELECT id FROM deposits WHERE lower(transaction_id)=lower(?) "
            "AND status IN ('pending','approved')",
            (txid,),
        ).fetchone()
    if used:
        bot.send_message(
            message.chat.id,
            "❌ এই Transaction ID আগেই ব্যবহার করা হয়েছে।",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    msg = bot.send_message(
        message.chat.id,
        "📸 এখন Payment-এর <b>Screenshot</b> পাঠান।\n\n<i>বাতিল করতে /cancel লিখুন।</i>",
    )
    bot.register_next_step_handler(
        msg, process_deposit_screenshot, method, txid, usd, rate, bdt
    )


def process_deposit_screenshot(message, method, txid, usd, rate, bdt):
    if message.content_type != "photo":
        if _is_cancel(message) and message.content_type == "text":
            bot.send_message(
                message.chat.id,
                "❌ Deposit বাতিল করা হয়েছে।",
                reply_markup=main_menu(message.from_user.id),
            )
            return
        msg = bot.send_message(
            message.chat.id,
            "❌ শুধু Screenshot (ছবি) পাঠান। বাতিল করতে /cancel লিখুন।",
        )
        bot.register_next_step_handler(
            msg, process_deposit_screenshot, method, txid, usd, rate, bdt
        )
        return

    file_id = message.photo[-1].file_id
    method_name = DEPOSIT_METHODS[method]
    auto = setting("auto_approve", "0") == "1"
    register_user(message.from_user)
    with db() as con:
        cur = con.execute(
            "INSERT INTO deposits(user_id,amount,method,transaction_id,screenshot,"
            "usd_amount,rate,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                message.from_user.id,
                bdt,
                method_name,
                txid,
                file_id,
                usd,
                rate,
                now_iso(),
            ),
        )
        dep_id = cur.lastrowid

    amount_line = (
        f"💵 ${usd:g} × ৳{rate:g} = ৳{bdt:g}" if method == "usdt" else f"💰 ৳{bdt:g}"
    )
    bot.send_message(
        message.chat.id,
        f"✅ <b>Deposit request submitted.</b>\n"
        f"🆔 Request ID: <code>#{dep_id}</code>\n"
        f"💳 Method: {method_name}\n"
        f"{amount_line}\n"
        f"🔢 Transaction ID: <code>{html.escape(txid)}</code>\n"
        + (
            "📌 Status: Auto-approving…"
            if auto
            else "📌 Status: Admin verification pending।\n\nVerify হলে আপনার balance-এ টাকা যোগ হবে।"
        ),
        reply_markup=main_menu(message.from_user.id),
    )

    user = message.from_user
    username = f"@{html.escape(user.username)}" if user.username else "—"
    kb = types.InlineKeyboardMarkup(row_width=2)
    if auto:
        footer = "⚡ <b>Auto-approve ON</b> — balance নিজে থেকে যোগ হয়েছে।"
        kb = None
    else:
        footer = "📸 Screenshot দেখে verify করুন।"
        kb.add(
            ibtn(f"✅ Approve ৳{bdt:g}", "success", callback_data=f"dep:ok:{dep_id}"),
            ibtn("❌ Reject", "danger", callback_data=f"dep:no:{dep_id}"),
        )
        kb.add(ibtn("✏️ Edit Amount", "primary", callback_data=f"dep:edit:{dep_id}"))

    if auto:
        do_approve_deposit(dep_id, bdt)

    try:
        bot.send_photo(
            ADMIN_ID,
            file_id,
            caption=(
                f"💰 <b>New Deposit #{dep_id}</b>\n\n"
                f"👤 <b>Name:</b> {html.escape(user.first_name or 'User')}\n"
                f"🔖 <b>Username:</b> {username}\n"
                f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                f"💳 <b>Method:</b> {method_name}\n"
                f"{amount_line}\n"
                f"🔢 <b>Transaction ID:</b> <code>{html.escape(txid)}</code>\n\n"
                f"{footer}"
            ),
            reply_markup=kb,
        )
    except Exception:
        log.exception("Could not notify admin about deposit")


def do_approve_deposit(dep_id, amount):
    """Atomically approves a pending deposit and credits the balance."""
    with db() as con:
        cur = con.execute(
            "UPDATE deposits SET status='approved', amount=? "
            "WHERE id=? AND status='pending'",
            (amount, dep_id),
        )
        if not cur.rowcount:
            return None
        dep = con.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
        con.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (amount, dep["user_id"]),
        )
    try:
        bot.send_message(
            dep["user_id"],
            f"✅ আপনার Deposit #{dep_id} approve হয়েছে।\n💰 ৳{amount:g} balance-এ যোগ হয়েছে।",
        )
    except Exception:
        pass
    return dep


def do_reject_deposit(dep_id):
    with db() as con:
        cur = con.execute(
            "UPDATE deposits SET status='rejected' WHERE id=? AND status='pending'",
            (dep_id,),
        )
        if not cur.rowcount:
            return None
        dep = con.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    try:
        bot.send_message(
            dep["user_id"],
            f"❌ আপনার Deposit #{dep_id} reject হয়েছে।\nসমস্যা থাকলে Support-এ যোগাযোগ করুন।",
        )
    except Exception:
        pass
    return dep


def _drop_buttons(chat_id, message_id):
    try:
        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("dep:"))
def deposit_admin_buttons(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Admin only.", show_alert=True)
        return
    try:
        _, action, dep_id = call.data.split(":")
        dep_id = int(dep_id)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid.")
        return

    with db() as con:
        dep = con.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    if not dep or dep["status"] != "pending":
        bot.answer_callback_query(call.id, "এই deposit আগেই process হয়েছে।", show_alert=True)
        _drop_buttons(call.message.chat.id, call.message.message_id)
        return

    if action == "no":
        do_reject_deposit(dep_id)
        bot.answer_callback_query(call.id, "❌ Rejected")
        _drop_buttons(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, f"❌ Deposit #{dep_id} rejected.")
    elif action == "ok" and float(dep["amount"] or 0) > 0:
        amount = float(dep["amount"])
        done = do_approve_deposit(dep_id, amount)
        if not done:
            bot.answer_callback_query(call.id, "এই deposit আগেই process হয়েছে।", show_alert=True)
        else:
            bot.answer_callback_query(call.id, f"✅ ৳{amount:g} যোগ হয়েছে")
            bot.send_message(
                call.message.chat.id,
                f"✅ Deposit #{dep_id} approved. User-এর balance-এ ৳{amount:g} যোগ হয়েছে।",
                reply_markup=admin_back_kb(),
            )
        _drop_buttons(call.message.chat.id, call.message.message_id)
    elif action in ("ok", "edit"):
        bot.answer_callback_query(call.id)
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        ask(
            call.message.chat.id,
            f"✅ <b>Deposit #{dep_id}</b>\n\nScreenshot দেখে user-এর balance-এ কত <b>টাকা (৳)</b> যোগ করবেন লিখুন:",
            process_deposit_approve_amount,
            dep_id,
            call.message.chat.id,
            call.message.message_id,
        )


def process_deposit_approve_amount(message, dep_id, src_chat=None, src_mid=None):
    text = admin_input(message)
    if text is None:
        return
    if text == "-":
        with db() as con:
            r = con.execute("SELECT amount FROM deposits WHERE id=?", (dep_id,)).fetchone()
        if r and float(r["amount"] or 0) > 0:
            text = str(r["amount"])
    try:
        amount = parse_amount(text)
    except ValueError:
        ask(
            message.chat.id,
            "❌ সঠিক amount লিখুন (যেমন: 100):",
            process_deposit_approve_amount,
            dep_id,
            src_chat,
            src_mid,
        )
        return
    dep = do_approve_deposit(dep_id, amount)
    if not dep:
        bot.send_message(message.chat.id, "❌ Pending deposit পাওয়া যায়নি (হয়তো আগেই process হয়েছে)।")
        return
    if src_chat and src_mid:
        _drop_buttons(src_chat, src_mid)
    bot.send_message(
        message.chat.id,
        f"✅ Deposit #{dep_id} approved. User-এর balance-এ ৳{amount:g} যোগ হয়েছে।",
        reply_markup=admin_back_kb(),
    )


def process_dep_approve_id(message):
    """Admin panel flow: ask the deposit ID, show the screenshot, ask the amount."""
    text = admin_input(message)
    if text is None:
        return
    if not text.isdigit():
        ask(message.chat.id, "❌ শুধু Deposit ID (সংখ্যা) লিখুন:", process_dep_approve_id)
        return
    dep_id = int(text)
    with db() as con:
        dep = con.execute(
            "SELECT * FROM deposits WHERE id=? AND status='pending'", (dep_id,)
        ).fetchone()
    if not dep:
        ask(message.chat.id, "❌ Pending deposit পাওয়া যায়নি। সঠিক ID লিখুন:", process_dep_approve_id)
        return
    if dep["screenshot"]:
        try:
            bot.send_photo(
                message.chat.id,
                dep["screenshot"],
                caption=(
                    f"💰 <b>Deposit #{dep_id}</b>\n"
                    f"🆔 User: <code>{dep['user_id']}</code>\n"
                    f"💳 {html.escape(dep['method'])}\n"
                    f"🔢 TX: <code>{html.escape(dep['transaction_id'])}</code>"
                ),
            )
        except Exception:
            pass
    ask(
        message.chat.id,
        f"✅ <b>Deposit #{dep_id}</b>\n"
        + (f"💰 User-এর দেওয়া amount: ৳{float(dep['amount']):g}\n(এটাই দিতে <code>-</code> লিখুন)\n"
           if float(dep["amount"] or 0) > 0 else "")
        + "\nকত <b>টাকা (৳)</b> balance-এ যোগ করবেন লিখুন:",
        process_deposit_approve_amount,
        dep_id,
    )


@bot.message_handler(func=lambda m: m.text == "🛒 Mʏ Oʀᴅᴇʀs")
def my_orders(message):
    if not require_join(message):
        return
    with db() as con:
        rows = con.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (message.from_user.id,),
        ).fetchall()
    if not rows:
        bot.send_message(
            message.chat.id,
            "📦 আপনার কোনো order নেই।",
            reply_markup=main_menu(message.from_user.id),
        )
        return
    lines = ["🛒 <b>Mʏ Oʀᴅᴇʀs</b>"]
    for row in rows:
        lines.append(
            f"\n🆔 Order #{row['id']} — {html.escape(row['package_name'])}\n"
            f"💰 Price: ৳{float(row['price']):g} | "
            f"📌 Status: {html.escape(row['status'])}\n"
            f"🪪 Player ID: <code>{html.escape(row['player_id'])}</code>"
        )
    bot.send_message(message.chat.id, "\n".join(lines), reply_markup=main_menu(message.from_user.id))


# ------------------------------------------------------------
# SUPPORT SYSTEM: user -> admin message, admin -> user reply
# ------------------------------------------------------------
MENU_LABELS = {
    "👤 Pʀᴏғɪʟᴇ", "💳 Dᴇᴘᴏsɪᴛ", "🎮 Tᴏᴘᴜᴘ", "🛒 Mʏ Oʀᴅᴇʀs",
    "🎁 Rᴇғᴇʀ", "🎟 Gɪғᴛ Cᴏᴅᴇ", "🎫 Sᴜᴘᴘᴏʀᴛ", "🛠 Aᴅᴍɪɴ Pᴀɴᴇʟ",
}


def _is_cancel(message):
    """True if the person sent a command / menu button / non-text instead of a message."""
    return (
        not message.text
        or message.text.startswith("/")
        or message.text in MENU_LABELS
    )


def ask_support_message(chat_id):
    msg = bot.send_message(
        chat_id,
        "╭━━━〔 🎫 <b>Sᴜᴘᴘᴏʀᴛ Cᴇɴᴛᴇʀ</b> 〕━━━╮\n\n"
        "✍️ আপনার সমস্যা বা মেসেজ লিখে পাঠান।\n"
        "Admin দেখে আপনাকে এখানেই reply দেবেন।\n\n"
        "বাতিল করতে /cancel লিখুন।\n"
        "╰━━━━━━━━━━━━━━━━━━╯",
    )
    bot.register_next_step_handler(msg, process_support_message)


@bot.message_handler(func=lambda m: m.text == "🎫 Sᴜᴘᴘᴏʀᴛ")
def connect_handler(message):
    if not require_join(message):
        return
    ask_support_message(message.chat.id)


def process_support_message(message):
    if _is_cancel(message):
        bot.send_message(
            message.chat.id,
            "❌ Support মেসেজ বাতিল করা হয়েছে।",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    text = message.text.strip()
    if len(text) > 1000:
        msg = bot.send_message(
            message.chat.id,
            "⚠️ মেসেজ অনেক বড়। ১০০০ অক্ষরের মধ্যে আবার লিখুন (বাতিল: /cancel):",
        )
        bot.register_next_step_handler(msg, process_support_message)
        return

    register_user(message.from_user)
    with db() as con:
        cur = con.execute(
            "INSERT INTO support_tickets(user_id,message,created_at) VALUES(?,?,?)",
            (message.from_user.id, text, now_iso()),
        )
        ticket_id = cur.lastrowid

    bot.send_message(
        message.chat.id,
        f"✅ <b>আপনার মেসেজ Admin-এর কাছে পাঠানো হয়েছে।</b>\n"
        f"🎫 Ticket: <code>#{ticket_id}</code>\n"
        "⏳ অনুগ্রহ করে reply-এর জন্য অপেক্ষা করুন।",
        reply_markup=main_menu(message.from_user.id),
    )

    user = message.from_user
    username = f"@{html.escape(user.username)}" if user.username else "—"
    kb = types.InlineKeyboardMarkup()
    kb.add(ibtn("↩️ Reply", "success", callback_data=f"sup:reply:{ticket_id}"))
    try:
        bot.send_message(
            ADMIN_ID,
            f"🎫 <b>New Support Ticket #{ticket_id}</b>\n\n"
            f"👤 <b>Name:</b> {html.escape(user.first_name or 'User')}\n"
            f"🔖 <b>Username:</b> {username}\n"
            f"🆔 <b>User ID:</b> <code>{user.id}</code>\n\n"
            f"💬 <b>Message:</b>\n{html.escape(text)}",
            reply_markup=kb,
        )
    except Exception:
        log.exception("Could not notify admin about support ticket")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("sup:reply:"))
def admin_reply_button(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Admin only.", show_alert=True)
        return
    try:
        ticket_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid ticket.")
        return
    with db() as con:
        ticket = con.execute(
            "SELECT * FROM support_tickets WHERE id=?", (ticket_id,)
        ).fetchone()
    if not ticket:
        bot.answer_callback_query(call.id, "Ticket পাওয়া যায়নি।", show_alert=True)
        return
    msg = bot.send_message(
        call.message.chat.id,
        f"✍️ <b>Ticket #{ticket_id}</b> — User <code>{ticket['user_id']}</code>-কে reply লিখুন:\n\n"
        f"💬 <i>{html.escape(ticket['message'])}</i>\n\n"
        "বাতিল করতে /cancel লিখুন।",
    )
    bot.register_next_step_handler(msg, process_admin_reply, ticket_id)
    bot.answer_callback_query(call.id)


def process_admin_reply(message, ticket_id):
    if not is_admin(message.from_user.id):
        return
    if _is_cancel(message):
        bot.send_message(message.chat.id, "❌ Reply বাতিল করা হয়েছে।")
        return

    reply_text = message.text.strip()
    with db() as con:
        ticket = con.execute(
            "SELECT * FROM support_tickets WHERE id=?", (ticket_id,)
        ).fetchone()
    if not ticket:
        bot.reply_to(message, "Ticket পাওয়া যায়নি।")
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(ibtn("💬 Reply to Support", "primary", callback_data=f"sup:user:{ticket_id}"))
    try:
        bot.send_message(
            ticket["user_id"],
            f"🎫 <b>Support Reply</b> (Ticket <code>#{ticket_id}</code>)\n\n"
            f"{html.escape(reply_text)}",
            reply_markup=kb,
        )
    except Exception as exc:
        log.warning("Could not send support reply: %s", exc)
        bot.reply_to(
            message,
            "❌ Reply পাঠানো যায়নি। User হয়তো bot block করেছে।",
        )
        return

    with db() as con:
        con.execute(
            "UPDATE support_tickets SET reply=?, status='answered' WHERE id=?",
            (reply_text, ticket_id),
        )
    bot.reply_to(message, f"✅ Ticket #{ticket_id}-এর reply user-কে পাঠানো হয়েছে।")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("sup:user:"))
def user_reply_button(call):
    bot.answer_callback_query(call.id)
    ask_support_message(call.message.chat.id)


# ------------------------------------------------------------
# GIFT CODE SYSTEM
# ------------------------------------------------------------
class _GiftError(Exception):
    pass


_gift_fails = {}


def _gift_blocked(user_id):
    now = time.time()
    recent = [t for t in _gift_fails.get(user_id, []) if now - t < 600]
    _gift_fails[user_id] = recent
    return len(recent) >= 5


def redeem_gift(user_id, code):
    """Returns (info dict, None) on success or (None, reason)."""
    try:
        with db() as con:
            row = con.execute("SELECT * FROM gift_codes WHERE code=?", (code,)).fetchone()
            if not row:
                raise _GiftError("invalid")
            if not row["active"]:
                raise _GiftError("disabled")
            try:
                con.execute(
                    "INSERT INTO gift_redeems(code,user_id,created_at) VALUES(?,?,?)",
                    (code, user_id, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise _GiftError("used")
            cur = con.execute(
                "UPDATE gift_codes SET used_count=used_count+1 "
                "WHERE code=? AND active=1 AND used_count<max_uses",
                (code,),
            )
            if not cur.rowcount:
                raise _GiftError("finished")
            con.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (row["amount"], user_id),
            )
            after = con.execute(
                "SELECT used_count,max_uses FROM gift_codes WHERE code=?", (code,)
            ).fetchone()
        info = {
            "amount": float(row["amount"]),
            "used": after["used_count"],
            "max": after["max_uses"],
        }
        return info, None
    except _GiftError as exc:
        return None, str(exc)


@bot.message_handler(func=lambda m: m.text == "🎟 Gɪғᴛ Cᴏᴅᴇ")
def gift_handler(message):
    if not require_join(message):
        return
    register_user(message.from_user)
    msg = bot.send_message(
        message.chat.id,
        "╭━━━〔 🎟 <b>Gɪғᴛ Cᴏᴅᴇ</b> 〕━━━╮\n\n"
        "🎁 আপনার Gift Code লিখে পাঠান।\n"
        "Code সঠিক হলে balance-এ টাকা যোগ হবে।\n\n"
        "╰━━━━━━━━━━━━━━━━━━╯\n"
        "<i>বাতিল করতে /cancel লিখুন।</i>",
    )
    bot.register_next_step_handler(msg, process_gift_code)


def process_gift_code(message):
    uid = message.from_user.id
    if _is_cancel(message):
        bot.send_message(
            message.chat.id,
            "❌ বাতিল করা হয়েছে।",
            reply_markup=main_menu(uid),
        )
        return
    if _gift_blocked(uid):
        bot.send_message(
            message.chat.id,
            "⛔ অনেকবার ভুল code দিয়েছেন। ১০ মিনিট পরে আবার চেষ্টা করুন।",
            reply_markup=main_menu(uid),
        )
        return

    code = message.text.strip().upper()
    info, error = redeem_gift(uid, code)
    if error:
        if error == "invalid":  # only wrong guesses count towards the lock
            _gift_fails.setdefault(uid, []).append(time.time())
        reasons = {
            "invalid": "❌ <b>Code সঠিক নয়।</b>",
            "disabled": "🚫 <b>এই Code বন্ধ করা হয়েছে।</b>",
            "used": "⚠️ <b>আপনি এই Code আগেই ব্যবহার করেছেন।</b>",
            "finished": "🔒 <b>এই Code-এর ব্যবহারের সীমা শেষ।</b>",
        }
        bot.send_message(
            message.chat.id,
            reasons.get(error, "❌ Code ব্যবহার করা যায়নি।"),
            reply_markup=main_menu(uid),
        )
        return

    bot.send_message(
        message.chat.id,
        "╭━━━〔 🎉 <b>Gɪғᴛ Rᴇᴅᴇᴇᴍᴇᴅ</b> 〕━━━╮\n\n"
        f"🎟 <b>Code:</b> <code>{html.escape(code)}</code>\n"
        f"💰 <b>Added:</b> ৳{info['amount']:g}\n"
        f"💳 <b>New Balance:</b> ৳{user_balance(uid):g}\n\n"
        "╰━━━━━━━━━━━━━━━━━━╯",
        reply_markup=main_menu(uid),
    )
    post_gift_notice(message.from_user, code, info)


def mask_id(user_id):
    t = str(user_id)
    return t if len(t) <= 6 else t[:3] + "•" * (len(t) - 5) + t[-2:]


def gift_target():
    return setting("gift_channel", "").strip() or setting("order_channel", "").strip()


def _gift_bar(used, total):
    n = 10
    filled = min(n, round(used * n / total)) if total else 0
    return "▰" * filled + "▱" * (n - filled)


def build_gift_post(r):
    """Text + keyboard of the live gift-code post."""
    used, total = r["used_count"], r["max_uses"]
    left = max(total - used, 0)
    code = html.escape(r["code"])
    bar = _gift_bar(used, total)
    kb = types.InlineKeyboardMarkup()
    kb.add(ibtn("🤖 OPEN TOPUP BOT", "primary", url=f"https://t.me/{BOT_USERNAME}"))

    if not r["active"]:
        text = (
            "╭━━━〔 🎟 <b>GIFT CODE — CLOSED</b> 〕━━━╮\n\n"
            f"🔑 <b>Code:</b> <s>{code}</s>\n"
            f"💰 <b>Reward:</b> ৳{float(r['amount']):g} (প্রতি user)\n"
            f"✅ <b>Claimed:</b> {used}/{total}  {bar}\n\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n"
            "🚫 <i>এই Gift Code বন্ধ করা হয়েছে।</i>"
        )
    elif left == 0:
        text = (
            "╭━━━〔 🎟 <b>GIFT CODE — FINISHED</b> 〕━━━╮\n\n"
            f"🔑 <b>Code:</b> <s>{code}</s>\n"
            f"💰 <b>Reward:</b> ৳{float(r['amount']):g} (প্রতি user)\n"
            f"✅ <b>Claimed:</b> {used}/{total}  {bar}\n"
        )
        if r["last_user"]:
            text += f"👤 <b>Last Claimed:</b> {html.escape(r['last_user'])}\n"
        text += (
            "\n╰━━━━━━━━━━━━━━━━━━╯\n"
            "🔒 <i>সব ব্যবহার শেষ। পরের Gift Code-এর জন্য চোখ রাখুন!</i>"
        )
    else:
        text = (
            "╭━━━〔 🎟 <b>GIFT CODE</b> 〕━━━╮\n\n"
            f"🔑 <b>Code:</b> <code>{code}</code>\n"
            f"💰 <b>Reward:</b> ৳{float(r['amount']):g} (প্রতি user)\n"
            f"👥 <b>Total Limit:</b> {total} user\n"
            f"✅ <b>Claimed:</b> {used}/{total}  {bar}\n"
            f"🔥 <b>Left:</b> {left} user\n"
        )
        if r["last_user"]:
            text += f"\n👤 <b>Last Claimed:</b> {html.escape(r['last_user'])}\n"
        text += (
            "\n╰━━━━━━━━━━━━━━━━━━╯\n"
            "⚡ <i>Bot-এ গিয়ে 🎟 Gift Code বাটনে চেপে এখনই Redeem করুন!</i>"
        )
    return text, kb


def sync_gift_post(code, last_user=None):
    """Create the live post for a code, or edit it with the latest numbers."""
    try:
        target = gift_target()
        if not target:
            return
        with db() as con:
            if last_user:
                con.execute(
                    "UPDATE gift_codes SET last_user=? WHERE code=?", (last_user, code)
                )
            r = con.execute("SELECT * FROM gift_codes WHERE code=?", (code,)).fetchone()
        if not r:
            return
        text, kb = build_gift_post(r)
        if r["channel_msg_id"] and r["channel_chat"] == target:
            try:
                bot.edit_message_text(
                    text,
                    target,
                    r["channel_msg_id"],
                    reply_markup=kb,
                    parse_mode="HTML",
                )
                return
            except Exception as exc:
                if "not modified" in str(exc).lower():
                    return
                log.warning("Gift post edit failed, sending a new one: %s", exc)
        msg = bot.send_message(target, text, reply_markup=kb)
        with db() as con:
            con.execute(
                "UPDATE gift_codes SET channel_chat=?, channel_msg_id=? WHERE code=?",
                (target, msg.message_id, code),
            )
    except Exception as exc:
        log.warning("Could not update gift post: %s", exc)


def post_gift_notice(user, code, info=None):
    """Called after a successful redeem: refresh the live post in the channel."""
    name = f"{user.first_name or 'User'} ({mask_id(user.id)})"
    threading.Thread(target=sync_gift_post, args=(code, name), daemon=True).start()


def process_gift_channel(message):
    text = admin_input(message)
    if text is None:
        return
    if text == "-":
        set_setting("gift_channel", "")
        bot.send_message(
            message.chat.id,
            "✅ Gift channel মুছে ফেলা হয়েছে। এখন Order channel-এ পোস্ট হবে।",
            reply_markup=admin_back_kb(),
        )
        return
    channel = parse_channel(text, allow_id=True)
    if not channel:
        ask(
            message.chat.id,
            "❌ সঠিক channel link বা @username দিন।\nযেমন: <code>@mychannel</code>",
            process_gift_channel,
        )
        return
    error = check_channel(channel)
    if error:
        ask(message.chat.id, error + "\nঅন্য channel দিন:", process_gift_channel)
        return
    set_setting("gift_channel", channel)
    bot.send_message(
        message.chat.id,
        f"✅ Gift notification channel set: {html.escape(channel)}",
        reply_markup=admin_back_kb(),
    )


# --- admin side ---
def process_gc_amount(message):
    text = admin_input(message)
    if text is None:
        return
    try:
        amount = parse_amount(text, maximum=1_000_000)
    except ValueError:
        ask(message.chat.id, "❌ সঠিক amount লিখুন (যেমন: 50):", process_gc_amount)
        return
    ask(
        message.chat.id,
        "👥 কতজন user এই code ব্যবহার করতে পারবে? (যেমন: 10)",
        process_gc_uses,
        amount,
    )


def process_gc_uses(message, amount):
    text = admin_input(message)
    if text is None:
        return
    if not text.isdigit() or not (1 <= int(text) <= 1000000):
        ask(message.chat.id, "❌ সঠিক সংখ্যা লিখুন (যেমন: 10):", process_gc_uses, amount)
        return
    ask(
        message.chat.id,
        "🔤 Code লিখুন (শুধু A-Z, 0-9, _ ও -, ৩–৩২ অক্ষর)।\nঅটো তৈরি করতে <code>-</code> লিখুন:",
        process_gc_code,
        amount,
        int(text),
    )


def process_gc_code(message, amount, uses):
    text = admin_input(message)
    if text is None:
        return
    if text == "-":
        alphabet = string.ascii_uppercase + string.digits
        code = "GIFT" + "".join(secrets.choice(alphabet) for _ in range(8))
    else:
        code = text.upper()
        if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
            ask(
                message.chat.id,
                "❌ Code শুধু A-Z, 0-9, _ ও - দিয়ে (৩–৩২ অক্ষর) হতে পারবে। আবার লিখুন:",
                process_gc_code,
                amount,
                uses,
            )
            return
    try:
        with db() as con:
            con.execute(
                "INSERT INTO gift_codes(code,amount,max_uses,created_at) VALUES(?,?,?,?)",
                (code, amount, uses, now_iso()),
            )
    except sqlite3.IntegrityError:
        ask(
            message.chat.id,
            "❌ এই Code আগে থেকেই আছে। অন্য Code লিখুন (বা <code>-</code>):",
            process_gc_code,
            amount,
            uses,
        )
        return
    bot.send_message(
        message.chat.id,
        "╭━━━〔 🎟 <b>Gɪғᴛ Cᴏᴅᴇ Cʀᴇᴀᴛᴇᴅ</b> 〕━━━╮\n\n"
        f"🔑 <b>Code:</b> <code>{code}</code>\n"
        f"💰 <b>Amount:</b> ৳{amount:g}\n"
        f"👥 <b>Limit:</b> {uses} user\n\n"
        "╰━━━━━━━━━━━━━━━━━━╯\n"
        + (
            "📣 Channel-এ live post করা হচ্ছে। কেউ Redeem করলে সংখ্যা নিজে থেকে আপডেট হবে।"
            if gift_target()
            else "ℹ️ Gift/Order channel সেট করা নেই, তাই channel-এ post হয়নি।"
        ),
        reply_markup=admin_back_kb(),
    )
    threading.Thread(target=sync_gift_post, args=(code,), daemon=True).start()


def gift_codes_text():
    with db() as con:
        rows = con.execute(
            "SELECT * FROM gift_codes ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    if not rows:
        return "🎟 কোনো Gift Code নেই।"
    lines = ["🎟 <b>Gift Codes</b> (সর্বশেষ ২০টি)\n"]
    for r in rows:
        if not r["active"]:
            status = "🚫 Disabled"
        elif r["used_count"] >= r["max_uses"]:
            status = "🔒 Finished"
        else:
            status = "✅ Active"
        lines.append(
            f"<code>{html.escape(r['code'])}</code> | ৳{float(r['amount']):g} | "
            f"{r['used_count']}/{r['max_uses']} | {status}"
        )
    return "\n".join(lines)


def process_gc_disable(message):
    text = admin_input(message)
    if text is None:
        return
    with db() as con:
        cur = con.execute(
            "UPDATE gift_codes SET active=0 WHERE code=?", (text.upper(),)
        )
    bot.send_message(
        message.chat.id,
        "🚫 Gift Code বন্ধ করা হয়েছে।" if cur.rowcount else "❌ এই Code পাওয়া যায়নি।",
        reply_markup=admin_back_kb(),
    )
    if cur.rowcount:
        threading.Thread(target=sync_gift_post, args=(text.upper(),), daemon=True).start()


@bot.message_handler(commands=["cancel"])
def cancel_command(message):
    bot.clear_step_handler_by_chat_id(message.chat.id)
    bot.send_message(
        message.chat.id,
        "✅ বাতিল করা হয়েছে।",
        reply_markup=main_menu(message.from_user.id),
    )


@bot.message_handler(func=lambda m: m.text == "🛠 Aᴅᴍɪɴ Pᴀɴᴇʟ")
def admin_button_handler(message):
    # Non-admins never see this button, but check anyway in case the text is typed manually.
    if not is_admin(message.from_user.id):
        return
    admin_panel(message.chat.id)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    if message.text and message.text.startswith("/"):
        return
    bot.send_message(
        message.chat.id,
        "মেনু থেকে একটি option নির্বাচন করুন।",
        reply_markup=main_menu(message.from_user.id),
    )


if __name__ == "__main__":
    init_db()
    try:
        _me = bot.get_me()
    except Exception as exc:
        raise SystemExit(f"Token ভুল অথবা network সমস্যা: {exc}")
    if _me.id == ADMIN_ID:
        raise SystemExit("ADMIN_ID হলো bot-এর নিজের ID! আপনার নিজের Telegram user ID দিন।")
    BOT_USERNAME = _me.username or BOT_USERNAME
    log.info("TopUp bot started as @%s", BOT_USERNAME)
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)