"""Small, dependency-free Telegram bridge for Brahma Echo.

The bot uses Telegram's long-polling API rather than a public webhook, so it
works on a normal home PC without opening another inbound port.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

import requests


class TelegramBotService:
    def __init__(self, *, status_callback: Callable[[str], None] | None = None,
                 log_callback: Callable[[str], None] | None = None) -> None:
        self._status = status_callback or (lambda _message: None)
        self._log = log_callback or (lambda _message: None)
        self._submitter: Callable[[str, str], None] | None = None
        self._token = ""
        self._chat_id = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._offset: int | None = None
        self._reply_chats: deque[str] = deque()

    def bind_app_submitter(self, submitter: Callable[[str, str], None]) -> None:
        self._submitter = submitter

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self, token: str, chat_id: str = "") -> None:
        token = (token or "").strip()
        if not token:
            raise ValueError("Telegram bot token is missing.")
        if self.is_running() and token == self._token and (chat_id or "").strip() == self._chat_id:
            return
        self.stop()
        self._token, self._chat_id = token, (chat_id or "").strip()
        self._offset = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="TelegramBotThread", daemon=True)
        self._thread.start()
        self._status("Connecting Telegram bot...")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None

    def _api(self, method: str, **payload):
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        response = requests.post(url, json=payload, timeout=35)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram API request failed"))
        return data.get("result")

    def _run(self) -> None:
        try:
            me = self._api("getMe")
            self._status(f"Telegram connected as @{me.get('username', 'bot')}")
        except Exception as exc:
            self._status(f"Telegram error: {exc}")
            return
        while not self._stop.is_set():
            try:
                updates = self._api("getUpdates", offset=self._offset, timeout=25,
                                    allowed_updates=["message"])
                for update in updates or []:
                    self._offset = int(update.get("update_id", 0)) + 1
                    message = update.get("message") or {}
                    text = str(message.get("text") or "").strip()
                    chat = str((message.get("chat") or {}).get("id") or "")
                    if not text or not chat:
                        continue
                    if self._chat_id and chat != self._chat_id:
                        self._send(chat, "This bot is restricted to its configured chat.")
                        continue
                    if not self._chat_id:
                        self._chat_id = chat
                        self._status(f"Telegram paired to chat {chat}")
                    if text in {"/start", "/help"}:
                        self._send(chat, "Brahma Echo connected. Send a command and I will forward it to your PC.")
                        continue
                    self._reply_chats.append(chat)
                    if self._submitter:
                        self._submitter(text, "telegram")
            except requests.RequestException as exc:
                if not self._stop.is_set():
                    self._status(f"Telegram network error: {exc}")
                    time.sleep(3)
            except Exception as exc:
                self._status(f"Telegram error: {exc}")
                time.sleep(2)

    def _send(self, chat_id: str, text: str) -> None:
        if not self._token or not text:
            return
        try:
            self._api("sendMessage", chat_id=chat_id, text=text[:4000])
        except Exception as exc:
            self._log(f"WARN: Telegram send failed: {exc}")

    def mirror_chat_event(self, event: dict) -> None:
        if not self.is_running() or not isinstance(event, dict):
            return
        if str(event.get("source") or "").lower() == "telegram" and str(event.get("role") or "").lower() == "user":
            return
        if str(event.get("role") or "").lower() != "assistant":
            return
        text = str(event.get("text") or "").strip()
        if not text:
            return
        target = self._reply_chats.popleft() if self._reply_chats else self._chat_id
        if target:
            self._send(target, text)
