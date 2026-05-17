# Immich to Telegram Bot

Automate the collection and transmission of past month's pictures from an Immich shared album to a Telegram channel.

## Features

- **Monthly Reminder:** Pings a list of users on the 1st of every month.
- **Confirmation System:** Waits for all users to reply with `/yes` before proceeding.
- **Chronological Order:** Fetches photos from a specific Immich album and sends them to a Telegram channel in chronological order.
- **Media Support:** Handles both photos and videos (up to Telegram's limits).

## Setup

1. **Clone the repository.**
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure the bot:**
   - Copy `.env.example` to `.env`.
   - Fill in your `TELEGRAM_BOT_TOKEN`.
   - Fill in your `TELEGRAM_CHANNEL_ID` (the bot must be an admin in the channel).
   - Add the Telegram user IDs of you and your friends to `ALLOWED_USER_IDS` (comma-separated).
   - Fill in your `IMMICH_URL` and `IMMICH_API_KEY`.
   - (Optional) Adjust `ALBUM_NAME_PATTERN` to match your Immich album naming convention. Default is `Pictures {month} {year}` (e.g., "Pictures April 2026").

4. **Run the bot:**
   ```bash
   python bot.py
   ```

## Usage

- **Automatic:** The bot will automatically message all allowed users at 10:00 AM on the 1st of each month.
- **Manual Trigger:** Use `/trigger` to manually start the check for the previous month (useful for testing or if you missed the start of the month).
- **Status:** Use `/status` to see who has confirmed so far.
- **Confirm:** Users reply with `/yes` when they have finished uploading their pictures.

## Architecture

- `bot.py`: Main entry point, handles Telegram logic and scheduling.
- `immich_client.py`: Wrapper for the Immich REST API.
- `state.json`: Local persistence for user confirmations (generated at runtime).
