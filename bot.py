import os
import json
import logging
import asyncio
import calendar
from datetime import datetime, timedelta
from typing import List, Dict

from tenacity import retry, wait_fixed, stop_after_attempt, retry_if_exception_type, before_sleep_log, wait_exponential
import telegram.error
from dotenv import load_dotenv
from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, filters
from telegram.request import HTTPXRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from immich_client import ImmichClient

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Retry decorator for Telegram API calls
def telegram_retry():
    return retry(
        retry=retry_if_exception_type((telegram.error.RetryAfter, telegram.error.TimedOut, telegram.error.NetworkError)),
        wait=wait_exponential(multiplier=1, min=5, max=60),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True
    )

async def send_media_group_with_retry(context, chat_id, media):
    """Sends a media group with precise handling of RetryAfter."""
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            return await context.bot.send_media_group(
                chat_id=chat_id,
                media=media,
                write_timeout=120,
                read_timeout=120
            )
        except telegram.error.RetryAfter as e:
            wait_time = e.retry_after + 1
            logger.warning(f"Flood control hit (attempt {attempt+1}). Waiting for {wait_time}s...")
            await asyncio.sleep(wait_time)
        except (telegram.error.TimedOut, telegram.error.NetworkError) as e:
            logger.warning(f"Network error (attempt {attempt+1}): {e}. Retrying in 5s...")
            await asyncio.sleep(5)
        if attempt == max_attempts - 1:
            raise

@telegram_retry()
async def send_message_with_retry(context, chat_id, text):
    """Sends a message, respecting flood control via tenacity."""
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text)
    except telegram.error.RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        raise

