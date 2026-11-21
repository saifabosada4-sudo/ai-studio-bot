import os
import time
import logging
import httpx
import aiosqlite
import json
import base64
import io
import re
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)
from pypdf import PdfReader
from docx import Document
from pptx import Presentation
import openpyxl

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
GH_GIST_TOKEN = os.environ.get("GH_GIST_TOKEN", "")
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
DB_PATH = os.getenv("DB_PATH", "/tmp/bot.sqlite3")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "20"))
MAX_HISTORY = 50

SYSTEM_PROMPT = (
    "أنت مساعد ذكي اسمك 'AI Studio'. أنت بوت على تطبيق تليجرام. "
    "صاحبك ومصممك هو حساب التليجرام @tumej. "
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
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload, headers=headers,
        )
        if r.status_code != 200:
            raise RuntimeError(r.text[:200])
        return r.json()["choices"][0]["message"]["content"]

# --- Gist Sync ---
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
        "ابعت أى سؤال أو ملف (PDF, Word, Excel, PowerPoint, Text) وأنا أرد عليك.\n"
        "يمكنك طلب كود أو مقال وسأرسله لك كملف جاهز للتحميل.\n\n"
        "/help — المساعدة\n"
        "/clear — مسح المحادثة"
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "الأوامر:\n"
        "/start — البداية\n"
        "/help — المساعدة\n"
        "/clear — مسح المحادثة\n\n"
        "الملفات المدعومة: PDF, DOCX, PPTX, XLSX, TXT, PY, JS, JSON, CSV."
    )

async def clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await clear_history(update.effective_user.id)
    await update.message.reply_text("تم مسح المحادثة ✦")

# دالة ذكية عشان تكتشف لو الرد فيه كود أو طويل
async def send_reply_with_file(update, reply_text):
    # لو الرد فيه كود بايثون
    if "```python" in reply_text:
        code = re.search(r"```python(.*?)```", reply_text, re.DOTALL)
        if code:
            file_content = code.group(1).strip()
            file = io.BytesIO(file_content.encode('utf-8'))
            file.name = "code.py"
            await update.message.reply_document(document=file, caption="اتفضل الكود بتاعك ✦")
            return # بنكتفي بإرسال الملف

    # لو الرد طويل جداً (أكتر من 3000 حرف)
    if len(reply_text) > 3000:
        file = io.BytesIO(reply_text.encode('utf-8'))
        file.name = "reply.txt"
        await update.message.reply_document(document=file, caption="الرد طويل، حمل الملف ده ✦")
        return

    # لو الرد عادي
    await update.message.reply_text(reply_text[:4000])

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

    await add_msg(uid, "user", text)
    history = await get_history(uid)
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        reply = await ask_ai(history)
        await add_msg(uid, "assistant", reply)
        await sync_to_gist()
        await send_reply_with_file(update, reply)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"حصل خطأ: {e}")

async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # نفس الكود القديم (مش هنغيره)
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
        await update.message.reply_text(f"عذراً، الملفات بامتداد .{ext} غير مدعومة حالياً.")
        return

    msg = await update.message.reply_text(f"جاري قراءة الملف ({file_name})... ⏳")
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")

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
            await msg.edit_text("الملف فاضي أو مش قادر أقرأ الكلام اللي جواه.")
            return

        prompt = f"هذا نص من ملف: {doc.file_name}\n\nالنص:\n{text_content[:10000]}\n\nمن فضلك لخص لي هذا الملف أو جاوبني على أسئلتي عنه."
        
        messages = [{"role": "user", "content": prompt}]
        reply = await ask_ai(messages)
        await send_reply_with_file(update, reply)
        await msg.delete() # بنمسح رسالة "جاري القراءة"
        
    except Exception as e:
        log.exception("File error")
        await msg.edit_text(f"حصل خطأ في قراءة الملف: {e}")

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
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    
    log.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
