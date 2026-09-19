import os
import sqlite3
import logging
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# بارگذاری فایل .env
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "dummy-key")
PROXY_BASE_URL = os.getenv("PROXY_BASE_URL", "http://localhost:20128/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

DB_FILE = "bot_database.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# کلاینت هوش مصنوعی متصل به 9Router
ai_client = AsyncOpenAI(
    api_key=PROXY_API_KEY,
    base_url=PROXY_BASE_URL,
)

# شخصیت Codey
SYSTEM_PROMPT = """
You are Codey, a warm, friendly, and supportive programming mentor and coding assistant.
Rules:
1. Always respond in the exact same language the user speaks (Persian/Farsi or English).
2. Focus strictly on programming, software engineering, databases, DevOps, and closely related technical fields.
3. If the user asks something completely unrelated to coding, politely and warmly guide them back to programming topics.
4. Keep your tone encouraging and clear, avoid stiff or robotic responses.
"""

# ==========================================
# دیتابیس (SQLite)
# ==========================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            daily_limit INTEGER DEFAULT 100,
            requests_today INTEGER DEFAULT 0,
            last_active_date TEXT,
            is_banned INTEGER DEFAULT 0,
            joined_date TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            feature TEXT,
            timestamp TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('global_limit', '100')")
    conn.commit()
    conn.close()


def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_global_limit():
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key='global_limit'").fetchone()
    conn.close()
    return int(row["value"]) if row else 100


def get_or_create_user(user_id: int, first_name: str):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    today_str = date.today().isoformat()

    if not user:
        default_limit = get_global_limit()
        joined_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.execute("""
            INSERT INTO users (user_id, first_name, daily_limit, requests_today, last_active_date, is_banned, joined_date)
            VALUES (?, ?, ?, 0, ?, 0, ?)
        """, (user_id, first_name, default_limit, today_str, joined_str))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    else:
        if user["last_active_date"] != today_str:
            conn.execute("""
                UPDATE users SET requests_today = 0, last_active_date = ? WHERE user_id = ?
            """, (today_str, user_id))
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    conn.close()
    return user


def can_user_send_request(user_id: int, first_name: str) -> tuple[bool, str]:
    user = get_or_create_user(user_id, first_name)
    if user["is_banned"] == 1:
        return False, "🚫 حساب کاربری شما مسدود شده است."
    if user["requests_today"] >= user["daily_limit"]:
        return False, "⏳ سقف مصرف امروزت تموم شده دوست من! فردا ساعت ۰۰:۰۰ دوباره شارژ میشه."
    return True, ""


def increment_user_usage(user_id: int, feature: str):
    conn = get_db_connection()
    conn.execute("UPDATE users SET requests_today = requests_today + 1 WHERE user_id = ?", (user_id,))
    conn.execute("INSERT INTO request_logs (user_id, feature, timestamp) VALUES (?, ?, ?)",
                 (user_id, feature, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def save_chat_message(user_id: int, role: str, content: str):
    conn = get_db_connection()
    conn.execute("INSERT INTO chat_history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                 (user_id, role, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_user_chat_history(user_id: int, limit: int = 8):
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT role, content FROM chat_history 
        WHERE user_id = ? 
        ORDER BY id DESC LIMIT ?
    """, (user_id, limit)).fetchall()
    conn.close()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def reset_user_history(user_id: int):
    conn = get_db_connection()
    conn.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# ==========================================
# ارسال به مدل زبانی
# ==========================================

async def call_llm(messages: list) -> str:
    try:
        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.4
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"LLM API Error: {type(e).__name__} - {e}")
        return f"متأسفم، در برقراری ارتباط با هوش مصنوعی مشکلی پیش اومد:\n`{str(e)}`"

# ==========================================
# کیبوردها (دکمه‌های رنگی شیشه‌ای + دکمه قرمز پایین)
# ==========================================

def get_main_menu_keyboard():
    """منوی اصلی شیشه‌ای با دکمه‌های رنگی"""
    buttons = [
        [
            InlineKeyboardButton("💬 چت و گفتگوی آزاد", callback_data="mode_chat", style="success"),
            InlineKeyboardButton("🔄 شروع مجدد", callback_data="user_reset", style="success")
        ],
        [
            InlineKeyboardButton("🐞 دیباگ و اصلاح کد", callback_data="mode_debug", style="primary"),
            InlineKeyboardButton("💡 توضیح خط به خط کد", callback_data="mode_explain", style="primary")
        ],
        [
            InlineKeyboardButton("👤 اطلاعات حساب من", callback_data="user_account", style="danger")
        ]
    ]
    return InlineKeyboardMarkup(buttons)


def get_back_keyboard():
    """دکمه قرمز رنگ پایین صفحه برای بازگشت به منو"""
    button = KeyboardButton(text="🔙 بازگشت به منوی اصلی", style="danger")
    return ReplyKeyboardMarkup([[button]], resize_keyboard=True, is_persistent=True)

# ==========================================
# هندلر منوها و کلیک‌ها
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.first_name)
    context.user_data["mode"] = "chat"

    welcome_msg = (
        f"سلام {user.first_name}! من **Codey** هستم، مربی و همراه برنامه‌نویسی شما 💻✨\n\n"
        "یکی از گزینه‌های زیر رو انتخاب کن:"
    )
    await update.message.reply_text(
        welcome_msg,
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )


async def user_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    back_kb = get_back_keyboard()

    if data == "mode_chat":
        context.user_data["mode"] = "chat"
        await query.message.reply_text(
            "💬 **حالت چت آزاد فعال شد.**\nهر سوال، ایده یا مشکلی در برنامه‌نویسی داری بنویس.\nبرای خروج، دکمه قرمز پایین رو بزن:",
            reply_markup=back_kb,
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "mode_debug":
        context.user_data["mode"] = "debug"
        await query.message.reply_text(
            "🐞 **حالت دیباگ فعال شد.**\nکدی که ارور میده رو بفرست تا عیب‌یابی و اصلاحش کنم:",
            reply_markup=back_kb,
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "mode_explain":
        context.user_data["mode"] = "explain"
        await query.message.reply_text(
            "💡 **حالت توضیح کد فعال شد.**\nکدی که می‌خوای برات ساده توضیح داده بشه رو بفرست:",
            reply_markup=back_kb,
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "user_account":
        user_data = get_or_create_user(user.id, user.first_name)
        remaining = max(0, user_data["daily_limit"] - user_data["requests_today"])
        msg = (
            f"👤 **حساب کاربری شما**\n\n"
            f"▫️ **نام:** {user_data['first_name']}\n"
            f"▫️ **شناسه عددی:** `{user_data['user_id']}`\n"
            f"▫️ **مصرف امروز:** {user_data['requests_today']} از {user_data['daily_limit']}\n"
            f"▫️ **فرصت باقیمانده امروز:** {remaining}\n"
            f"▫️ **تاریخ عضویت:** {user_data['joined_date']}\n"
        )
        # نمایش دکمه بازگشت در بخش حساب کاربری
        await query.message.reply_text(
            msg,
            reply_markup=back_kb,
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "user_reset":
        reset_user_history(user.id)
        context.user_data["mode"] = "chat"
        await query.message.reply_text("حافظه مکالمه قبلی پاک شد! از اول شروع می‌کنیم 🚀")


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    # ۱. اگر کاربر دکمه بازگشت قرمز را زد:
    if text == "🔙 بازگشت به منوی اصلی":
        context.user_data["mode"] = "chat"
        context.user_data.clear()
        await update.message.reply_text("منوی اصلی باز شد 👇", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("لطفاً یک بخش را انتخاب کن:", reply_markup=get_main_menu_keyboard())
        return

    # ۲. ورودی‌های پنل مدیریت
    if user.id in ADMIN_IDS and context.user_data.get("admin_state"):
        await handle_admin_text_input(update, context)
        return

    # ۳. بررسی محدودیت و مسدودی
    allowed, error_msg = can_user_send_request(user.id, user.first_name)
    if not allowed:
        await update.message.reply_text(error_msg)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    mode = context.user_data.get("mode", "chat")

    # دیباگ کد
    if mode == "debug":
        prompt = f"""
You are debugging this code. Reply in the user's language.
Format your output strictly as follows:
1. Provide the corrected code in a proper markdown code block.
2. Underneath, use numbered bullets to explain:
   - What was changed.
   - Why it was changed (the core bug).
   - What would happen if this change was NOT made.

Code:
{text}
"""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        reply = await call_llm(messages)
        increment_user_usage(user.id, "debug")
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    # توضیح کد
    elif mode == "explain":
        prompt = f"""
Explain the following code step-by-step in simple words for a beginner.
Use intuitive real-world metaphors where helpful.
At the very end, provide a concise 1-2 sentence overall summary of what the code achieves.
Reply in the user's language.

Code:
{text}
"""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        reply = await call_llm(messages)
        increment_user_usage(user.id, "explain")
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    # چت آزاد
    else:
        history = get_user_chat_history(user.id, limit=8)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": text}]
        reply = await call_llm(messages)
        save_chat_message(user.id, "user", text)
        save_chat_message(user.id, "assistant", reply)
        increment_user_usage(user.id, "chat")
        await update.message.reply_text(reply)

# ==========================================
# دستورات Slash و پنل ادمین
# ==========================================

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_user_history(update.effective_user.id)
    context.user_data["mode"] = "chat"
    await update.message.reply_text("مکالمه قبلی ریست شد! 🚀")


async def account_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data = get_or_create_user(user.id, user.first_name)
    remaining = max(0, user_data["daily_limit"] - user_data["requests_today"])
    msg = (
        f"👤 **حساب کاربری شما**\n\n"
        f"▫️ **نام:** {user_data['first_name']}\n"
        f"▫️ **شناسه عددی:** `{user_data['user_id']}`\n"
        f"▫️ **مصرف امروز:** {user_data['requests_today']} از {user_data['daily_limit']}\n"
        f"▫️ **فرصت باقیمانده امروز:** {remaining}\n"
        f"▫️ **تاریخ عضویت:** {user_data['joined_date']}\n"
    )
    await update.message.reply_text(msg, reply_markup=get_back_keyboard(), parse_mode=ParseMode.MARKDOWN)


def get_admin_keyboard():
    buttons = [
        [InlineKeyboardButton("📊 مشاهده آمار کلی", callback_data="admin_stats")],
        [InlineKeyboardButton("👤 مشاهده اطلاعات کاربر", callback_data="admin_user_info")],
        [InlineKeyboardButton("✏️ ویرایش سقف سراسری", callback_data="admin_set_global_limit")],
        [InlineKeyboardButton("✏️ ویرایش سقف کاربر خاص", callback_data="admin_set_user_limit")],
        [InlineKeyboardButton("🚫 مسدود/رفع مسدودی کاربر", callback_data="admin_toggle_ban")],
        [InlineKeyboardButton("📢 ارسال پیام همگانی", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 خروج از پنل", callback_data="admin_close")]
    ]
    return InlineKeyboardMarkup(buttons)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ شما دسترسی ادمین ندارید.")
        return
    context.user_data.clear()
    await update.message.reply_text("پنل مدیریت Codey:", reply_markup=get_admin_keyboard())


async def admin_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return

    data = query.data
    if data == "admin_close":
        context.user_data.clear()
        await query.edit_message_text("از پنل مدیریت خارج شدید.")
    elif data == "admin_stats":
        conn = get_db_connection()
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today_reqs = conn.execute("SELECT SUM(requests_today) FROM users").fetchone()[0] or 0
        top_feature_row = conn.execute("""
            SELECT feature, COUNT(*) as cnt FROM request_logs 
            GROUP BY feature ORDER BY cnt DESC LIMIT 1
        """).fetchone()
        top_feature = f"{top_feature_row['feature']} ({top_feature_row['cnt']} بار)" if top_feature_row else "بدون داده"
        conn.close()
        stats_msg = (
            f"📊 **آمار ربات:**\n\n"
            f"👥 کل کاربران: {total_users}\n"
            f"⚡ کل درخواست‌های امروز: {today_reqs}\n"
            f"🔥 پرکاربردترین بخش: {top_feature}\n"
        )
        await query.edit_message_text(stats_msg, reply_markup=get_admin_keyboard(), parse_mode=ParseMode.MARKDOWN)
    elif data == "admin_user_info":
        context.user_data["admin_state"] = "awaiting_user_id_for_info"
        await query.edit_message_text("شناسه عددی (User ID) کاربر را ارسال کنید:")
    elif data == "admin_set_global_limit":
        context.user_data["admin_state"] = "awaiting_global_limit"
        await query.edit_message_text("سقف روزانه جدید برای تمام کاربران را ارسال کنید:")
    elif data == "admin_set_user_limit":
        context.user_data["admin_state"] = "awaiting_user_id_for_limit"
        await query.edit_message_text("شناسه کاربر و سقف جدید را با فاصله بفرستید (مثال: `123456 150`):", parse_mode=ParseMode.MARKDOWN)
    elif data == "admin_toggle_ban":
        context.user_data["admin_state"] = "awaiting_user_id_for_ban"
        await query.edit_message_text("شناسه عددی کاربر برای مسدود/رفع مسدودی را ارسال کنید:")
    elif data == "admin_broadcast":
        context.user_data["admin_state"] = "awaiting_broadcast_text"
        await query.edit_message_text("متن پیام همگانی را بفرستید:")


async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("admin_state")
    text = update.message.text.strip()
    conn = get_db_connection()

    if state == "awaiting_user_id_for_info":
        if text.isdigit():
            user = conn.execute("SELECT * FROM users WHERE user_id = ?", (int(text),)).fetchone()
            if user:
                info = (
                    f"👤 شناسه: `{user['user_id']}`\n"
                    f"نام: {user['first_name']}\n"
                    f"مصرف امروز: {user['requests_today']}/{user['daily_limit']}\n"
                    f"عضویت: {user['joined_date']}\n"
                    f"مسدود: {'بله' if user['is_banned'] else 'خیر'}"
                )
                await update.message.reply_text(info, parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_keyboard())
            else:
                await update.message.reply_text("کاربر یافت نشد.", reply_markup=get_admin_keyboard())
        context.user_data.clear()

    elif state == "awaiting_global_limit":
        if text.isdigit():
            new_lim = int(text)
            conn.execute("UPDATE settings SET value = ? WHERE key = 'global_limit'", (str(new_lim),))
            conn.execute("UPDATE users SET daily_limit = ?", (new_lim,))
            conn.commit()
            await update.message.reply_text(f"سقف همه کاربران به {new_lim} تغییر کرد.", reply_markup=get_admin_keyboard())
        context.user_data.clear()

    elif state == "awaiting_user_id_for_limit":
        parts = text.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            uid, lim = int(parts[0]), int(parts[1])
            conn.execute("UPDATE users SET daily_limit = ? WHERE user_id = ?", (lim, uid))
            conn.commit()
            await update.message.reply_text(f"سقف کاربر {uid} به {lim} تغییر کرد.", reply_markup=get_admin_keyboard())
        context.user_data.clear()

    elif state == "awaiting_user_id_for_ban":
        if text.isdigit():
            uid = int(text)
            target = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (uid,)).fetchone()
            if target:
                new_ban = 0 if target["is_banned"] == 1 else 1
                conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (new_ban, uid))
                conn.commit()
                status = "مسدود شد 🚫" if new_ban == 1 else "فعال شد ✅"
                await update.message.reply_text(f"کاربر {uid} {status}", reply_markup=get_admin_keyboard())
        context.user_data.clear()

    elif state == "awaiting_broadcast_text":
        all_users = conn.execute("SELECT user_id FROM users WHERE is_banned = 0").fetchall()
        for row in all_users:
            try:
                await context.bot.send_message(chat_id=row["user_id"], text=f"📢 **پیام مدیریت:**\n\n{text}", parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        await update.message.reply_text("پیام ارسال شد.", reply_markup=get_admin_keyboard())
        context.user_data.clear()

    conn.close()

# ==========================================
# شروع به کار ربات
# ==========================================

def main():
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # دستورات
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("account", account_command))
    app.add_handler(CommandHandler("admin", admin_command))

    # رویداد دکمه‌ها
    app.add_handler(CallbackQueryHandler(admin_button_callback, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(user_menu_callback, pattern="^(mode_|user_)"))

    # پیام‌های متنی
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

    logging.info("Codey bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()