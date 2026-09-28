# Telegram Bot Handler

![Build Status](https://img.shields.io/badge/build-passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-blue)
![Version](https://img.shields.io/badge/version-1.0.0-orange)

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Methods](#methods)
- [Logging](#logging)
- [Contribution Guidelines](#contribution-guidelines)
- [License](#license)

## Overview

This Python module provides a handler for a Telegram bot using the `telebot` library. It allows the bot to interact with users, send notifications, images, geolocations, and documents, and manage user registrations.

## Features

- Load configuration from an INI file.
- Register users who interact with the bot.
- Send notifications, images, geolocations, and documents to users.
- Handle errors gracefully with logging.

## Requirements

- Python 3.x
- `pyTelegramBotAPI` library
- `configparser` library

## Installation

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd <repository-directory>
   ```

2. Install the required packages:
   ```bash
   pip3 install pyTelegramBotAPI
   ```

3. Create a configuration file at `config/bot_config.ini` with the following structure:
   ```ini
   [TELEGRAM]
   api_token = YOUR_API_TOKEN

   [USERS]
   interacted_users = 123456789, 987654321
   ```

   **Note**: Replace `YOUR_API_TOKEN` with your bot token. `interacted_users` is an
   **allowlist**: only these chat IDs can subscribe and receive notifications (which
   may include `zip_pw` archive passwords). A chat that is not listed gets a reply
   with its chat ID so an administrator can add it.

## Usage

To use the `TelegramBot` class, instantiate it with a logger and start the bot:

```python
import logging
from src.telegram_bot import TelegramBot

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create and start the bot
bot = TelegramBot(logger)
bot.start_polling_for_limited_time(timeout=60)  # Poll for 60 seconds
```

### Example of Sending a Notification

```python
bot.send_notification("Hello, this is a test notification!")
```

## Methods

### `send_notification(text)`

Sends a notification to all interacted users.

### `send_image(file_path, caption=None)`

Sends an image to all interacted users.

### `send_geolocation(latitude, longitude, live_period=60)`

Sends a geolocation to all interacted users.

### `send_document(file_path, caption=None)`

Sends a document to all interacted users.

## Logging

All actions and errors are logged using the provided logger. Ensure to configure the logger as needed for your application.

## Contribution Guidelines

We welcome contributions! Please follow these steps:

1. Fork the repository.
2. Create a new branch (`git checkout -b feature/YourFeature`).
3. Make your changes and commit them (`git commit -m 'Add some feature'`).
4. Push to the branch (`git push origin feature/YourFeature`).
5. Open a pull request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
