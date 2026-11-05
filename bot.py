#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SABBIR XYZ FILE STORE BOT
Single-file Telegram bot for GitHub + Render Web Service.

Required environment variables:
  BOT_TOKEN  - Telegram bot token
  ADMIN_ID   - numeric Telegram user ID of the owner/admin

Optional:
  PORT       - Render web-service port (default 10000)
  DB_PATH    - SQLite path (default ./data/bot.sqlite3)

Render:
  Build command: pip install --upgrade python-telegram-bot==22.8
  Start command: python bot.py

IMPORTANT:
- The admin must open the bot and press /start before the first upload.
- Uploaded messages are copied into the admin's private chat as the storage area.
- On Render, attach a persistent disk and set DB_PATH to a file on that disk.
- The bot must be an administrator in every channel it verifies.
"""

import os
import re
import time
import html
import sqlite3
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError, Forbidden, BadRequest
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# --------------------------- CONFIGURATION ---------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = Path(os.getenv("DB_PATH", "./data/bot.sqlite3"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.")
ADMIN_ID = int(ADMIN_ID_RAW)

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("file-store")

# Conversation states kept in memory; persistent business data is in SQLite.
STATES = {}
PENDING_DELIVERY = {}
BOT_USERNAME = ""
BOT_ACTIVE = True

# --------------------------- DATABASE -------------------------------

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT, first_name TEXT, joined_at INTEGER,
            last_seen INTEGER, banned INTEGER DEFAULT 0, ban_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS files(
            file_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL, stored_message_id INTEGER NOT NULL,
            title TEXT, created_at INTEGER, downloads INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            title TEXT, username TEXT, invite_link TEXT,
            is_private INTEGER DEFAULT 0, created_at INTEGER,
            UNIQUE(owner_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER,
            action TEXT, target TEXT, created_at INTEGER
        );
        """)
        defaults = {
            "welcome": (
                "✨ <b>WELCOME TO PREMIUM FILE STORE</b>\n\n"
                "📁 Store and share your Telegram content\n"
                "🔗 Generate unique deep links\n"
                "🔒 Protect files with channel membership\n"
                "⚡ Fast and simple file delivery\n\n"
                "Choose an option from the menu below."
            ),
            "update_url": "",
            "support_url": "",
            "owner_url": "https://t.me/",
            "admin_contact_url": "https://t.me/",
            "maintenance": "0",
            "maintenance_reason": "The bot is temporarily under maintenance.",
            "tutorial": (
                "<b>HOW TO USE</b>\n\n"
                "1. Open <b>ADD CHANNELS</b>.\n"
                "2. Add the bot as an administrator to your channel.\n"
                "3. Forward a post from that channel to the bot.\n"
                "4. Open <b>UPLOAD FILE</b> and send or forward your content.\n"
                "5. Copy the generated deep link and share it.\n"
                "6. Visitors join the required channels and press JOINED DONE.\n"
                "7. The bot verifies membership and delivers the content.\n\n"
                "Use <b>MY FILE</b> to manage your uploads."
            ),
        }
        for key, value in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (key, value)
            )


def setting(key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with db() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )


def log_admin(action, target=""):
    with db() as con:
        con.execute(
            "INSERT INTO admin_logs(admin_id,action,target,created_at) VALUES(?,?,?,?)",
            (ADMIN_ID, action, str(target), int(time.time()))
        )


def touch_user(user):
    now = int(time.time())
    with db() as con:
        con.execute("""
            INSERT INTO users(user_id,username,first_name,joined_at,last_seen)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_seen=excluded.last_seen
        """, (user.id, user.username or "", user.first_name or "", now, now))


