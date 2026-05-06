import os
import re
import asyncio
import sys
import aiosqlite
from dotenv import load_dotenv
from jira import JIRA
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from transformers import pipeline

# ---- Настройка окружения ----
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", 0))

JIRA_SERVER = os.getenv("JIRA_SERVER")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")
JIRA_ISSUE_TYPE = os.getenv("JIRA_ISSUE_TYPE", "Task")
JIRA_PAT = os.getenv("JIRA_PAT")
JIRA_USERNAME = os.getenv("JIRA_USERNAME")
JIRA_PASSWORD = os.getenv("JIRA_PASSWORD")
JIRA_REAL_REPORTER_FIELD = os.getenv("JIRA_REAL_REPORTER_FIELD")

# --- Подключение к Jira ---
try:
    if JIRA_PAT:
        jira_client = JIRA(server=JIRA_SERVER, token_auth=JIRA_PAT)
    elif JIRA_USERNAME and JIRA_PASSWORD:
        jira_client = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))
    else:
        raise ValueError("Нет данных для авторизации в Jira")
    jira_connection_ok = True
    print("✅ Подключение к Jira установлено")
except Exception as e:
    print(f"❌ Ошибка Jira: {e}")
    jira_connection_ok = False

# --- Инициализация локальной нейросети ---
print("🧠 Загрузка локальной нейросети (Zero-Shot)...")
try:
    ai_classifier = pipeline(
        "zero-shot-classification",
        model="cointegrated/rubert-tiny-bilingual-nli"
    )
    print("✅ Нейросеть успешно загружена!")
except Exception as e:
    print(f"❌ Ошибка загрузки нейросети: {e}")
    ai_classifier = None

# --- Инициализация бота ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
DB_PATH = "bot_users.db"
RESERVED_TEXTS = ["📖 Помощь", "📊 Статус", "🆔 Мой ID", "🛠 Админка"]


# --- Состояния FSM ---
class AdminStates(StatesGroup):
    waiting_for_new_user = State()
    waiting_for_new_admin = State()
    waiting_for_broadcast = State()


class TaskStates(StatesGroup):
    waiting_for_comment = State()