async def notify_allowed_users(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Sends a message to all allowed users who have a numeric ID."""
    for user_ref in ALLOWED_USERS:
        if isinstance(user_ref, int):
            try:
                await send_message_with_retry(context, user_ref, text)
            except Exception as e:
                logger.error(f"Could not message user {user_ref}: {e}")
        else:
            logger.warning(f"Cannot proactively message user {user_ref} (username only). They must message the bot first.")

# Constants from environment
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

def parse_allowed_users(raw_val: str) -> List:
    users = []
    for uid in raw_val.split(","):
        uid = uid.strip()
        if not uid:
            continue
        if uid.startswith("@"):
            users.append(uid.lower())
        else:
            try:
                users.append(int(uid))
            except ValueError:
                users.append(f"@{uid.lower()}")
    return users

ALLOWED_USERS = parse_allowed_users(os.getenv("ALLOWED_USER_IDS", ""))
IMMICH_URL = os.getenv("IMMICH_URL")
IMMICH_KEY = os.getenv("IMMICH_API_KEY")
IMMICH_VERIFY_SSL = os.getenv("IMMICH_VERIFY_SSL", "true").lower() == "true"
ALBUM_PATTERN = os.getenv("ALBUM_NAME_PATTERN", "{year}/{month}")

STATE_FILE = "state.json"

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {"current_month": "", "confirmations": {}, "processed": False}

def save_state(state: dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def get_last_month_info():
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_day_of_last_month = first_of_this_month - timedelta(days=1)
    
    month_num = f"{last_day_of_last_month.month:02d}"
    year = last_day_of_last_month.year
    month_key = last_day_of_last_month.strftime("%Y-%m")
    
    return month_key, month_num, year

def is_user_allowed(user) -> bool:
    if not user:
        return False
    if user.id in ALLOWED_USERS:
        return True
    if user.username and f"@{user.username.lower()}" in ALLOWED_USERS:
        return True
    return False

def get_user_key(user) -> str:
    """Returns a unique string key for the user (ID or username)."""
    if user.id in ALLOWED_USERS:
        return str(user.id)
    if user.username and f"@{user.username.lower()}" in ALLOWED_USERS:
        return f"@{user.username.lower()}"
    return str(user.id)

async def send_photos_to_channel(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    month_key, month_num, year = get_last_month_info()
    
    if state["processed"]:
        logger.info(f"Photos for {month_key} already processed.")
        return

    try:
        immich = ImmichClient(IMMICH_URL, IMMICH_KEY, verify_ssl=IMMICH_VERIFY_SSL)
        album_name = ALBUM_PATTERN.format(month=month_num, year=year)
        
        logger.info(f"Looking for album: {album_name}")
        album = await immich.find_album_by_name(album_name)
        
        if not album:
            await notify_allowed_users(context, f"Error: Could not find album '{album_name}' in Immich.")
            await immich.close()
            return

        # Fetch full album details to get description
        response = await immich.client.get(f"/albums/{album['id']}")
        album_details = response.json()
        description = album_details.get('description')

        assets = await immich.get_album_assets(album['id'])
        # Sort assets by creation date (oldest first)
        assets.sort(key=lambda x: (x.get('fileCreatedAt', ''), x.get('originalFileName', '')))

        if not assets:
            await notify_allowed_users(context, f"Album '{album_name}' is empty.")
            await immich.close()
            return

        logger.info(f"Processing {len(assets)} assets for album {album_name}")
        failed_assets = []

        # Process in batches of 10 for Telegram MediaGroup
        for i in range(0, len(assets), 10):
            batch = assets[i:i+10]
            media_group = []
            
            for idx, asset in enumerate(batch):
                try:
                    is_video = asset['type'] == 'VIDEO'
                    content = await immich.download_asset(asset['id'], is_video=is_video)
                    caption = f"{year}/{month_num} - Photos" if i == 0 and idx == 0 else None
                    
                    if asset['type'] == 'IMAGE':
                        media_group.append(InputMediaPhoto(media=content, caption=caption))
                    elif is_video:
                        media_group.append(InputMediaVideo(media=content, caption=caption))
                except Exception as e:
                    asset_name = asset.get('originalFileName', asset['id'])
                    logger.error(f"Failed to download/prepare asset {asset_name}: {e}")
                    failed_assets.append(f"{asset_name} (Download/Codec error)")

            if media_group:
                try:
                    await send_media_group_with_retry(context, CHANNEL_ID, media_group)
                    # Base delay between successful batches
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(f"Failed to send media group after retries: {e}")
                    failed_assets.append(f"Batch starting at index {i} failed to send.")

        # Send album description as a final message if it exists
        if description:
            try:
                await send_message_with_retry(context, CHANNEL_ID, description)
            except Exception as e:
                logger.error(f"Failed to send album description: {e}")

        await immich.close()
        
        state["processed"] = True
        save_state(state)
        
        success_msg = f"All photos for {year}/{month_num} have been processed!"
        if failed_assets:
            success_msg += "\n\n⚠️ Some assets were skipped:\n- " + "\n- ".join(failed_assets)
        
        await notify_allowed_users(context, success_msg)
    except Exception as e:
        logger.error(f"Critical error in send_photos_to_channel: {e}")
        await notify_allowed_users(context, f"❌ Failed to process photos: {str(e)}")

async def start_monthly_check(context: ContextTypes.DEFAULT_TYPE):
    month_key, month_num, year = get_last_month_info()
    state = load_state()
    
    # New month, reset state
    if state["current_month"] != month_key:
        state = {
            "current_month": month_key,
            "confirmations": {str(u): False for u in ALLOWED_USERS},
            "processed": False
        }
        save_state(state)

    await notify_allowed_users(context, f"Happy new month! Have you finished uploading pictures for {year}/{month_num}?\nReply with /yes when you're done.")

async def yes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user):
        return

    user_key = get_user_key(update.effective_user)
    state = load_state()
    month_key, month_num, year = get_last_month_info()
    
    # Ensure we are in the right month cycle
    if state["current_month"] != month_key:
        state["current_month"] = month_key
        state["confirmations"] = {str(u): False for u in ALLOWED_USERS}
        state["processed"] = False

    state["confirmations"][user_key] = True
    save_state(state)
    
    await update.message.reply_text(f"Got it! Thanks for confirming for {year}/{month_num}.")
    
    # Check if everyone is done
    if all(state["confirmations"].values()):
        if not state["processed"]:
            await update.message.reply_text("Everyone has confirmed! Starting the transmission to the channel...")
            await send_photos_to_channel(context)
        else:
            await update.message.reply_text("Everyone has confirmed, but photos were already processed.")
    else:
        pending = [u for u, confirmed in state["confirmations"].items() if not confirmed]
        await update.message.reply_text(f"Waiting for {len(pending)} more friend(s) to confirm.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user):
        return
    
    state = load_state()
    month_key, month_num, year = get_last_month_info()
    
    text = f"Status for {year}/{month_num}:\n"
    for u in ALLOWED_USERS:
        u_key = str(u)
        status = "✅" if state["confirmations"].get(u_key) else "❌"
        text += f"- {u}: {status}\n"
    
    text += f"\nProcessed: {'Yes' if state['processed'] else 'No'}"
    await update.message.reply_text(text)


async def post_init(application):
    # Scheduler setup
    scheduler = AsyncIOScheduler()
    # Run on the 1st of every month at 10:00 AM
    scheduler.add_job(start_monthly_check, 'cron', day=1, hour=10, args=[application])
    scheduler.start()
    logger.info("Scheduler started.")

async def trigger_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user):
        return

    # Check for manual overrides: /trigger [MonthNum] [Year]
    args = context.args
    if len(args) >= 1:
        try:
            month_val = args[0]
            month_num_int = int(month_val)
            if not (1 <= month_num_int <= 12):
                raise ValueError("Month must be between 1 and 12")
            
            month_str = f"{month_num_int:02d}"
            year = int(args[1]) if len(args) > 1 else datetime.now().year
            month_key = f"{year}-{month_str}"
            
            # Manually reset state for this specific month override
            state = load_state()
            state = {
                "current_month": month_key,
                "confirmations": {str(u): False for u in ALLOWED_USERS},
                "processed": False
            }
            save_state(state)
            
            await update.message.reply_text(f"Manually triggered check for {year}/{month_str}...")
            await notify_allowed_users(context, f"Manual trigger: Have you finished uploading pictures for {year}/{month_str}?\nReply with /yes when you're done.")
        except Exception as e:
            await update.message.reply_text(f"Invalid arguments. Use: /trigger [MonthNumber] [Year]\nError: {str(e)}")
    else:
        # Default behavior: last month
        await update.message.reply_text("Triggering standard monthly check...")
        await start_monthly_check(context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_user:
        try:
            await update.message.reply_text(f"An unexpected error occurred: {str(context.error)}")
        except:
            pass

if __name__ == '__main__':
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env")
        exit(1)

    # Configure custom timeouts for the bot to handle large media uploads
    request = HTTPXRequest(connect_timeout=20, read_timeout=60, write_timeout=120)
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).request(request).build()
    
    application.add_error_handler(error_handler)
    
    application.add_handler(CommandHandler("yes", yes_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Welcome! I'll ping you at the start of each month.")))
    application.add_handler(CommandHandler("trigger", trigger_command))

    print("Bot is starting...")
    application.run_polling()
