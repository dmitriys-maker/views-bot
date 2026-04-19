import os
import json
import asyncio
import base64
import httpx
import tempfile
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")

SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

user_states = {}


# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_creds():
    if GOOGLE_CREDENTIALS_JSON:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        return Credentials.from_service_account_file("credentials.json", scopes=SCOPES)


def get_sheet():
    creds = get_creds()
    client = gspread.authorize(creds)
    sh = client.open_by_key(GOOGLE_SHEET_ID)

    try:
        ws = sh.worksheet("Все записи")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Все записи", rows=5000, cols=8)
        ws.append_row(["Дата", "Нарезчик", "Канал", "Telegram ID", "Платформа", "Просмотры", "Роликов на скрине", "Описание"])
        ws.format("A1:H1", {"textFormat": {"bold": True}})

    try:
        ws_sum = sh.worksheet("Итоги")
    except gspread.WorksheetNotFound:
        ws_sum = sh.add_worksheet("Итоги", rows=500, cols=6)
        ws_sum.update("A1", [["Нарезчик", "Канал", "Всего просмотров", "Роликов", "Скринов", "Обновлено"]])
        ws_sum.format("A1:F1", {"textFormat": {"bold": True}})

    return sh, ws, ws_sum


def save_to_sheet(editor_name, channel, tg_id, platform, videos):
    sh, ws, ws_sum = get_sheet()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    existing = ws.get_all_values()
    existing_keys = set()
    for row in existing[1:]:
        if len(row) >= 8:
            key = (row[1], row[2], str(row[5]), row[7].strip().lower()[:50])
            existing_keys.add(key)

    rows_to_add = []
    skipped = []
    for v in videos:
        desc = v.get("description", "")
        key = (editor_name, channel, str(v["views"]), desc.strip().lower()[:50])
        if key in existing_keys:
            skipped.append(v)
        else:
            rows_to_add.append([now, editor_name, channel, str(tg_id), platform, v["views"], len(videos), desc])
            existing_keys.add(key)

    total_views = sum(r[5] for r in rows_to_add)
    if rows_to_add:
        ws.append_rows(rows_to_add)
        update_summary(ws_sum, editor_name, channel, total_views, len(rows_to_add), 1, now)

    return total_views, skipped


def update_summary(ws_sum, editor_name, channel, new_views, new_vids, new_screens, now):
    data = ws_sum.get_all_values()
    rows = data[1:] if len(data) > 1 else []
    keys = [(r[0], r[1]) for r in rows]
    key = (editor_name, channel)

    if key in keys:
        idx = keys.index(key)
        row_num = idx + 2
        old_views = int(rows[idx][2]) if rows[idx][2] else 0
        old_vids = int(rows[idx][3]) if rows[idx][3] else 0
        old_screens = int(rows[idx][4]) if len(rows[idx]) > 4 and rows[idx][4] else 0
        ws_sum.update(f"A{row_num}:F{row_num}", [[editor_name, channel, old_views + new_views, old_vids + new_vids, old_screens + new_screens, now]])
    else:
        ws_sum.append_row([editor_name, channel, new_views, new_vids, new_screens, now])


# ─── Claude Vision ────────────────────────────────────────────────────────────

async def scan_image(image_bytes, mime_type="image/jpeg"):
    b64 = base64.b64encode(image_bytes).decode()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-opus-4-5",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                    {"type": "text", "text": "На скрине статистика видео (TikTok/YouTube/VK Видео). Найди ВСЕ числа просмотров для КАЖДОГО видео. Просмотры: 'просмотры', 'views', иконка глаза. Конвертируй: 1.2K=1200, 1.5M=1500000, 2.3млн=2300000. Ответь ТОЛЬКО JSON: {\"videos\":[{\"views\":число,\"description\":\"название\"}]}"}
                ]}]
            }
        )
    data = resp.json()
    text = "".join(c.get("text", "") for c in data.get("content", [])).replace("```json", "").replace("```", "").strip()
    return json.loads(text).get("videos", [])


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_state(user_id):
    if user_id not in user_states:
        user_states[user_id] = {"platform": None, "channel": None, "waiting_channel": False, "pending_platform": None, "queue": [], "processing": False, "session_views": 0, "session_videos": 0, "session_screens": 0}
    return user_states[user_id]


