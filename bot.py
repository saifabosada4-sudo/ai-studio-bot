import os
import time
import logging
import httpx
import aiosqlite
import base64
import io
import re
import asyncio
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)

# --- محاولة استيراد المكتبات الاختيارية ---
try:
    from pypdf import PdfReader
    from docx import Document
    from pptx import Presentation
    import openpyxl
except ImportError as e:
    logging.error(f"Missing library: {e}")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("ai_studio_bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
GH_GIST_TOKEN = os.environ.get("GH_GIST_TOKEN", "")
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/auto")
DB_PATH = os.getenv("DB_PATH", "/tmp/bot.sqlite3")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "20"))
MAX_HISTORY = 50

SYSTEM_PROMPT = (
    "أنت مساعد ذكي اسمك 'AI Studio'. أنت بوت على تطبيق تليجرام. "
    "صاحبك ومصممك هو حساب التليجرام @tumej. "
    "أنت شغال بنظام ذكاء اصطناعي متقدم. "
    "أجب بالعربية بوضوح، وكن مفيداً ودقيقاً. "
    "عندما يطلب منك كوداً، اكتبه دائماً داخل بلوكات مارك داون هكذا: ```python ... ```"
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
            "INSERT INTO usage VALUES (?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET count=count+1",
            (uid, day),
        )
        await db.commit()
        async with db.execute("SELECT count FROM usage WHERE user_id=? AND day=?", (uid, day)) as cur:
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
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
        if r.status_code != 200:
            error_data = r.json().get("error", {})
            err_msg = error_data.get("message", r.text[:200])
            raise RuntimeError(err_msg)
        return r.json()["choices"][0]["message"]["content"]

# --- مزامنة الذاكرة مع GitHub Gist ---
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

# --- إرسال الرد الذكي ---
async def send_smart_reply(update: Update, reply_text: str, status_msg):
    await status_msg.delete() # مسح رسالة الانتظار
    
    code_blocks = re.findall(r"```(?:\w+)?\n?(.*?)```", reply_text, re.DOTALL)
    if code_blocks:
        longest_code = max(code_blocks, key=len).strip()
        file = io.BytesIO(longest_code.encode('utf-8'))
        file.name = "code.txt"
        await update.message.reply_document(document=file, caption="اتفضل الكود بتاعك جاهز ✦")
        return

    if len(reply_text) > 3000:
        file = io.BytesIO(reply_text.encode('utf-8'))
        file.name = "response.txt"
        await update.message.reply_document(document=file, caption="الرد طويل، حمل الملف ده ✦")
        return

    await update.message.reply_text(reply_text[:4000])

# --- الأوامر الأساسية ---
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بك فى AI Studio ✦\n"
        "أنا مساعدك الذكي، صممني حساب التليجرام @tumej مخصوص عشان أساعدك.\n\n"
        "ابعت أى سؤال أو ملف وأنا أرد عليك.\n\n"
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

# --- النصوص ---
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    if not text or len(text) > 4000:
        await update.message.reply_text("الرسالة طويلة جداً (الحد 4000 حرف).")
        return

    count = await bump_usage(uid)
    if count > DAILY_LIMIT:
        await update.message.reply_text(f"وصلت للحد اليومى ({DAILY_LIMIT} رسالة). ارجع بكرة ✦")
        return

    # هنا هنبعت الرسالة اللي بتدل على التحميل
    status_msg = await update.message.reply_text("⏳ جاري التفكير...")

    await add_msg(uid, "user", text)
    history = await get_history(uid)
    try:
        reply = await ask_ai(history)
        await add_msg(uid, "assistant", reply)
        await sync_to_gist()
        await send_smart_reply(update, reply, status_msg)
    except Exception as e:
        log.exception("AI error")
        await status_msg.edit_text(f"عذراً، حدث خطأ: {e}")

# --- الملفات ---
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    count = await bump_usage(uid)
    if count > DAILY_LIMIT:
        await update.message.reply_text(f"وصلت للحد اليومى ({DAILY_LIMIT}). ارجع بكرة ✦")
        return

    doc = update.message.document
    file_name = doc.file_name.lower()
    ext = file_name.split('.')[-1]

    supported_exts = ['pdf', 'docx', 'txt', 'pptx', 'xlsx', 'py', 'js', 'html', 'css', 'json', 'csv', 'md', 'xml']
    if ext not in supported_exts:
        await update.message.reply_text(f"عذراً، الملفات بامتداد .{ext} غير مدعومة.")
        return

    status_msg = await update.message.reply_text("⏳ جاري قراءة الملف...")

    try:
        file = await doc.get_file()
        file_bytes = await file.download_as_bytearray()
        text_content = ""

        if ext == 'pdf':
            pdf = PdfReader(io.BytesIO(file_bytes))
            for page in pdf.pages:
                text_content += page.extract_text() or ""
        elif ext == 'docx':
            docx = Document(io.BytesIO(file_bytes))
            for para in docx.paragraphs:
                text_content += para.text + "\n"
        elif ext == 'pptx':
            prs = Presentation(io.BytesIO(file_bytes))
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text_content += shape.text + "\n"
        elif ext == 'xlsx':
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    row_text = " | ".join([str(cell) for cell in row if cell is not None])
                    if row_text: text_content += row_text + "\n"
        else: 
            text_content = file_bytes.decode("utf-8", errors="ignore")

        if not text_content.strip():
            await status_msg.edit_text("الملف فاضي أو لا يحتوي على نص.")
            return

        prompt = f"هذا نص من ملف: {doc.file_name}\n\nالنص:\n{text_content[:10000]}\n\nمن فضلك لخص لي هذا الملف أو جاوبني على أسئلتي عنه."
        
        messages = [{"role": "user", "content": prompt}]
        reply = await ask_ai(messages)
        await send_smart_reply(update, reply, status_msg)
        
    except Exception as e:
        log.exception("File error")
        await status_msg.edit_text(f"عذراً، حدث خطأ في قراءة الملف: {e}")

async def post_init(app):
    commands = [
        BotCommand("start", "بدء المحادثة"),
        BotCommand("help", "المساعدة"),
        BotCommand("clear", "مسح المحادثة")
    ]
    await app.bot.set_my_commands(commands)
    await sync_from_gist()
    await init_db()

def main():
    if not BOT_TOKEN or not OPENROUTER_API_KEY:
        log.critical("BOT_TOKEN or OPENROUTER_API_KEY is missing!")
        return

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    
    log.info("AI Studio Bot is starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
