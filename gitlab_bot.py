import os
import telebot
import gitlab
from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env
load_dotenv()

# ================= НАСТРОЙКИ TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ALLOWED_USER_ID = int(os.getenv('ALLOWED_USER_ID', 0))

# ================= НАСТРОЙКИ GITLAB =================
GITLAB_URL = os.getenv('GITLAB_URL')
GITLAB_PROJECT_ID = os.getenv('GITLAB_PROJECT_ID')  # ID проекта или full path
GITLAB_TOKEN = os.getenv('GITLAB_TOKEN')

if not TELEGRAM_BOT_TOKEN or not ALLOWED_USER_ID:
    raise ValueError("Ошибка: В .env не указаны настройки Telegram (TELEGRAM_BOT_TOKEN или ALLOWED_USER_ID).")
if not GITLAB_URL or not GITLAB_PROJECT_ID or not GITLAB_TOKEN:
    raise ValueError("Ошибка: В .env не указаны настройки GitLab (GITLAB_URL, GITLAB_PROJECT_ID или GITLAB_TOKEN).")

# ================= АВТОРИЗАЦИЯ В GITLAB =================
print("🔄 Подключение к GitLab...")
try:
    gl = gitlab.Gitlab(url=GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()
    project = gl.projects.get(GITLAB_PROJECT_ID)
    print(f"✅ Успешное подключение к GitLab. Проект: {project.name_with_namespace}")
except Exception as e:
    print(f"❌ Ошибка подключения к GitLab: {e}")
    exit(1)

# Инициализация бота
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)


@bot.message_handler(content_types=['text', 'photo', 'document'])
def handle_message(message):
    # 1. ПРОВЕРКА БЕЗОПАСНОСТИ
    if message.from_user.id != ALLOWED_USER_ID:
        bot.reply_to(message, "⛔️ У вас нет прав на использование этого бота.")
        return

    # 2. ОПРЕДЕЛЯЕМ АВТОРА
    if message.forward_from:
        user = message.forward_from
        author_name = f"{user.first_name} {user.last_name or ''}".strip()
        author_username = f"@{user.username}" if user.username else "нет юзернейма"
        contact_info = f"{author_name} ({author_username})"
    elif message.forward_sender_name:
        contact_info = f"{message.forward_sender_name} (профиль скрыт)"
    else:
        user = message.from_user
        author_name = f"{user.first_name} {user.last_name or ''}".strip()
        author_username = f"@{user.username}" if user.username else "нет юзернейма"
        contact_info = f"{author_name} ({author_username})"

    # 3. ПОЛУЧАЕМ ТЕКСТ ИЛИ СТАВИМ ЗАГЛУШКУ
    text = message.text or message.caption
    if not text:
        text = "Вложение из Telegram (без текста)"

    lines = text.split('\n', 1)
    title = lines[0][:250]
    description = f"**Автор в Telegram:** {contact_info}\n\n---\n{text}"

    status_msg = bot.send_message(message.chat.id, "⏳ Создаю тикет в GitLab...")

    try:
        # 4. ОБРАБОТКА ВЛОЖЕНИЙ (КАРТИНКИ И ДОКУМЕНТЫ)
        attachment_markdown = ""
        if message.photo or message.document:
            bot.edit_message_text("⏳ Загружаю файл в GitLab...", chat_id=message.chat.id,
                                  message_id=status_msg.message_id)

            if message.photo:
                file_info = bot.get_file(message.photo[-1].file_id)
                file_name = f"photo_{message.message_id}.jpg"
            elif message.document:
                file_info = bot.get_file(message.document.file_id)
                file_name = message.document.file_name
            else:
                file_info = None
                file_name = None

            if file_info:
                downloaded_file = bot.download_file(file_info.file_path)
                
                # Загружаем файл в GitLab проект
                uploaded_file = project.upload(file_name, filedata=downloaded_file)
                attachment_markdown = f"\n\n---\n**Вложение:**\n{uploaded_file['markdown']}"

        # 5. СОЗДАНИЕ ISSUE
        issue_data = {
            'title': title,
            'description': description + attachment_markdown
        }
        
        new_issue = project.issues.create(issue_data)

        # 6. ФИНАЛЬНЫЙ ОТВЕТ
        issue_link = new_issue.web_url
        bot.edit_message_text(
            f"✅ **Тикет успешно создан!**\n\n🔑 IID: {new_issue.iid}\n🔗 [Открыть в GitLab]({issue_link})",
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            parse_mode='Markdown')

    except Exception as e:
        bot.edit_message_text(f"❌ **Произошла ошибка:**\n`{str(e)}`",
                              chat_id=message.chat.id,
                              message_id=status_msg.message_id,
                              parse_mode='Markdown')


if __name__ == '__main__':
    print("🤖 Бот запущен и ожидает сообщений...")
    bot.polling(none_stop=True, timeout=60)
