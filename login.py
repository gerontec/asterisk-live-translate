#!/home/gh/python/venv_tgcall/bin/python3
"""One-time Telegram login → creates telegram_translate.session next to this file.
Run interactively:  venv_tgcall/bin/python login.py
Prompts for phone number, the login code Telegram sends, and 2FA password if set.
"""
from pyrogram import Client
from tg_credentials import API_ID, API_HASH

SESSION  = "/home/gh/python/telegram_translate/telegram_translate"

with Client(SESSION, api_id=API_ID, api_hash=API_HASH) as app:
    me = app.get_me()
    print(f"Logged in as {me.first_name} (id={me.id}). Session saved: {SESSION}.session")