def user_row(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def is_banned(user_id):
    row = user_row(user_id)
    return bool(row and row["banned"])


def user_channels(owner_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM channels WHERE owner_id=? ORDER BY id", (owner_id,)
        ).fetchall()


def file_row(file_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM files WHERE file_id=? AND active=1", (file_id,)
        ).fetchone()


# --------------------------- KEYBOARDS ------------------------------

def green(text):
    return KeyboardButton(text=text, style="success")


def red(text):
    return KeyboardButton(text=text, style="danger")


def main_keyboard():
    return ReplyKeyboardMarkup([
        [green("UPLOAD FILE")],
        [green("PROFILE"), green("ADD CHANNELS")],
        [green("HOW TO USE"), green("MY FILE")],
        [green("UPDATE CHANNEL")],
        [green("SUPPORT"), green("BOT OWNER")],
    ], resize_keyboard=True, is_persistent=True)


def channel_keyboard():
    return ReplyKeyboardMarkup([
        [green("➕ ADD CHANNEL"), red("DELETE CHANNELS")],
        [green("CHANNEL LIST"), red("BACK")],
    ], resize_keyboard=True, is_persistent=True)


def admin_keyboard():
    return ReplyKeyboardMarkup([
        [green("📊 DASHBOARD"), green("👥 ALL USERS")],
        [green("🚫 BAN USER"), green("✅ UNBAN USER")],
        [green("📁 ALL FILES"), green("📢 BROADCAST")],
        [green("📡 ALL CHANNELS"), green("⚙️ BOT SETTINGS")],
        [green("🟢 BOT ON"), red("🔴 BOT OFF")],
        [red("⬅️ EXIT ADMIN")],
    ], resize_keyboard=True, is_persistent=True)


def inline_url_or_text(label, url):
    if url and re.match(r"^https?://", url):
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)]])
    return None


# --------------------------- COMMON HELPERS -------------------------

async def send_home(update, text=None):
    await update.effective_message.reply_text(
        text or setting("welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
        disable_web_page_preview=True
    )


async def maintenance_notice(update):
    reason = setting("maintenance_reason", "The bot is temporarily under maintenance.")
    await update.effective_message.reply_text(
        "🛠 <b>BOT UNDER MAINTENANCE</b>\n\n"
        + html.escape(reason)
        + "\n\nPlease try again later.",
        parse_mode=ParseMode.HTML
    )


async def public_access_allowed(update):
    if update.effective_user and is_banned(update.effective_user.id):
        row = user_row(update.effective_user.id)
        reason = (row["ban_reason"] if row else "") or "No reason was provided."
        markup = inline_url_or_text("CONTACT ADMIN", setting("admin_contact_url"))
        await update.effective_message.reply_text(
            "⛔ <b>ACCESS RESTRICTED</b>\n\nReason: "
            + html.escape(reason),
            parse_mode=ParseMode.HTML,
            reply_markup=markup
        )
        return False
    if setting("maintenance", "0") == "1" and update.effective_user.id != ADMIN_ID:
        await maintenance_notice(update)
        return False
    return True


def get_message_title(message):
    if message.text:
        return message.text[:70].replace("\n", " ")
    if message.caption:
        return message.caption[:70].replace("\n", " ")
    if message.document:
        return message.document.file_name or "Document"
    if message.photo:
        return "Photo"
    if message.video:
        return message.video.file_name or "Video"
    if message.audio:
        return message.audio.file_name or "Audio"
    if message.voice:
        return "Voice message"
    if message.animation:
        return "Animation"
    if message.sticker:
        return "Sticker"
    return "Telegram message"


async def make_deep_link(context, file_id):
    me = await context.bot.get_me()
    return f"https://t.me/{me.username}?start=f_{file_id}"


async def copy_file_to_user(context, user_id, row):
    await context.bot.copy_message(
        chat_id=user_id,
        from_chat_id=ADMIN_ID,
        message_id=row["stored_message_id"]
    )
    with db() as con:
        con.execute(
            "UPDATE files SET downloads=downloads+1 WHERE file_id=?",
            (row["file_id"],)
        )


async def get_channel_join_url(context, channel):
    if channel["username"]:
        return "https://t.me/" + channel["username"].lstrip("@")
    if channel["invite_link"]:
        return channel["invite_link"]
    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=channel["chat_id"],
            name="File Store Join Request",
            creates_join_request=True
        )
        with db() as con:
            con.execute(
                "UPDATE channels SET invite_link=? WHERE id=?",
                (invite.invite_link, channel["id"])
            )
        return invite.invite_link
    except TelegramError:
        return None


async def check_user_in_channel(context, user_id, channel_id):
    try:
        member = await context.bot.get_chat_member(channel_id, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        ) or (member.status == ChatMemberStatus.RESTRICTED and member.is_member)
    except TelegramError:
        return False


