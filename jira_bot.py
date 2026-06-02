import os
import io
import telebot
from jira import JIRA
from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env
load_dotenv()

# ================= НАСТРОЙКИ TELEGRAM =================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ALLOWED_USER_ID = int(os.getenv('ALLOWED_USER_ID', 0))

# ================= НАСТРОЙКИ JIRA =================
JIRA_SERVER = os.getenv('JIRA_SERVER')
JIRA_PROJECT_KEY = os.getenv('JIRA_PROJECT_KEY')
JIRA_ISSUE_TYPE = os.getenv('JIRA_ISSUE_TYPE', 'Task')
JIRA_CONTACT_FIELD = os.getenv('JIRA_CONTACT_FIELD')

JIRA_PAT = os.getenv('JIRA_PAT')
JIRA_USERNAME = os.getenv('JIRA_USERNAME')
JIRA_PASSWORD = os.getenv('JIRA_PASSWORD')

if not TELEGRAM_BOT_TOKEN or not ALLOWED_USER_ID:
    raise ValueError("Ошибка: В .env не указаны настройки Telegram (TELEGRAM_BOT_TOKEN или ALLOWED_USER_ID).")
if not JIRA_SERVER or not JIRA_PROJECT_KEY:
    raise ValueError("Ошибка: В .env не указаны основные настройки Jira (JIRA_SERVER или JIRA_PROJECT_KEY).")

# ================= АВТОРИЗАЦИЯ В JIRA =================
print("🔄 Подключение к Jira...")
try:
    if JIRA_PAT:
        jira_client = JIRA(server=JIRA_SERVER, token_auth=JIRA_PAT)
        print("✅ Авторизация через PAT успешна.")
    elif JIRA_USERNAME and JIRA_PASSWORD:
        jira_client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
        print("✅ Авторизация через логин/пароль успешна.")
    else:
        raise ValueError("Ошибка: В .env не указаны данные для авторизации.")
except Exception as e:
    print(f"❌ Ошибка подключения к Jira: {e}")
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
    summary = lines[0][:250]
    description = f"**Автор в Telegram:** {contact_info}\n\n---\n{text}"

    status_msg = bot.send_message(message.chat.id, "⏳ Создаю заявку в Jira...")

    try:
        # 4. СОЗДАНИЕ ЗАДАЧИ
        issue_dict = {
            'project': {'key': JIRA_PROJECT_KEY},
            'summary': summary,
            'description': description,
            'issuetype': {'name': JIRA_ISSUE_TYPE},
        }

        if JIRA_CONTACT_FIELD:
            issue_dict[JIRA_CONTACT_FIELD] = contact_info

        new_issue = jira_client.create_issue(fields=issue_dict)

        # 5. ОБРАБОТКА ВЛОЖЕНИЙ (КАРТИНКИ И ДОКУМЕНТЫ)
        if message.photo or message.document:
            bot.edit_message_text("⏳ Загружаю файл в Jira...", chat_id=message.chat.id,
                                  message_id=status_msg.message_id)

            # Если это сжатое фото, берем самую большую версию (последний элемент массива)
            if message.photo:
                file_info = bot.get_file(message.photo[-1].file_id)
                file_name = f"photo_{message.message_id}.jpg"
            # Если это файл/документ (в том числе картинка без сжатия)
            elif message.document:
                file_info = bot.get_file(message.document.file_id)
                file_name = message.document.file_name

            # Скачиваем файл из Telegram в оперативную память
            downloaded_file = bot.download_file(file_info.file_path)
            file_stream = io.BytesIO(downloaded_file)

            # Отправляем в Jira
            jira_client.add_attachment(issue=new_issue.key, attachment=file_stream, filename=file_name)

        # 6. ФИНАЛЬНЫЙ ОТВЕТ
        issue_link = f"{JIRA_SERVER}/browse/{new_issue.key}"
        bot.edit_message_text(
            f"✅ **Заявка успешно создана!**\n\n🔑 Ключ: {new_issue.key}\n🔗 [Открыть в Jira]({issue_link})",
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            parse_mode='Markdown')

    except Exception as e:
        bot.edit_message_text(f"❌ **Произошла ошибка:**\n`{str(e)}`",
                              chat_id=message.chat.id,
                              message_id=status_msg.message_id,
                              parse_mode='Markdown')


if __name__ == '__main__':
    print("🤖 Бот запущен и ожидает сообщений с картинками...")
    bot.polling(none_stop=True, timeout=60)