def fmt(n):
    return f"{n:,}".replace(",", " ")


def plural(n):
    if n % 10 == 1 and n % 100 != 11: return ''
    if n % 10 in [2, 3, 4] and n % 100 not in [12, 13, 14]: return 'а'
    return 'ов'


# ─── Queue processor ──────────────────────────────────────────────────────────

async def process_queue(user_id, editor_name, ctx):
    state = get_state(user_id)
    if state["processing"]:
        return
    state["processing"] = True

    while state["queue"]:
        item = state["queue"].pop(0)
        try:
            tg_file = await ctx.bot.get_file(item["file_id"])
            image_bytes = await tg_file.download_as_bytearray()
            videos = await scan_image(bytes(image_bytes), item["mime"])

            if not videos:
                await ctx.bot.send_message(user_id, "Один скрин пропущен — не нашёл просмотры.")
                continue

            total_views, skipped = save_to_sheet(editor_name, state["channel"], user_id, state["platform"], videos)
            state["session_views"] += total_views
            state["session_videos"] += len(videos) - len(skipped)
            state["session_screens"] += 1

            remaining = len(state["queue"])
            lines = [f"Скрин обработан ({state['platform']}, канал: {state['channel']})"]
            for v in videos:
                desc = f" — {v['description']}" if v.get("description") else ""
                is_dup = any(s["views"] == v["views"] and s.get("description", "") == v.get("description", "") for s in skipped)
                dup_mark = " (дубль, пропущен)" if is_dup else ""
                lines.append(f"  · {fmt(v['views'])} просмотров{desc}{dup_mark}")
            if skipped:
                lines.append(f"Пропущено дублей: {len(skipped)}")
            if remaining > 0:
                lines.append(f"Осталось: {remaining} скринов")
            await ctx.bot.send_message(user_id, "\n".join(lines))

        except Exception as e:
            await ctx.bot.send_message(user_id, f"Ошибка: {e}")

        await asyncio.sleep(0.5)

    if state["session_screens"] > 0:
        await ctx.bot.send_message(user_id,
            f"Готово! Итог сессии:\n"
            f"Канал: {state['channel']}\n"
            f"Платформа: {state['platform']}\n"
            f"Скринов: {state['session_screens']}\n"
            f"Роликов: {state['session_videos']}\n"
            f"Просмотров: {fmt(state['session_views'])}\n\n"
            f"Всё записано в таблицу\n"
            f"Для новой сессии выбери платформу: /tiktok /youtube /vk"
        )
        state["session_views"] = 0
        state["session_videos"] = 0
        state["session_screens"] = 0

    state["processing"] = False


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я считаю просмотры со скринов статистики.\n\n"
        "Как пользоваться:\n"
        "1. Выбери платформу:\n"
        "   /tiktok — TikTok\n"
        "   /youtube — YouTube\n"
        "   /vk — VK Видео\n\n"
        "2. Введи название канала\n"
        "3. Кидай скрины — хоть 200 штук подряд\n\n"
        "Команды:\n"
        "/stats — моя статистика\n"
        "/total — общая статистика (админ)"
    )


async def set_platform(update, ctx, platform):
    state = get_state(update.effective_user.id)
    state["pending_platform"] = platform
    state["waiting_channel"] = True
    state["queue"] = []
    state["session_views"] = 0
    state["session_videos"] = 0
    state["session_screens"] = 0
    await update.message.reply_text(f"Платформа: {platform}\n\nВведи название канала:")


async def set_tiktok(update, ctx): await set_platform(update, ctx, "TikTok")
async def set_youtube(update, ctx): await set_platform(update, ctx, "YouTube")
async def set_vk(update, ctx): await set_platform(update, ctx, "VK Видео")


