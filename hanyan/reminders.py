"""
Hanyan Chat — 提醒 / 定时任务系统
- 短期提醒（<10 分钟）：threading.Timer
- 重复提醒：每天固定时间
- 一次性提醒：指定日期时间
所有提醒持久化到 data/reminders.json。

解耦自 bot.py：不直接依赖 SerenaBot/HanyanBot 实例，而是接受两个回调，
方便单独测试、也避免 reminders.py ↔ bot.py 之间的循环 import。
"""

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from . import config

logger = logging.getLogger("hanyan.reminders")

REMINDERS_FILE = os.path.join(config.ROOT_DIR, "data", "reminders.json")

# send_callback(room_id, user_id, message) -> None
SendCallback = Callable[[str, str, str], None]
# get_room_id(user_id) -> Optional[str]（用于重复/长期提醒触发时，session 可能已被回收，
# 需要一个兜底方式找到应该发到哪个房间）
GetRoomIdCallback = Callable[[str], Optional[str]]


class ReminderSystem:
    """提醒系统。"""

    def __init__(self, send_callback: SendCallback, get_room_id: GetRoomIdCallback):
        self._send = send_callback
        self._get_room_id = get_room_id
        self._lock = threading.Lock()
        self._active_timers: dict[str, threading.Timer] = {}
        self._recurring_reminders: list[dict] = []
        self._next_timer_id = 0
        self._checker_running = False
        os.makedirs(os.path.dirname(REMINDERS_FILE), exist_ok=True)

    # ── 永久存储 ─────────────────────────────────────────────────

    def _load_reminders(self):
        if not os.path.exists(REMINDERS_FILE):
            return
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self._recurring_reminders = data.get("recurring", [])
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load reminders: %s", e)

    def _save_reminders(self):
        try:
            with self._lock:
                data = {"recurring": self._recurring_reminders}
            tmp = REMINDERS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            shutil.move(tmp, REMINDERS_FILE)
        except OSError as e:
            logger.error("Failed to save reminders: %s", e)

    # ── 设置提醒 ─────────────────────────────────────────────────

    def set_short_reminder(self, user_id: str, room_id: str, delay_seconds: int, message: str):
        """设置短期一次性提醒（延迟 <= 600 秒）。"""
        timer_id = f"timer_{self._next_timer_id}_{int(time.time())}"
        self._next_timer_id += 1

        def callback():
            logger.info("Short reminder triggered for %s: %s", user_id, message)
            self._send(room_id, user_id, message)
            with self._lock:
                self._active_timers.pop(timer_id, None)

        timer = threading.Timer(delay_seconds, callback)
        timer.daemon = True
        timer._deadline = time.monotonic() + delay_seconds
        with self._lock:
            self._active_timers[timer_id] = timer
        timer.start()
        logger.info("Short reminder set for %s in %ds: %s", user_id, delay_seconds, message)

    def set_long_reminder(self, user_id: str, room_id: str, target_dt: datetime, message: str):
        """设置长期一次性提醒（保存到文件，由检查线程触发）。"""
        reminder = {
            "type": "one-off",
            "user_id": user_id,
            "room_id": room_id,
            "target_datetime": target_dt.strftime("%Y-%m-%d %H:%M"),
            "message": message,
        }
        self._save_one_off(reminder)
        logger.info("Long reminder set for %s at %s: %s", user_id, target_dt.strftime("%Y-%m-%d %H:%M"), message)

    def _save_one_off(self, reminder: dict):
        try:
            existing = []
            if os.path.exists(REMINDERS_FILE):
                with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                existing = existing_data.get("one_off", [])
            existing.append(reminder)
            data = {"recurring": self._recurring_reminders, "one_off": existing}
            tmp = REMINDERS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            shutil.move(tmp, REMINDERS_FILE)
        except OSError as e:
            logger.error("Failed to save one-off reminder: %s", e)

    def set_recurring_reminder(self, user_id: str, room_id: str, time_str: str, message: str):
        """设置每日重复提醒。"""
        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            logger.warning("Invalid time format for recurring: %s", time_str)
            return

        reminder = {"type": "recurring", "user_id": user_id, "room_id": room_id, "time_str": time_str, "message": message}
        with self._lock:
            for r in self._recurring_reminders:
                if (r.get("user_id") == user_id and r.get("time_str") == time_str and r.get("message") == message):
                    logger.info("Duplicate recurring reminder skipped")
                    return
            self._recurring_reminders.append(reminder)
        self._save_reminders()
        logger.info("Recurring reminder set for %s at daily %s: %s", user_id, time_str, message)

    def list_reminders(self, user_id: str) -> list[dict]:
        """列出用户的所有提醒。"""
        result = []
        with self._lock:
            for tid, timer in self._active_timers.items():
                deadline = getattr(timer, "_deadline", None)
                remaining = max(0, int(deadline - time.monotonic())) if deadline else 0
                result.append({"id": tid, "type": "short", "remaining_seconds": remaining})
        try:
            if os.path.exists(REMINDERS_FILE):
                with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for r in data.get("one_off", []):
                    if r.get("user_id") == user_id:
                        result.append({"type": "one-off", "target": r["target_datetime"], "message": r["message"]})
                for r in data.get("recurring", []):
                    if r.get("user_id") == user_id:
                        result.append({"type": "recurring", "time": r["time_str"], "message": r["message"]})
        except (json.JSONDecodeError, OSError):
            pass
        return result

    # ── 检查线程 ─────────────────────────────────────────────────

    def _checker_loop(self):
        self._checker_running = True
        last_checked_minute = -1
        while self._checker_running:
            try:
                now = datetime.now()
                current_minute = now.hour * 60 + now.minute
                if current_minute != last_checked_minute:
                    last_checked_minute = current_minute
                    self._check_recurring(now)
                    self._check_one_off(now)
                time.sleep(15)
            except Exception as e:
                logger.error("Reminder checker error: %s", e, exc_info=True)
                time.sleep(30)

    def _check_recurring(self, now: datetime):
        time_str = now.strftime("%H:%M")
        with self._lock:
            reminders = list(self._recurring_reminders)
        for r in reminders:
            if r.get("time_str") == time_str:
                user_id = r["user_id"]
                logger.info("Recurring reminder triggered for %s: %s", user_id, r["message"])
                room_id = r.get("room_id") or self._get_room_id(user_id)
                if room_id:
                    self._send(room_id, user_id, r["message"])

    def _check_one_off(self, now: datetime):
        now_str = now.strftime("%Y-%m-%d %H:%M")
        triggered: list[dict] = []
        remaining: list[dict] = []
        try:
            if not os.path.exists(REMINDERS_FILE):
                return
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for r in data.get("one_off", []):
                if r.get("target_datetime", "") <= now_str:
                    triggered.append(r)
                else:
                    remaining.append(r)
            if triggered:
                data["one_off"] = remaining
                tmp = REMINDERS_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                shutil.move(tmp, REMINDERS_FILE)
                for r in triggered:
                    user_id = r["user_id"]
                    logger.info("One-off reminder triggered for %s: %s", user_id, r["message"])
                    room_id = r.get("room_id") or self._get_room_id(user_id)
                    if room_id:
                        self._send(room_id, user_id, r["message"])
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Reminder check error: %s", e)

    def start(self):
        """启动提醒检查线程。"""
        self._load_reminders()
        t = threading.Thread(target=self._checker_loop, name="ReminderChecker", daemon=True)
        t.start()
        logger.info("Reminder checker started")

    def stop(self):
        """停止提醒检查线程。"""
        self._checker_running = False
        with self._lock:
            for timer in self._active_timers.values():
                timer.cancel()
            self._active_timers.clear()