async def show_file_gate(message, context, row):
    channels = user_channels(row["owner_id"])
    missing = []
    for ch in channels:
        if not await check_user_in_channel(context, message.from_user.id, ch["chat_id"]):
            missing.append(ch)

    if not missing:
        await copy_file_to_user(context, message.from_user.id, row)
        await message.reply_text(
            "✅ <b>FILE DELIVERED SUCCESSFULLY</b>\n\nThank you for using Premium File Store.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )
        return

    buttons = []
    for index, ch in enumerate(missing, start=1):
        url = await get_channel_join_url(context, ch)
        if url:
            buttons.append([InlineKeyboardButton(f"JOIN {index} CHANNEL", url=url)])
    buttons.append([InlineKeyboardButton("JOINED DONE. ✅", callback_data=f"joined:{row['file_id']}")])
    PENDING_DELIVERY[message.from_user.id] = row["file_id"]
    await message.reply_text(
        "🔐 <b>CHANNEL MEMBERSHIP REQUIRED</b>\n\n"
        "Join all required channels using the buttons below.\n"
        "If a private channel requires approval, wait until your request is accepted.\n\n"
        "Then press <b>JOINED DONE. ✅</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# --------------------------- USER COMMANDS ---------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user)
    if not await public_access_allowed(update):
        return

    if context.args and context.args[0].startswith("f_"):
        raw_id = context.args[0][2:]
        if raw_id.isdigit():
            row = file_row(int(raw_id))
            if not row:
                await update.effective_message.reply_text(
                    "❌ This file link is invalid or the file is no longer available.",
                    reply_markup=main_keyboard()
                )
                return
            await show_file_gate(update.effective_message, context, row)
            return
    await send_home(update)


async def profile(update, context):
    user = update.effective_user
    touch_user(user)
    with db() as con:
        files_count = con.execute(
            "SELECT COUNT(*) n FROM files WHERE owner_id=? AND active=1", (user.id,)
        ).fetchone()["n"]
        channels_count = con.execute(
            "SELECT COUNT(*) n FROM channels WHERE owner_id=?", (user.id,)
        ).fetchone()["n"]
    username = "@" + user.username if user.username else "Not set"
    joined = user_row(user.id)["joined_at"]
    text = (
        "👤 <b>𝐏ʀᴏғɪʟᴇ 𝐈ɴғᴏʀᴍᴀᴛɪᴏɴ</b>\n\n"
        "👤 <b>𝐅ɪʀsᴛ 𝐍ᴀᴍᴇ</b>\n"
        f"├─ {html.escape(user.first_name or 'Not set')}\n\n"
        "🔗 <b>𝐔sᴇʀɴᴀᴍᴇ</b>\n"
        f"├─ {html.escape(username)}\n\n"
        "🆔 <b>𝐔sᴇʀ 𝐈ᴅ</b>\n"
        f"├─ <code>{user.id}</code>\n\n"
        f"📁 <b>Uploaded Files:</b> {files_count}\n"
        f"📡 <b>Added Channels:</b> {channels_count}\n"
        f"📅 <b>Joined:</b> {time.strftime('%Y-%m-%d', time.localtime(joined))}"
    )
    photos = await context.bot.get_user_profile_photos(user.id, limit=1)
    if photos.total_count:
        await update.effective_message.reply_photo(
            photos.photos[0][-1].file_id, caption=text,
            parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
        )


async def my_files(update, context):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM files WHERE owner_id=? AND active=1 ORDER BY file_id DESC LIMIT 30",
            (update.effective_user.id,)
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text(
            "📁 <b>MY FILES</b>\n\nYou have not uploaded any files yet.",
            parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
        )
        return
    for row in rows:
        link = await make_deep_link(context, row["file_id"])
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("OPEN DEEP LINK", url=link),
            InlineKeyboardButton("DELETE", callback_data=f"delfile:{row['file_id']}")
        ]])
        await update.effective_message.reply_text(
            f"📄 <b>{html.escape(row['title'] or 'Telegram content')}</b>\n"
            f"🆔 File ID: <code>{row['file_id']}</code>\n"
            f"📥 Downloads: {row['downloads']}\n"
            f"🔗 <code>{html.escape(link)}</code>",
            parse_mode=ParseMode.HTML, reply_markup=markup
        )


async def add_channel_instructions(update, context):
    STATES[update.effective_user.id] = "add_channel"
    await update.effective_message.reply_text(
        "📡 <b>ADD A CHANNEL</b>\n\n"
        "1. Add this bot to your Telegram channel.\n"
        "2. Promote the bot to administrator.\n"
        "3. Forward any post from that channel directly to this bot.\n"
        "4. The bot will verify its admin permissions and register the channel.\n\n"
        "For private channels, the bot will create a join-request invitation link.\n"
        "Do not send a screenshot; forward an actual channel post.",
        parse_mode=ParseMode.HTML, reply_markup=channel_keyboard()
    )


