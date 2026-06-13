import os
import json
import asyncio
import requests
import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import google.generativeai as genai

# --- Конфигурация ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CLICKUP_API_KEY = os.getenv("CLICKUP_API_KEY")
CLICKUP_LIST_ID = "901818750380"

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Хранилище в памяти (в контексте одного запуска)
# Структура: { user_id: { "categories": ["cat1"], "tasks": [{"id": "...", "title": "..."}] } }
user_sessions = {}

def get_user_session(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = {"categories": [], "tasks": []}
    return user_sessions[user_id]

# --- Интеграция с ClickUp ---
def create_clickup_task(task_data):
    headers = {
        "Authorization": f"Bearer {CLICKUP_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Маппинг приоритетов
    priority_map = {"urgent": 1, "high": 2, "normal": 3, "low": 4}
    priority = priority_map.get(task_data.get("priority", "normal"), 3)

    # Обработка даты (ClickUp требует Unix timestamp в миллисекундах)
    due_date_ms = None
    if task_data.get("due_date"):
        try:
            dt = datetime.datetime.strptime(task_data["due_date"], "%Y-%m-%d")
            due_date_ms = int(dt.timestamp() * 1000)
        except ValueError:
            pass

    # Формируем описание: добавляем инициатора, если он есть, к полной транскрипции
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
    response = requests.post(url, headers=headers)
    response.raise_for_status()

# --- Интеграция с Gemini ---
async def process_audio_with_gemini(audio_bytes, session_data):
    model = genai.GenerativeModel(
        'gemini-2.0-flash',
        generation_config={"response_mime_type": "application/json"}
    )
    
    categories_str = ", ".join(session_data["categories"]) if session_data["categories"] else "Пока нет категорий"
    tasks_context = json.dumps(session_data["tasks"], ensure_ascii=False)
    
    prompt = f"""
    Ты — ИИ-ассистент. Слушай аудио и создавай задачу.
    В аудио может быть смесь русского и узбекского языков. Сохраняй оригинальный текст в поле 'description', НЕ переводи.
    
    Правила:
    1. Если дедлайн не упомянут → due_date: null. НЕ выдумывай. Формат YYYY-MM-DD.
    2. Если инициатор не назван → initiator: null. НЕ угадывай.
    3. Приоритет определяй по тону и словам (например, "срочно", "горит" → urgent). Возможные значения: urgent, high, normal, low.
    4. Категории. Текущие известные категории: [{categories_str}]. Переиспользуй подходящую или создай новую краткую, если ни одна не подходит.
    5. Связь. Текущие задачи сессии: {tasks_context}. Если новая задача логически связана с одной из прошлых задач, укажи её ID в 'link_to_task_id'. Иначе null.
    
    Верни строго JSON (без markdown, без пояснений):
    {{
      "title": "краткое название задачи, до 10 слов",
      "priority": "urgent | high | normal | low",
      "due_date": "YYYY-MM-DD или null",
      "category": "категория",
      "initiator": "имя или null",
      "description": "полная транскрипция аудио",
      "link_to_task_id": "ID связанной задачи или null"
    }}
    """
    
    audio_part = {
        "mime_type": "audio/ogg",
        "data": audio_bytes
    }
    
    # Gemini 2.0 Flash поддерживает мультимодальность напрямую
    response = await asyncio.to_thread(model.generate_content, [prompt, audio_part])
    return json.loads(response.text)

# --- Обработчик Telegram ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    session = get_user_session(user_id)
    
    msg = await update.message.reply_text("⏳ Обрабатываю аудиозапись...")
    
    try:
        # Скачиваем аудио в память
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        audio_bytes = await voice_file.download_as_bytearray()
        
        # 1. Отправляем в Gemini
        task_data = await process_audio_with_gemini(audio_bytes, session)
        
        # Обновляем память категорий
        category = task_data.get("category")
        if category and category not in session["categories"]:
            session["categories"].append(category)

        # 2. Создаем задачу в ClickUp
        created_task = await asyncio.to_thread(create_clickup_task, task_data)
        new_task_id = created_task["id"]
        
        # Обновляем память задач
        session["tasks"].append({"id": new_task_id, "title": task_data.get("title")})
        
        # 3. Линкуем задачу, если требуется
        linked_status = "Нет"
        link_to_id = task_data.get("link_to_task_id")
        if link_to_id:
            try:
                await asyncio.to_thread(link_clickup_tasks, new_task_id, link_to_id)
                linked_status = f"Да (связана с {link_to_id})"
            except Exception as e:
                linked_status = "Ошибка при связывании"
                print(f"Ошибка линковки: {e}")

        # 4. Отвечаем пользователю
        response_text = (
            f"✅ **Задача успешно создана!**\n\n"
            f"**Название:** {task_data.get('title')}\n"
            f"**Приоритет:** {task_data.get('priority')}\n"
            f"**Категория:** {task_data.get('category')}\n"
            f"**Связана с предыдущими:** {linked_status}"
        )
        await msg.edit_text(response_text, parse_mode="Markdown")

    except Exception as e:
        print(f"Error: {e}")
        await msg.edit_text("❌ Произошла ошибка при обработке сообщения. Проверьте логи.")

def main():
    if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, CLICKUP_API_KEY]):
        print("Ошибка: Не заданы переменные окружения!")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
