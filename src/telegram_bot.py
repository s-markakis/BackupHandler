import configparser
import json
import os
import time
from pathlib import Path as _Path
from threading import Event, Thread

import telebot

CONFIG_FILE = str(_Path(__file__).parent.parent / "config" / "bot_config.ini")
_RUNTIME_USERS_FILE = str(_Path(__file__).parent.parent / "config" / ".bot_users.json")


class TelegramBot:
    def __init__(self, logger):
        """
        Initializes the TelegramBot instance with the given API token.
        Loads config once and reuses parsed values.
        """
        config = configparser.ConfigParser()
        if os.path.exists(CONFIG_FILE):
            config.read(CONFIG_FILE)
        else:
            raise FileNotFoundError(f"Configuration file '{CONFIG_FILE}' not found.")
        self.api_token = config["TELEGRAM"]["api_token"]
        self._config_user_ids = config["USERS"]["interacted_users"]
        self.allowed_users = self._parse_user_ids(self._config_user_ids)
        self.bot = telebot.TeleBot(self.api_token)
        self.interacted_users = []
        self.polling_thread = None
        self.stop_event = Event()
        self.logger = logger
        self.load_interacted_users()
        self.setup_handlers()

    def setup_handlers(self):
        """
        Sets up message handlers for the bot to respond to incoming messages.
        """

        @self.bot.message_handler(func=lambda message: True)
        def handle_any_message(message):
            user_id = message.chat.id
            # Only chat IDs listed in bot_config.ini may subscribe. Anyone can
            # message a public bot, and subscribers receive backup passwords.
            if user_id not in self.allowed_users:
                self.logger.warning(f"Ignoring Telegram message from unauthorized chat ID {user_id}")
                self.bot.send_message(
                    user_id,
                    f"Not authorized. Ask the administrator to add chat ID {user_id} "
                    "to [USERS] interacted_users in bot_config.ini.",
                )
                return
            if user_id not in self.interacted_users:
                self.interacted_users.append(user_id)
                self.save_interacted_users()
                self.bot.send_message(user_id, "You're now registered for notifications!")

    def get_username(self, user_id):
        """
        Retrieves the username of a user given their user ID.
        """
        try:
            chat = self.bot.get_chat(user_id)
            return chat.username if chat.username else None
        except telebot.apihelper.ApiTelegramException as e:
            self.logger.error(
                f"Telegram API error occurred while retrieving username for user ID {user_id}: {e}"
            )
            return None
        except telebot.apihelper.ApiException as e:
            self.logger.error(f"API error occurred while retrieving username for user ID {user_id}: {e}")
            return None
        except Exception as e:
            self.logger.error(
                f"Unexpected error occurred while retrieving username for user ID {user_id}: {e}"
            )
            return None

    @staticmethod
    def _parse_user_ids(raw):
        """Parse a comma-separated list of numeric chat IDs, skipping placeholders."""
        ids = []
        for uid in (raw or "").split(","):
            uid = uid.strip()
            try:
                ids.append(int(uid))
            except ValueError:
                continue
        return ids

    def load_interacted_users(self):
        """
        Loads interacted users from runtime JSON file, falling back to config values.
        The runtime file is separate from bot_config.ini to avoid writing back to config.
        Runtime entries not present in the config allowlist are dropped.
        """
        # Try runtime file first
        if os.path.exists(_RUNTIME_USERS_FILE):
            try:
                with open(_RUNTIME_USERS_FILE) as f:
                    data = json.load(f)
                runtime_ids = data.get("user_ids", [])
                self.interacted_users = [uid for uid in runtime_ids if uid in self.allowed_users]
                dropped = len(runtime_ids) - len(self.interacted_users)
                if dropped:
                    self.logger.warning(f"Dropped {dropped} unauthorized chat ID(s) from runtime users file")
                    self.save_interacted_users()
                return
            except (json.JSONDecodeError, OSError) as e:
                self.logger.warning(f"Failed to read runtime users file: {e}")

        # Fall back to config values
        self.interacted_users = list(self.allowed_users)

    def save_interacted_users(self):
        """
        Saves interacted users to a separate runtime JSON file.
        Does not modify bot_config.ini.
        """
        try:
            fd = os.open(_RUNTIME_USERS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)  # the mode above only applies when the file is created
            with os.fdopen(fd, "w") as f:
                json.dump({"user_ids": self.interacted_users}, f)
        except OSError as e:
            self.logger.error(f"Failed to save runtime users file: {e}")

    def send_notification(self, text):
        """
        Sends a notification to all interacted users.
        Continues sending to remaining users even if one fails.
        """
        if not self.interacted_users:
            return
        failed_count = 0
        for user_id in self.interacted_users:
            try:
                self.bot.send_message(user_id, text)
                self.logger.info(f"Notification sent to {self.get_username(user_id)}: {text}")
            except telebot.apihelper.ApiTelegramException as e:
                self.logger.error(f"Telegram API error sending to user {user_id}: {e}")
                failed_count += 1
            except telebot.apihelper.ApiException as e:
                self.logger.error(f"API error sending to user {user_id}: {e}")
                failed_count += 1
            except Exception as e:
                self.logger.error(f"Failed to send notification to user {user_id}: {e}")
                failed_count += 1
        if failed_count == len(self.interacted_users):
            self.logger.error("Failed to send notification to all users. Try to interact with the bot first.")
            self.start_polling_for_limited_time(timeout=5)

    def send_image(self, file_path, caption=None):
        """
        Sends an image to all interacted users.
        """
        if not self.interacted_users:
            return
        for user_id in self.interacted_users:
            try:
                with open(file_path, "rb") as f:
                    self.bot.send_photo(user_id, photo=f, caption=caption)
                self.logger.info(f"Image sent to {self.get_username(user_id)}")
            except telebot.apihelper.ApiTelegramException as e:
                self.logger.error(f"Telegram API error sending image to user {user_id}: {e}")
            except telebot.apihelper.ApiException as e:
                self.logger.error(f"API error sending image to user {user_id}: {e}")
            except Exception as e:
                self.logger.error(f"Failed to send image to user {user_id}: {e}")

    def send_geolocation(self, latitude, longitude, live_period=60):
        """
        Sends a geolocation to all interacted users.
        """
        if not self.interacted_users:
            return
        for user_id in self.interacted_users:
            try:
                self.bot.send_location(
                    user_id, latitude=latitude, longitude=longitude, live_period=live_period
                )
                self.logger.info(
                    f"Geolocation sent to user ID {user_id}: Latitude {latitude}, Longitude {longitude}"
                )
            except telebot.apihelper.ApiTelegramException as e:
                self.logger.error(f"Telegram API error sending geolocation to user {user_id}: {e}")
            except telebot.apihelper.ApiException as e:
                self.logger.error(f"API error sending geolocation to user {user_id}: {e}")
            except Exception as e:
                self.logger.error(f"Failed to send geolocation to user {user_id}: {e}")

    def send_document(self, file_path=None, caption=None, document=None):
        """
        Sends a document to all interacted users.
        Supports both file paths and file-like objects (e.g. BytesIO).
        """
        if not self.interacted_users:
            return
        for user_id in self.interacted_users:
            try:
                if document is not None:
                    self.bot.send_document(user_id, document=document, caption=caption)
                    if hasattr(document, "seek"):
                        document.seek(0)
                elif file_path is not None:
                    with open(file_path, "rb") as f:
                        self.bot.send_document(user_id, document=f, caption=caption)
                else:
                    self.logger.error("send_document called with no file_path or document")
                    return
                self.logger.info(f"Document sent to {self.get_username(user_id)}")
            except telebot.apihelper.ApiTelegramException as e:
                self.logger.error(f"Telegram API error sending document to user {user_id}: {e}")
            except telebot.apihelper.ApiException as e:
                self.logger.error(f"API error sending document to user {user_id}: {e}")
            except Exception as e:
                self.logger.error(f"Failed to send document to user {user_id}: {e}")

    def start_polling_for_limited_time(self, timeout=30):
        """
        Starts polling the Telegram API for a limited amount of time.
        Runs in a background thread so it doesn't block the caller.
        """
        self.stop_event.clear()
        self.polling_thread = Thread(target=self._poll_and_stop, args=(timeout,), daemon=True)
        self.polling_thread.start()

    def _poll_and_stop(self, timeout):
        """
        Internal method: starts polling, waits for timeout, then stops.
        """
        polling_thread = Thread(target=self.start_polling, daemon=True)
        polling_thread.start()
        time.sleep(timeout)
        self.stop_polling()

    def start_polling(self):
        """
        Starts the bot's polling process to receive messages.
        """
        try:
            self.logger.info("Starting bot polling...")
            self.bot.polling(non_stop=True)
        except Exception as e:
            self.logger.error(f"Polling error occurred: {e}")
        finally:
            self.stop_event.set()

    def stop_polling(self):
        """
        Stops the bot's polling process.
        """
        if self.polling_thread and self.polling_thread.is_alive():
            self.logger.info("Stopping bot polling...")
            self.bot.stop_polling()
            self.polling_thread.join(timeout=10)