async def channel_list(update, context):
    rows = user_channels(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text(
            "📡 You have not added any channels yet.",
            reply_markup=channel_keyboard()
        )
        return
    for ch in rows:
        kind = "Private / Join request" if ch["is_private"] else "Public"
        await update.effective_message.reply_text(
            f"📡 <b>{html.escape(ch['title'] or 'Channel')}</b>\n"
            f"Type: {kind}\nChannel ID: <code>{ch['chat_id']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("DELETE CHANNEL", callback_data=f"delch:{ch['id']}")
            ]])
        )


async def delete_channels_menu(update, context):
    rows = user_channels(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No channels to delete.", reply_markup=channel_keyboard())
        return
    buttons = [[InlineKeyboardButton(
        f"DELETE: {(ch['title'] or 'Channel')[:30]}",
        callback_data=f"delch:{ch['id']}"
    )] for ch in rows]
    await update.effective_message.reply_text(
        "Select a channel to remove:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def upload_prompt(update, context):
    STATES[update.effective_user.id] = "upload"
    await update.effective_message.reply_text(
        "📤 <b>UPLOAD FILE</b>\n\n"
        "Send or forward a Telegram message, document, photo, video, audio, voice message or other supported content.\n\n"
        "The bot will store a copy and generate a unique deep link.",
        parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )


async def update_channel(update, context):
    url = setting("update_url")
    if url and re.match(r"^https?://", url):
        await update.effective_message.reply_text(
            "📢 <b>OFFICIAL UPDATE CHANNEL</b>\n\nTap below to open the channel.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("OPEN UPDATE CHANNEL", url=url)]]),
        )
    else:
        await update.effective_message.reply_text("The update channel has not been configured yet.", reply_markup=main_keyboard())


async def support(update, context):
    url = setting("support_url")
    if url and re.match(r"^https?://", url):
        await update.effective_message.reply_text(
            "💬 <b>SUPPORT CENTER</b>\n\nTap below to contact support.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("CONTACT SUPPORT", url=url)]]),
        )
    else:
        await update.effective_message.reply_text("Support link is not configured yet.", reply_markup=main_keyboard())


async def owner(update, context):
    url = setting("owner_url")
    if url and re.match(r"^https?://", url):
        await update.effective_message.reply_text(
            "👑 <b>BOT OWNER</b>\n\nTap below to contact the bot owner.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("CONTACT BOT OWNER", url=url)]]),
        )
    else:
        await update.effective_message.reply_text("Bot owner link is not configured yet.", reply_markup=main_keyboard())


# --------------------------- ADMIN PANEL -----------------------------

def admin_only(user_id):
    return user_id == ADMIN_ID


async def admin_command(update, context):
    if not admin_only(update.effective_user.id):
        return
    await update.effective_message.reply_text(
        "🛡 <b>ADMIN CONTROL PANEL</b>\nChoose a management section.",
        parse_mode=ParseMode.HTML, reply_markup=admin_keyboard()
    )


async def dashboard(update, context):
    with db() as con:
        users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        banned = con.execute("SELECT COUNT(*) n FROM users WHERE banned=1").fetchone()["n"]
        files = con.execute("SELECT COUNT(*) n FROM files WHERE active=1").fetchone()["n"]
        downloads = con.execute("SELECT COALESCE(SUM(downloads),0) n FROM files").fetchone()["n"]
        channels = con.execute("SELECT COUNT(*) n FROM channels").fetchone()["n"]
        today = int(time.time()) - 86400
        new_users = con.execute("SELECT COUNT(*) n FROM users WHERE joined_at>=?", (today,)).fetchone()["n"]
    text = (
        "📊 <b>BOT DASHBOARD</b>\n\n"
        f"👥 Total users: <b>{users}</b>\n"
        f"🚫 Banned users: <b>{banned}</b>\n"
        f"📁 Active files: <b>{files}</b>\n"
        f"📡 Registered channels: <b>{channels}</b>\n"
        f"📥 Total downloads: <b>{downloads}</b>\n"
        f"🆕 New users (24h): <b>{new_users}</b>\n"
        f"⚙️ Bot status: <b>{'OFF' if setting('maintenance') == '1' else 'ON'}</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())


async def list_all_users(update, context):
    with db() as con:
        rows = con.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT 40").fetchall()
    if not rows:
        await update.effective_message.reply_text("No users found.", reply_markup=admin_keyboard())
        return
    for r in rows:
        status = "BANNED" if r["banned"] else "ACTIVE"
        await update.effective_message.reply_text(
            f"👤 <b>{html.escape(r['first_name'] or 'Unknown')}</b>\n"
            f"Username: @{html.escape(r['username'] or 'none')}\n"
            f"ID: <code>{r['user_id']}</code>\nStatus: <b>{status}</b>",
            parse_mode=ParseMode.HTML
        )


async def list_all_files(update, context):
    with db() as con:
        rows = con.execute("SELECT * FROM files WHERE active=1 ORDER BY file_id DESC LIMIT 30").fetchall()
    if not rows:
        await update.effective_message.reply_text("No files found.", reply_markup=admin_keyboard())
        return
    for r in rows:
        await update.effective_message.reply_text(
            f"📁 <b>{html.escape(r['title'] or 'Content')}</b>\n"
            f"File ID: <code>{r['file_id']}</code>\n"
            f"Owner: <code>{r['owner_id']}</code>\n"
            f"Storage message: <code>{r['stored_message_id']}</code>\n"
            f"Downloads: {r['downloads']}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("DELETE FILE", callback_data=f"admindelfile:{r['file_id']}")
            ]])
        )


