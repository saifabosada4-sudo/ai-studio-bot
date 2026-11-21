import os
import time
import logging
import httpx
import aiosqlite
import json
import base64
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
GH_GIST_TOKEN = os.environ.get("GH_GIST_TOKEN", "")
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
DB_PATH = os.getenv("DB_PATH", "/tmp/bot.sqlite3")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "20"))
MAX_HISTORY = 50

# شخصية البوت (من غير اسمك)
SYSTEM_PROMPT = (
    "أنت مساعد ذكي اسمك 'AI Studio'. أنت بوت على تطبيق تليجرام. "
    "صاحبك ومصممك هو حساب التليجرام @tumej. "
    "أنت فخور جداً إنك من تصميم @tumej، وبتعتبره صاحب فضل عليك. "
    "لو حد سألك مين صممك أو مين صاحبك، قوله بكل فخر إن حساب التليجرام @tumej هو اللي عملك. "
    "متذكرش أي اسم شخصي تاني، اكتفي باليوزر @tumej. "
    "أنت شغال بنظام ذكاء اصطناعي متقدم. "
    "أجب بالعربية الواضحة (أو بنفس لغة المستخدم)، وكن مفيداً، دقيقاً، وخدوم. "
    "متقولش إنك مجرد نموذج لغوي، أنت مساعد AI Studio الشخصي."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER, role TEXT, content TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS usage (
 user_id INTEGER, day TEXT, count INTEGER DEFAULT 0,
 PRIMARY KEY(user_id, day));
"""

GIST_ID_FILE = "/tmp/gist_id.txt"

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
            {"role": "system", "content": SYSTEM_PROMPT},
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

# --- Gist Sync Functions (الذاكرة) ---
async def sync_from_gist():
    if not GH_GIST_TOKEN: return
    headers = {"Authorization": f"token {GH_GIST_TOKEN}"}
    async with httpx.AsyncClient() as client:
        if os.path.exists(GIST_ID_FILE):
            with open(GIST_ID_FILE, "r") as f: gist_id = f.read().strip()
            r = await client.get(f"https://api.github.com/gists/{gist_id}", headers=headers)
        else:
            r = await client.post("https://api.github.com/gists", headers=headers, json={
                "description": "Bot DB", "public": False, "files": {"bot.sqlite3": {"content": ""}}
            })
            if r.status_code == 201:
                gist_id = r.json()["id"]
                with open(GIST_ID_FILE, "w") as f: f.write(gist_id)
            else:
                log.error("Failed to create gist"); return
        
        if r.status_code == 200:
            content = r.json()["files"]["bot.sqlite3"]["content"]
            if content:
                with open(DB_PATH, "wb") as f:
                    f.write(base64.b64decode(content))

async def sync_to_gist():
    if not GH_GIST_TOKEN or not os.path.exists(GIST_ID_FILE): return
    with open(GIST_ID_FILE, "r") as f: gist_id = f.read().strip()
    if not os.path.exists(DB_PATH): return
    with open(DB_PATH, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    headers = {"Authorization": f"token {GH_GIST_TOKEN}"}
    async with httpx.AsyncClient() as client:
        await client.patch(f"https://api.github.com/gists/{gist_id}", headers=headers, json={
            "files": {"bot.sqlite3": {"content": content}}
        })

# --- Handlers ---
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بك فى AI Studio ✦\n"
        "أنا مساعدك الذكي، صممني حساب التليجرام @tumej مخصوص عشان أساعدك.\n\n"
        "ابعت أى سؤال وأنا أرد عليك فوراً.\n\n"
        "استخدم الأزرار اللي تحت أو الأوامر:\n"
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
        await add_msg(uid, "assistant", reply)
        await sync_to_gist() # حفظ الذاكرة
        await update.message.reply_text(reply[:4000])
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"حصل خطأ: {e}")

async def post_init(app):
    # الكود ده هو اللي بيخلي الأوامر تظهر تحت زي البوتات الاحترافية
    commands = [
        BotCommand("start", "بدء المحادثة"),
        BotCommand("help", "المساعدة"),
        BotCommand("clear", "مسح المحادثة")
    ]
    await app.bot.set_my_commands(commands)
    await sync_from_gist()
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
