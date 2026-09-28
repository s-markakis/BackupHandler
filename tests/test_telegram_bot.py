"""Tests for Telegram bot authorisation (telebot mocked, no network)."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from src import telegram_bot


@pytest.fixture
def bot_env(tmp_dir, monkeypatch):
    cfg = tmp_dir / "bot_config.ini"
    cfg.write_text("[TELEGRAM]\napi_token = 123:abc\n\n[USERS]\ninteracted_users = 111, 222\n")
    monkeypatch.setattr(telegram_bot, "CONFIG_FILE", str(cfg))
    monkeypatch.setattr(telegram_bot, "_RUNTIME_USERS_FILE", str(tmp_dir / ".bot_users.json"))
    handlers = []
    fake = mock.Mock()
    fake.message_handler.side_effect = lambda **_: lambda fn: handlers.append(fn) or fn
    monkeypatch.setattr(telegram_bot.telebot, "TeleBot", mock.Mock(return_value=fake))
    return tmp_dir, fake, handlers


def _message(chat_id):
    return mock.Mock(chat=mock.Mock(id=chat_id))


def test_unknown_chat_cannot_subscribe(bot_env, logger):
    _tmp, fake, handlers = bot_env
    bot = telegram_bot.TelegramBot(logger)
    handlers[0](_message(999))
    assert 999 not in bot.interacted_users
    bot.send_notification("password: hunter2")
    recipients = [c.args[0] for c in fake.send_message.call_args_list if "password" in c.args[1]]
    assert recipients == [111, 222]


def test_stale_runtime_entries_are_dropped(bot_env, logger):
    tmp, _fake, _h = bot_env
    runtime = tmp / ".bot_users.json"
    runtime.write_text(json.dumps({"user_ids": [111, 666]}))
    bot = telegram_bot.TelegramBot(logger)
    assert bot.interacted_users == [111]
    assert json.loads(runtime.read_text())["user_ids"] == [111]
    assert runtime.stat().st_mode & 0o777 == 0o600


def test_placeholder_ids_are_ignored(bot_env, logger):
    tmp, _f, _h = bot_env
    (tmp / "bot_config.ini").write_text(
        "[TELEGRAM]\napi_token = t\n\n[USERS]\ninteracted_users = YOUR_USER_ID\n"
    )
    assert telegram_bot.TelegramBot(logger).allowed_users == []
