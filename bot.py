import os
import json
import asyncio
import requests
import datetime
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import google.generativeai as genai

# --- ФЕЙКОВЫЙ СЕРВЕР ДЛЯ РАБОТЫ БЕЗ ПЛАТЫ НА RENDER ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()
# ------------------------------------------------------

# --- Конфигурация ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CLICKUP_API_KEY = os.getenv("CLICKUP_API_KEY")
CLICKUP_LIST_ID = "901818750380"

genai.configure(api_key=GEMINI_API_KEY)
user_sessions = {}

def get_user_session(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = {"categories": [], "tasks": []}
    return user_sessions[user_id]

def create_clickup_task(task_data):
    headers = {
        "Authorization": f"Bearer {CLICKUP_API_KEY}",
        "Content-Type": "application/json"
    }
    priority_map = {"urgent": 1, "high": 2, "normal": 3, "low": 4}
    priority = priority_map.get(task_data.get("priority", "normal"), 3)

    due_date_ms = None
    if task_data.get("due_date"):
        try:
            dt = datetime.datetime.strptime(task_data["due_date"], "%Y-%m-%d")
            due_date_ms = int(dt.timestamp() * 1000)
        except ValueError:
            pass

    desc = task_data.get("description", "")
    if task_data.get("initiator"):
        desc = f"**Инициатор:** {task_data['initiator']}\n\n{desc}"

    payload = {
        "name": task_data.get("title", "Новая задача из аудио"),
        "description": desc,
        "priority": priority,
        "tags": [task_data.get("category")] if task_data.get("category") else []
    }
    if due_date_ms:
        payload["due_date"] = due_date_ms

    url = f"https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task"
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()

def link_clickup_tasks(task_id, linked_task_id):
    headers = {
        "Authorization": f"Bearer {CLICKUP_API_KEY}",
        "Content-Type": "application/json"
    }
    url = f"https://api.clickup.com/api/v2/task/{task_id}/link/{linked_task_id}"
    requests.post(url, headers=headers)

async def process_audio_with_gemini(audio_bytes, session_data):
    model = genai.GenerativeModel('gemini-2.0-flash', generation_config={"response_mime_type": "application/json"})
    categories_str = ", ".join(session_data["categories"]) if session_data["categories"] else "Пока нет категорий"
    tasks_context = json.dumps(session_data["tasks"], ensure_ascii=False)
    
    prompt = f"""
    Ты ИИ-ассистент. Слушай аудио и создавай задачу.
    В аудио смесь русского и узбекского. Сохраняй оригинальный текст в 'description', НЕ переводи.
    1. Дедлайн не упомянут → due_date: null. Формат YYYY-MM-DD.
    2. Инициатор не назван → initiator: null.
    3. Приоритет (urgent, high, normal, low) по тону.
    4. Известные категории: [{categories_str}]. Переиспользуй или создай новую.
    5. Задачи сессии: {tasks_context}. Если есть связь, укажи ID в 'link_to_task_id'. Иначе null.
    Верни строго JSON. Обязательные ключи: title, priority, due_date, category, initiator, description, link_to_task_id.
    """
    audio_part = {"mime_type": "audio/ogg", "data": audio_bytes}
    response = await asyncio.to_thread(model.generate_content, [prompt, audio_part])
    return json.loads(response.text)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    session = get_user_session(user_id)
    msg = await update.message.reply_text("⏳ Обрабатываю аудиозапись...")
    
    try:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        audio_bytes = bytes(await voice_file.download_as_bytearray())
        
        task_data = await process_audio_with_gemini(audio_bytes, session)
        
        category = task_data.get("category")
        if category and category not in session["categories"]:
            session["categories"].append(category)

        created_task = await asyncio.to_thread(create_clickup_task, task_data)
        new_task_id = created_task["id"]
        session["tasks"].append({"id": new_task_id, "title": task_data.get("title")})
        
        linked_status = "Нет"
        link_to_id = task_data.get("link_to_task_id")
        if link_to_id:
            try:
                await asyncio.to_thread(link_clickup_tasks, new_task_id, link_to_id)
                linked_status = f"Да (связана с {link_to_id})"
            except Exception:
                linked_status = "Ошибка при связывании"

        response_text = (
            f"✅ **Задача успешно создана!**\n\n"
            f"**Название:** {task_data.get('title')}\n"
            f"**Приоритет:** {task_data.get('priority')}\n"
            f"**Категория:** {task_data.get('category')}\n"
            f"**Связана:** {linked_status}"
        )
        await msg.edit_text(response_text, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text("❌ Произошла ошибка. Попробуй еще раз.")
        print(e)

def main():
    # Фикс для серверов Render (принудительно создаем цикл событий)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    print("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
