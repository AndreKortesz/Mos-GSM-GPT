import asyncio
import os
import sqlite3
import time
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI

import re

def format_answer(text: str) -> str:
    # Находим строки вида "### ..."
    def repl(match):
        title = match.group(1).strip()
        return f"**{title}**"  # жирный текст, без ###
    
    # заменяем все заголовки "### ..." на жирный текст
    text = re.sub(r"^###\s*(.*)", repl, text, flags=re.MULTILINE)
    return text


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

        await m.reply(format_answer(answer), reply_markup=reply_menu())
    except Exception as e:
        await m.reply(f"❌ Ошибка OpenAI: `{e}`", reply_markup=reply_menu())

# ===== RUN =====
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
