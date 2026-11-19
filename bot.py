import os
import time
import logging
import httpx
import aiosqlite
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
DB_PATH = os.getenv("DB_PATH", "/tmp/bot.sqlite3")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "20"))
MAX_HISTORY = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER, role TEXT, content TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS usage (
 user_id INTEGER, day TEXT, count INTEGER DEFAULT 0,
 PRIMARY KEY(user_id, day));
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def add_msg(uid, role, content):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, role, content, ts) VALUES (?,?,?,?)",
            (uid, role, content, int(time.time())),
        )
        await db.commit()


async def get_history(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, MAX_HISTORY),
        ) as cur:
            rows = await cur.fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


async def clear_history(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE user_id=?", (uid,))
        await db.commit()


async def bump_usage(uid):
    day = time.strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO usage VALUES (?,?,1) "
            "ON CONFLICT(user_id,day) DO UPDATE SET count=count+1",
            (uid, day),
        )
        await db.commit()
        async with db.execute(
            "SELECT count FROM usage WHERE user_id=? AND day=?", (uid, day)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def ask_ai(messages):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://ai-studio.example",
        "X-Title": "AI Studio Bot",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "أنت مساعد AI Studio. أجب بالعربية بوضوح وإيجاز."},
            *messages,
        ],
    }
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload, headers=headers,
        )
        if r.status_code != 200:
            raise RuntimeError(r.text[:200])
        return r.json()["choices"][0]["message"]["content"]


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بك فى AI Studio ✦\n"
        "ابعت أى سؤال وأنا أرد عليك.\n\n"
        "/help — المساعدة\n"
        "/clear — مسح المحادثة"
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "الأوامر:\n"
        "/start — البداية\n"
        "/help — المساعدة\n"
        "/clear — مسح المحادثة"
    )


async def clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await clear_history(update.effective_user.id)
    await update.message.reply_text("تم مسح المحادثة ✦")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    if not text or len(text) > 4000:
        await update.message.reply_text("الرسالة طويلة جداً (الحد 4000 حرف).")
        return

    count = await bump_usage(uid)
    if count > DAILY_LIMIT:
        await update.message.reply_text(
            f"وصلت للحد اليومى ({DAILY_LIMIT} رسالة). ارجع بكرة ✦"
        )
        return

    await add_msg(uid, "user", text)
    history = await get_history(uid)
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        reply = await ask_ai(history)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"حصل خطأ: {e}")
        return
    await add_msg(uid, "assistant", reply)
    await update.message.reply_text(reply[:4000])


async def post_init(app):
    await init_db()


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
