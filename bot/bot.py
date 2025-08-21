# стандартная библиотека
import asyncio
import os
import sqlite3
import time
import re

# сторонние библиотеки
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI

def format_answer(text: str) -> str:
    lines = text.splitlines()
    in_code = False
    out = []

    header_re = re.compile(r"^(#{1,6})\s+(.+)$")  # # .. ## .. ###### ..

    for line in lines:
        stripped = line.strip()
        # переключаемся, если встретили границу кода ``` (любой язык)
        if stripped.startswith("```"):
            in_code = not in_code
            out.append(line)
            continue

        if not in_code:
            m = header_re.match(line)
            if m:
                # берём текст заголовка и делаем жирным
                title = m.group(2).strip()
                out.append(f"**{title}**")
                continue

        out.append(line)

    return "\n".join(out)

# ===== ENV =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MODEL = os.getenv("OPENAI_MODEL_CHAT", "gpt-4o")
DB_PATH = os.getenv("BOT_DB_PATH", "/data/bot.sqlite")
DAILY_LIMIT = int(os.getenv("USER_DAILY_TOKENS", "100000"))
ALLOWED = {x.strip() for x in os.getenv("ALLOWED_TG_IDS", "").split(",") if x.strip()}

# ===== INIT =====
client = OpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

# ===== DB =====
def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS sessions(
        user_id INTEGER, chat_id INTEGER, updated_at INTEGER,
        PRIMARY KEY(user_id, chat_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, chat_id INTEGER, role TEXT, content TEXT, created_at INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS active_chat(
        user_id INTEGER PRIMARY KEY, chat_id INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS quotas(
        user_id INTEGER, yyyymmdd TEXT, used_tokens INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, yyyymmdd)
    )""")
    return c

def access(uid: int) -> bool:
    return (not ALLOWED) or (str(uid) in ALLOWED)

def set_active(c, uid, chat_id):
    c.execute("INSERT OR REPLACE INTO active_chat(user_id, chat_id) VALUES(?,?)", (uid, chat_id))
    c.commit()

def ensure_active_chat(c, uid) -> int:
    row = c.execute("SELECT chat_id FROM active_chat WHERE user_id=?", (uid,)).fetchone()
    if row:
        return row[0]
    return new_chat(c, uid)

def new_chat(c, uid) -> int:
    new_id = c.execute("SELECT COALESCE(MAX(chat_id),0)+1 FROM sessions WHERE user_id=?", (uid,)).fetchone()[0]
    now = int(time.time())
    c.execute("INSERT OR REPLACE INTO sessions(user_id, chat_id, updated_at) VALUES(?,?,?)", (uid, new_id, now))
    set_active(c, uid, new_id)
    c.commit()
    return new_id

def list_chats(c, uid):
    return c.execute("SELECT chat_id, updated_at FROM sessions WHERE user_id=? ORDER BY updated_at DESC", (uid,)).fetchall()

def history(c, uid, chat_id, limit=None):
    q = "SELECT role, content FROM messages WHERE user_id=? AND chat_id=? ORDER BY id"
    if limit:
        q += f" LIMIT {limit}"
    rows = c.execute(q, (uid, chat_id)).fetchall()
    return [{"role": r, "content": t} for r, t in rows]

def add_msg(c, uid, chat_id, role, content):
    now = int(time.time())
    c.execute("INSERT INTO messages(user_id, chat_id, role, content, created_at) VALUES(?,?,?,?,?)",
              (uid, chat_id, role, content, now))
    c.execute("UPDATE sessions SET updated_at=? WHERE user_id=? AND chat_id=?", (now, uid, chat_id))
    c.commit()

def can_spend(c, uid, tokens):
    key = time.strftime("%Y%m%d")
    row = c.execute("SELECT used_tokens FROM quotas WHERE user_id=? AND yyyymmdd=?", (uid, key)).fetchone()
    used = row[0] if row else 0
    return used + tokens <= DAILY_LIMIT

def add_tokens(c, uid, tokens):
    key = time.strftime("%Y%m%d")
    c.execute("""INSERT INTO quotas(user_id, yyyymmdd, used_tokens)
        VALUES(?,?,COALESCE((SELECT used_tokens FROM quotas WHERE user_id=? AND yyyymmdd=?),0)+?)
        ON CONFLICT(user_id, yyyymmdd) DO UPDATE SET used_tokens=used_tokens+?""",
        (uid, key, uid, key, tokens, tokens))
    c.commit()

# ===== UI =====
def menu_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🆕 Новый диалог", callback_data="new_chat"),
            InlineKeyboardButton(text="📜 Мои диалоги", callback_data="list_chats"),
        ],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
            InlineKeyboardButton(text="📕 База знаний", callback_data="menu_kb"),
        ],
    ])

def menu_manage():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📜 Мои диалоги", callback_data="list_chats"),
            InlineKeyboardButton(text="🆕 Новый диалог", callback_data="new_chat"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")]
    ])

def reply_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🆕 Новый диалог", callback_data="new_chat"),
            InlineKeyboardButton(text="📜 Диалоги", callback_data="list_chats"),
        ]
    ])

# ===== COMMANDS =====
@dp.message(Command("start", "menu", "gpt"))
async def cmd_start(m: Message):
    if not access(m.from_user.id):
        await m.reply("🚫 Доступ ограничен. Обратитесь к администратору.")
        return
    c = db()
    ensure_active_chat(c, m.from_user.id)
    await m.reply(
        "👋 Привет! Я *ChatGPT для красавчиков из Mos-GSM* в Telegram.\n"
        "Пиши вопрос или пользуйся меню ниже.",
        reply_markup=menu_main()
    )

@dp.message(Command("new"))
async def new_cmd(m: Message):
    if not access(m.from_user.id): return
    c = db()
    cid = new_chat(c, m.from_user.id)
    await m.reply(f"🆕 Создан новый диалог *#{cid}*. Пиши сообщение.", reply_markup=reply_menu())

@dp.message(Command("chats"))
async def chats_cmd(m: Message):
    if not access(m.from_user.id): return
    c = db()
    chats = list_chats(c, m.from_user.id)
    if not chats:
        await m.reply("Пока нет диалогов. Нажми *Новый диалог*.", reply_markup=menu_main())
        return
    lines = ["📜 *Ваши диалоги:*"]
    active = ensure_active_chat(c, m.from_user.id)
    for chat_id, upd in chats:
        last = c.execute("SELECT content FROM messages WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT 1",
                         (m.from_user.id, chat_id)).fetchone()
        preview = (last[0][:40] + "…") if last else "(пусто)"
        date_str = time.strftime("%d.%m %H:%M", time.localtime(upd))
        mark = "✅" if chat_id == active else " "
        lines.append(f"{mark} #{chat_id} — {date_str} — {preview}")
    lines.append("\nПереключиться: `/use <номер>`")
    await m.reply("\n".join(lines))

@dp.message(Command("use"))
async def use_cmd(m: Message):
    if not access(m.from_user.id): return
    parts = m.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.reply("Использование: `/use <номер>`", reply_markup=menu_main())
        return
    chat_id = int(parts[1])
    c = db()
    exists = c.execute("SELECT 1 FROM sessions WHERE user_id=? AND chat_id=?", (m.from_user.id, chat_id)).fetchone()
    if not exists:
        await m.reply("❌ Такого диалога нет.", reply_markup=menu_main())
        return
    set_active(c, m.from_user.id, chat_id)
    await m.reply(f"✅ Переключено на диалог *#{chat_id}*.", reply_markup=reply_menu())

# ===== CALLBACKS =====
@dp.callback_query(F.data.in_({"menu_main", "chat_mode"}))
async def cb_main(q: CallbackQuery):
    c = db()
    ensure_active_chat(c, q.from_user.id)
    await q.message.edit_text(
        "👋 Привет! Я *ChatGPT для красавчиков из Mos-GSM* в Telegram.\n"
        "Пиши вопрос или пользуйся меню ниже.",
        reply_markup=menu_main())
    await q.answer()

@dp.callback_query(F.data == "menu_manage")
async def cb_manage(q: CallbackQuery):
    await q.message.edit_text("🤖 Управление:", reply_markup=menu_manage())
    await q.answer()

@dp.callback_query(F.data == "menu_profile")
async def cb_profile(q: CallbackQuery):
    c = db()
    key = time.strftime("%Y%m%d")
    row = c.execute("SELECT used_tokens FROM quotas WHERE user_id=? AND yyyymmdd=?", (q.from_user.id, key)).fetchone()
    used = row[0] if row else 0
    text = (f"👤 *Профиль*\n"
            f"• Модель: `{MODEL}`\n"
            f"• Лимит на сегодня: *{DAILY_LIMIT}* токенов\n"
            f"• Использовано сегодня: *{used}* токенов")
    await q.message.edit_text(text, reply_markup=menu_manage())
    await q.answer()

@dp.callback_query(F.data == "menu_kb")
async def cb_kb(q: CallbackQuery):
    await q.message.edit_text(
        "📕 База знаний пока не подключена.\n"
        "Можно загрузить регламенты/FAQ и искать по ним — добавим позже.",
        reply_markup=menu_manage()
    )
    await q.answer()

@dp.callback_query(F.data == "new_chat")
async def cb_new_chat(q: CallbackQuery):
    c = db()
    cid = new_chat(c, q.from_user.id)
    await q.message.answer(f"🆕 Создан новый диалог *#{cid}*. Пишите сообщение.", reply_markup=reply_menu())
    await q.answer()

@dp.callback_query(F.data == "list_chats")
async def cb_list_chats(q: CallbackQuery):
    c = db()
    chats = list_chats(c, q.from_user.id)
    if not chats:
        await q.message.answer("Пока нет диалогов. Нажмите *Новый диалог*.", reply_markup=menu_main())
        await q.answer()
        return
    lines = ["📜 *Ваши диалоги:*"]
    active = ensure_active_chat(c, q.from_user.id)
    for chat_id, upd in chats:
        last = c.execute("SELECT content FROM messages WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT 1",
                         (q.from_user.id, chat_id)).fetchone()
        preview = (last[0][:40] + "…") if last else "(пусто)"
        date_str = time.strftime("%d.%m %H:%M", time.localtime(upd))
        mark = "✅" if chat_id == active else " "
        lines.append(f"{mark} #{chat_id} — {date_str} — {preview}")
    lines.append("\nПереключиться: `/use <номер>`")
    await q.message.answer("\n".join(lines))
    await q.answer()

# ===== CHAT =====
@dp.message(F.text)
async def chat(m: Message):
    if not access(m.from_user.id):
        await m.reply("🚫 Доступ ограничен.")
        return

    c = db()
    uid = m.from_user.id
    chat_id = ensure_active_chat(c, uid)

    add_msg(c, uid, chat_id, "user", m.text)

    msgs = history(c, uid, chat_id)
    est_in = sum(len(x['content']) // 4 for x in msgs)
    if not can_spend(c, uid, est_in):
        await m.reply("❌ Превышен лимит токенов на сегодня.")
        return

    try:
        await bot.send_chat_action(chat_id=m.chat.id, action="typing")
    except:
        pass

    try:
        system_prompt = {
            "role": "system",
            "content": (
                "Ты умный ассистент компании MOS‑GSM. Отвечай как ChatGPT Plus: "
                "полно и по делу, сохраняй форматирование (Markdown), используй списки/заголовки, emoji, ссылки и блоки кода."
                "ВНИМАНИЕ: не используй #-заголовки. Все заголовки оформляй просто жирным (**Заголовок**)."
            )
        }
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[system_prompt] + msgs
        )
        answer = resp.choices[0].message.content
        answer = format_answer(answer)  # 🔹 вот тут добавляем фильтрацию
        usage = resp.usage.total_tokens if resp.usage else est_in

        add_msg(c, uid, chat_id, "assistant", answer)
        add_tokens(c, uid, usage)

        await m.reply(
    format_answer(answer),
    reply_markup=reply_menu(),
    parse_mode="Markdown"
)
    except Exception as e:
        await m.reply(f"❌ Ошибка OpenAI: `{e}`", reply_markup=reply_menu())

#Ниже то, что касается отрпавки и получения файлов

import aiofiles
from io import BytesIO
from PIL import Image
from PyPDF2 import PdfReader
from docx import Document as DocxDocument
import base64

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "50"))
OCR_ENGINE = os.getenv("OCR_ENGINE", "openai").lower()
OCR_LANG = os.getenv("OCR_LANG", "rus+eng")

async def download_by_file_id(file_id: str, prefix: str = "file") -> tuple[str, bytes, str]:
    """
    Скачивает файл по file_id. Возвращает: (имя_файла, bytes, ext)
    """
    f = await bot.get_file(file_id)
    ext = os.path.splitext(f.file_path)[1] or ""
    # aiogram v3: скачивание по file_path
    bio = BytesIO()
    await bot.download_file(f.file_path, destination=bio)
    content = bio.getvalue()
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"Файл больше {MAX_FILE_MB} МБ")
    filename = f"{prefix}_{int(time.time())}{ext}"
    return filename, content, ext.lower()

async def save_bytes_local(filename: str, content: bytes) -> str:
    os.makedirs("files", exist_ok=True)
    full = os.path.join("files", filename)
    async with aiofiles.open(full, "wb") as f:
        await f.write(content)
    return full

def extract_text_from_pdf_bytes(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    out = []
    for p in reader.pages:
        out.append(p.extract_text() or "")
    return "\n".join(out).strip()

def extract_text_from_docx_bytes(content: bytes) -> str:
    bio = BytesIO(content)
    doc = DocxDocument(bio)
    return "\n".join(p.text for p in doc.paragraphs).strip()

def ocr_tesseract_image_bytes(content: bytes, lang: str = OCR_LANG) -> str:
    img = Image.open(BytesIO(content))
    import pytesseract
    return pytesseract.image_to_string(img, lang=lang).strip()

def ocr_openai_image_bytes(content: bytes) -> tuple[str, int]:
    """
    Используем GPT-4o для OCR/рукописей. Возвращаем (text, used_tokens).
    """
    b64 = base64.b64encode(content).decode("utf-8")
    # Chat Completions с изображением
    resp = client.chat.completions.create(
        model=MODEL,  # gpt-4o
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "Извлеки весь текст с изображения. Сохрани строки и порядок. Без комментариев."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            ]
        }]
    )
    text = resp.choices[0].message.content or ""
    used = resp.usage.total_tokens if resp.usage else 0
    return text.strip(), used

def guess_mediatype(ext: str, mime: str | None = None) -> str:
    ext = (ext or "").lower()
    if mime and "pdf" in mime: return "pdf"
    if ext in [".pdf"]: return "pdf"
    if ext in [".docx"]: return "docx"
    if ext in [".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"]: return "image"
    return "bin"

# Ниже Хендлеры: документы и фото

@dp.message(F.document)
async def on_document(m: Message):
    if not access(m.from_user.id):
        await m.reply("🚫 Доступ ограничен.")
        return

    doc = m.document
    try:
        await bot.send_chat_action(m.chat.id, "upload_document")
    except:
        pass

    try:
        filename, content, ext = await download_by_file_id(doc.file_id, prefix="doc")
        path = await save_bytes_local(filename, content)
        kind = guess_mediatype(ext, doc.mime_type)

        # Базовый ответ
        base_info = f"📥 Документ: *{doc.file_name or filename}*\nТип: `{kind}`\nСохранено: `{path}`"

        # Извлечение текста по типу
        extracted = ""
        used_tokens = 0

        if kind == "pdf":
            extracted = extract_text_from_pdf_bytes(content)
        elif kind == "docx":
            extracted = extract_text_from_docx_bytes(content)
        elif kind == "image":
            if OCR_ENGINE == "openai":
                extracted, used_tokens = ocr_openai_image_bytes(content)
            else:
                extracted = ocr_tesseract_image_bytes(content, OCR_LANG)
        else:
            await m.reply(base_info + "\n\nЭтот тип пока не обрабатываю. Отправь PDF/DOCX/изображение.")
            return

        if not extracted.strip():
            await m.reply(base_info + "\n\nТекст не найден или не распознан.")
            return

        # Сохраняем в историю и считаем квоту
        c = db()
        uid = m.from_user.id
        chat_id = ensure_active_chat(c, uid)

        prompt = f"Распознанный текст из файла {doc.file_name or filename}:\n\n{extracted[:8000]}"
        add_msg(c, uid, chat_id, "user", prompt)

        est_in = len(prompt) // 4
        if not can_spend(c, uid, est_in + used_tokens):
            await m.reply("❌ Превышен лимит токенов на сегодня.")
            return

        # Дальше можно сразу попросить модель сделать резюме/форматирование
        await bot.send_chat_action(m.chat.id, "typing")
        system_prompt = {
            "role": "system",
            "content": (
                "Ты ассистент MOS-GSM. Кратко структурируй распознанный текст: "
                "сделай заголовок, тезисы-списком, при необходимости — вопросы по уточнению. Markdown."
                "Не используй #-заголовки, заголовки делай жирным (**Заголовок**)."
            )
        }
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[system_prompt, {"role":"user","content":extracted[:15000]}]
        )
        answer = format_answer(resp.choices[0].message.content or "")
        add_msg(c, uid, chat_id, "assistant", answer)
        add_tokens(c, uid, (resp.usage.total_tokens if resp.usage else est_in) + used_tokens)

        await m.reply(base_info + "\n\n" + answer, reply_markup=reply_menu())

    except Exception as e:
        await m.reply(f"❌ Ошибка при обработке файла: `{e}`")

@dp.message(F.photo)
async def on_photo(m: Message):
    if not access(m.from_user.id):
        await m.reply("🚫 Доступ ограничен.")
        return

    try:
        await bot.send_chat_action(m.chat.id, "upload_photo")
    except:
        pass

    try:
        ph = m.photo[-1]  # максимальное качество
        filename, content, ext = await download_by_file_id(ph.file_id, prefix="photo")
        path = await save_bytes_local(filename, content)

        # OCR
        if OCR_ENGINE == "openai":
            extracted, used_tokens = ocr_openai_image_bytes(content)
        else:
            extracted = ocr_tesseract_image_bytes(content, OCR_LANG)
            used_tokens = 0

        base_info = f"🖼 Фото сохранено: `{path}`"

        if not extracted.strip():
            await m.reply(base_info + "\n\nТекст не найден/не распознан. Попробуй сделать фото чётче.")
            return

        # Сохраняем и отправляем структурированный результат
        c = db()
        uid = m.from_user.id
        chat_id = ensure_active_chat(c, uid)

        user_note = (m.caption or "").strip()
        task = user_note if user_note else "Переведи текст с фото в печатный вид и оформи структурно."
        prompt = f"{task}\n\nТекст с фото:\n{extracted[:8000]}"
        add_msg(c, uid, chat_id, "user", prompt)

        est_in = len(prompt) // 4
        if not can_spend(c, uid, est_in + used_tokens):
            await m.reply("❌ Превышен лимит токенов на сегодня.")
            return

        await bot.send_chat_action(m.chat.id, "typing")
        system_prompt = {
            "role": "system",
            "content": "Ты ассистент MOS-GSM. Преобразуй текст в читабельный вид: сохрани абзацы, списки. Markdown."
            "Не используй #-заголовки, заголовки делай жирным (**Заголовок**)."
        }
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[system_prompt, {"role":"user","content":prompt}]
        )
        answer = format_answer(resp.choices[0].message.content or "")
        add_msg(c, uid, chat_id, "assistant", answer)
        add_tokens(c, uid, (resp.usage.total_tokens if resp.usage else est_in) + used_tokens)

        await m.reply(base_info + "\n\n" + answer, reply_markup=reply_menu())

    except Exception as e:
        await m.reply(f"❌ Ошибка при обработке фото: `{e}`")

# Чтобы бот мог вернуть файл

@dp.message(Command("send_example"))
async def send_example(m: Message):
    path = os.path.join("files", "example.txt")
    os.makedirs("files", exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write("Пример файла от бота MOS-GSM.")
    async with aiofiles.open(path, "rb") as f:
        await m.answer_document(f, caption="Вот пример файла 📄")

# ===== RUN =====
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
