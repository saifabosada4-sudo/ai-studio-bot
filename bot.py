import os
import time
import logging
import httpx
import aiosqlite
import base64
import io
import re
from datetime import datetime
from telegram import Update, BotCommand, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    PreCheckoutQueryHandler, CallbackQueryHandler
)

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
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7112513322")) 
MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
DB_PATH = os.getenv("DB_PATH", "/tmp/bot.sqlite3")

FREE_MSG_LIMIT = 100
FREE_FILE_LIMIT = 15
VIP_PRICE_STARS = 50
VIP_PRICE_CASH = 50

SYSTEM_PROMPT = (
    "أنت مساعد ذكي اسمك 'AI Studio'. صاحبك ومصممك هو حساب التليجرام @tumej. "
    "أنت بوت محترف ومتقدم جداً، ومهمتك تنفيذ أوامر المستخدم بدقة واحترافية. "
    "أجب بالعربية الواضحة (أو بنفس لغة المستخدم)، وكن مفيداً، دقيقاً، وخدوم، ومختصراً. "
    "ممنوع تماماً أن تقول للمستخدم (انسخ النص التالي) أو (لا أستطيع إنشاء ملف). "
    "إذا طلب منك المستخدم كتابة (شعر، مقال، قصة، كود برمجي، أو أي نص طويل)، "
    "ضع المحتوى بالكامل داخل بلوك كود هكذا: ```text [المحتوى هنا] ```، "
    "وأن تكتفي ببلوك الكود فقط، بدون أي كلام إضافي أو شرح أو خطوات."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS usage (
 user_id INTEGER, day TEXT, msg_count INTEGER DEFAULT 0, file_count INTEGER DEFAULT 0, PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS users (
 user_id INTEGER PRIMARY KEY, first_name TEXT, vip_until INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS support_cooldown (
 user_id INTEGER PRIMARY KEY, last_time INTEGER DEFAULT 0);
"""

# ----------------- قاعدة البيانات -----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT vip_until, first_name FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
            return row if row else (0, "")

async def update_user(uid, name=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?)", (uid, name))
        if name:
            await db.execute("UPDATE users SET first_name=? WHERE user_id=?", (name, uid))
        await db.commit()

async def set_vip(uid, days=30):
    async with aiosqlite.connect(DB_PATH) as db:
        vip_until = int(time.time()) + (days * 86400)
        await db.execute(
            "INSERT INTO users (user_id, vip_until) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET vip_until=excluded.vip_until", 
            (uid, vip_until)
        )
        await db.commit()

async def check_usage(uid):
    day = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT msg_count, file_count FROM usage WHERE user_id=? AND day=?", (uid, day)) as cur:
            row = await cur.fetchone()
            return row if row else (0, 0)

async def bump_usage(uid, is_file=False):
    day = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        if is_file:
            await db.execute("INSERT INTO usage (user_id, day, file_count) VALUES (?, ?, 1) ON CONFLICT(user_id, day) DO UPDATE SET file_count = file_count + 1", (uid, day))
        else:
            await db.execute("INSERT INTO usage (user_id, day, msg_count) VALUES (?, ?, 1) ON CONFLICT(user_id, day) DO UPDATE SET msg_count = msg_count + 1", (uid, day))
        await db.commit()

# ----------------- الذاكرة (Gist) -----------------
async def sync_from_gist():
    if not GH_GIST_TOKEN: return
    headers = {"Authorization": f"token {GH_GIST_TOKEN}"}
    async with httpx.AsyncClient() as client:
        if os.path.exists("/tmp/gist_id.txt"):
            with open("/tmp/gist_id.txt", "r") as f: gist_id = f.read().strip()
            r = await client.get(f"https://api.github.com/gists/{gist_id}", headers=headers)
        else:
            r = await client.post("https://api.github.com/gists", headers=headers, json={"description": "Bot DB", "public": False, "files": {"bot.sqlite3": {"content": ""}}})
            if r.status_code in [200, 201]:
                with open("/tmp/gist_id.txt", "w") as f: f.write(r.json()["id"])
            else: return
        if r.status_code == 200:
            content = r.json()["files"]["bot.sqlite3"]["content"]
            if content:
                with open(DB_PATH, "wb") as f: f.write(base64.b64decode(content))

async def sync_to_gist():
    if not GH_GIST_TOKEN or not os.path.exists("/tmp/gist_id.txt") or not os.path.exists(DB_PATH): return
    with open("/tmp/gist_id.txt", "r") as f: gist_id = f.read().strip()
    with open(DB_PATH, "rb") as f: content = base64.b64encode(f.read()).decode()
    headers = {"Authorization": f"token {GH_GIST_TOKEN}"}
    async with httpx.AsyncClient() as client:
        await client.patch(f"https://api.github.com/gists/{gist_id}", headers=headers, json={"files": {"bot.sqlite3": {"content": content}}})

# ----------------- الذكاء الاصطناعي (مع max_tokens) -----------------
async def ask_ai(messages):
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
        "max_tokens": 1000
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
        if r.status_code != 200: raise RuntimeError(r.text[:200])
        return r.json()["choices"][0]["message"]["content"]

# ----------------- إرسال الردود -----------------
async def send_smart_reply(update: Update, reply_text: str, status_msg):
    try: await status_msg.delete()
    except: pass
    code_blocks = re.findall(r"```(?:\w+)?\n?(.*?)```", reply_text, re.DOTALL)
    if code_blocks:
        longest_code = max(code_blocks, key=len).strip()
        file = io.BytesIO(longest_code.encode('utf-8'))
        file.name = "AI_Studio_File.txt"
        await update.message.reply_document(document=file, caption="خد الملف بتاعك اهو ✦")
    elif len(reply_text) > 3000:
        file = io.BytesIO(reply_text.encode('utf-8'))
        file.name = "response.txt"
        await update.message.reply_document(document=file, caption="الرد طويل، حمل الملف ده ✦")
    else:
        await update.message.reply_text(reply_text[:4000])

# ----------------- الأوامر -----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update_user(update.effective_user.id, update.effective_user.first_name)
    await update.message.reply_text(
        "أهلاً بك فى AI Studio ✦\n"
        "أنا مساعدك الذكي، صممني حساب التليجرام @tumej مخصوص عشان أساعدك.\n\n"
        "/vip — الاشتراك المميز\n"
        "/profile — حسابك وإحصائياتك\n"
        "/support — الدعم الفني\n"
        "/help — المساعدة"
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "الأوامر المتاحة:\n"
        "/start — البداية\n"
        "/vip — الاشتراك المميز\n"
        "/profile — بروفايلك\n"
        "/support — الدعم الفني\n"
        "/cancel — إلغاء أي عملية\n"
        "/help — المساعدة"
    )

async def profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name
    await update_user(uid, name)
    vip_until, _ = await get_user(uid)
    msgs, files = await check_usage(uid)
    is_vip = vip_until > time.time()
    vip_status = "👑 VIP" if is_vip else "👤 عادي"
    vip_days = f"\n📅 فاضلك: {max(0, (vip_until - int(time.time())) // 86400)} يوم" if is_vip else ""
    msg_text = "بلا حدود ♾️" if is_vip else f"{msgs} / {FREE_MSG_LIMIT}"
    file_text = "بلا حدود ♾️" if is_vip else f"{files} / {FREE_FILE_LIMIT}"
    await update.message.reply_text(
        f"👤 اسمك: {name}\n"
        f"🆔 الآي دي بتاعك: `{uid}`\n"
        f"⭐ حالتك: {vip_status}{vip_days}\n"
        f"💬 رسايل النهاردة: {msg_text}\n"
        f"📁 ملفات النهاردة: {file_text}",
        parse_mode="Markdown"
    )

async def vip_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⭐ دفع بـ 50 نجمة تليجرام", callback_data="pay_stars")],
        [InlineKeyboardButton("💵 دفع كاش (اتصالات)", callback_data="pay_cash")]
    ]
    await update.message.reply_text(
        "👑 **اشتراك VIP لمدة 30 يوم:**\n\n"
        "المميزات:\n✅ رسايل بلا حدود\n✅ ملفات بلا حدود\n✅ أولوية في الرد\n✅ ملفات حصرية\n\n"
        "اختر طريقة الدفع:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ----------------- الدفع بالنجوم -----------------
async def pay_stars_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await ctx.bot.send_invoice(
        chat_id=query.message.chat_id, title="اشتراك VIP - AI Studio",
        description="تفعيل اشتراك VIP لمدة 30 يوم", payload="vip_30_days",
        provider_token="", currency="XTR", prices=[LabeledPrice("اشتراك VIP", VIP_PRICE_STARS)]
    )

async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.successful_payment.invoice_payload != "vip_30_days": return
    uid = update.effective_user.id
    await set_vip(uid, 30)
    await sync_to_gist()
    await update.message.reply_text("🎉 ألف مبروك! تم تفعيل اشتراك VIP بنجاح لمدة 30 يوم. استمتع بكل المميزات!")
    await ctx.bot.send_message(ADMIN_ID, f"💰 تم شراء اشتراك VIP بالنجوم!\nالمستخدم: {update.effective_user.first_name}\nID: `{uid}`", parse_mode="Markdown")

# ----------------- الدفع الكاش -----------------
async def pay_cash_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("✅ نعم، اطلب الرقم من الإدارة", callback_data="request_cash")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_cash")]
    ]
    await query.message.reply_text("💵 **الدفع الكاش:**\n\nهل تريد طلب رقم الدفع من الإدارة للتحويل؟", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def cancel_cash_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("❌ تم إلغاء طلب الدفع.")

async def request_cash_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    name = query.from_user.first_name
    keyboard = [[
        InlineKeyboardButton("✅ إرسال الرقم", callback_data=f"send_cash_{uid}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"reject_cash_{uid}")
    ]]
    await ctx.bot.send_message(chat_id=ADMIN_ID, text=f"💵 **طلب دفع كاش جديد:**\n👤 الاسم: {name}\n🆔 ID: `{uid}`\n\nهل تريد إرسال الرقم له؟", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.message.edit_text("✅ تم إرسال طلبك للإدارة. يرجى الانتظار.")

async def admin_cash_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    
    parts = query.data.rsplit("_", 1)
    action = parts[0]
    uid = int(parts[1])
    
    if action == "send_cash":
        ctx.user_data["waiting_to_send_number"] = uid
        await query.message.reply_text("✍️ اكتب رقم اتصالات كاش اللي هتبعته للمستخدم:")
        await query.message.edit_reply_markup(reply_markup=None)
    elif action == "reject_cash":
        await ctx.bot.send_message(uid, "❌ تم رفض طلبك. يرجى المحاولة مرة أخرى لاحقاً.")
        await query.message.edit_text("❌ تم رفض الطلب.")
        await query.message.edit_reply_markup(reply_markup=None)

async def send_number_to_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.user_data.get("waiting_to_send_number")
    if not uid: return False
    ctx.user_data["waiting_to_send_number"] = None
    ctx.user_data[f"waiting_for_receipt_{uid}"] = True
    await ctx.bot.send_message(uid, f"💵 **رقم الدفع (اتصالات كاش):**\n\n`{update.message.text}`\n\nمن فضلك قم بالتحويل، ثم ابعت صورة التحويل هنا.", parse_mode="Markdown")
    await update.message.reply_text("✅ تم إرسال الرقم للمستخدم.")
    return True

# ----------------- الإيصالات -----------------
async def handle_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.user_data.get(f"waiting_for_receipt_{uid}"): return
    
    ctx.user_data[f"waiting_for_receipt_{uid}"] = False
    photo = update.message.photo[-1].file_id
    name = update.effective_user.first_name
    keyboard = [[
        InlineKeyboardButton("✅ قبول", callback_data=f"approve_{uid}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"reject_{uid}")
    ]]
    await ctx.bot.send_photo(chat_id=ADMIN_ID, photo=photo, caption=f"🧾 إيصال دفع جديد:\n👤 الاسم: {name}\n🆔 ID: `{uid}`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await update.message.reply_text("✅ تم إرسال الإيصال، يرجى الانتظار حتى يتم المراجعة.")

async def admin_approval_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    
    parts = query.data.rsplit("_", 1)
    action = parts[0]
    uid = int(parts[1])
    
    if action == "approve":
        await set_vip(uid, 30)
        await sync_to_gist()
        await ctx.bot.send_message(uid, "🎉 ألف مبروك! تم قبول الدفع وتفعيل VIP لمدة 30 يوم. استمتع!")
        await query.edit_message_caption(caption=f"✅ تم قبول الدفع وتفعيل VIP للمستخدم {uid}")
    elif action == "reject":
        await ctx.bot.send_message(uid, "❌ عذراً، تم رفض الإيصال. يرجى التأكد من التحويل والمحاولة مرة أخرى.")
        await query.edit_message_caption(caption=f"❌ تم رفض الإيصال للمستخدم {uid}")

# ----------------- الدعم -----------------
async def support_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_time FROM support_cooldown WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
            last_time = row[0] if row else 0
    if time.time() - last_time < 7200:
        await update.message.reply_text("⏳ عذراً، لا يمكنك إرسال طلب دعم جديد قبل مرور ساعتين من طلبك السابق.")
        return
    ctx.user_data["waiting_for_support"] = True
    await update.message.reply_text("📝 اكتب رسالتك أو مشكلتك دلوقتى، وهتتبعت للإدارة على طول. (لو غيرت رأيك اكتب /cancel).")

async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["waiting_for_support"] = False
    ctx.user_data["waiting_to_send_number"] = None
    ctx.user_data["admin_reply_to"] = None
    await update.message.reply_text("✅ تم إلغاء العملية الحالية.")

async def handle_support_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("waiting_for_support"): return
    ctx.user_data["waiting_for_support"] = False
    uid = update.effective_user.id
    name = update.effective_user.first_name
    text = update.message.text
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO support_cooldown (user_id, last_time) VALUES (?, ?)", (uid, int(time.time())))
        await db.commit()
    vip_until, _ = await get_user(uid)
    is_vip = vip_until > time.time()
    vip_tag = "👑 VIP" if is_vip else "👤 عادي"
    keyboard = [[InlineKeyboardButton("↩️ رد على المستخدم", callback_data=f"reply_{uid}")]]
    await ctx.bot.send_message(chat_id=ADMIN_ID, text=f"📩 رسالة دعم جديدة\n👤 الاسم: {name}\n🆔 ID: `{uid}`\n⭐ الحالة: {vip_tag}\n\n💬 الرسالة:\n{text}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await update.message.reply_text("✅ تم إرسال رسالتك للإدارة، وهيتم الرد عليك في أقرب وقت.")

async def admin_reply_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    parts = query.data.rsplit("_", 1)
    uid = int(parts[1])
    ctx.user_data["admin_reply_to"] = uid
    await query.message.reply_text("✍️ اكتب الرد اللي عايز تبعته للمستخدم دلوقتى:")

async def send_admin_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.user_data.get("admin_reply_to")
    if not uid: return False
    ctx.user_data["admin_reply_to"] = None
    await ctx.bot.send_message(uid, f"📬 **رد الإدارة على رسالتك:**\n\n{update.message.text}", parse_mode="Markdown")
    await update.message.reply_text("✅ تم إرسال الرد للمستخدم.")
    return True

# ----------------- المعالجة الأساسية -----------------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await send_admin_reply(update, ctx): return
    if await send_number_to_user(update, ctx): return
    if ctx.user_data.get("waiting_for_support"):
        await handle_support_msg(update, ctx)
        return
    
    uid = update.effective_user.id
    text = update.message.text
    await update_user(uid, update.effective_user.first_name)
    vip_until, _ = await get_user(uid)
    is_vip = vip_until > time.time()
    msgs, _ = await check_usage(uid)
    if not is_vip and msgs >= FREE_MSG_LIMIT:
        await update.message.reply_text(f"خلصت حد النهاردة ({FREE_MSG_LIMIT} رسالة). تعالي بكرة أو اشترك VIP من /vip.")
        return
    
    status_msg = await update.message.reply_text("⏳ جاري التفكير...")
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO messages (user_id, role, content, ts) VALUES (?,?,?,?)", (uid, "user", text, int(time.time())))
        await db.commit()
        async with db.execute("SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)) as cur:
            rows = await cur.fetchall()
    history = [{"role": r, "content": c} for r, c in reversed(rows)]
    
    try:
        reply = await ask_ai(history)
        await bump_usage(uid, is_file=False)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO messages (user_id, role, content, ts) VALUES (?,?,?,?)", (uid, "assistant", reply, int(time.time())))
            await db.commit()
        await sync_to_gist()
        await send_smart_reply(update, reply, status_msg)
    except Exception as e:
        await status_msg.edit_text(f"عذراً، حدث خطأ: {e}")

async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    if not doc or not doc.file_name:
        await update.message.reply_text("الملف غير صالح.")
        return
    file_name = doc.file_name.lower()
    ext = file_name.split('.')[-1]
    await update_user(uid, update.effective_user.first_name)
    vip_until, _ = await get_user(uid)
    is_vip = vip_until > time.time()
    _, files = await check_usage(uid)
    if not is_vip and files >= FREE_FILE_LIMIT:
        await update.message.reply_text(f"خلصت حد الملفات النهاردة ({FREE_FILE_LIMIT} ملف). تعالي بكرة أو اشترك VIP من /vip.")
        return
    status_msg = await update.message.reply_text("⏳ جاري قراءة الملف...")
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        file = await doc.get_file()
        file_bytes = await file.download_as_bytearray()
        text_content = ""
        if ext == 'pdf':
            pdf = PdfReader(io.BytesIO(file_bytes))
            for page in pdf.pages: text_content += page.extract_text() or ""
        elif ext == 'docx':
            docx = Document(io.BytesIO(file_bytes))
            for para in docx.paragraphs: text_content += para.text + "\n"
        elif ext == 'pptx':
            prs = Presentation(io.BytesIO(file_bytes))
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"): text_content += shape.text + "\n"
        elif ext == 'xlsx':
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    row_text = " | ".join([str(cell) for cell in row if cell is not None])
                    if row_text: text_content += row_text + "\n"
        else: text_content = file_bytes.decode("utf-8", errors="ignore")
        if not text_content.strip():
            await status_msg.edit_text("الملف فاضي أو لا يحتوي على نص.")
            return
        prompt = f"هذا نص من ملف: {doc.file_name}\n\nالنص:\n{text_content[:10000]}\n\nمن فضلك لخص لي هذا الملف."
        messages = [{"role": "user", "content": prompt}]
        reply = await ask_ai(messages)
        await bump_usage(uid, is_file=True)
        await sync_to_gist()
        await send_smart_reply(update, reply, status_msg)
    except Exception as e:
        await status_msg.edit_text(f"عذراً، حدث خطأ: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error(f"Update {update} caused error {context.error}")

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "بداية البوت"), BotCommand("vip", "الاشتراك المميز"),
        BotCommand("profile", "حسابك وإحصائياتك"), BotCommand("support", "الدعم الفني"),
        BotCommand("cancel", "إلغاء أي عملية"), BotCommand("help", "المساعدة")
    ])
    await sync_from_gist()
    await init_db()

def main():
    if not BOT_TOKEN or not OPENROUTER_API_KEY: return
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_error_handler(error_handler)
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("vip", vip_menu))
    app.add_handler(CommandHandler("support", support_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    
    app.add_handler(CallbackQueryHandler(pay_stars_cb, pattern="^pay_stars$"))
    app.add_handler(CallbackQueryHandler(pay_cash_cb, pattern="^pay_cash$"))
    app.add_handler(CallbackQueryHandler(cancel_cash_cb, pattern="^cancel_cash$"))
    app.add_handler(CallbackQueryHandler(request_cash_cb, pattern="^request_cash$"))
    
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    
    app.add_handler(CallbackQueryHandler(admin_cash_action, pattern="^(send_cash|reject_cash)_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_approval_cb, pattern="^(approve|reject)_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_reply_cb, pattern="^reply_\\d+$"))
    
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_receipt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