async def list_all_channels(update, context):
    with db() as con:
        rows = con.execute("SELECT * FROM channels ORDER BY id DESC LIMIT 40").fetchall()
    if not rows:
        await update.effective_message.reply_text("No channels registered.", reply_markup=admin_keyboard())
        return
    for r in rows:
        await update.effective_message.reply_text(
            f"📡 <b>{html.escape(r['title'] or 'Channel')}</b>\n"
            f"Channel ID: <code>{r['chat_id']}</code>\nOwner: <code>{r['owner_id']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("REMOVE CHANNEL", callback_data=f"admin_delch:{r['id']}")
            ]])
        )


async def settings_menu(update, context):
    text = (
        "⚙️ <b>BOT SETTINGS</b>\n\n"
        "Send one of these commands followed by a value:\n"
        "<code>/setwelcome Your HTML welcome message</code>\n"
        "<code>/setupdate https://t.me/channel</code>\n"
        "<code>/setsupport https://t.me/support</code>\n"
        "<code>/setowner https://t.me/owner</code>\n"
        "<code>/setcontact https://t.me/admin</code>\n"
        "<code>/settutorial Your tutorial text</code>\n"
        "<code>/setmaintenance_reason Reason text</code>\n\n"
        "Use /admin to return to the panel."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())


async def set_value(update, context, key, label, validate_url=False):
    if update.effective_user.id != ADMIN_ID:
        return
    value = " ".join(context.args).strip()
    if not value:
        await update.effective_message.reply_text(f"Usage: /{label} VALUE")
        return
    if validate_url and not re.match(r"^https?://", value):
        await update.effective_message.reply_text("Please provide a valid http:// or https:// URL.")
        return
    set_setting(key, value)
    log_admin("set_" + key, value[:100])
    await update.effective_message.reply_text(f"✅ {label} updated.")


async def setwelcome(u,c): await set_value(u,c,"welcome","setwelcome")
async def setupdate(u,c): await set_value(u,c,"update_url","setupdate",True)
async def setsupport(u,c): await set_value(u,c,"support_url","setsupport",True)
async def setowner(u,c): await set_value(u,c,"owner_url","setowner",True)
async def setcontact(u,c): await set_value(u,c,"admin_contact_url","setcontact",True)
async def settutorial(u,c): await set_value(u,c,"tutorial","settutorial")
async def setmaintenance_reason(u,c): await set_value(u,c,"maintenance_reason","setmaintenance_reason")


async def ban_prompt(update, context):
    STATES[ADMIN_ID] = "ban_user_id"
    await update.effective_message.reply_text("Send the numeric Telegram user ID to ban.")


async def unban_prompt(update, context):
    STATES[ADMIN_ID] = "unban_user_id"
    await update.effective_message.reply_text("Send the numeric Telegram user ID to unban.")


async def broadcast_prompt(update, context):
    STATES[ADMIN_ID] = "broadcast"
    await update.effective_message.reply_text(
        "Send the message you want to broadcast. Text, photo, video and forwarded messages are supported."
    )


