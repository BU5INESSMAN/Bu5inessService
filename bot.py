import os
import asyncio
import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp
import ffmpeg

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Для Railway

TEMP_DIR = "downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

pending_urls = {}

# Контроль частоты обновлений прогресса (по пользователю)
last_progress_update = {}

def clean_ansi(text: str) -> str:
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь ссылку на видео или аудио с YouTube, Instagram, TikTok, Rutube, Pinterest и других платформ.\n\n"
        "Я скачаю и отправлю тебе файл (видео или аудио по выбору)."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    message_id = update.message.message_id

    pending_urls[message_id] = {
        "url": url,
        "user_id": update.effective_user.id
    }

    keyboard = [
        [
            InlineKeyboardButton("📹 Видео", callback_data=f"video|{message_id}"),
            InlineKeyboardButton("🎵 Аудио (MP3)", callback_data=f"audio|{message_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выбери формат:", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    mode, msg_id_str = data.split("|", 1)
    msg_id = int(msg_id_str)

    if msg_id not in pending_urls:
        await query.edit_message_text("Ссылка устарела. Отправь заново.")
        return

    stored = pending_urls.pop(msg_id)
    if stored["user_id"] != query.from_user.id:
        await query.edit_message_text("Это не твоя ссылка 😉")
        return

    url = stored["url"]
    is_audio = mode == "audio"

    status_message = await query.edit_message_text("Подготовка к скачиванию... ⏳")
    user_id = query.from_user.id

    # Инициализация времени последнего обновления
    last_progress_update[user_id] = 0

    def progress_hook(d):
        now = asyncio.get_event_loop().time()
        if d['status'] == 'downloading' and now - last_progress_update[user_id] > 5:  # Не чаще 5 сек
            percent = clean_ansi(d.get('_percent_str', '0%')).strip()
            speed = clean_ansi(d.get('_speed_str', 'N/A')).strip()
            eta = clean_ansi(d.get('_eta_str', 'N/A')).strip()
            text = f"Скачивание: {percent}\nСкорость: {speed}\nОсталось: {eta}"
            last_progress_update[user_id] = now
            # Безопасное редактирование с try/except
            asyncio.create_task(safe_edit(status_message, text))
        elif d['status'] == 'finished':
            asyncio.create_task(safe_edit(status_message, "Скачивание завершено! Обрабатываю..."))

    async def safe_edit(message, text):
        try:
            await message.edit_text(text)
        except Exception as e:
            logger.debug(f"Не удалось обновить прогресс: {e}")

    ydl_opts = {
        'outtmpl': os.path.join(TEMP_DIR, '%(id)s.%(ext)s'),
        'noplaylist': True,
        'progress_hooks': [progress_hook],
        'quiet': True,
        'no_color': True,
    }

    if is_audio:
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    else:
        ydl_opts.update({
            'format': 'best[height<=720][ext=mp4]/bestvideo[height<=720]+bestaudio/best',
            'merge_output_format': 'mp4',
        })

    filename = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if is_audio and not filename.endswith('.mp3'):
                filename = filename.rsplit('.', 1)[0] + '.mp3'

        if not os.path.exists(filename):
            await status_message.edit_text("Ошибка: файл не скачан.")
            return

        file_size_mb = os.path.getsize(filename) / (1024 * 1024)

        if not is_audio and file_size_mb > 45:
            await status_message.edit_text(f"Видео большое ({file_size_mb:.1f} МБ), сжимаю... ⏳")
            compressed = filename.rsplit('.', 1)[0] + '_compressed.mp4'
            stream = ffmpeg.input(filename)
            stream = ffmpeg.output(
                stream, compressed,
                vcodec='libx264', crf=28, preset='fast',
                acodec='aac', audio_bitrate='128k',
                vf='scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2',
                movflags='+faststart', loglevel='error'
            )
            ffmpeg.run(stream, overwrite_output=True, quiet=True)
            if os.path.exists(compressed):
                os.remove(filename)
                filename = compressed

        if os.path.getsize(filename) > 50 * 1024 * 1024:
            await status_message.edit_text("Файл слишком большой даже после сжатия (>50 МБ).")
            os.remove(filename)
            return

        caption = info.get('title', 'Медиа') or info.get('id', '')
        if info.get('uploader'):
            caption += f"\nОт: {info.get('uploader')}"

        await status_message.edit_text("Отправляю... 🚀")

        with open(filename, 'rb') as f:
            if is_audio:
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=f,
                    caption=caption,
                    title=info.get('title', 'Audio')
                )
            else:
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=f,
                    caption=caption,
                    supports_streaming=True
                )

        os.remove(filename)
        await status_message.delete()

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        try:
            await status_message.edit_text(f"Ошибка:\n{str(e)[:300]}\nПопробуй другую ссылку.")
        except:
            pass

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r'https?://[^\s]+'), handle_url))
    app.add_handler(CallbackQueryHandler(button_callback, pattern=r'^(video|audio)\|\d+$'))

    print("Бот запущен на Railway...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()