# --- Работа с БД ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                issue_key TEXT, 
                author_id INTEGER, 
                summary TEXT
            )
        """)
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (SUPER_ADMIN_ID,))
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (SUPER_ADMIN_ID,))
        await db.commit()


async def get_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cursor:
            return [row[0] for row in await cursor.fetchall()]


# ==========================================
# ИИ: АВТООПРЕДЕЛЕНИЕ ПРИОРИТЕТА
# ==========================================
def detect_priority_ai(text: str) -> str:
    if not ai_classifier:
        return "Medium"

    # Расширенные вариации смыслов для нейросети
    labels_map = {
        "High": [
            "критическая ошибка, сервер недоступен, авария, всё сломалось, очень срочно",
            "полная остановка работы, блокер, инцидент, ничего не работает"
        ],
        "Medium": [
            "стандартная рабочая задача, требуется настройка, доработка, нужна помощь",
            "создание нового функционала, рядовой баг, нужно проверить"
        ],
        "Low": [
            "вопрос, консультация, идея, предложение, пожелание",
            "незначительная проблема, опечатка, не горит, не срочно"
        ]
    }

    # Собираем все фразы в один список
    candidate_labels = [label for sublist in labels_map.values() for label in sublist]

    try:
        # Прогоняем текст через нейросеть
        result = ai_classifier(text, candidate_labels=candidate_labels, multi_label=False)
        top_label = result['labels'][0]

        # Ищем, к какому приоритету относится победившая фраза
        for priority, labels in labels_map.items():
            if top_label in labels:
                return priority
    except Exception as e:
        print(f"Ошибка ИИ: {e}")

    return "Medium"


# ==========================================
# КЛАВИАТУРЫ
# ==========================================
async def get_main_kb(user_id: int):
    admins = await get_admins()
    kb = [
        [KeyboardButton(text="📖 Помощь"), KeyboardButton(text="📊 Статус")],
        [KeyboardButton(text="🆔 Мой ID")]
    ]
    if user_id in admins:
        kb.append([KeyboardButton(text="🛠 Админка")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, input_field_placeholder="Опишите проблему...")


def get_admin_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Юзер", callback_data="admin_add_user")
    builder.button(text="👑 Админ", callback_data="admin_add_admin")
    builder.button(text="➖ Удалить", callback_data="admin_remove_users")
    builder.button(text="📋 Список", callback_data="admin_list")
    builder.button(text="📝 История", callback_data="admin_history")
    builder.button(text="📢 Рассылка", callback_data="admin_broadcast")
    builder.button(text="🔄 Рестарт", callback_data="admin_restart")
    builder.adjust(3, 2, 2)
    return builder.as_markup()


# ==========================================
# ГЛАВНОЕ МЕНЮ И БАЗОВЫЕ КОМАНДЫ
# ==========================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = await get_main_kb(message.from_user.id)
    await message.answer(
        "👋 **Бот Jira Ready!**\n\nПросто отправь мне текст или фото — и я создам задачу.\n"
        "🤖 *Встроенный ИИ автоматически оценит срочность заявки.*",
        reply_markup=kb, parse_mode="Markdown"
    )


@dp.message(F.text == "🆔 Мой ID")
@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    await message.answer(f"Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")


@dp.message(F.text == "📊 Статус")
@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    is_allowed = message.from_user.id in await get_users()
    is_admin = message.from_user.id in await get_admins()
    text = "📊 **Статус системы:**\n\n"
    text += f"✅ Доступ: {'Разрешен' if is_allowed else '⛔ Запрещен'}\n"
    text += f"🛠 Права: {'Администратор' if is_admin else 'Пользователь'}\n"
    text += f"🔧 Jira: {'Связь установлена' if jira_connection_ok else '❌ Ошибка'}\n"
    text += f"🧠 Нейросеть: {'Включена' if ai_classifier else 'Выключена'}\n"
    await message.answer(text, parse_mode="Markdown")


@dp.message(F.text == "📖 Помощь")
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer("📖 Отправь мне текст или файл для создания тикета в Jira.\nИИ сам поймет, если это срочно.",
                         parse_mode="Markdown")


# ==========================================
# АДМИН-ПАНЕЛЬ
# ==========================================
@dp.message(F.text == "🛠 Админка")
@dp.message(Command("admin"))
async def show_admin_menu(message: types.Message):
    if message.from_user.id in await get_admins():
        await message.answer("🛠 **Админ-панель:**", reply_markup=get_admin_kb(), parse_mode="Markdown")


@dp.callback_query(F.data == "admin_list")
async def list_users(callback: types.CallbackQuery):
    users = await get_users()
    admins = await get_admins()
    text = f"👑 **Админы:**\n" + "\n".join([f"`{u}`" for u in admins])
    text += f"\n\n👥 **Юзеры:**\n" + "\n".join([f"`{u}`" for u in users])
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "admin_history")
async def show_history(callback: types.CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT issue_key, summary FROM tasks ORDER BY id DESC LIMIT 10") as cursor:
            rows = await cursor.fetchall()
    if not rows: return await callback.answer("История пуста", show_alert=True)
    await callback.message.answer("📝 **Последние 10 задач:**\n" + "\n".join([f"• {r[0]}: {r[1]}" for r in rows]),
                                  parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "admin_add_user")
async def add_user_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_new_user)
    await callback.message.answer("Введите Telegram ID для доступа:")
    await callback.answer()


@dp.message(AdminStates.waiting_for_new_user)
async def process_add_user(message: types.Message, state: FSMContext):
    if message.text.startswith('/') or message.text in RESERVED_TEXTS: return
    try:
        new_id = int(message.text)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (new_id,))
            await db.commit()
        await message.answer(f"✅ Юзер {new_id} добавлен.")
        await state.clear()
    except ValueError:
        await message.answer("❌ ID должен быть числом.")


@dp.callback_query(F.data == "admin_add_admin")
async def add_admin_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_new_admin)
    await callback.message.answer("Введите Telegram ID для прав Админа:")
    await callback.answer()


@dp.message(AdminStates.waiting_for_new_admin)
async def process_add_admin(message: types.Message, state: FSMContext):
    if message.text.startswith('/') or message.text in RESERVED_TEXTS: return
    try:
        new_id = int(message.text)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (new_id,))
            await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_id,))
            await db.commit()
        await message.answer(f"✅ Админ {new_id} добавлен.")
        await state.clear()
    except ValueError:
        await message.answer("❌ ID должен быть числом.")


@dp.callback_query(F.data == "admin_broadcast")
async def broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.message.answer("Введите текст рассылки:")
    await callback.answer()


@dp.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.text.startswith('/') or message.text in RESERVED_TEXTS: return
    for uid in await get_users():
        try:
            await bot.send_message(uid, f"📢 **Рассылка:**\n\n{message.text}", parse_mode="Markdown")
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer("✅ Отправлено.")
    await state.clear()


@dp.callback_query(F.data == "admin_remove_users")
async def remove_list(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    for u in await get_users(): builder.button(text=f"❌ {u}", callback_data=f"del_{u}")
    builder.button(text="🔙 Назад", callback_data="admin_back")
    builder.adjust(2)
    await callback.message.edit_text("Нажмите на ID для удаления:", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("del_"))
async def process_del(callback: types.CallbackQuery):
    uid = int(callback.data.split("_")[1])
    if uid == SUPER_ADMIN_ID: return await callback.answer("Нельзя удалить себя", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (uid,))
        await db.execute("DELETE FROM admins WHERE user_id = ?", (uid,))
        await db.commit()
    await remove_list(callback)


@dp.callback_query(F.data == "admin_restart")
async def restart(callback: types.CallbackQuery):
    await callback.message.answer("🔄 Рестарт...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


@dp.callback_query(F.data == "admin_back")
async def back(callback: types.CallbackQuery):
    await callback.message.edit_text("🛠 **Админ-панель:**", reply_markup=get_admin_kb(), parse_mode="Markdown")


# ==========================================
# JIRA: КОММЕНТАРИИ
# ==========================================
@dp.callback_query(F.data.startswith("comment_"))
async def start_commenting(callback: types.CallbackQuery, state: FSMContext):
    issue_key = callback.data.split("_")[1]
    await state.update_data(issue_key=issue_key)
    await state.set_state(TaskStates.waiting_for_comment)
    await callback.message.answer(f"✍️ Напишите комментарий для **{issue_key}**:\n_(Для отмены нажмите кнопку в меню)_",
                                  parse_mode="Markdown")
    await callback.answer()


@dp.message(TaskStates.waiting_for_comment)
async def process_comment(message: types.Message, state: FSMContext):
    if message.text in RESERVED_TEXTS or (message.text and message.text.startswith('/')): return await state.clear()
    data = await state.get_data()
    issue_key = data.get("issue_key")
    try:
        await asyncio.to_thread(jira_client.add_comment, issue_key,
                                f"(TG) {message.from_user.full_name}: {message.text}")
        await message.answer(f"✅ Комментарий к **{issue_key}** добавлен!", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        await state.clear()


# ==========================================
# СОЗДАНИЕ ЗАДАЧИ (ОСНОВНОЙ ХЕНДЛЕР)
# ==========================================
@dp.message(~F.text.in_(RESERVED_TEXTS), ~F.text.startswith('/'), ~F.caption.startswith('/'), ~F.reply_to_message,
            StateFilter(None))
async def create_task(message: types.Message):
    if message.from_user.id not in await get_users(): return
    if not jira_connection_ok: return await message.reply("❌ Jira недоступна. Проверьте логи.")

    wait_msg = await message.answer("⏳ Создаю задачу и анализирую приоритет...")
    text = message.text or message.caption or "Без текста"
    summary = text.split('\n')[0][:100]

    # Анализ текста нейросетью в отдельном потоке (чтобы бот не зависал при вычислениях)
    detected_priority = await asyncio.to_thread(detect_priority_ai, text)

    if message.forward_origin:
        if isinstance(message.forward_origin, types.MessageOriginUser):
            original_author = f"{message.forward_origin.sender_user.full_name} (@{message.forward_origin.sender_user.username or 'без_юзернейма'})"
        elif isinstance(message.forward_origin, types.MessageOriginHiddenUser):
            original_author = f"{message.forward_origin.sender_user_name} (профиль скрыт)"
        elif isinstance(message.forward_origin, types.MessageOriginChannel):
            original_author = f"Канал: {message.forward_origin.chat.title}"
        else:
            original_author = "Пересланное сообщение"
        description_text = f"**Переслал(а) в бот:** {message.from_user.full_name}\n\n**Текст:**\n{text}"
    else:
        original_author = f"{message.from_user.full_name} (@{message.from_user.username or 'без_юзернейма'})"
        description_text = text

    try:
        issue_fields = {
            'project': {'key': JIRA_PROJECT_KEY},
            'summary': summary,
            'description': description_text,
            'issuetype': {'name': JIRA_ISSUE_TYPE},
            'priority': {'name': detected_priority}  # Устанавливаем приоритет от ИИ
        }

        if JIRA_REAL_REPORTER_FIELD:
            issue_fields[JIRA_REAL_REPORTER_FIELD] = original_author
        else:
            issue_fields['description'] = f"**Автор:** {original_author}\n\n{description_text}"

        issue = await asyncio.to_thread(jira_client.create_issue, fields=issue_fields)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO tasks (issue_key, author_id, summary) VALUES (?, ?, ?)",
                             (issue.key, message.from_user.id, summary))
            await db.commit()

        file_obj = None
        if message.photo:
            file_obj = message.photo[-1]
        elif message.document:
            file_obj = message.document

        if file_obj:
            info = await bot.get_file(file_obj.file_id)
            content = await bot.download_file(info.file_path)
            file_name = message.document.file_name if message.document else f"photo_{issue.key}.jpg"
            await asyncio.to_thread(jira_client.add_attachment, issue=issue, attachment=content.read(),
                                    filename=file_name)

        kb = InlineKeyboardBuilder()
        kb.button(text="💬 Написать комментарий", callback_data=f"comment_{issue.key}")

        prio_emoji = "🔥" if detected_priority == "High" else "🟢" if detected_priority == "Low" else "🟡"

        await wait_msg.edit_text(
            f"✅ Задача **{issue.key}** создана!\n"
            f"🎯 Установлен приоритет: {prio_emoji} {detected_priority}\n"
            f"🔗 {JIRA_SERVER}/browse/{issue.key}\n\n"
            f"💡 _Для комментария нажмите кнопку ниже:_",
            reply_markup=kb.as_markup(), parse_mode="Markdown", disable_web_page_preview=True
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка создания задачи:\n`{e}`", parse_mode="Markdown")


# ==========================================
# ЗАПУСК
# ==========================================
async def main():
    await init_db()
    print("🚀 Telegram бот запущен!")
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен.")