async def bot_off(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    reason = " ".join(context.args).strip() or "The bot is temporarily under maintenance."
    set_setting("maintenance", "1")
    set_setting("maintenance_reason", reason)
    log_admin("bot_off", reason)
    await notify_all(context, "🛠 <b>BOT MAINTENANCE</b>\n\n" + html.escape(reason))
    await update.effective_message.reply_text("🔴 Public bot access is now OFF.", reply_markup=admin_keyboard())


async def bot_on(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    set_setting("maintenance", "0")
    log_admin("bot_on")
    await notify_all(context, "🟢 <b>BOT IS ONLINE</b>\n\nAll services are available again.")
    await update.effective_message.reply_text("🟢 Public bot access is now ON.", reply_markup=admin_keyboard())


async def notify_all(context, text):
    with db() as con:
        ids = [r["user_id"] for r in con.execute("SELECT user_id FROM users").fetchall()]
    sent = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio_sleep()
        except TelegramError:
            pass
    log_admin("broadcast_notice", f"{sent}/{len(ids)}")


async def asyncio_sleep():
    import asyncio
    await asyncio.sleep(0.04)


# --------------------------- CALLBACKS -------------------------------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""

    if data.startswith("copy:"):
        file_id = int(data.split(":")[1])
        row = file_row(file_id)
        if not row or row["owner_id"] != uid:
            await q.answer("This file link is unavailable.", show_alert=True)
            return
        link = await make_deep_link(context, file_id)
        await q.message.reply_text(f"Your deep link:\n<code>{html.escape(link)}</code>", parse_mode=ParseMode.HTML)
        await q.answer("Deep link sent below. Copy it from the message.")
        return

    if data.startswith("joined:"):
        if not await public_access_allowed(update):
            return
        file_id = int(data.split(":")[1])
        row = file_row(file_id)
        if not row:
            await q.edit_message_text("This file is no longer available.")
            return
        missing = []
        for ch in user_channels(row["owner_id"]):
            if not await check_user_in_channel(context, uid, ch["chat_id"]):
                missing.append(ch)
        if missing:
            await q.answer("You have not joined all required channels yet.", show_alert=True)
            return
        try:
            await copy_file_to_user(context, uid, row)
            await q.edit_message_text("✅ File delivered successfully.")
            await context.bot.send_message(uid, "Choose another option below.", reply_markup=main_keyboard())
        except TelegramError:
            await q.edit_message_text("❌ Delivery failed. Please contact the bot owner.")
        return

    if data.startswith("delfile:"):
        file_id = int(data.split(":")[1])
        with db() as con:
            cur = con.execute(
                "UPDATE files SET active=0 WHERE file_id=? AND owner_id=?",
                (file_id, uid)
            )
        await q.edit_message_text("🗑 File deleted." if cur.rowcount else "File not found.")
        return

    if data.startswith("delch:"):
        channel_row_id = int(data.split(":")[1])
        with db() as con:
            cur = con.execute(
                "DELETE FROM channels WHERE id=? AND owner_id=?",
                (channel_row_id, uid)
            )
        await q.edit_message_text("✅ Channel removed." if cur.rowcount else "Channel not found.")
        return

    if uid != ADMIN_ID:
        return

    if data.startswith("admindelfile:"):
        file_id = int(data.split(":")[1])
        with db() as con:
            con.execute("UPDATE files SET active=0 WHERE file_id=?", (file_id,))
        log_admin("delete_file", file_id)
        await q.edit_message_text("Admin deleted the file.")
    elif data.startswith("admin_delch:"):
        channel_row_id = int(data.split(":")[1])
        with db() as con:
            con.execute("DELETE FROM channels WHERE id=?", (channel_row_id,))
        log_admin("delete_channel", channel_row_id)
        await q.edit_message_text("Admin removed the channel.")


# --------------------------- MESSAGE ROUTER --------------------------

async def register_forwarded_channel(message, context):
    origin = message.forward_origin
    chat = getattr(origin, "chat", None)
    if not chat or chat.type not in ("channel",):
        return False
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await message.reply_text("❌ Please promote the bot to administrator in that channel first.")
            return True
        username = chat.username or ""
        private = not bool(username)
        invite_link = ""
        if private:
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat.id, creates_join_request=True,
                name="File Store Join Request"
            )
            invite_link = invite.invite_link
        with db() as con:
            con.execute("""
                INSERT INTO channels(owner_id,chat_id,title,username,invite_link,is_private,created_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(owner_id,chat_id) DO UPDATE SET
                  title=excluded.title, username=excluded.username,
                  invite_link=excluded.invite_link, is_private=excluded.is_private
            """, (
                message.from_user.id, chat.id, chat.title or "Channel",
                username, invite_link, int(private), int(time.time())
            ))
        await message.reply_text(
            "✅ <b>CHANNEL ADDED SUCCESSFULLY</b>\n\n"
            f"Channel: {html.escape(chat.title or 'Channel')}\n"
            f"Type: {'Private (join request)' if private else 'Public'}",
            parse_mode=ParseMode.HTML, reply_markup=channel_keyboard()
        )
    except TelegramError as exc:
        log.exception("Channel registration failed")
        await message.reply_text(
            "❌ Could not register this channel. Check that the bot is an administrator "
            "with permission to invite users, then forward a channel post again."
        )
    return True


async def handle_upload(message, context):
    uid = message.from_user.id
    if uid == ADMIN_ID and message.chat.type == "private":
        # Admin messages should not accidentally be stored as user uploads unless explicitly requested.
        pass
    try:
        # The admin's private chat is used as a private storage destination.
        stored = await context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat_id,
            message_id=message.message_id
        )
    except TelegramError:
        log.exception("Could not copy upload to admin storage chat")
        await message.reply_text(
            "❌ Upload storage is not ready. The bot owner must open this bot and press /start first."
        )
        return

    with db() as con:
        cur = con.execute(
            "INSERT INTO files(owner_id,stored_message_id,title,created_at) VALUES(?,?,?,?)",
            (uid, stored.message_id, get_message_title(message), int(time.time()))
        )
        file_id = cur.lastrowid
    link = await make_deep_link(context, file_id)
    await message.reply_text(
        "✅ <b>UPLOAD COMPLETED</b>\n\n"
        f"📄 <b>{html.escape(get_message_title(message))}</b>\n"
        f"🆔 File ID: <code>{file_id}</code>\n\n"
        "🔗 <b>YOUR DEEP LINK</b>\n"
        f"<code>{html.escape(link)}</code>\n\n"
        "Share this link to let others access your content.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("COPY DEEP LINK", callback_data=f"copy:{file_id}"),
            InlineKeyboardButton("OPEN DEEP LINK", url=link)
        ]])
    )


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    touch_user(user)

    if user.id != ADMIN_ID and not await public_access_allowed(update):
        return

    # Admin conversation states
    state = STATES.get(user.id)
    if user.id == ADMIN_ID and state:
        if state == "ban_user_id":
            if not message.text or not message.text.strip().isdigit():
                await message.reply_text("Please send a numeric user ID.")
                return
            target = int(message.text.strip())
            STATES[user.id] = "ban_reason:" + str(target)
            await message.reply_text("Send an optional ban reason, or send /skip.")
            return
        if state.startswith("ban_reason:"):
            target = int(state.split(":")[1])
            reason = (message.text or "").strip()
            if reason == "/skip":
                reason = "No reason was provided."
            with db() as con:
                con.execute("UPDATE users SET banned=1,ban_reason=? WHERE user_id=?", (reason, target))
            STATES.pop(user.id, None)
            try:
                markup = inline_url_or_text("CONTACT ADMIN", setting("admin_contact_url"))
                await context.bot.send_message(
                    target,
                    "⛔ <b>YOUR ACCESS HAS BEEN RESTRICTED</b>\n\nReason: "
                    + html.escape(reason),
                    parse_mode=ParseMode.HTML, reply_markup=markup
                )
            except TelegramError:
                pass
            log_admin("ban_user", f"{target}: {reason}")
            await message.reply_text("User banned. Notification attempted.", reply_markup=admin_keyboard())
            return
        if state == "unban_user_id":
            if not message.text or not message.text.strip().isdigit():
                await message.reply_text("Please send a numeric user ID.")
                return
            target = int(message.text.strip())
            with db() as con:
                cur = con.execute("UPDATE users SET banned=0,ban_reason='' WHERE user_id=?", (target,))
            STATES.pop(user.id, None)
            try:
                await context.bot.send_message(target, "✅ <b>Your bot access has been restored.</b>", parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
            log_admin("unban_user", target)
            await message.reply_text(
                "User unbanned." if cur.rowcount else "User record not found.",
                reply_markup=admin_keyboard()
            )
            return
        if state == "broadcast":
            STATES.pop(user.id, None)
            with db() as con:
                ids = [r["user_id"] for r in con.execute("SELECT user_id FROM users WHERE banned=0").fetchall()]
            success = failed = 0
            for target in ids:
                try:
                    await context.bot.copy_message(
                        chat_id=target, from_chat_id=message.chat_id,
                        message_id=message.message_id
                    )
                    success += 1
                except TelegramError:
                    failed += 1
                await asyncio_sleep()
            await message.reply_text(
                f"📢 Broadcast finished.\nSuccessful: {success}\nFailed: {failed}",
                reply_markup=admin_keyboard()
            )
            log_admin("broadcast", f"{success}/{failed}")
            return

    # Menu navigation
    text = (message.text or "").strip()
    if text == "UPLOAD FILE":
        await upload_prompt(update, context)
        return
    if text == "PROFILE":
        await profile(update, context)
        return
    if text == "ADD CHANNELS":
        await update.effective_message.reply_text(
            "Choose a channel action:", reply_markup=channel_keyboard()
        )
        return
    if text == "HOW TO USE":
        await message.reply_text(setting("tutorial"), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())
        return
    if text == "MY FILE":
        await my_files(update, context)
        return
    if text == "UPDATE CHANNEL":
        await update_channel(update, context)
        return
    if text == "SUPPORT":
        await support(update, context)
        return
    if text == "BOT OWNER":
        await owner(update, context)
        return
    if text == "BACK" or text == "⬅️ EXIT ADMIN":
        STATES.pop(user.id, None)
        await send_home(update)
        return
    if text == "➕ ADD CHANNEL":
        await add_channel_instructions(update, context)
        return
    if text == "CHANNEL LIST":
        await channel_list(update, context)
        return
    if text == "DELETE CHANNELS":
        await delete_channels_menu(update, context)
        return

    # Admin menu
    if user.id == ADMIN_ID:
        if text == "📊 DASHBOARD":
            await dashboard(update, context); return
        if text == "👥 ALL USERS":
            await list_all_users(update, context); return
        if text == "🚫 BAN USER":
            await ban_prompt(update, context); return
        if text == "✅ UNBAN USER":
            await unban_prompt(update, context); return
        if text == "📁 ALL FILES":
            await list_all_files(update, context); return
        if text == "📢 BROADCAST":
            await broadcast_prompt(update, context); return
        if text == "📡 ALL CHANNELS":
            await list_all_channels(update, context); return
        if text == "⚙️ BOT SETTINGS":
            await settings_menu(update, context); return
        if text == "🟢 BOT ON":
            await bot_on(update, context); return
        if text == "🔴 BOT OFF":
            await bot_off(update, context); return

    # Channel forwarding has priority over file upload.
    if message.forward_origin and await register_forwarded_channel(message, context):
        return

    # Upload mode: accept any supported message. Also allow a sent item directly after prompt.
    is_media_upload = any((
        message.document, message.photo, message.video, message.audio,
        message.voice, message.animation, message.sticker, message.video_note,
        message.contact, message.location, message.poll
    ))
    is_forwarded_content = bool(message.forward_origin)
    if state == "upload" or is_media_upload or is_forwarded_content:
        if user.id == ADMIN_ID and text.startswith("/"):
            return
        await handle_upload(message, context)
        STATES.pop(user.id, None)
        return
    if state == "upload" and message.text:
        await handle_upload(message, context)
        STATES.pop(user.id, None)
        return

    await message.reply_text("Please choose an option from the menu.", reply_markup=main_keyboard())


# --------------------------- ERROR HANDLER ---------------------------

async def error_handler(update, context):
    log.exception("Unhandled bot error", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong while processing that request. Please try again."
            )
    except TelegramError:
        pass


# --------------------------- HEALTH CHECK SERVER ---------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            body = b"OK - Telegram File Store Bot is running"
            self.send_response(200)
        else:
            body = b"Not Found"
            self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("Health check: " + fmt, *args)


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Health server listening on port %s", PORT)


# --------------------------- APPLICATION -----------------------------

def main():
    init_db()
    start_health_server()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("setupdate", setupdate))
    app.add_handler(CommandHandler("setsupport", setsupport))
    app.add_handler(CommandHandler("setowner", setowner))
    app.add_handler(CommandHandler("setcontact", setcontact))
    app.add_handler(CommandHandler("settutorial", settutorial))
    app.add_handler(CommandHandler("setmaintenance_reason", setmaintenance_reason))
    app.add_handler(CommandHandler("on", bot_on))
    app.add_handler(CommandHandler("off", bot_off))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.ALL, message_router))
    app.add_error_handler(error_handler)

    log.info("Starting Telegram File Store Bot...")
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