async def text_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_user.id)
    if state["waiting_channel"]:
        channel = update.message.text.strip()
        state["channel"] = channel
        state["platform"] = state["pending_platform"]
        state["waiting_channel"] = False
        await update.message.reply_text(f"Канал: {channel}\nПлатформа: {state['platform']}\n\nТеперь кидай скрины!")
    else:
        await update.message.reply_text("Выбери платформу:\n/tiktok /youtube /vk")


async def photo_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = get_state(user.id)

    if state["waiting_channel"]:
        await update.message.reply_text("Сначала введи название канала текстом.")
        return

    if not state["platform"] or not state["channel"]:
        await update.message.reply_text("Сначала выбери платформу:\n/tiktok /youtube /vk")
        return

    photo = update.message.photo[-1]
    state["queue"].append({"file_id": photo.file_id, "mime": "image/jpeg"})

    if len(state["queue"]) == 1 and not state["processing"]:
        await update.message.reply_text(f"Получил! Платформа: {state['platform']}, канал: {state['channel']}\nКидай остальные скрины!")

    editor_name = user.username or user.first_name or str(user.id)
    asyncio.create_task(process_queue(user.id, editor_name, ctx))


async def document_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        return

    state = get_state(user.id)
    if state["waiting_channel"]:
        await update.message.reply_text("Сначала введи название канала текстом.")
        return
    if not state["platform"] or not state["channel"]:
        await update.message.reply_text("Сначала выбери платформу:\n/tiktok /youtube /vk")
        return

    state["queue"].append({"file_id": doc.file_id, "mime": doc.mime_type})
    if len(state["queue"]) == 1 and not state["processing"]:
        await update.message.reply_text(f"Получил! Платформа: {state['platform']}, канал: {state['channel']}\nКидай остальные скрины!")

    editor_name = user.username or user.first_name or str(user.id)
    asyncio.create_task(process_queue(user.id, editor_name, ctx))


async def my_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    editor_name = user.username or user.first_name or str(user.id)
    try:
        _, _, ws_sum = get_sheet()
        data = ws_sum.get_all_values()
        rows = [r for r in data[1:] if r[0] == editor_name]
        if rows:
            lines = [f"Твоя статистика ({editor_name}):\n"]
            total_views = 0
            for r in rows:
                views = int(r[2]) if r[2] else 0
                total_views += views
                lines.append(f"• {r[1]}: {fmt(views)} просмотров ({r[3]} роликов)")
            lines.append(f"\nИтого: {fmt(total_views)} просмотров")
            await update.message.reply_text("\n".join(lines))
        else:
            await update.message.reply_text("У тебя пока нет записей.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def total_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Эта команда только для администратора.")
        return
    try:
        _, _, ws_sum = get_sheet()
        data = ws_sum.get_all_values()
        rows = data[1:]
        if not rows:
            await update.message.reply_text("Данных пока нет.")
            return

        editors = {}
        for r in rows:
            name = r[0]
            views = int(r[2]) if r[2] else 0
            vids = int(r[3]) if r[3] else 0
            if name not in editors:
                editors[name] = {"views": 0, "vids": 0, "channels": []}
            editors[name]["views"] += views
            editors[name]["vids"] += vids
            editors[name]["channels"].append(r[1])

        total_views = sum(e["views"] for e in editors.values())
        lines = ["Статистика по всем нарезчикам:\n"]
        for name, d in sorted(editors.items(), key=lambda x: x[1]["views"], reverse=True):
            channels = ", ".join(set(d["channels"]))
            lines.append(f"• {name}: {fmt(d['views'])} просмотров ({d['vids']} роликов)\n  Каналы: {channels}")
        lines.append(f"\nИТОГО: {fmt(total_views)} просмотров")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tiktok", set_tiktok))
    app.add_handler(CommandHandler("youtube", set_youtube))
    app.add_handler(CommandHandler("vk", set_vk))
    app.add_handler(CommandHandler("stats", my_stats))
    app.add_handler(CommandHandler("total", total_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_received))
    app.add_handler(MessageHandler(filters.PHOTO, photo_received))
    app.add_handler(MessageHandler(filters.Document.IMAGE, document_received))
    print("Бот запущен...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
