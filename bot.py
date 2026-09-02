# ==================== bot.py - ربات کامل مطالعه هوشمند ====================
# نسخه نهایی با سیستم سطوح، چت AI، ساخت دستی برنامه، و مدیریت کامل

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum

import psycopg2
from psycopg2 import pool, sql
import jdatetime
import pytz
import httpx
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, JobQueue
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

from openai import AsyncOpenAI
import asyncio

# ==================== بارگذاری تنظیمات از محیط ====================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_BASE_URL = os.getenv("AI_BASE_URL")
AI_MODEL = os.getenv("AI_MODEL")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "study_bot_db"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
    "port": os.getenv("DB_PORT", "5432")
}

ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

IRAN_TZ = pytz.timezone('Asia/Tehran')

GRADE_RULES = {
    1: {"name": "آسان", "duration": 20, "emoji": "⭐"},
    2: {"name": "نسبتاً آسان", "duration": 30, "emoji": "⭐⭐"},
    3: {"name": "متوسط", "duration": 45, "emoji": "⭐⭐⭐"},
    4: {"name": "نسبتاً سخت", "duration": 60, "emoji": "⭐⭐⭐⭐"},
    5: {"name": "سخت", "duration": 75, "emoji": "⭐⭐⭐⭐⭐"},
}

PLAN_LEVELS = {
    0: {"name": "اولیه", "days": 1, "emoji": "🌱"},
    1: {"name": "روزانه", "days": 2, "emoji": "📈"},
    2: {"name": "شخصی‌سازی‌شده", "days": 8, "emoji": "🎯"},
    3: {"name": "شناور", "days": 15, "emoji": "🚀"}
}

# ==================== لاگ ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== AsyncOpenAI Client ====================
client = AsyncOpenAI(
    base_url=AI_BASE_URL,
    api_key=AI_API_KEY,
    timeout=httpx.Timeout(30.0, connect=10.0)
)

# ==================== دیتابیس ====================
db_pool = None

def init_db_pool():
    global db_pool
    try:
        db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 20,
            host=DB_CONFIG["host"],
            database=DB_CONFIG["database"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            port=DB_CONFIG["port"]
        )
        logger.info("✅ Connection Pool ایجاد شد")
    except Exception as e:
        logger.error(f"❌ خطا در اتصال به دیتابیس: {e}")
        raise

def get_connection():
    return db_pool.getconn()

def return_connection(conn):
    db_pool.putconn(conn)

def execute_query(query, params=None, fetch=False, fetchall=False, commit=True):
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(query, params or ())
        if fetch:
            result = cursor.fetchone()
            if commit:
                conn.commit()
            return result
        elif fetchall:
            result = cursor.fetchall()
            if commit:
                conn.commit()
            return result
        else:
            if commit:
                conn.commit()
            return cursor.rowcount
    except Exception as e:
        logger.error(f"❌ خطا در اجرای کوئری: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_connection(conn)

# ==================== توابع کمکی با تاریخ ایران ====================
def get_iran_now() -> datetime:
    return datetime.now(IRAN_TZ)

def get_today_date() -> str:
    return get_iran_now().strftime("%Y-%m-%d")

def get_today_shamsi() -> str:
    now = get_iran_now()
    jdate = jdatetime.datetime.fromgregorian(datetime=now)
    return jdate.strftime("%Y/%m/%d")

def get_iran_time_str() -> str:
    return get_iran_now().strftime("%H:%M")

def get_shamsi_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        dt = IRAN_TZ.localize(dt)
        jdate = jdatetime.datetime.fromgregorian(datetime=dt)
        return jdate.strftime("%Y/%m/%d")
    except:
        return date_str

def format_time_hours_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} دقیقه"
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f"{hours} ساعت"
    return f"{hours} ساعت و {mins} دقیقه"

def convert_persian_to_int(text: str) -> int:
    persian_to_english = {
        '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4',
        '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9'
    }
    result = text
    for persian, english in persian_to_english.items():
        result = result.replace(persian, english)
    try:
        return int(result)
    except:
        return None

def time_to_minutes(time_str: str) -> int:
    try:
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except:
        return 0

def minutes_to_time(minutes: int) -> str:
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"

def get_iran_date_for_db() -> str:
    return get_today_date()

# ==================== ایجاد جداول دیتابیس ====================
def create_tables():
    queries = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id VARCHAR(50) UNIQUE NOT NULL,
            username VARCHAR(100),
            full_name VARCHAR(200),
            goal VARCHAR(50),
            grade VARCHAR(50),
            field VARCHAR(50),
            exam_date DATE,
            study_hours_per_week INTEGER,
            peak_time VARCHAR(20),
            learning_style VARCHAR(30),
            focus_duration INTEGER DEFAULT 45,
            break_duration INTEGER DEFAULT 10,
            weak_subjects JSONB,
            strong_subjects JSONB,
            daily_schedule JSONB,
            is_active BOOLEAN DEFAULT TRUE,
            is_onboarded BOOLEAN DEFAULT FALSE,
            current_phase INTEGER DEFAULT 0,
            plan_level INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity_date DATE,
            version INTEGER DEFAULT 1
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS subject_status (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            subject VARCHAR(50) NOT NULL,
            level VARCHAR(20),
            mastery_score FLOAT DEFAULT 0,
            total_sessions INTEGER DEFAULT 0,
            completed_sessions INTEGER DEFAULT 0,
            total_study_minutes INTEGER DEFAULT 0,
            avg_score FLOAT,
            best_score FLOAT,
            worst_score FLOAT,
            current_chapter INTEGER,
            current_topic VARCHAR(200),
            completed_chapters JSONB,
            completed_topics JSONB,
            weak_chapters JSONB,
            weak_topics JSONB,
            strong_topics JSONB,
            progress FLOAT DEFAULT 0,
            improvement_rate FLOAT DEFAULT 0,
            avg_session_duration INTEGER,
            best_time VARCHAR(20),
            last_studied DATE,
            last_score FLOAT,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, subject)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            date DATE NOT NULL,
            session_id INTEGER,
            subject VARCHAR(50) NOT NULL,
            topic VARCHAR(200),
            activity_type VARCHAR(30),
            planned_duration INTEGER,
            actual_duration INTEGER,
            start_time TIME,
            end_time TIME,
            score FLOAT,
            status VARCHAR(20),
            difficulty VARCHAR(20),
            focus_rating INTEGER CHECK (focus_rating BETWEEN 1 AND 5),
            energy_level INTEGER CHECK (energy_level BETWEEN 1 AND 5),
            mood VARCHAR(20),
            distractions JSONB,
            notes TEXT,
            break_duration INTEGER,
            break_time TIME,
            pages_count INTEGER,
            test_count INTEGER,
            correct_count INTEGER,
            part_order INTEGER DEFAULT 0,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_archived BOOLEAN DEFAULT FALSE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS advisory_rules (
            id SERIAL PRIMARY KEY,
            topic VARCHAR(50) NOT NULL,
            label VARCHAR(50),
            condition TEXT,
            advice TEXT NOT NULL,
            priority INTEGER DEFAULT 5,
            time VARCHAR(20),
            frequency VARCHAR(30),
            days JSONB,
            applicable_for JSONB,
            subjects JSONB,
            is_active BOOLEAN DEFAULT TRUE,
            is_system_generated BOOLEAN DEFAULT FALSE,
            usage_count INTEGER DEFAULT 0,
            success_rate FLOAT DEFAULT 0,
            last_used TIMESTAMP,
            created_by BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            version INTEGER DEFAULT 1
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS study_sessions (
            session_id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            date VARCHAR(20),
            total_parts INT,
            completed_parts INT DEFAULT 0,
            edit_count INT DEFAULT 0,
            max_edits INT DEFAULT 2,
            confirmed BOOLEAN DEFAULT FALSE,
            is_finished BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            time_slots TEXT,
            topics TEXT,
            archived BOOLEAN DEFAULT FALSE,
            plan_level INT DEFAULT 0,
            UNIQUE(user_id, date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS study_parts (
            part_id SERIAL PRIMARY KEY,
            session_id INT REFERENCES study_sessions(session_id),
            part_number INT,
            title VARCHAR(200),
            grade INT,
            planned_minutes INT,
            actual_minutes INT DEFAULT 0,
            time_slot VARCHAR(50),
            completed BOOLEAN DEFAULT FALSE,
            is_hardest BOOLEAN DEFAULT FALSE,
            is_easiest BOOLEAN DEFAULT FALSE,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            pages INT DEFAULT 0,
            planned_start_time TIME,
            planned_end_time TIME,
            actual_start_time TIMESTAMP,
            actual_end_time TIMESTAMP,
            is_fixed_time BOOLEAN DEFAULT FALSE,
            delay_minutes INT DEFAULT 0,
            alert_sent BOOLEAN DEFAULT FALSE,
            reason TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_insights (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            analysis_date DATE,
            best_time VARCHAR(20),
            weakest_subject VARCHAR(50),
            strongest_subject VARCHAR(50),
            avg_daily_hours FLOAT,
            completion_rate FLOAT,
            time_patterns JSONB,
            performance_patterns JSONB,
            quality_patterns JSONB,
            burnout_risk VARCHAR(20),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, analysis_date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS daily_alerts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            part_id INTEGER REFERENCES study_parts(part_id) ON DELETE CASCADE,
            alert_time TIMESTAMP,
            message TEXT,
            sent BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS personalized_plans (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            date DATE NOT NULL,
            daily_plan JSONB NOT NULL,
            reasoning JSONB,
            expected_outcome JSONB,
            applied_advice_ids JSONB,
            is_active BOOLEAN DEFAULT TRUE,
            was_completed BOOLEAN DEFAULT FALSE,
            completion_report JSONB,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            role VARCHAR(20) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_quota (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE PRIMARY KEY,
            daily_messages INTEGER DEFAULT 0,
            last_reset DATE DEFAULT CURRENT_DATE,
            plan_type VARCHAR(20) DEFAULT 'trial',
            plan_expiry DATE,
            UNIQUE(user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS change_history (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES study_sessions(session_id),
            part_id INTEGER REFERENCES study_parts(part_id),
            action_type VARCHAR(50),
            previous_data JSONB,
            new_data JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_reverted BOOLEAN DEFAULT FALSE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS timer_state (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            part_id INTEGER REFERENCES study_parts(part_id),
            elapsed_seconds INTEGER DEFAULT 0,
            total_minutes INTEGER DEFAULT 0,
            is_running BOOLEAN DEFAULT FALSE,
            started_at TIMESTAMP,
            last_update TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    ]
    
    for query in queries:
        try:
            execute_query(query)
        except Exception as e:
            logger.warning(f"خطا در ایجاد جدول: {e}")
    
    # ایجاد تابع و تریگر برای optimistic locking
    try:
        execute_query("""
            CREATE OR REPLACE FUNCTION update_version()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.version = OLD.version + 1;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        
        execute_query("""
            DROP TRIGGER IF EXISTS update_users_version ON users;
            CREATE TRIGGER update_users_version
            BEFORE UPDATE ON users
            FOR EACH ROW
            EXECUTE FUNCTION update_version();
        """)
    except Exception as e:
        logger.warning(f"خطا در ایجاد تریگر: {e}")
    
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)",
        "CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_subject_user ON subject_status(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_log(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_activity_date ON activity_log(date)",
        "CREATE INDEX IF NOT EXISTS idx_advice_active ON advisory_rules(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON study_sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_parts_session ON study_parts(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_insights_user ON user_insights(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_alerts_user ON daily_alerts(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_change_user ON change_history(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_timer_user ON timer_state(user_id)"
    ]
    
    for idx in indexes:
        try:
            execute_query(idx)
        except:
            pass
    
    logger.info("✅ جداول دیتابیس ایجاد شدند")

# ==================== کیبوردها ====================

def get_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["📝 برنامه امروز", "💬 چت با AI"],
        ["📊 گزارش", "📅 تقویم"],
        ["💰 خرید اشتراک", "👤 پروفایل"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_plan_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["🧠 ساخت با AI", "✏️ ساخت دستی"],
        ["🔙 بازگشت"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_part_buttons_initial(parts: List[Dict]) -> ReplyKeyboardMarkup:
    keyboard = []
    for part in parts:
        status = "⬜" if not part.get("completed") else "✅"
        grade_emoji = GRADE_RULES.get(part.get("grade", 3), GRADE_RULES[3])["emoji"]
        title = part.get("title", "بدون عنوان")
        planned_start = part.get("planned_start_time") or part.get("planned_start") or ""
        planned_end = part.get("planned_end_time") or part.get("planned_end") or ""
        time_info = ""
        if planned_start and planned_end:
            time_info = f" {planned_start}-{planned_end}"
        elif part.get("time_slot"):
            time_info = f" {part['time_slot']}"
        text = f"{status} {grade_emoji} {title} ({part.get('planned_minutes', 0)}د){time_info} ↕️ [{part.get('part_id', 0)}]"
        keyboard.append([text])
    keyboard.append(["✅ تایید برنامه"])
    keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_part_buttons_final(parts: List[Dict], show_date: bool = False) -> ReplyKeyboardMarkup:
    keyboard = []
    if not parts:
        keyboard.append(["🔙 بازگشت"])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    sorted_parts = sorted(parts, key=lambda x: x.get("part_number", 0))
    for part in sorted_parts:
        part_id = part.get("part_id")
        if not part_id:
            continue
        status = "✅" if part.get("completed", False) else "⬜"
        grade_emoji = GRADE_RULES.get(part.get("grade", 3), GRADE_RULES[3])["emoji"]
        title = part.get("title", "بدون عنوان")
        planned_start = part.get("planned_start_time") or part.get("planned_start") or ""
        planned_end = part.get("planned_end_time") or part.get("planned_end") or ""
        time_info = ""
        if planned_start and planned_end:
            time_info = f" {planned_start}-{planned_end}"
        elif part.get("time_slot"):
            time_info = f" {part['time_slot']}"
        fixed_tag = " 🔒" if part.get("is_fixed_time", False) else ""
        text = f"{status} {grade_emoji} {title} ({part.get('planned_minutes', 0)}د){time_info}{fixed_tag} [{part_id}]"
        keyboard.append([text])
    keyboard.append(["➕ اضافه کردن فعالیت"])
    keyboard.append(["✏️ ویرایش برنامه"])
    keyboard.append(["✅ اتمام برنامه"])
    keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_part_detail_buttons(part_id: int, is_timer_running: bool = False, has_timer_state: bool = False) -> ReplyKeyboardMarkup:
    keyboard = []
    if is_timer_running:
        keyboard.append(["⏹ توقف", "⏱ ادامه"])
    elif has_timer_state:
        keyboard.append(["⏱ ادامه", "⏹ توقف"])
    else:
        keyboard.append(["⏱ تایمر", "⏹ توقف"])
    keyboard.append(["✅ تکمیل", "🗑 حذف پارت"])
    keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_edit_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["✏️ ویرایش دستی"],
        ["💬 ویرایش با AI"],
        ["🔙 بازگشت"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_calendar_keyboard(dates: List[str]) -> ReplyKeyboardMarkup:
    keyboard = []
    for date_str in dates:
        shamsi = get_shamsi_date(date_str)
        keyboard.append([f"📅 {shamsi}"])
    keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_add_activity_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["📖 مطالعه", "📝 تست"],
        ["📚 خلاصه‌نویسی", "🔁 مرور"],
        ["🔙 بازگشت"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_duration_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["⏱ ۲۰ دقیقه", "⏱ ۳۰ دقیقه"],
        ["⏱ ۴۵ دقیقه", "⏱ ۶۰ دقیقه"],
        ["✏️ دلخواه"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_ai_chat_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["🔄 مکالمه جدید", "🔙 بازگشت به منو"],
        ["📊 مصرف امروز"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_confirm_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["✅ تایید تغییرات", "❌ لغو تغییرات"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_manual_plan_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["🧠 ساخت با AI", "✏️ ساخت دستی"],
        ["🔙 بازگشت"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==================== توابع کاربری ====================
def get_user_id_by_telegram(telegram_id: int) -> Optional[int]:
    result = execute_query(
        "SELECT id FROM users WHERE telegram_id = %s",
        (str(telegram_id),),
        fetch=True
    )
    return result[0] if result else None

def get_user_data(telegram_id: int) -> Optional[Dict]:
    result = execute_query(
        """SELECT id, telegram_id, username, full_name, goal, grade, field, 
                  is_onboarded, current_phase, weak_subjects, strong_subjects,
                  study_hours_per_week, peak_time, learning_style, focus_duration,
                  break_duration, plan_level, created_at, version
           FROM users WHERE telegram_id = %s""",
        (str(telegram_id),),
        fetch=True
    )
    if not result:
        return None
    return {
        "id": result[0],
        "telegram_id": result[1],
        "username": result[2],
        "full_name": result[3],
        "goal": result[4],
        "grade": result[5],
        "field": result[6],
        "is_onboarded": result[7],
        "current_phase": result[8],
        "weak_subjects": result[9] or [],
        "strong_subjects": result[10] or [],
        "study_hours_per_week": result[11],
        "peak_time": result[12],
        "learning_style": result[13],
        "focus_duration": result[14] or 45,
        "break_duration": result[15] or 10,
        "plan_level": result[16] or 0,
        "created_at": result[17],
        "version": result[18] or 1
    }

def get_plan_by_date(user_id: int, date_str: str) -> Optional[Dict]:
    query = """
    SELECT s.session_id, s.total_parts, s.completed_parts, s.edit_count, 
           s.max_edits, s.confirmed, s.time_slots, s.topics, s.is_finished, s.archived, s.plan_level
    FROM study_sessions s
    WHERE s.user_id = %s AND s.date = %s AND s.archived = FALSE
    ORDER BY s.created_at DESC
    LIMIT 1
    """
    result = execute_query(query, (user_id, date_str), fetch=True)
    if not result:
        return None
    
    session_id, total_parts, completed_parts, edit_count, max_edits, confirmed, time_slots, topics, is_finished, archived, plan_level = result
    
    query_parts = """
    SELECT part_id, part_number, title, grade, planned_minutes, actual_minutes,
           time_slot, completed, is_hardest, is_easiest, pages,
           to_char(started_at, 'HH24:MI') as start_time,
           to_char(completed_at, 'HH24:MI') as end_time,
           planned_start_time, planned_end_time,
           actual_start_time, actual_end_time, is_fixed_time, delay_minutes,
           reason, alert_sent
    FROM study_parts
    WHERE session_id = %s
    ORDER BY part_number
    """
    parts_result = execute_query(query_parts, (session_id,), fetchall=True)
    
    parts = []
    for row in parts_result:
        planned_start = row[13]
        planned_end = row[14]
        if planned_start and hasattr(planned_start, 'strftime'):
            planned_start = planned_start.strftime('%H:%M')
        if planned_end and hasattr(planned_end, 'strftime'):
            planned_end = planned_end.strftime('%H:%M')
        parts.append({
            "part_id": row[0],
            "part_number": row[1],
            "title": row[2],
            "grade": row[3],
            "planned_minutes": row[4],
            "actual_minutes": row[5] or 0,
            "time_slot": row[6] or "",
            "completed": row[7],
            "is_hardest": row[8],
            "is_easiest": row[9],
            "pages": row[10] or 0,
            "start_time": row[11] or "",
            "end_time": row[12] or "",
            "planned_start_time": planned_start or "",
            "planned_end_time": planned_end or "",
            "planned_start": planned_start or "",
            "planned_end": planned_end or "",
            "actual_start": row[15],
            "actual_end": row[16],
            "is_fixed_time": row[17] or False,
            "delay_minutes": row[18] or 0,
            "reason": row[19] or "",
            "alert_sent": row[20] or False
        })
    
    if isinstance(time_slots, str):
        try:
            time_slots = json.loads(time_slots)
        except:
            time_slots = []
    if isinstance(topics, str):
        try:
            topics = json.loads(topics)
        except:
            topics = []
    
    return {
        "session_id": session_id,
        "total_parts": total_parts,
        "completed_parts": completed_parts,
        "edit_count": edit_count,
        "max_edits": max_edits,
        "confirmed": confirmed,
        "time_slots": time_slots,
        "topics": topics,
        "parts": parts,
        "date": date_str,
        "is_finished": is_finished,
        "archived": archived,
        "plan_level": plan_level or 0
    }

def get_today_activities(user_id: int) -> List[Dict]:
    today = get_today_date()
    results = execute_query(
        """SELECT id, subject, topic, activity_type, planned_duration, actual_duration,
                  start_time, end_time, score, status, difficulty, focus_rating,
                  energy_level, mood, distractions, notes, pages_count, test_count,
                  correct_count, part_order, created_at
           FROM activity_log 
           WHERE user_id = %s AND date = %s AND is_archived = FALSE
           ORDER BY part_order ASC, created_at ASC""",
        (user_id, today),
        fetchall=True
    )
    return [
        {
            "id": r[0],
            "subject": r[1],
            "topic": r[2],
            "activity_type": r[3],
            "planned_duration": r[4] or 0,
            "actual_duration": r[5] or 0,
            "start_time": r[6],
            "end_time": r[7],
            "score": r[8],
            "status": r[9] or "pending",
            "difficulty": r[10],
            "focus_rating": r[11],
            "energy_level": r[12],
            "mood": r[13],
            "distractions": r[14] or [],
            "notes": r[15],
            "pages_count": r[16] or 0,
            "test_count": r[17] or 0,
            "correct_count": r[18] or 0,
            "part_order": r[19] or 0,
            "created_at": r[20]
        }
        for r in results
    ] if results else []

def get_recent_dates(user_id: int, days: int = 10) -> List[str]:
    results = execute_query(
        """SELECT DISTINCT date FROM study_sessions 
           WHERE user_id = %s 
           ORDER BY date DESC LIMIT %s""",
        (user_id, days),
        fetchall=True
    )
    return [r[0] for r in results] if results else []

def get_active_advice(user_id: int) -> List[Dict]:
    results = execute_query(
        """SELECT id, topic, label, condition, advice, priority, time, frequency,
                  days, subjects
           FROM advisory_rules 
           WHERE is_active = TRUE 
           ORDER BY priority DESC""",
        fetchall=True
    )
    return [
        {
            "id": r[0],
            "topic": r[1],
            "label": r[2],
            "condition": r[3],
            "advice": r[4],
            "priority": r[5],
            "time": r[6],
            "frequency": r[7],
            "days": r[8] or [],
            "subjects": r[9] or []
        }
        for r in results
    ] if results else []

def get_activity_by_id(activity_id: int) -> Optional[Dict]:
    result = execute_query(
        """SELECT id, user_id, subject, topic, activity_type, planned_duration, 
                  actual_duration, status, score, part_order
           FROM activity_log WHERE id = %s""",
        (activity_id,),
        fetch=True
    )
    if not result:
        return None
    return {
        "id": result[0],
        "user_id": result[1],
        "subject": result[2],
        "topic": result[3],
        "activity_type": result[4],
        "planned_duration": result[5] or 0,
        "actual_duration": result[6] or 0,
        "status": result[7] or "pending",
        "score": result[8],
        "part_order": result[9] or 0
    }

def get_subject_status(user_id: int) -> List[Dict]:
    results = execute_query(
        """SELECT subject, level, avg_score, total_sessions, completed_sessions,
                  total_study_minutes, progress, last_studied, last_score
           FROM subject_status 
           WHERE user_id = %s
           ORDER BY total_study_minutes DESC""",
        (user_id,),
        fetchall=True
    )
    return [
        {
            "subject": r[0],
            "level": r[1],
            "avg_score": r[2] or 0,
            "total_sessions": r[3] or 0,
            "completed_sessions": r[4] or 0,
            "total_study_minutes": r[5] or 0,
            "progress": r[6] or 0,
            "last_studied": r[7],
            "last_score": r[8]
        }
        for r in results
    ] if results else []

def get_user_insights(user_id: int) -> Optional[Dict]:
    result = execute_query(
        """SELECT best_time, weakest_subject, strongest_subject, avg_daily_hours,
                  completion_rate, time_patterns, performance_patterns, quality_patterns,
                  burnout_risk
           FROM user_insights 
           WHERE user_id = %s 
           ORDER BY analysis_date DESC LIMIT 1""",
        (user_id,),
        fetch=True
    )
    if not result:
        return None
    return {
        "best_time": result[0] or "نامشخص",
        "weakest_subject": result[1] or "نامشخص",
        "strongest_subject": result[2] or "نامشخص",
        "avg_daily_hours": result[3] or 0,
        "completion_rate": result[4] or 0,
        "time_patterns": result[5] or {},
        "performance_patterns": result[6] or {},
        "quality_patterns": result[7] or {},
        "burnout_risk": result[8] or "low"
    }

def get_last_n_days_data(user_id: int, days: int = 7) -> List[Dict]:
    results = execute_query(
        """SELECT date, total_parts, completed_parts, plan_level
           FROM study_sessions 
           WHERE user_id = %s 
           ORDER BY date DESC LIMIT %s""",
        (user_id, days),
        fetchall=True
    )
    return [
        {
            "date": r[0],
            "total_parts": r[1] or 0,
            "completed_parts": r[2] or 0,
            "plan_level": r[3] or 0
        }
        for r in results
    ] if results else []

def get_last_change(user_id: int) -> Optional[Dict]:
    """دریافت آخرین تغییر برای برگشت"""
    result = execute_query(
        """SELECT id, action_type, previous_data, part_id, session_id
           FROM change_history 
           WHERE user_id = %s AND is_reverted = FALSE
           ORDER BY created_at DESC LIMIT 1""",
        (user_id,),
        fetch=True
    )
    if not result:
        return None
    return {
        "id": result[0],
        "action_type": result[1],
        "previous_data": result[2],
        "part_id": result[3],
        "session_id": result[4]
    }

def save_change_history(user_id: int, session_id: int, part_id: int, 
                        action_type: str, previous_data: Dict, new_data: Dict = None) -> None:
    """ذخیره تغییر برای برگشت"""
    execute_query(
        """INSERT INTO change_history (user_id, session_id, part_id, action_type, previous_data, new_data)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (user_id, session_id, part_id, action_type, json.dumps(previous_data), json.dumps(new_data or {}))
    )

def revert_change(change_id: int) -> bool:
    """برگشت یک تغییر"""
    try:
        # دریافت داده قبلی
        result = execute_query(
            "SELECT part_id, previous_data, session_id FROM change_history WHERE id = %s",
            (change_id,),
            fetch=True
        )
        if not result:
            return False
        
        part_id = result[0]
        previous_data = result[1]
        session_id = result[2]
        
        # بازیابی داده قبلی
        if previous_data:
            # به‌روزرسانی پارت با داده قبلی
            for key, value in previous_data.items():
                if key == 'title':
                    execute_query("UPDATE study_parts SET title = %s WHERE part_id = %s", (value, part_id))
                elif key == 'planned_minutes':
                    execute_query("UPDATE study_parts SET planned_minutes = %s WHERE part_id = %s", (value, part_id))
                elif key == 'planned_start_time':
                    execute_query("UPDATE study_parts SET planned_start_time = %s WHERE part_id = %s", (value, part_id))
                elif key == 'planned_end_time':
                    execute_query("UPDATE study_parts SET planned_end_time = %s WHERE part_id = %s", (value, part_id))
                elif key == 'completed':
                    execute_query("UPDATE study_parts SET completed = %s WHERE part_id = %s", (value, part_id))
                elif key == 'time_slot':
                    execute_query("UPDATE study_parts SET time_slot = %s WHERE part_id = %s", (value, part_id))
                elif key == 'grade':
                    execute_query("UPDATE study_parts SET grade = %s WHERE part_id = %s", (value, part_id))
                elif key == 'reason':
                    execute_query("UPDATE study_parts SET reason = %s WHERE part_id = %s", (value, part_id))
        
        # علامت‌گذاری برگشت
        execute_query("UPDATE change_history SET is_reverted = TRUE WHERE id = %s", (change_id,))
        return True
    except Exception as e:
        logger.error(f"خطا در برگشت تغییر: {e}")
        return False

def get_timer_state(user_id: int, part_id: int) -> Optional[Dict]:
    """دریافت وضعیت تایمر"""
    result = execute_query(
        """SELECT elapsed_seconds, total_minutes, is_running, started_at
           FROM timer_state 
           WHERE user_id = %s AND part_id = %s""",
        (user_id, part_id),
        fetch=True
    )
    if not result:
        return None
    return {
        "elapsed_seconds": result[0] or 0,
        "total_minutes": result[1] or 0,
        "is_running": result[2] or False,
        "started_at": result[3]
    }

def save_timer_state(user_id: int, part_id: int, elapsed_seconds: int, total_minutes: int, is_running: bool) -> None:
    """ذخیره وضعیت تایمر"""
    execute_query(
        """INSERT INTO timer_state (user_id, part_id, elapsed_seconds, total_minutes, is_running, last_update)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id, part_id) DO UPDATE SET
               elapsed_seconds = EXCLUDED.elapsed_seconds,
               total_minutes = EXCLUDED.total_minutes,
               is_running = EXCLUDED.is_running,
               last_update = EXCLUDED.last_update""",
        (user_id, part_id, elapsed_seconds, total_minutes, is_running, get_iran_now())
    )

def clear_timer_state(user_id: int, part_id: int) -> None:
    """پاک کردن وضعیت تایمر"""
    execute_query("DELETE FROM timer_state WHERE user_id = %s AND part_id = %s", (user_id, part_id))

# ==================== توابع چت AI و سقف مصرف ====================

AI_CHAT_SYSTEM_PROMPT = """تو دستیار مطالعه‌ی هوشمند هستی. می‌توانی:
1. برنامه مطالعه امروز را بسازی یا تغییر دهی
2. پارت‌های برنامه را تکمیل کنی
3. فعالیت جدید به برنامه اضافه کنی
4. به سوالات کاربر پاسخ دهی

دستورات ویژه:
- "برنامه بساز" یا "برنامه امروز" → ساخت برنامه جدید
- "تغییر بده" یا "ویرایش کن" → تغییر برنامه موجود
- "تموم کردم" یا "انجام شد" → تکمیل پارت
- "اضافه کن" → اضافه کردن فعالیت جدید

پاسخ‌ها مختصر، مفید و به فارسی روان باشن.
از اطلاعات کاربر برای شخصی‌سازی پاسخ‌ها استفاده کن."""

def init_user_quota(user_id: int) -> None:
    try:
        execute_query(
            """INSERT INTO user_quota (user_id, daily_messages, last_reset, plan_type)
               VALUES (%s, 0, %s, 'trial')""",
            (user_id, get_today_date())
        )
    except:
        pass

def get_user_quota(user_id: int) -> Optional[Dict]:
    result = execute_query(
        """SELECT daily_messages, last_reset, plan_type, plan_expiry 
           FROM user_quota WHERE user_id = %s""",
        (user_id,),
        fetch=True
    )
    if not result:
        return None
    return {
        "daily_messages": result[0] or 0,
        "last_reset": result[1],
        "plan_type": result[2] or "trial",
        "plan_expiry": result[3]
    }

def get_remaining_messages(user_id: int) -> int:
    quota = get_user_quota(user_id)
    if not quota:
        return 10
    
    today = get_today_date()
    if str(quota["last_reset"]) != today:
        execute_query(
            "UPDATE user_quota SET daily_messages = 0, last_reset = %s WHERE user_id = %s",
            (today, user_id)
        )
        quota["daily_messages"] = 0
    
    if quota["plan_type"] == "trial":
        limit = 10
    elif quota["plan_type"] == "basic":
        limit = 15
    elif quota["plan_type"] == "premium":
        limit = 30
    else:
        limit = 10
    
    remaining = limit - quota["daily_messages"]
    return max(0, remaining)

def increment_quota(user_id: int) -> bool:
    try:
        execute_query(
            "UPDATE user_quota SET daily_messages = daily_messages + 1 WHERE user_id = %s",
            (user_id,)
        )
        return True
    except:
        return False

def save_chat_message(user_id: int, role: str, content: str) -> None:
    try:
        execute_query(
            """INSERT INTO chat_messages (user_id, role, content)
               VALUES (%s, %s, %s)""",
            (user_id, role, content)
        )
    except Exception as e:
        logger.error(f"خطا در ذخیره پیام چت: {e}")

def get_chat_history(user_id: int, limit: int = 10) -> List[Dict]:
    results = execute_query(
        """SELECT role, content FROM chat_messages 
           WHERE user_id = %s 
           ORDER BY created_at DESC LIMIT %s""",
        (user_id, limit * 2),
        fetchall=True
    )
    if not results:
        return []
    reversed_results = list(reversed(results))
    return [{"role": r[0], "content": r[1]} for r in reversed_results]

def clear_chat_history(user_id: int) -> None:
    execute_query("DELETE FROM chat_messages WHERE user_id = %s", (user_id,))

# ==================== توابع ذخیره‌سازی ====================
def save_user(user_data: Dict) -> Optional[int]:
    query = """
    INSERT INTO users (telegram_id, username, full_name, goal, grade, field,
                       exam_date, study_hours_per_week, peak_time, learning_style,
                       focus_duration, break_duration, weak_subjects, strong_subjects,
                       daily_schedule, is_active, is_onboarded, current_phase, plan_level)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (telegram_id) DO UPDATE SET
        username = EXCLUDED.username,
        full_name = EXCLUDED.full_name,
        goal = EXCLUDED.goal,
        grade = EXCLUDED.grade,
        field = EXCLUDED.field,
        exam_date = EXCLUDED.exam_date,
        study_hours_per_week = EXCLUDED.study_hours_per_week,
        peak_time = EXCLUDED.peak_time,
        learning_style = EXCLUDED.learning_style,
        focus_duration = EXCLUDED.focus_duration,
        break_duration = EXCLUDED.break_duration,
        weak_subjects = EXCLUDED.weak_subjects,
        strong_subjects = EXCLUDED.strong_subjects,
        daily_schedule = EXCLUDED.daily_schedule,
        is_onboarded = EXCLUDED.is_onboarded,
        plan_level = EXCLUDED.plan_level,
        updated_at = CURRENT_TIMESTAMP
    RETURNING id
    """
    result = execute_query(
        query,
        (
            user_data["telegram_id"],
            user_data.get("username"),
            user_data.get("full_name"),
            user_data.get("goal"),
            user_data.get("grade"),
            user_data.get("field"),
            user_data.get("exam_date"),
            user_data.get("study_hours_per_week"),
            user_data.get("peak_time"),
            user_data.get("learning_style"),
            user_data.get("focus_duration", 45),
            user_data.get("break_duration", 10),
            json.dumps(user_data.get("weak_subjects", [])),
            json.dumps(user_data.get("strong_subjects", [])),
            json.dumps(user_data.get("daily_schedule", {})),
            user_data.get("is_active", True),
            user_data.get("is_onboarded", False),
            user_data.get("current_phase", 0),
            user_data.get("plan_level", 0)
        ),
        fetch=True
    )
    return result[0] if result else None

def update_user_plan_level(user_id: int, level: int) -> bool:
    current = execute_query(
        "SELECT version FROM users WHERE id = %s",
        (user_id,),
        fetch=True
    )
    if not current:
        return False
    
    current_version = current[0]
    
    result = execute_query(
        """UPDATE users 
           SET plan_level = %s, 
               updated_at = CURRENT_TIMESTAMP,
               version = version + 1
           WHERE id = %s AND version = %s""",
        (level, user_id, current_version)
    )
    
    if result == 0:
        logger.warning(f"Optimistic lock failed for user {user_id}")
        return False
    return True

def save_activity(activity_data: Dict) -> Optional[int]:
    query = """
    INSERT INTO activity_log (
        user_id, date, subject, topic, activity_type, planned_duration,
        actual_duration, start_time, end_time, score, status, difficulty,
        focus_rating, energy_level, mood, distractions, notes,
        break_duration, pages_count, test_count, correct_count, part_order
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING id
    """
    result = execute_query(
        query,
        (
            activity_data["user_id"],
            activity_data["date"],
            activity_data["subject"],
            activity_data.get("topic"),
            activity_data.get("activity_type"),
            activity_data.get("planned_duration"),
            activity_data.get("actual_duration"),
            activity_data.get("start_time"),
            activity_data.get("end_time"),
            activity_data.get("score"),
            activity_data.get("status", "pending"),
            activity_data.get("difficulty"),
            activity_data.get("focus_rating"),
            activity_data.get("energy_level"),
            activity_data.get("mood"),
            json.dumps(activity_data.get("distractions", [])),
            activity_data.get("notes"),
            activity_data.get("break_duration"),
            activity_data.get("pages_count"),
            activity_data.get("test_count"),
            activity_data.get("correct_count"),
            activity_data.get("part_order", 0)
        ),
        fetch=True
    )
    return result[0] if result else None

def update_activity_status(activity_id: int, status: str, score: float = None, 
                           actual_duration: int = None) -> None:
    query = """
    UPDATE activity_log 
    SET status = %s, score = COALESCE(%s, score),
        actual_duration = COALESCE(%s, actual_duration),
        updated_at = CURRENT_TIMESTAMP
    WHERE id = %s
    """
    execute_query(query, (status, score, actual_duration, activity_id))

def update_activity_part_order(activity_id: int, new_order: int) -> None:
    execute_query(
        "UPDATE activity_log SET part_order = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (new_order, activity_id)
    )

def delete_activity(activity_id: int) -> None:
    execute_query("DELETE FROM activity_log WHERE id = %s", (activity_id,))

def save_plan(plan_data: Dict) -> Optional[int]:
    query = """
    INSERT INTO personalized_plans (
        user_id, date, daily_plan, reasoning, expected_outcome,
        applied_advice_ids, is_active
    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (user_id, date) DO UPDATE SET
        daily_plan = EXCLUDED.daily_plan,
        reasoning = EXCLUDED.reasoning,
        expected_outcome = EXCLUDED.expected_outcome,
        applied_advice_ids = EXCLUDED.applied_advice_ids,
        is_active = EXCLUDED.is_active,
        updated_at = CURRENT_TIMESTAMP
    RETURNING id
    """
    result = execute_query(
        query,
        (
            plan_data["user_id"],
            plan_data["date"],
            json.dumps(plan_data["daily_plan"]),
            json.dumps(plan_data.get("reasoning", {})),
            json.dumps(plan_data.get("expected_outcome", {})),
            json.dumps(plan_data.get("applied_advice_ids", [])),
            plan_data.get("is_active", True)
        ),
        fetch=True
    )
    return result[0] if result else None

def save_advice(advice_data: Dict) -> Optional[int]:
    query = """
    INSERT INTO advisory_rules (
        topic, label, condition, advice, priority, time, frequency,
        days, applicable_for, subjects, is_active, is_system_generated,
        created_by
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING id
    """
    result = execute_query(
        query,
        (
            advice_data["topic"],
            advice_data.get("label"),
            advice_data.get("condition"),
            advice_data["advice"],
            advice_data.get("priority", 5),
            advice_data.get("time"),
            advice_data.get("frequency"),
            json.dumps(advice_data.get("days", [])),
            json.dumps(advice_data.get("applicable_for", {})),
            json.dumps(advice_data.get("subjects", [])),
            advice_data.get("is_active", True),
            advice_data.get("is_system_generated", False),
            advice_data.get("created_by")
        ),
        fetch=True
    )
    return result[0] if result else None

def update_subject_status(user_id: int, subject: str, activity_data: Dict) -> None:
    current = execute_query(
        "SELECT * FROM subject_status WHERE user_id = %s AND subject = %s",
        (user_id, subject),
        fetch=True
    )
    
    if current:
        total_sessions = (current[4] or 0) + 1
        completed = (current[5] or 0) + (1 if activity_data.get("status") == "done" else 0)
        total_minutes = (current[6] or 0) + (activity_data.get("actual_duration") or 0)
        
        old_avg = current[7] or 0
        new_score = activity_data.get("score")
        if new_score is not None:
            avg_score = (old_avg * (total_sessions - 1) + new_score) / total_sessions
        else:
            avg_score = old_avg
        
        execute_query(
            """UPDATE subject_status 
               SET total_sessions = %s, completed_sessions = %s,
                   total_study_minutes = %s, avg_score = %s,
                   last_studied = %s, last_score = %s,
                   updated_at = CURRENT_TIMESTAMP
               WHERE user_id = %s AND subject = %s""",
            (
                total_sessions, completed, total_minutes, avg_score,
                activity_data["date"], new_score,
                user_id, subject
            )
        )
    else:
        execute_query(
            """INSERT INTO subject_status (user_id, subject, total_sessions, 
               completed_sessions, total_study_minutes, avg_score, last_studied, last_score)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                user_id, subject, 1,
                1 if activity_data.get("status") == "done" else 0,
                activity_data.get("actual_duration") or 0,
                activity_data.get("score"),
                activity_data["date"],
                activity_data.get("score")
            )
        )

def save_session_with_parts(user_id: int, parts: List[Dict], time_slots: List[str], 
                           topics: List[Dict], plan_level: int = 0) -> Optional[int]:
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        date = get_today_date()
        
        cursor.execute("""
            INSERT INTO study_sessions (user_id, date, total_parts, max_edits, time_slots, topics, archived, plan_level)
            VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)
            RETURNING session_id
        """, (
            user_id,
            date,
            len(parts),
            2,
            json.dumps(time_slots),
            json.dumps(topics),
            plan_level
        ))
        
        result = cursor.fetchone()
        if not result:
            conn.rollback()
            return None
        
        session_id = result[0]
        
        for part in parts:
            planned_start = part.get("planned_start_time")
            planned_end = part.get("planned_end_time")
            
            if not planned_start and part.get("time_slot"):
                try:
                    start_str, end_str = part["time_slot"].split("-")
                    planned_start = start_str
                    planned_end = end_str
                except:
                    pass
            
            cursor.execute("""
                INSERT INTO study_parts (
                    session_id, part_number, title, grade,
                    planned_minutes, time_slot, is_hardest, is_easiest, pages,
                    planned_start_time, planned_end_time, is_fixed_time, reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                session_id,
                part["part_number"],
                part["title"],
                part.get("grade", 3),
                part["planned_minutes"],
                part.get("time_slot", ""),
                part.get("is_hardest", False),
                part.get("is_easiest", False),
                part.get("pages", 0),
                planned_start,
                planned_end,
                part.get("is_fixed_time", False),
                part.get("reason", "")
            ))
        
        conn.commit()
        return session_id
        
    except Exception as e:
        logger.error(f"❌ خطا در save_session_with_parts: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_connection(conn)

def add_part_to_session(session_id: int, part_data: Dict) -> Optional[int]:
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT COALESCE(MAX(part_number), 0) + 1 FROM study_parts WHERE session_id = %s",
            (session_id,)
        )
        result = cursor.fetchone()
        new_part_number = result[0] if result else 1
        
        cursor.execute("""
            INSERT INTO study_parts (
                session_id, part_number, title, grade,
                planned_minutes, time_slot, pages, completed,
                planned_start_time, planned_end_time, is_fixed_time, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING part_id
        """, (
            session_id,
            new_part_number,
            part_data["title"],
            part_data.get("grade", 3),
            part_data["planned_minutes"],
            part_data.get("time_slot", ""),
            part_data.get("pages", 0),
            False,
            part_data.get("planned_start_time"),
            part_data.get("planned_end_time"),
            part_data.get("is_fixed_time", False),
            part_data.get("reason", "")
        ))
        
        result = cursor.fetchone()
        if not result:
            conn.rollback()
            return None
        
        part_id = result[0]
        
        cursor.execute("""
            UPDATE study_sessions 
            SET total_parts = total_parts + 1
            WHERE session_id = %s
        """, (session_id,))
        
        conn.commit()
        return part_id
        
    except Exception as e:
        logger.error(f"❌ خطا در add_part_to_session: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_connection(conn)

def confirm_session(session_id: int) -> None:
    execute_query(
        "UPDATE study_sessions SET confirmed = TRUE WHERE session_id = %s",
        (session_id,)
    )

def finish_session(session_id: int) -> None:
    execute_query(
        "UPDATE study_sessions SET is_finished = TRUE, archived = TRUE WHERE session_id = %s",
        (session_id,)
    )

def update_part_times_and_shift_remaining(session_id: int, completed_part_id: int, actual_end_time: datetime) -> None:
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT planned_start_time, planned_end_time, planned_minutes, is_fixed_time, part_number
            FROM study_parts
            WHERE part_id = %s
        """, (completed_part_id,))
        part_info = cursor.fetchone()
        if not part_info:
            return
        
        planned_end = part_info[1]
        planned_minutes = part_info[2]
        part_number = part_info[4]
        
        planned_end_time = datetime.combine(actual_end_time.date(), planned_end)
        if planned_end_time.tzinfo is None:
            planned_end_time = IRAN_TZ.localize(planned_end_time)
        
        delay = int((actual_end_time - planned_end_time).total_seconds() / 60)
        
        cursor.execute("""
            UPDATE study_parts
            SET actual_end_time = %s, actual_minutes = %s, completed = TRUE, delay_minutes = %s
            WHERE part_id = %s
        """, (actual_end_time, planned_minutes, delay, completed_part_id))
        
        cursor.execute("""
            SELECT part_id, planned_start_time, planned_end_time, is_fixed_time, planned_minutes, part_number
            FROM study_parts
            WHERE session_id = %s AND part_number > %s AND completed = FALSE
            ORDER BY part_number
        """, (session_id, part_number))
        
        next_parts = cursor.fetchall()
        
        if not next_parts:
            conn.commit()
            return
        
        current_time = actual_end_time
        
        for next_part in next_parts:
            next_part_id = next_part[0]
            next_duration = next_part[4]
            
            new_start = current_time
            new_end = current_time + timedelta(minutes=next_duration)
            
            if new_end.hour >= 23 and new_end.minute > 30:
                tomorrow = (datetime.now(IRAN_TZ) + timedelta(days=1)).date()
                
                cursor.execute("""
                    SELECT session_id FROM study_sessions
                    WHERE user_id = (SELECT user_id FROM study_sessions WHERE session_id = %s)
                    AND date = %s AND archived = FALSE
                """, (session_id, tomorrow.strftime("%Y-%m-%d")))
                tomorrow_session = cursor.fetchone()
                
                if tomorrow_session:
                    tomorrow_session_id = tomorrow_session[0]
                else:
                    cursor.execute("""
                        INSERT INTO study_sessions (user_id, date, total_parts, max_edits, time_slots, topics)
                        SELECT user_id, %s, 0, 2, '[]', '[]'
                        FROM study_sessions
                        WHERE session_id = %s
                        RETURNING session_id
                    """, (tomorrow.strftime("%Y-%m-%d"), session_id))
                    tomorrow_session_id = cursor.fetchone()[0]
                
                cursor.execute("""
                    INSERT INTO study_parts (
                        session_id, part_number, title, grade, planned_minutes,
                        time_slot, pages, planned_start_time, planned_end_time,
                        is_fixed_time, completed, delay_minutes, reason
                    ) VALUES (
                        %s,
                        (SELECT COALESCE(MAX(part_number), 0) + 1 FROM study_parts WHERE session_id = %s),
                        (SELECT title FROM study_parts WHERE part_id = %s),
                        (SELECT grade FROM study_parts WHERE part_id = %s),
                        %s,
                        %s,
                        (SELECT pages FROM study_parts WHERE part_id = %s),
                        %s, %s,
                        %s, FALSE, %s,
                        (SELECT reason FROM study_parts WHERE part_id = %s)
                    )
                """, (
                    tomorrow_session_id,
                    tomorrow_session_id,
                    next_part_id,
                    next_part_id,
                    next_duration,
                    f"{new_start.strftime('%H:%M')}-{new_end.strftime('%H:%M')}",
                    next_part_id,
                    new_start.strftime("%H:%M"),
                    new_end.strftime("%H:%M"),
                    next_part[3],
                    delay,
                    next_part_id
                ))
                
                cursor.execute("DELETE FROM study_parts WHERE part_id = %s", (next_part_id,))
                continue
            
            cursor.execute("""
                UPDATE study_parts
                SET planned_start_time = %s, 
                    planned_end_time = %s,
                    time_slot = %s, 
                    delay_minutes = delay_minutes + %s
                WHERE part_id = %s
            """, (
                new_start.strftime("%H:%M"),
                new_end.strftime("%H:%M"),
                f"{new_start.strftime('%H:%M')}-{new_end.strftime('%H:%M')}",
                delay,
                next_part_id
            ))
            
            current_time = new_end
        
        conn.commit()
        
    except Exception as e:
        logger.error(f"❌ خطا در update_part_times_and_shift_remaining: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_connection(conn)

# ==================== سیستم سطوح تولید برنامه ====================

def calculate_plan_level(user_id: int) -> int:
    user_data = get_user_data(str(user_id))
    if not user_data:
        return 0
    
    created_at = user_data.get("created_at")
    if not created_at:
        return 0
    
    if hasattr(created_at, 'days'):
        days_active = (get_iran_now() - created_at).days
    else:
        try:
            if isinstance(created_at, datetime):
                days_active = (get_iran_now() - created_at).days
            else:
                days_active = 0
        except:
            days_active = 0
    
    sessions = get_last_n_days_data(user_id, 30)
    study_days = len(sessions)
    
    if days_active >= 14 and study_days >= 10:
        return 3
    elif days_active >= 7 and study_days >= 5:
        return 2
    elif days_active >= 1 and study_days >= 1:
        return 1
    else:
        return 0

def get_plan_level_name(level: int) -> str:
    return PLAN_LEVELS.get(level, PLAN_LEVELS[0])["name"]

def get_plan_level_emoji(level: int) -> str:
    return PLAN_LEVELS.get(level, PLAN_LEVELS[0])["emoji"]

# ==================== پرامپت‌های AI بر اساس سطح ====================

def generate_plan_prompt_level_0(user_data: Dict, user_id: int) -> str:
    weak = ", ".join(user_data.get("weak_subjects", [])) or "ندارد"
    strong = ", ".join(user_data.get("strong_subjects", [])) or "ندارد"
    
    return f"""شما یک دستیار برنامه‌ریزی مطالعه هستید.

=== اطلاعات کاربر ===
هدف: {user_data.get('goal', 'نامشخص')}
پایه: {user_data.get('grade', 'نامشخص')}
رشته: {user_data.get('field', 'نامشخص')}
تاریخ آزمون: {user_data.get('exam_date', 'نامشخص')}

=== نقطه شروع ===
کاربر تازه وارد ربات شده است.

=== درس‌های ضعیف ===
{weak}

=== درس‌های قوی ===
{strong}

=== زمان موجود ===
{user_data.get('study_hours_per_week', 10)} ساعت در هفته
بهترین زمان: {user_data.get('peak_time', 'نامشخص')}

=== وظیفه ===
یک برنامه مطالعه اولیه برای امروز طراحی کن.

قوانین:
1. درس‌های ضعیف اولویت دارند
2. هر جلسه {user_data.get('focus_duration', 45)} دقیقه با {user_data.get('break_duration', 10)} دقیقه استراحت
3. حداکثر ۳ جلسه در روز

خروجی JSON:
{{
  "subjects": [
    {{"subject": "نام درس", "topic": "مبحث", "duration": 45, "priority": "high"}}
  ],
  "breaks": [{{"duration": 10}}],
  "total_hours": 2.5,
  "recommendations": ["توصیه کلی"]
}}"""

def generate_plan_prompt_level_1(user_data: Dict, user_id: int, 
                                 subject_status: List[Dict], 
                                 yesterday_activities: List[Dict]) -> str:
    status_text = "\n".join([
        f"- {s['subject']}: میانگین {s.get('avg_score', 0):.1f}% | {s.get('completed_sessions', 0)} جلسه"
        for s in subject_status[:5]
    ]) if subject_status else "داده‌ای موجود نیست"
    
    yesterday_text = "\n".join([
        f"- {a['subject']}: {a.get('actual_duration', a.get('planned_duration', 0))} دقیقه | {'✅' if a.get('status') == 'done' else '⬜'}"
        for a in yesterday_activities[:5]
    ]) if yesterday_activities else "فعالیتی ثبت نشده"
    
    total_time = sum(a.get('actual_duration', a.get('planned_duration', 0)) for a in yesterday_activities)
    done = len([a for a in yesterday_activities if a.get('status') == 'done'])
    total = len(yesterday_activities)
    scores = [a.get('score') for a in yesterday_activities if a.get('score') is not None]
    avg_score = sum(scores) / len(scores) if scores else 0
    
    advice = get_active_advice(user_id)
    advice_text = "\n".join([f"- {a['advice']}" for a in advice[:3]]) if advice else "توصیه‌ای موجود نیست"
    
    return f"""شما یک دستیار برنامه‌ریزی مطالعه هستید.

=== اطلاعات کاربر ===
نام: {user_data.get('full_name', 'کاربر')}
هدف: {user_data.get('goal', 'نامشخص')}
پایه: {user_data.get('grade', 'نامشخص')}

=== وضعیت دروس (امروز) ===
{status_text}

=== فعالیت‌های دیروز ===
{yesterday_text}

=== عملکرد دیروز ===
- کل زمان: {format_time_hours_minutes(total_time)}
- تکمیل‌شده: {done}/{total}
- میانگین نمره: {avg_score:.1f}%

=== توصیه‌های ادمین ===
{advice_text}

=== وظیفه ===
برنامه مطالعه امروز را بر اساس عملکرد دیروز طراحی کن.

قوانین:
1. درس‌های ضعیف را صبح بگذار
2. درس‌های قوی را عصر بگذار
3. زمان هر جلسه بر اساس {user_data.get('focus_duration', 45)} دقیقه تنظیم شود
4. بین هر جلسه {user_data.get('break_duration', 10)} دقیقه استراحت

خروجی JSON:
{{
  "subjects": [
    {{
      "subject": "نام درس",
      "topic": "مبحث خاص",
      "duration": 45,
      "priority": "high",
      "reason": "دلیل انتخاب"
    }}
  ],
  "breaks": [{{"duration": 10, "type": "استراحت"}}],
  "total_hours": 3,
  "recommendations": ["توصیه امروز"]
}}"""

def generate_plan_prompt_level_2(user_data: Dict, user_id: int, 
                                 insights: Dict, advice: List[Dict]) -> str:
    advice_text = "\n".join([f"- {a['advice']}" for a in advice[:3]]) if advice else "توصیه‌ای موجود نیست"
    
    sessions = get_last_n_days_data(user_id, 7)
    daily_data = "\n".join([
        f"روز {i+1}: {s['date']} - {s['completed_parts']}/{s['total_parts']} پارت"
        for i, s in enumerate(sessions)
    ]) if sessions else "داده‌ای موجود نیست"
    
    return f"""شما یک تحلیلگر و برنامه‌ریز هوشمند مطالعه هستید.

=== اطلاعات کاربر ===
نام: {user_data.get('full_name', 'کاربر')}
هدف: {user_data.get('goal', 'نامشخص')}
پایه: {user_data.get('grade', 'نامشخص')}
رشته: {user_data.get('field', 'نامشخص')}

=== داده‌های ۷ روز اخیر ===
{daily_data}

=== تحلیل الگوها ===
🔍 الگوهای شناسایی‌شده:
- بهترین زمان: {insights.get('best_time', 'نامشخص')}
- ضعیف‌ترین درس: {insights.get('weakest_subject', 'نامشخص')}
- قوی‌ترین درس: {insights.get('strongest_subject', 'نامشخص')}
- میانگین روزانه: {insights.get('avg_daily_hours', 0):.1f} ساعت
- نرخ تکمیل: {insights.get('completion_rate', 0):.1f}%

=== توصیه‌های ادمین ===
{advice_text}

=== وظیفه ===
برنامه شخصی‌سازی‌شده برای امروز طراحی کن.

قوانین شخصی‌سازی:
1. درس ضعیف ({insights.get('weakest_subject', 'نامشخص')}) را در بهترین زمان ({insights.get('best_time', 'نامشخص')}) بگذار
2. درس قوی ({insights.get('strongest_subject', 'نامشخص')}) را در زمان کم‌انرژی بگذار
3. زمان هر جلسه بر اساس {user_data.get('focus_duration', 45)} دقیقه تنظیم شود
4. از توصیه‌های ادمین استفاده کن

خروجی JSON:
{{
  "subjects": [
    {{
      "subject": "نام درس",
      "topic": "مبحث",
      "duration": 45,
      "priority": "high",
      "time_slot": "morning/afternoon/night",
      "reason": "چرا این زمان"
    }}
  ],
  "breaks": [
    {{"duration": 10, "type": "استراحت کوتاه", "time": "بین جلسات"}},
    {{"duration": 30, "type": "ناهار", "time": "۱۳:۰۰"}}
  ],
  "total_hours": 3.5,
  "recommendations": ["توصیه شخصی‌سازی‌شده"],
  "expected_outcome": {{
    "completion_probability": 0.85,
    "expected_score": 75
  }}
}}"""

def generate_plan_prompt_level_3(user_data: Dict, user_id: int, 
                                 insights: Dict, advice: List[Dict]) -> str:
    advice_text = "\n".join([f"- {a['advice']} (اولویت {a.get('priority', 5)})" for a in advice[:5]]) if advice else "توصیه‌ای موجود نیست"
    
    sessions = get_last_n_days_data(user_id, 14)
    daily_data = "\n".join([
        f"روز {i+1}: {s['date']} - {s['completed_parts']}/{s['total_parts']} پارت"
        for i, s in enumerate(sessions)
    ]) if sessions else "داده‌ای موجود نیست"
    
    time_patterns = insights.get('time_patterns', {})
    perf_patterns = insights.get('performance_patterns', {})
    quality_patterns = insights.get('quality_patterns', {})
    
    return f"""شما یک دستیار هوشمند برنامه‌ریزی تطبیقی هستید.

=== اطلاعات کاربر ===
نام: {user_data.get('full_name', 'کاربر')}
هدف: {user_data.get('goal', 'نامشخص')}
پایه: {user_data.get('grade', 'نامشخص')}

=== داده‌های ۱۴ روز اخیر ===
{daily_data}

=== الگوهای پیشرفته ===
⏰ الگوهای زمانی:
{json.dumps(time_patterns, ensure_ascii=False) if time_patterns else 'در حال جمع‌آوری'}

📊 الگوهای عملکردی:
{json.dumps(perf_patterns, ensure_ascii=False) if perf_patterns else 'در حال جمع‌آوری'}

🎯 الگوهای کیفی:
{json.dumps(quality_patterns, ensure_ascii=False) if quality_patterns else 'در حال جمع‌آوری'}

=== توصیه‌های ادمین (اولویت‌بندی‌شده) ===
{advice_text}

=== وضعیت امروز ===
- انرژی: {user_data.get('energy_level', 'نامشخص')}
- تمرکز: {user_data.get('focus_level', 'نامشخص')}
- فعالیت‌های انجام‌شده: {len(get_today_activities(user_id))}

=== وظیفه ===
برنامه شناور امروز را با زمان‌بندی دقیق طراحی کن.

قوانین شناور:
1. زمان‌ها بر اساس الگوهای کاربر تنظیم شود
2. درس‌های سخت در زمان‌های با انرژی بالا
3. درس‌های آسان در زمان‌های با انرژی پایین
4. هر جلسه بر اساس {user_data.get('focus_duration', 45)} دقیقه تنظیم شود
5. استراحت‌ها بر اساس الگوهای کاربر تنظیم شود
6. ۱۰ دقیقه قبل از هر جلسه اعلان تنظیم شود
7. در صورت تاخیر، برنامه تطبیق داده شود

خروجی JSON:
{{
  "subjects": [
    {{
      "subject": "نام درس",
      "topic": "مبحث",
      "duration": 45,
      "priority": "high",
      "time": "08:00",
      "end_time": "08:45",
      "alert_before": 10,
      "flexible": true,
      "reason": "دلیل زمان‌بندی"
    }}
  ],
  "breaks": [
    {{"time": "08:45", "duration": 10, "type": "استراحت کوتاه"}},
    {{"time": "13:00", "duration": 30, "type": "ناهار"}}
  ],
  "total_hours": 4,
  "adaptive_rules": {{
    "if_late": "تغییر زمان به بعد",
    "if_tired": "کاهش زمان جلسه",
    "if_energy_high": "افزایش زمان جلسه"
  }},
  "recommendations": ["توصیه شناور"],
  "expected_outcome": {{
    "completion_probability": 0.9,
    "expected_score": 80,
    "burnout_risk": "{insights.get('burnout_risk', 'low')}"
  }},
  "alerts": [
    {{"time": "07:50", "message": "۱۰ دقیقه تا شروع ریاضی"}}
  ]
}}"""

# ==================== تولید برنامه با AI بر اساس سطح (Async) ====================

async def call_ai(prompt: str, max_tokens: int = 1500, temperature: float = 0.3) -> Optional[str]:
    for attempt in range(3):
        try:
            completion = await client.chat.completions.create(
                model=AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature
            )
            return completion.choices[0].message.content
        except Exception as e:
            logger.error(f"AI error (attempt {attempt+1}): {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
            else:
                return None
    return None

async def generate_plan_with_ai(user_id: int, user_data: Dict) -> Dict:
    level = user_data.get('plan_level', 0)
    
    subject_status = get_subject_status(user_id)
    yesterday_activities = get_today_activities(user_id)
    advice = get_active_advice(user_id)
    insights = get_user_insights(user_id)
    
    prompt = ""
    
    if level == 0:
        prompt = generate_plan_prompt_level_0(user_data, user_id)
    elif level == 1:
        prompt = generate_plan_prompt_level_1(user_data, user_id, subject_status, yesterday_activities)
    elif level == 2:
        prompt = generate_plan_prompt_level_2(user_data, user_id, insights or {}, advice)
    else:
        prompt = generate_plan_prompt_level_3(user_data, user_id, insights or {}, advice)
    
    response = await call_ai(prompt, max_tokens=1200, temperature=0.3)
    if not response:
        return {}
    
    try:
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return {}
    except Exception as e:
        logger.error(f"❌ خطا در پارس JSON: {e}")
        fix_prompt = f"خروجی قبلی JSON معتبر نبود. لطفاً فقط JSON خالص برگردان. خطا: {e}\nخروجی قبلی: {response[:200]}..."
        fixed_response = await call_ai(fix_prompt, max_tokens=800, temperature=0.1)
        if fixed_response:
            try:
                json_match = re.search(r'\{.*\}', fixed_response, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
            except:
                pass
        return {}

def create_plan_from_ai_response(user_id: int, user_data: Dict, ai_response: Dict) -> Optional[int]:
    subjects = ai_response.get('subjects', [])
    if not subjects:
        return None
    
    level = user_data.get('plan_level', 0)
    parts = []
    current_time = 8 * 60
    
    for idx, subj in enumerate(subjects):
        duration = subj.get('duration', user_data.get('focus_duration', 45))
        duration = max(20, min(90, duration))
        
        grade = 3
        if subj.get('priority') == 'high':
            grade = 4
        elif subj.get('priority') == 'low':
            grade = 2
        
        start_h = current_time // 60
        start_m = current_time % 60
        end_time = current_time + duration
        end_h = end_time // 60
        end_m = end_time % 60
        
        part = {
            "part_number": idx + 1,
            "title": subj.get('subject', 'مطالعه'),
            "topic": subj.get('topic', ''),
            "grade": grade,
            "planned_minutes": duration,
            "pages": 0,
            "time_slot": f"{start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}",
            "planned_start_time": f"{start_h:02d}:{start_m:02d}",
            "planned_end_time": f"{end_h:02d}:{end_m:02d}",
            "completed": False,
            "is_fixed_time": False,
            "reason": subj.get('reason', '')
        }
        parts.append(part)
        
        break_after = ai_response.get('breaks', [])
        if break_after and idx < len(subjects) - 1:
            break_duration = break_after[0].get('duration', 10) if idx < len(break_after) else 10
            current_time = end_time + break_duration
        else:
            current_time = end_time + 5
    
    session_id = save_session_with_parts(user_id, parts, [], subjects, level)
    
    if session_id:
        if level == 3:
            alerts = ai_response.get('alerts', [])
            for alert in alerts:
                try:
                    alert_time_str = alert.get('time', '')
                    if alert_time_str:
                        h, m = map(int, alert_time_str.split(':'))
                        alert_dt = get_iran_now().replace(hour=h, minute=m, second=0, microsecond=0)
                        if alert_dt < get_iran_now():
                            alert_dt += timedelta(days=1)
                        
                        for part in parts:
                            if part.get('title') in alert.get('message', ''):
                                execute_query(
                                    """INSERT INTO daily_alerts (user_id, part_id, alert_time, message)
                                       VALUES (%s, %s, %s, %s)""",
                                    (user_id, part.get('part_id'), alert_dt, alert.get('message', ''))
                                )
                                break
                except Exception as e:
                    logger.error(f"خطا در ذخیره اعلان: {e}")
        
        return session_id
    
    return None

# ==================== ساخت دستی برنامه ====================

def parse_time_slots(text: str) -> List[Dict]:
    """پارس بازه‌های زمانی از ورودی کاربر"""
    time_slots = []
    lines = text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # الگوهای مختلف برای بازه زمانی
        patterns = [
            r'(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?',
            r'(\d{1,2})(?::(\d{2}))?\s*تا\s*(\d{1,2})(?::(\d{2}))?',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                start_h = int(match.group(1))
                start_m = int(match.group(2)) if match.group(2) else 0
                end_h = int(match.group(3))
                end_m = int(match.group(4)) if match.group(4) else 0
                
                start_min = start_h * 60 + start_m
                end_min = end_h * 60 + end_m
                duration = end_min - start_min
                
                if duration > 0:
                    time_slots.append({
                        "start": f"{start_h:02d}:{start_m:02d}",
                        "end": f"{end_h:02d}:{end_m:02d}",
                        "duration": duration,
                        "start_min": start_min,
                        "end_min": end_min
                    })
                break
    
    return time_slots

def parse_activities(text: str) -> List[Dict]:
    """پارس فعالیت‌ها از ورودی کاربر"""
    activities = []
    lines = text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # الگو: عنوان | مدت | اولویت
        parts = line.split('|')
        if len(parts) >= 2:
            title = parts[0].strip()
            duration = int(parts[1].strip()) if parts[1].strip().isdigit() else 45
            
            priority = "medium"
            if len(parts) >= 3:
                pri = parts[2].strip().lower()
                if pri in ["بالا", "high"]:
                    priority = "high"
                elif pri in ["پایین", "low"]:
                    priority = "low"
            
            activities.append({
                "title": title,
                "duration": duration,
                "priority": priority
            })
        else:
            # فقط عنوان
            activities.append({
                "title": line,
                "duration": 45,
                "priority": "medium"
            })
    
    return activities

def create_manual_plan(user_id: int, time_slots: List[Dict], activities: List[Dict]) -> Optional[int]:
    """ساخت برنامه دستی از بازه‌ها و فعالیت‌ها"""
    if not time_slots or not activities:
        return None
    
    # تطبیق بازه‌ها با فعالیت‌ها
    parts = []
    for i, (slot, activity) in enumerate(zip(time_slots, activities)):
        duration = min(activity.get('duration', 45), slot['duration'])
        
        grade = 3
        if activity.get('priority') == 'high':
            grade = 4
        elif activity.get('priority') == 'low':
            grade = 2
        
        part = {
            "part_number": i + 1,
            "title": activity['title'],
            "grade": grade,
            "planned_minutes": duration,
            "pages": 0,
            "time_slot": f"{slot['start']}-{slot['end']}",
            "planned_start_time": slot['start'],
            "planned_end_time": slot['end'],
            "completed": False,
            "is_fixed_time": True,
            "reason": "ساخت دستی"
        }
        parts.append(part)
    
    user_data = get_user_data(str(user_id))
    level = user_data.get('plan_level', 0) if user_data else 0
    
    return save_session_with_parts(user_id, parts, [], [], level)

def add_manual_activity(session_id: int, time_slot: Dict, activity: Dict) -> Optional[int]:
    """اضافه کردن فعالیت دستی به برنامه موجود"""
    part_data = {
        "title": activity['title'],
        "grade": 3 if activity.get('priority') != 'high' else 4,
        "planned_minutes": min(activity.get('duration', 45), time_slot['duration']),
        "time_slot": f"{time_slot['start']}-{time_slot['end']}",
        "planned_start_time": time_slot['start'],
        "planned_end_time": time_slot['end'],
        "is_fixed_time": True,
        "reason": "اضافه دستی"
    }
    return add_part_to_session(session_id, part_data)

# ==================== تایمر ====================
active_timers = {}
timer_data = {}

async def update_timer(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    chat_id = job_data.get("chat_id")
    part_id = job_data.get("part_id")
    start_time = job_data.get("start_time")
    timer_message_id = job_data.get("timer_message_id")
    total_minutes = job_data.get("total_minutes", 0)
    elapsed_offset = job_data.get("elapsed_offset", 0)
    user_id = job_data.get("user_id")
    
    elapsed = elapsed_offset + int((datetime.now(IRAN_TZ) - start_time).total_seconds())
    minutes = elapsed // 60
    seconds = elapsed % 60
    
    query = """
    SELECT title, planned_minutes, completed
    FROM study_parts
    WHERE part_id = %s
    """
    result = execute_query(query, (part_id,), fetch=True)
    
    if not result:
        context.job.schedule_removal()
        if part_id in active_timers:
            del active_timers[part_id]
        if part_id in timer_data:
            del timer_data[part_id]
        return
    
    title, planned_minutes, completed = result
    
    if completed:
        context.job.schedule_removal()
        if part_id in active_timers:
            del active_timers[part_id]
        if part_id in timer_data:
            del timer_data[part_id]
        clear_timer_state(user_id, part_id)
        return
    
    # ذخیره وضعیت تایمر در دیتابیس
    save_timer_state(user_id, part_id, elapsed, total_minutes, True)
    
    if elapsed >= total_minutes * 60:
        context.job.schedule_removal()
        if part_id in active_timers:
            del active_timers[part_id]
        if part_id in timer_data:
            del timer_data[part_id]
        clear_timer_state(user_id, part_id)
        
        try:
            await context.bot.edit_message_text(
                f"✅ **تایمر {title} به پایان رسید!**\n\n"
                f"⏱ زمان: {total_minutes} دقیقه\n"
                f"🎯 هدف کامل شد!",
                chat_id=chat_id,
                message_id=timer_message_id,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"خطا در ارسال پیام پایان تایمر: {e}")
        return
    
    progress = min(100, int((elapsed / (total_minutes * 60)) * 100))
    
    bar_length = 20
    filled = int(bar_length * progress / 100)
    bar = "█" * filled + "░" * (bar_length - filled)
    
    remaining_seconds = (total_minutes * 60) - elapsed
    remaining_minutes = remaining_seconds // 60
    remaining_secs = remaining_seconds % 60
    
    message_text = f"⏱ **تایمر: {title}**\n\n"
    message_text += f"⏳ زمان سپری شده: {minutes:02d}:{seconds:02d}\n"
    message_text += f"⏳ زمان باقی‌مانده: {remaining_minutes:02d}:{remaining_secs:02d}\n"
    message_text += f"📊 پیشرفت: {progress}%\n"
    message_text += f"`{bar}`\n"
    message_text += f"🎯 هدف: {total_minutes} دقیقه"
    
    if remaining_minutes <= 2:
        message_text += f"\n\n⚠️ **{remaining_minutes} دقیقه تا پایان!**"
    
    try:
        if timer_message_id:
            await context.bot.edit_message_text(
                message_text,
                chat_id=chat_id,
                message_id=timer_message_id,
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"خطا در آپدیت تایمر: {e}")

# ==================== هندلرهای اصلی ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    telegram_id = str(user.id)
    
    user_data = get_user_data(telegram_id)
    
    if user_data and user_data.get("is_onboarded"):
        level = user_data.get('plan_level', 0)
        level_name = get_plan_level_name(level)
        level_emoji = get_plan_level_emoji(level)
        
        if not get_user_quota(user_data["id"]):
            init_user_quota(user_data["id"])
        
        await update.message.reply_text(
            f"🎯 سلام {user.full_name}! به کمپ خوش آمدید.\n\n"
            f"📚 امروز {get_today_shamsi()} - ساعت {get_iran_time_str()}\n"
            f"📊 سطح برنامه: {level_emoji} {level_name}\n"
            f"💬 پیام‌های باقی‌مانده AI: {get_remaining_messages(user_data['id'])}\n\n"
            "برای شروع، دکمه‌های منو رو بزن.",
            reply_markup=get_main_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return
    
    context.user_data["onboarding_step"] = 0
    context.user_data["onboarding_data"] = {
        "telegram_id": telegram_id,
        "username": user.username,
        "full_name": user.full_name
    }
    
    await update.message.reply_text(
        "👋 سلام! به ربات هوشمند مطالعه خوش اومدی!\n\n"
        "📋 لطفاً به سوالات زیر جواب بده:\n\n"
        "❓ هدف اصلی‌ات از مطالعه چیه؟\n"
        "[کنکور] [معدل] [تقویت پایه] [✏️ سایر]",
        reply_markup=ReplyKeyboardMarkup(
            [["کنکور"], ["معدل"], ["تقویت پایه"], ["✏️ سایر"]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )

async def onboarding_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    step = context.user_data.get("onboarding_step", 0)
    data = context.user_data.get("onboarding_data", {})
    
    if step == 0:
        if text == "✏️ سایر":
            await update.message.reply_text("✏️ لطفاً هدف خودت رو بنویس:")
            context.user_data["awaiting_custom"] = "goal"
            return
        data["goal"] = text
        context.user_data["onboarding_step"] = 1
        await update.message.reply_text(
            "❓ پایه تحصیلی‌ات چیه؟\n"
            "[دهم] [یازدهم] [دوازدهم] [دانشجو] [✏️ سایر]",
            reply_markup=ReplyKeyboardMarkup(
                [["دهم"], ["یازدهم"], ["دوازدهم"], ["دانشجو"], ["✏️ سایر"]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
    
    elif step == 1:
        if text == "✏️ سایر":
            await update.message.reply_text("✏️ لطفاً پایه خودت رو بنویس:")
            context.user_data["awaiting_custom"] = "grade"
            return
        data["grade"] = text
        context.user_data["onboarding_step"] = 2
        await update.message.reply_text(
            "❓ رشته‌ات چیه؟\n"
            "[ریاضی] [تجربی] [انسانی] [سایر] [✏️ سایر]",
            reply_markup=ReplyKeyboardMarkup(
                [["ریاضی"], ["تجربی"], ["انسانی"], ["سایر"], ["✏️ سایر"]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
    
    elif step == 2:
        if text == "✏️ سایر":
            await update.message.reply_text("✏️ لطفاً رشته خودت رو بنویس:")
            context.user_data["awaiting_custom"] = "field"
            return
        data["field"] = text
        context.user_data["onboarding_step"] = 3
        await update.message.reply_text(
            "❓ تاریخ کنکور یا آزمون مهم رو بگو (مثلاً 1404/04/15):\n"
            "(اگر ندارید، 'ندارم' رو بزنید)",
            reply_markup=ReplyKeyboardMarkup(
                [["ندارم"]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
    
    elif step == 3:
        if text != "ندارم":
            try:
                parts = text.split("/")
                if len(parts) == 3:
                    year, month, day = map(int, parts)
                    jdate = jdatetime.date(year, month, day)
                    data["exam_date"] = jdate.togregorian().strftime("%Y-%m-%d")
            except:
                data["exam_date"] = None
        else:
            data["exam_date"] = None
        
        context.user_data["onboarding_step"] = 4
        await update.message.reply_text(
            "❓ چند درصد از کل مطالب رو خوندی؟\n"
            "[کمتر از ۲۰%] [۲۰-۴۰%] [۴۰-۶۰%] [۶۰-۸۰%] [بیشتر از ۸۰%]",
            reply_markup=ReplyKeyboardMarkup(
                [["کمتر از ۲۰%"], ["۲۰-۴۰%"], ["۴۰-۶۰%"], ["۶۰-۸۰%"], ["بیشتر از ۸۰%"]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
    
    elif step == 4:
        data["progress_estimate"] = text
        context.user_data["onboarding_step"] = 5
        await update.message.reply_text(
            "❓ بهترین زمان مطالعه‌ت کیه؟\n"
            "[صبح] [عصر] [شب]",
            reply_markup=ReplyKeyboardMarkup(
                [["صبح"], ["عصر"], ["شب"]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
    
    elif step == 5:
        data["peak_time"] = text
        context.user_data["onboarding_step"] = 6
        await update.message.reply_text(
            "❓ درس‌هایی که ضعیفی رو بگو (مثلاً: ریاضی، فیزیک):",
            reply_markup=ReplyKeyboardMarkup(
                [["رد کردن"]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
    
    elif step == 6:
        if text != "رد کردن":
            data["weak_subjects"] = [s.strip() for s in text.split("،") if s.strip()]
        else:
            data["weak_subjects"] = []
        
        context.user_data["onboarding_step"] = 7
        await update.message.reply_text(
            "❓ چقدر می‌تونی تمرکز کنی؟\n"
            "[۲۰ دقیقه] [۳۰ دقیقه] [۴۵ دقیقه] [۶۰ دقیقه] [۹۰ دقیقه]",
            reply_markup=ReplyKeyboardMarkup(
                [["۲۰ دقیقه"], ["۳۰ دقیقه"], ["۴۵ دقیقه"], ["۶۰ دقیقه"], ["۹۰ دقیقه"]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
    
    elif step == 7:
        try:
            focus = int(text.replace("دقیقه", "").strip())
            data["focus_duration"] = focus
        except:
            data["focus_duration"] = 45
        
        data["is_onboarded"] = True
        data["plan_level"] = 0
        
        user_id = save_user(data)
        
        if user_id:
            init_user_quota(user_id)
            
            await update.message.reply_text(
                "✅ **ثبت‌نام شما با موفقیت انجام شد!**\n\n"
                f"📚 هدف: {data.get('goal')}\n"
                f"🎓 پایه: {data.get('grade')}\n"
                f"🧪 رشته: {data.get('field')}\n"
                f"🌱 سطح برنامه: اولیه\n"
                f"💬 ۱۰ پیام رایگان AI برای آزمایش\n\n"
                "🧠 در حال ساخت برنامه اولیه...",
                reply_markup=get_main_keyboard(),
                parse_mode=ParseMode.HTML
            )
            
            await generate_initial_plan(update, context, user_id, data)
        else:
            await update.message.reply_text(
                "❌ خطا در ثبت اطلاعات. لطفاً دوباره /start رو بزن.",
                reply_markup=get_main_keyboard()
            )

async def generate_initial_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                user_id: int, user_data: Dict) -> None:
    level = calculate_plan_level(user_id)
    user_data['plan_level'] = level
    update_user_plan_level(user_id, level)
    
    wait_msg = await update.message.reply_text("🧠 در حال ساخت برنامه شخصی‌سازی‌شده...")
    
    ai_response = await generate_plan_with_ai(user_id, user_data)
    
    await wait_msg.delete()
    
    if ai_response and ai_response.get('subjects'):
        session_id = create_plan_from_ai_response(user_id, user_data, ai_response)
        
        if session_id:
            plan = get_plan_by_date(user_id, get_today_date())
            if plan:
                context.user_data["current_plan"] = plan
                await show_parts_initial(update, context, plan["parts"])
                return
    
    await update.message.reply_text(
        "📝 برنامه‌ای برای امروز وجود ندارد.\n"
        "می‌تونی با دکمه <b>➕ اضافه کردن</b> فعالیت ثبت کنی.",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.HTML
    )

# ==================== نمایش پارت‌ها ====================

async def show_parts_initial(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[Dict]) -> None:
    if not parts:
        await update.message.reply_text("❌ هیچ پارتی وجود ندارد.", reply_markup=get_main_keyboard())
        return
    
    user_id = get_user_id_by_telegram(update.effective_user.id)
    user_data = get_user_data(str(update.effective_user.id)) if user_id else None
    level = user_data.get('plan_level', 0) if user_data else 0
    level_name = get_plan_level_name(level)
    level_emoji = get_plan_level_emoji(level)
    
    text = f"📋 **برنامه پیشنهادی** {level_emoji} سطح {level_name}\n\n"
    text += f"📊 تعداد پارت‌ها: {len(parts)}\n"
    text += f"⏱ زمان کل: {format_time_hours_minutes(sum(p['planned_minutes'] for p in parts))}\n\n"
    
    for part in sorted(parts, key=lambda x: x.get("part_number", 0)):
        grade_emoji = GRADE_RULES.get(part.get("grade", 3), GRADE_RULES[3])["emoji"]
        planned_start = part.get("planned_start_time") or part.get("planned_start") or ""
        planned_end = part.get("planned_end_time") or part.get("planned_end") or ""
        time_info = ""
        if planned_start and planned_end:
            time_info = f" {planned_start}-{planned_end}"
        elif part.get("time_slot"):
            time_info = f" {part['time_slot']}"
        text += f"{part['part_number']}. ⬜ {grade_emoji} {part['title']} ({part['planned_minutes']}د){time_info} ↕️\n"
    
    text += "\n🔧 **مرحله اول: تنظیم ترتیب پارت‌ها**\n"
    text += "• با زدن دکمه <b>↕️</b> کنار هر پارت، آن پارت یک ردیف بالا می‌رود\n"
    text += "• بعد از رضایت، دکمه <b>تایید برنامه</b> رو بزن"
    
    if level >= 2:
        text += f"\n\n💡 **توصیه‌های سطح {level_name}:**\n"
        if level == 2:
            text += "• این برنامه بر اساس ۷ روز داده شما شخصی‌سازی شده است\n"
            text += "• درس ضعیف شما در بهترین زمان قرار داده شده است"
        elif level == 3:
            text += "• برنامه شناور با زمان‌بندی دقیق تنظیم شده است\n"
            text += "• اعلان‌ها ۱۰ دقیقه قبل از هر جلسه ارسال می‌شوند\n"
            text += "• در صورت تاخیر، برنامه به‌صورت خودکار تطبیق داده می‌شود"
    
    await update.message.reply_text(
        text,
        reply_markup=get_part_buttons_initial(parts),
        parse_mode=ParseMode.HTML
    )

async def show_parts_final(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[Dict], show_date: bool = False) -> None:
    if not parts:
        await update.message.reply_text("📭 هیچ پارتی وجود ندارد.", reply_markup=get_main_keyboard())
        return
    
    sorted_parts = sorted(parts, key=lambda x: x.get("part_number", 0))
    
    user_id = get_user_id_by_telegram(update.effective_user.id)
    user_data = get_user_data(str(update.effective_user.id)) if user_id else None
    level = user_data.get('plan_level', 0) if user_data else 0
    level_name = get_plan_level_name(level)
    level_emoji = get_plan_level_emoji(level)
    
    text = f"📋 برنامه نهایی {level_emoji} سطح {level_name}\n\n"
    
    if show_date:
        date_str = context.user_data.get("selected_date", "")
        if not date_str:
            date_str = get_today_date()
        shamsi = get_shamsi_date(date_str)
        text = f"📋 برنامه {shamsi} {level_emoji} سطح {level_name}\n\n"
    
    total_parts = len(sorted_parts)
    completed_parts = sum(1 for p in sorted_parts if p.get("completed", False))
    total_minutes = sum(p.get("planned_minutes", 0) for p in sorted_parts)
    
    text += f"📊 تعداد پارت‌ها: {total_parts}\n"
    text += f"✅ انجام شده: {completed_parts}\n"
    text += f"⬜ انجام نشده: {total_parts - completed_parts}\n"
    text += f"⏱ زمان کل: {format_time_hours_minutes(total_minutes)}\n\n"
    
    for part in sorted_parts:
        status = "✅" if part.get("completed", False) else "⬜"
        grade_emoji = GRADE_RULES.get(part.get("grade", 3), GRADE_RULES[3])["emoji"]
        planned_start = part.get("planned_start_time") or part.get("planned_start") or ""
        planned_end = part.get("planned_end_time") or part.get("planned_end") or ""
        time_info = ""
        if planned_start and planned_end:
            time_info = f" {planned_start}-{planned_end}"
        elif part.get("time_slot"):
            time_info = f" {part['time_slot']}"
        actual_info = ""
        if part.get("completed", False) and part.get("actual_minutes", 0) > 0:
            actual_info = f" (زمان واقعی: {part['actual_minutes']}د)"
        part_num = part.get("part_number", 0)
        reason = f" 📝 {part.get('reason', '')}" if part.get('reason') else ""
        if part.get("completed", False):
            text += f"{part_num}. ✅ {grade_emoji} {part['title']} ({part.get('planned_minutes', 0)}د){time_info}{actual_info}{reason}\n"
        else:
            text += f"{part_num}. ⬜ {grade_emoji} {part['title']} ({part.get('planned_minutes', 0)}د){time_info}{actual_info}{reason}\n"
    
    text += "\n⏰ مرحله دوم: اجرا و تکمیل\n"
    text += "• روی هر پارت کلیک کن تا دکمه‌های عملیاتی نمایش داده شوند\n"
    text += "• برای اضافه کردن فعالیت جدید، دکمه ➕ اضافه کردن فعالیت رو بزن\n"
    text += "• برای پایان برنامه، دکمه ✅ اتمام برنامه رو بزن"
    
    # دکمه برگشت
    last_change = get_last_change(user_id) if user_id else None
    if last_change:
        text += f"\n\n🔙 **یک تغییر قابل برگشت وجود دارد**\n"
        text += f"📝 نوع تغییر: {last_change['action_type']}"
    
    if level >= 3:
        text += "\n\n🔔 **اعلان‌های امروز:**\n"
        alerts = execute_query(
            "SELECT message, alert_time FROM daily_alerts WHERE user_id = %s AND sent = FALSE",
            (user_id,),
            fetchall=True
        )
        if alerts:
            for alert in alerts:
                text += f"• {alert[1].strftime('%H:%M')}: {alert[0]}\n"
        else:
            text += "• هیچ اعلان فعالی وجود ندارد"
    
    await update.message.reply_text(
        text,
        reply_markup=get_part_buttons_final(sorted_parts, show_date),
        parse_mode=ParseMode.HTML
    )

async def show_part_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    
    part = next((p for p in parts if p.get("part_id") == part_id), None)
    
    if not part:
        query = """
        SELECT part_id, part_number, title, grade, planned_minutes, actual_minutes,
               time_slot, completed, pages, planned_start_time, planned_end_time,
               is_fixed_time, delay_minutes, reason
        FROM study_parts
        WHERE part_id = %s
        """
        db_result = execute_query(query, (part_id,), fetch=True)
        if not db_result:
            await update.message.reply_text("❌ پارت یافت نشد.")
            return
        planned_start = db_result[9]
        planned_end = db_result[10]
        if planned_start and hasattr(planned_start, 'strftime'):
            planned_start = planned_start.strftime('%H:%M')
        if planned_end and hasattr(planned_end, 'strftime'):
            planned_end = planned_end.strftime('%H:%M')
        part = {
            "part_id": db_result[0],
            "part_number": db_result[1],
            "title": db_result[2],
            "grade": db_result[3],
            "planned_minutes": db_result[4],
            "actual_minutes": db_result[5] or 0,
            "time_slot": db_result[6] or "",
            "completed": db_result[7],
            "pages": db_result[8] or 0,
            "planned_start_time": planned_start or "",
            "planned_end_time": planned_end or "",
            "planned_start": planned_start or "",
            "planned_end": planned_end or "",
            "is_fixed_time": db_result[11] or False,
            "delay_minutes": db_result[12] or 0,
            "reason": db_result[13] or ""
        }
        if not any(p.get("part_id") == part_id for p in parts):
            parts.append(part)
            plan["parts"] = parts
            context.user_data["current_plan"] = plan
    
    if part.get("completed"):
        grade_info = GRADE_RULES.get(part.get("grade", 3), GRADE_RULES[3])
        text = f"✅ <b>{part['title']}</b> (انجام شده)\n\n"
        text += f"⭐ درجه: {grade_info['name']} {grade_info['emoji']}\n"
        text += f"⏱ زمان برنامه: {part['planned_minutes']} دقیقه\n"
        text += f"⏱ زمان واقعی: {part.get('actual_minutes', part['planned_minutes'])} دقیقه\n"
        if part.get("planned_start") and part.get("planned_end"):
            text += f"🕒 زمان برنامه: {part['planned_start']} - {part['planned_end']}\n"
        if part.get("pages", 0) > 0:
            text += f"📄 صفحات: {part['pages']}\n"
        if part.get("reason"):
            text += f"📝 دلیل: {part['reason']}\n"
        text += "\n✅ این پارت قبلاً تکمیل شده است."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return
    
    grade_info = GRADE_RULES.get(part.get("grade", 3), GRADE_RULES[3])
    text = f"📖 <b>{part['title']}</b>\n\n"
    text += f"⭐ درجه: {grade_info['name']} {grade_info['emoji']}\n"
    text += f"⏱ زمان: {part['planned_minutes']} دقیقه\n"
    if part.get("planned_start") and part.get("planned_end"):
        text += f"🕒 زمان برنامه: {part['planned_start']} - {part['planned_end']}\n"
    if part.get("time_slot"):
        text += f"🕒 ساعت برنامه: {part['time_slot']}\n"
    if part.get("pages", 0) > 0:
        text += f"📄 صفحات: {part['pages']}\n"
    if part.get("is_fixed_time"):
        text += "🔒 زمان ثابت - قابل جابه‌جایی نیست\n"
    if part.get("reason"):
        text += f"📝 دلیل: {part['reason']}\n"
    text += f"✅ وضعیت: در انتظار ⬜\n"
    
    context.user_data["active_part"] = part_id
    
    # بررسی وضعیت تایمر
    user_id = get_user_id_by_telegram(update.effective_user.id)
    timer_state = None
    if user_id:
        timer_state = get_timer_state(user_id, part_id)
    
    is_running = timer_state.get("is_running", False) if timer_state else False
    has_state = timer_state is not None and timer_state.get("elapsed_seconds", 0) > 0
    
    await update.message.reply_text(
        text,
        reply_markup=get_part_detail_buttons(part_id, is_running, has_state),
        parse_mode=ParseMode.HTML
    )

# ==================== مدیریت دکمه‌های پارت ====================

async def handle_part_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    is_confirmed = plan.get("confirmed", False)
    is_edit_mode = context.user_data.get("edit_mode", False)
    
    if "[" in text and "]" in text:
        id_match = re.search(r'\[(\d+)\]', text)
        if id_match:
            part_id = int(id_match.group(1))
            found_part = None
            for p in parts:
                if p.get("part_id") == part_id:
                    found_part = p
                    break
            if not found_part:
                await update.message.reply_text("❌ پارت یافت نشد.")
                return
            if is_edit_mode:
                await move_part_up(update, context, part_id)
                return
            if is_confirmed:
                await show_part_detail(update, context, part_id)
                return
            await move_part_up(update, context, part_id)
            return

async def move_part_up(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    
    index = next((i for i, p in enumerate(parts) if p["part_id"] == part_id), None)
    if index is None:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    
    if index > 0:
        # ذخیره تغییر برای برگشت
        user_id = get_user_id_by_telegram(update.effective_user.id)
        previous_data = {
            "part_number": parts[index]["part_number"],
            "planned_start_time": parts[index]["planned_start_time"],
            "planned_end_time": parts[index]["planned_end_time"],
            "time_slot": parts[index]["time_slot"]
        }
        save_change_history(user_id, plan.get("session_id"), part_id, "move", previous_data)
        
        parts[index], parts[index-1] = parts[index-1], parts[index]
        for i, p in enumerate(parts):
            p["part_number"] = i + 1
        
        current_time = 8 * 60
        for p in sorted(parts, key=lambda x: x.get("part_number", 0)):
            duration = p["planned_minutes"]
            start_h = current_time // 60
            start_m = current_time % 60
            end_time = current_time + duration
            end_h = end_time // 60
            end_m = end_time % 60
            p["planned_start_time"] = f"{start_h:02d}:{start_m:02d}"
            p["planned_end_time"] = f"{end_h:02d}:{end_m:02d}"
            p["time_slot"] = f"{p['planned_start_time']}-{p['planned_end_time']}"
            current_time = end_time
        
        for p in parts:
            execute_query(
                """UPDATE study_parts 
                   SET part_number = %s, planned_start_time = %s, planned_end_time = %s, time_slot = %s
                   WHERE part_id = %s""",
                (p["part_number"], p["planned_start_time"], p["planned_end_time"], p["time_slot"], p["part_id"])
            )
        
        plan["parts"] = parts
        context.user_data["current_plan"] = plan
        
        await update.message.reply_text(f"⬆️ {parts[index]['title']} یک ردیف بالا رفت!")
        
        if plan.get("confirmed", False):
            await show_parts_final(update, context, parts)
        else:
            await show_parts_initial(update, context, parts)
    else:
        await update.message.reply_text("❌ این پارت در بالاترین ردیف است.")

# ==================== مدیریت دکمه‌های برنامه ====================

async def handle_plan_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    
    if text == "🔙 بازگشت":
        context.user_data.pop("current_plan", None)
        context.user_data.pop("active_part", None)
        context.user_data.pop("edit_mode", None)
        context.user_data.pop("manual_plan_step", None)
        context.user_data.pop("manual_time_slots", None)
        context.user_data.pop("manual_activities", None)
        await update.message.reply_text("🔙 بازگشت به صفحه اصلی", reply_markup=get_main_keyboard())
        return
    
    if text == "🔙 برگشت به حالت قبل":
        await handle_undo(update, context)
        return
    
    if text == "✅ تایید برنامه":
        await confirm_plan(update, context)
        return
    
    if text == "✅ اتمام برنامه":
        await handle_finish_plan(update, context)
        return
    
    if text == "✏️ ویرایش برنامه":
        await show_edit_menu(update, context)
        return
    
    if text == "✏️ ویرایش دستی":
        context.user_data["edit_mode"] = True
        await show_parts_initial(update, context, parts)
        return
    
    if text == "💬 ویرایش با AI":
        # رفتن به حالت چت با AI برای ویرایش
        await handle_ai_chat(update, context)
        return
    
    if text == "➕ اضافه کردن فعالیت":
        await start_add_activity(update, context)
        return
    
    if text == "🔄 بازنشانی":
        today = get_today_date()
        if plan.get("session_id"):
            execute_query("UPDATE study_sessions SET archived = TRUE WHERE session_id = %s", (plan["session_id"],))
        context.user_data.pop("current_plan", None)
        await update.message.reply_text("🔄 برنامه بازنشانی شد!", reply_markup=get_main_keyboard())
        return
    
    if text in ["⏱ تایمر", "⏹ توقف", "⏱ ادامه", "✅ تکمیل", "🗑 حذف پارت"]:
        active_part = context.user_data.get("active_part")
        if not active_part:
            await update.message.reply_text("❌ هیچ پارت فعالی وجود ندارد.\nابتدا روی یک پارت کلیک کن.")
            return
        if text == "⏱ تایمر":
            await start_timer_command(update, context, active_part)
        elif text == "⏹ توقف":
            await stop_timer_command(update, context, active_part)
        elif text == "⏱ ادامه":
            await resume_timer_command(update, context, active_part)
        elif text == "✅ تکمیل":
            await handle_done_part(update, context, active_part)
        elif text == "🗑 حذف پارت":
            await handle_delete_part(update, context, active_part)
        return
    
    if text == "✅ تایید تغییرات":
        await confirm_edit(update, context)
        return
    
    if text == "❌ لغو تغییرات":
        await cancel_edit(update, context)
        return
    
    if text == "🧠 ساخت با AI":
        await handle_today_plan_ai(update, context)
        return
    
    if text == "✏️ ساخت دستی":
        await start_manual_plan(update, context)
        return

async def handle_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """برگشت به حالت قبل"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    last_change = get_last_change(user_id)
    if not last_change:
        await update.message.reply_text("❌ هیچ تغییری برای برگشت وجود ندارد.")
        return
    
    success = revert_change(last_change["id"])
    if success:
        await update.message.reply_text("✅ برگشت انجام شد. برنامه به حالت قبل بازگشت.")
        # نمایش مجدد برنامه
        plan = context.user_data.get("current_plan", {})
        if plan.get("parts"):
            if plan.get("confirmed", False):
                await show_parts_final(update, context, plan["parts"])
            else:
                await show_parts_initial(update, context, plan["parts"])
        else:
            await update.message.reply_text("🔙 برگشتی به منو", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("❌ خطا در برگشت تغییر.")

async def confirm_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    session_id = plan.get("session_id")
    
    if not parts:
        await update.message.reply_text("❌ برنامه‌ای وجود ندارد.")
        return
    
    if session_id:
        confirm_session(session_id)
    
    plan["confirmed"] = True
    context.user_data["current_plan"] = plan
    context.user_data["edit_mode"] = False
    
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if user_id:
        level = calculate_plan_level(user_id)
        update_user_plan_level(user_id, level)
    
    level = context.user_data.get("current_plan", {}).get("plan_level", 0)
    level_name = get_plan_level_name(level)
    level_emoji = get_plan_level_emoji(level)
    
    await update.message.reply_text(
        f"✅ <b>برنامه تایید شد!</b> {level_emoji} سطح {level_name}",
        parse_mode=ParseMode.HTML
    )
    await show_parts_final(update, context, parts)

async def handle_finish_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    session_id = plan.get("session_id")
    
    if not parts:
        await update.message.reply_text("❌ برنامه‌ای وجود ندارد.")
        return
    
    completed_parts = [p for p in parts if p.get("completed", False)]
    incomplete_parts = [p for p in parts if not p.get("completed", False)]
    total_parts = len(parts)
    done_count = len(completed_parts)
    
    if session_id:
        finish_session(session_id)
    
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if user_id:
        level = calculate_plan_level(user_id)
        update_user_plan_level(user_id, level)
    
    text = f"📅 برنامه امروز به پایان رسید!\n\n"
    text += f"📊 پیشرفت: {done_count}/{total_parts}\n"
    text += f"✅ پارت‌های انجام شده: {done_count} مورد\n"
    text += f"⬜ پارت‌های انجام نشده: {len(incomplete_parts)} مورد\n\n"
    
    if incomplete_parts:
        text += "📋 پارت‌های انجام نشده (در تقویم باقی ماندند):\n"
        for part in incomplete_parts[:5]:
            text += f"⬜ {part['title']} ({part.get('planned_minutes', 0)}د)\n"
    
    context.user_data.pop("current_plan", None)
    context.user_data.pop("active_part", None)
    
    await update.message.reply_text(text, reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML)

async def show_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✏️ <b>ویرایش برنامه</b>\n\n"
        "نوع ویرایش رو انتخاب کن:\n\n"
        "📌 <b>ویرایش دستی</b> - جابه‌جایی و حذف پارت‌ها\n"
        "📌 <b>ویرایش با AI</b> - تغییرات با دستور متنی (رفتن به چت)\n\n"
        "🔙 برای برگشت به برنامه از دکمه بازگشت استفاده کن.",
        reply_markup=get_edit_menu_keyboard(),
        parse_mode=ParseMode.HTML
    )

async def confirm_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تایید تغییرات در ویرایش دستی"""
    # تغییرات قبلاً اعمال شده، فقط نمایش برنامه
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    
    await update.message.reply_text("✅ تغییرات اعمال شد!")
    await show_parts_final(update, context, parts)

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """لغو تغییرات در ویرایش دستی"""
    # برگشت آخرین تغییر
    await handle_undo(update, context)

async def handle_delete_part(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int = None) -> None:
    if part_id is None:
        part_id = context.user_data.get("active_part")
    if not part_id:
        await update.message.reply_text("❌ هیچ پارت فعالی وجود ندارد.")
        return
    
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    part = next((p for p in parts if p["part_id"] == part_id), None)
    if not part:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    if part.get("completed"):
        await update.message.reply_text("❌ پارت انجام شده را نمی‌توان حذف کرد.")
        return
    
    # ذخیره برای برگشت
    user_id = get_user_id_by_telegram(update.effective_user.id)
    previous_data = {
        "title": part["title"],
        "planned_minutes": part["planned_minutes"],
        "planned_start_time": part.get("planned_start_time"),
        "planned_end_time": part.get("planned_end_time"),
        "time_slot": part.get("time_slot"),
        "grade": part.get("grade"),
        "reason": part.get("reason")
    }
    save_change_history(user_id, plan.get("session_id"), part_id, "delete", previous_data)
    
    execute_query("DELETE FROM study_parts WHERE part_id = %s", (part_id,))
    parts = [p for p in parts if p["part_id"] != part_id]
    for i, p in enumerate(parts):
        p["part_number"] = i + 1
    plan["parts"] = parts
    context.user_data["current_plan"] = plan
    context.user_data.pop("active_part", None)
    
    await update.message.reply_text(f"🗑 <b>{part['title']}</b> حذف شد!", parse_mode=ParseMode.HTML)
    
    # نمایش برنامه با دکمه برگشت
    keyboard = get_part_buttons_final(parts) if plan.get("confirmed", False) else get_part_buttons_initial(parts)
    # اضافه کردن دکمه برگشت
    if plan.get("confirmed", False):
        await show_parts_final(update, context, parts)
    else:
        await show_parts_initial(update, context, parts)

async def handle_done_part(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    
    if part_id in active_timers:
        active_timers[part_id].schedule_removal()
        del active_timers[part_id]
    if part_id in timer_data:
        del timer_data[part_id]
    clear_timer_state(user_id, part_id)
    
    check_query = """
    SELECT completed, title, planned_minutes, actual_minutes, session_id,
           planned_start_time, planned_end_time, is_fixed_time, part_number
    FROM study_parts
    WHERE part_id = %s
    """
    check_result = execute_query(check_query, (part_id,), fetch=True)
    if not check_result:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    
    is_completed, title, planned_minutes, actual_minutes, session_id, planned_start, planned_end, is_fixed, part_number = check_result
    
    if is_completed:
        await update.message.reply_text(f"⚠️ <b>{title}</b> قبلاً انجام شده است.", parse_mode=ParseMode.HTML)
        return
    
    now = datetime.now(IRAN_TZ)
    actual_minutes_calc = planned_minutes
    
    # ذخیره برای برگشت
    previous_data = {
        "completed": False,
        "actual_minutes": 0,
        "actual_end_time": None
    }
    save_change_history(user_id, session_id, part_id, "complete", previous_data)
    
    execute_query(
        """UPDATE study_parts 
           SET completed = TRUE, 
               completed_at = %s, 
               actual_minutes = %s,
               actual_end_time = %s
           WHERE part_id = %s""",
        (now, actual_minutes_calc, now, part_id)
    )
    
    update_part_times_and_shift_remaining(session_id, part_id, now)
    
    execute_query(
        """UPDATE study_sessions 
           SET completed_parts = (
               SELECT COUNT(*) FROM study_parts 
               WHERE session_id = %s AND completed = TRUE
           )
           WHERE session_id = %s""",
        (session_id, session_id)
    )
    
    # ثبت در activity_log با تاریخ امروز
    try:
        activity_data = {
            "user_id": user_id,
            "date": get_today_date(),
            "subject": title,
            "topic": part.get("topic", ""),
            "activity_type": "مطالعه",
            "planned_duration": planned_minutes,
            "actual_duration": actual_minutes_calc,
            "status": "done",
            "score": None,
            "part_order": part_number
        }
        save_activity(activity_data)
        update_subject_status(user_id, title, activity_data)
    except Exception as e:
        logger.error(f"خطا در ثبت فعالیت: {e}")
    
    context.user_data.pop("active_part", None)
    
    await update.message.reply_text(
        f"✅ <b>{title} تکمیل شد!</b>\n\n"
        f"⏱ زمان واقعی: {actual_minutes_calc} دقیقه\n"
        f"🎯 موفقیت آمیز بود!",
        parse_mode=ParseMode.HTML
    )
    
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    for p in parts:
        if p["part_id"] == part_id:
            p["completed"] = True
            p["actual_minutes"] = actual_minutes_calc
            break
    
    await show_parts_final(update, context, parts, True)

# ==================== توابع تایمر ====================

async def start_timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    chat_id = update.effective_chat.id
    user_id = get_user_id_by_telegram(update.effective_user.id)
    
    if part_id in active_timers:
        await update.message.reply_text("⏱ تایمر در حال اجراست!")
        return
    
    query = "SELECT title, planned_minutes, completed FROM study_parts WHERE part_id = %s"
    result = execute_query(query, (part_id,), fetch=True)
    if not result:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    
    title, total_minutes, completed = result
    if completed:
        await update.message.reply_text("❌ این پارت قبلاً انجام شده.")
        return
    
    # بررسی وضعیت ذخیره‌شده تایمر
    timer_state = get_timer_state(user_id, part_id)
    elapsed_offset = timer_state.get("elapsed_seconds", 0) if timer_state else 0
    
    if elapsed_offset > 0:
        # تایمر قبلاً شروع شده بود، ادامه از همان نقطه
        await resume_timer_command(update, context, part_id)
        return
    
    start_time = datetime.now(IRAN_TZ)
    msg = await update.message.reply_text(
        f"⏱ **شروع تایمر: {title}**\n\n"
        f"🎯 هدف: {total_minutes} دقیقه\n"
        f"⏳ در حال اجرا...",
        parse_mode=ParseMode.HTML
    )
    
    job_data = {
        "chat_id": chat_id,
        "part_id": part_id,
        "start_time": start_time,
        "timer_message_id": msg.message_id,
        "total_minutes": total_minutes,
        "elapsed_offset": 0,
        "user_id": user_id
    }
    
    if context.job_queue:
        job = context.job_queue.run_repeating(update_timer, interval=10, first=10, data=job_data)
        active_timers[part_id] = job
        save_timer_state(user_id, part_id, 0, total_minutes, True)

async def stop_timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    
    if part_id in active_timers:
        job = active_timers[part_id]
        job_data = job.data
        start_time = job_data.get("start_time")
        elapsed_offset = job_data.get("elapsed_offset", 0)
        total_minutes = job_data.get("total_minutes", 0)
        elapsed = elapsed_offset + int((datetime.now(IRAN_TZ) - start_time).total_seconds())
        
        # ذخیره وضعیت
        save_timer_state(user_id, part_id, elapsed, total_minutes, False)
        
        active_timers[part_id].schedule_removal()
        del active_timers[part_id]
        
        remaining = max(0, total_minutes * 60 - elapsed)
        await update.message.reply_text(
            f"⏹ **تایمر متوقف شد.**\n\n"
            f"⏱ زمان سپری شده: {elapsed // 60:02d}:{elapsed % 60:02d}\n"
            f"⏳ زمان باقی‌مانده: {remaining // 60:02d}:{remaining % 60:02d}\n\n"
            f"برای ادامه، دکمه <b>⏱ ادامه</b> رو بزن.",
            parse_mode=ParseMode.HTML
        )
        
        # آپدیت دکمه‌ها
        await show_part_detail(update, context, part_id)
    else:
        await update.message.reply_text("❌ تایمر فعالی وجود ندارد.")

async def resume_timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    chat_id = update.effective_chat.id
    user_id = get_user_id_by_telegram(update.effective_user.id)
    
    if part_id in active_timers:
        await update.message.reply_text("⏱ تایمر در حال اجراست!")
        return
    
    timer_state = get_timer_state(user_id, part_id)
    if not timer_state:
        await update.message.reply_text("❌ هیچ تایمر ذخیره‌شده‌ای وجود ندارد.")
        return
    
    elapsed = timer_state.get("elapsed_seconds", 0)
    total_minutes = timer_state.get("total_minutes", 0)
    
    if elapsed >= total_minutes * 60:
        await update.message.reply_text("✅ تایمر قبلاً به پایان رسیده است.")
        clear_timer_state(user_id, part_id)
        return
    
    query = "SELECT title, planned_minutes, completed FROM study_parts WHERE part_id = %s"
    result = execute_query(query, (part_id,), fetch=True)
    if not result:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    
    title, total_minutes_db, completed = result
    if completed:
        await update.message.reply_text("❌ این پارت قبلاً انجام شده.")
        return
    
    start_time = datetime.now(IRAN_TZ)
    msg = await update.message.reply_text(
        f"⏱ **ادامه تایمر: {title}**\n\n"
        f"⏳ زمان سپری شده: {elapsed // 60:02d}:{elapsed % 60:02d}\n"
        f"🎯 زمان باقی‌مانده: {(total_minutes * 60 - elapsed) // 60:02d}:{(total_minutes * 60 - elapsed) % 60:02d}\n"
        f"📊 در حال ادامه...",
        parse_mode=ParseMode.HTML
    )
    
    job_data = {
        "chat_id": chat_id,
        "part_id": part_id,
        "start_time": start_time,
        "timer_message_id": msg.message_id,
        "total_minutes": total_minutes,
        "elapsed_offset": elapsed,
        "user_id": user_id
    }
    
    if context.job_queue:
        job = context.job_queue.run_repeating(update_timer, interval=10, first=10, data=job_data)
        active_timers[part_id] = job
        save_timer_state(user_id, part_id, elapsed, total_minutes, True)

# ==================== تقویم ====================

async def handle_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    dates = get_recent_dates(user_id, 10)
    if not dates:
        await update.message.reply_text(
            "📭 هیچ برنامه‌ای در ۱۰ روز اخیر نداشتی.\n\n"
            "📝 برای شروع، دکمه <b>برنامه امروز</b> رو بزن.",
            reply_markup=get_main_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return
    
    context.user_data["calendar_mode"] = True
    await update.message.reply_text(
        "📅 <b>۱۰ روز اخیر:</b>\n\n"
        "تاریخ مورد نظر رو انتخاب کن:",
        reply_markup=get_calendar_keyboard(dates),
        parse_mode=ParseMode.HTML
    )

async def handle_calendar_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if not text.startswith("📅 "):
        return
    
    shamsi_date = text.replace("📅 ", "").strip()
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    try:
        parts = shamsi_date.split("/")
        if len(parts) == 3:
            year, month, day = map(int, parts)
            jdate = jdatetime.date(year, month, day)
            gregorian = jdate.togregorian()
            date_str = gregorian.strftime("%Y-%m-%d")
        else:
            await update.message.reply_text("❌ تاریخ نامعتبر.")
            return
    except:
        await update.message.reply_text("❌ تاریخ نامعتبر.")
        return
    
    plan = get_plan_by_date(user_id, date_str)
    if not plan or not plan["parts"]:
        await update.message.reply_text(f"📭 در تاریخ {shamsi_date} برنامه‌ای نداشتی.", reply_markup=get_main_keyboard())
        return
    
    context.user_data["current_plan"] = plan
    context.user_data["selected_date"] = date_str
    
    if plan.get("confirmed", False):
        await show_parts_final(update, context, plan["parts"], True)
    else:
        await show_parts_initial(update, context, plan["parts"])

# ==================== برنامه امروز ====================

async def handle_today_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نمایش برنامه امروز یا گزینه‌های ساخت"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    today = get_today_date()
    plan = get_plan_by_date(user_id, today)
    
    # اگر برنامه وجود دارد، نمایش بده
    if plan and plan["parts"] and not plan.get("archived", False):
        context.user_data["current_plan"] = plan
        context.user_data["selected_date"] = today
        if plan.get("confirmed", False):
            await show_parts_final(update, context, plan["parts"])
        else:
            await show_parts_initial(update, context, plan["parts"])
        return
    
    # اگر برنامه وجود ندارد، گزینه‌های ساخت را نمایش بده
    remaining = get_remaining_messages(user_id)
    has_ai = remaining > 0
    
    text = "📝 **برنامه امروز**\n\n"
    text += "هنوز برنامه‌ای برای امروز نداری.\n\n"
    
    if has_ai:
        text += "🧠 می‌تونی با AI برنامه بسازی (سریع و هوشمند)\n"
        text += f"💬 پیام‌های باقی‌مانده: {remaining}\n\n"
    else:
        text += "⛔️ سقف پیام AI امروز تموم شده.\n"
        text += "✏️ می‌تونی دستی برنامه بسازی.\n\n"
    
    text += "لطفاً یکی از گزینه‌های زیر رو انتخاب کن:"
    
    keyboard = []
    if has_ai:
        keyboard.append(["🧠 ساخت با AI"])
    keyboard.append(["✏️ ساخت دستی"])
    keyboard.append(["🔙 بازگشت"])
    
    await update.message.reply_text(
        text,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )

async def handle_today_plan_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ساخت برنامه با AI"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    remaining = get_remaining_messages(user_id)
    if remaining <= 0:
        await update.message.reply_text(
            "⛔️ سقف پیام AI امروز تموم شده.\n"
            "✏️ لطفاً از گزینه <b>ساخت دستی</b> استفاده کن.",
            parse_mode=ParseMode.HTML
        )
        return
    
    user_data = get_user_data(str(update.effective_user.id))
    if not user_data:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    # حذف برنامه قبلی اگر وجود دارد
    today = get_today_date()
    existing = get_plan_by_date(user_id, today)
    if existing and existing.get("session_id"):
        execute_query("UPDATE study_sessions SET archived = TRUE WHERE session_id = %s", (existing["session_id"],))
    
    level = calculate_plan_level(user_id)
    user_data['plan_level'] = level
    update_user_plan_level(user_id, level)
    
    wait_msg = await update.message.reply_text("🧠 در حال ساخت برنامه هوشمند...")
    ai_response = await generate_plan_with_ai(user_id, user_data)
    await wait_msg.delete()
    
    if ai_response and ai_response.get('subjects'):
        session_id = create_plan_from_ai_response(user_id, user_data, ai_response)
        if session_id:
            # افزایش مصرف AI
            increment_quota(user_id)
            
            plan = get_plan_by_date(user_id, today)
            if plan:
                context.user_data["current_plan"] = plan
                await show_parts_initial(update, context, plan["parts"])
                return
    
    await update.message.reply_text(
        "❌ خطا در ساخت برنامه با AI.\n"
        "لطفاً از گزینه <b>ساخت دستی</b> استفاده کن.",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.HTML
    )

async def start_manual_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """شروع ساخت دستی برنامه - مرحله ۱: وارد کردن ساعت‌ها"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    # حذف برنامه قبلی اگر وجود دارد
    today = get_today_date()
    existing = get_plan_by_date(user_id, today)
    if existing and existing.get("session_id"):
        execute_query("UPDATE study_sessions SET archived = TRUE WHERE session_id = %s", (existing["session_id"],))
    
    context.user_data["manual_plan_step"] = "time_slots"
    context.user_data["manual_time_slots"] = []
    context.user_data["manual_activities"] = []
    
    await update.message.reply_text(
        "✏️ **ساخت دستی برنامه امروز**\n\n"
        "📍 **مرحله ۱: ساعت‌های مطالعه**\n\n"
        "ساعت‌های مطالعه خود را وارد کنید.\n"
        "هر سطر یک بازه زمانی باشد.\n\n"
        "📝 مثال:\n"
        "۸-۱۰ صبح\n"
        "۱۰:۳۰-۱۲ ظهر\n"
        "۱۶-۱۸ عصر\n\n"
        "⚠️ حتماً ساعت شروع و پایان را مشخص کنید.\n"
        "برای اتمام، <b>تموم</b> رو بفرست.",
        reply_markup=ReplyKeyboardMarkup([["تموم", "🔙 بازگشت"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )

async def start_add_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """شروع اضافه کردن فعالیت - مرحله ۱: انتخاب بازه"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    if not parts:
        await update.message.reply_text("❌ ابتدا برنامه‌ای ایجاد کن.")
        return
    
    # پیدا کردن بازه‌های خالی
    used_slots = []
    for part in parts:
        if part.get("time_slot"):
            used_slots.append(part["time_slot"])
    
    # پیشنهاد بازه‌های خالی (ساده: استفاده از زمان‌های پیش‌فرض)
    default_slots = ["۸-۹", "۹-۱۰", "۱۰-۱۱", "۱۱-۱۲", "۱۳-۱۴", "۱۴-۱۵", "۱۵-۱۶", "۱۶-۱۷", "۱۷-۱۸", "۱۸-۱۹", "۱۹-۲۰"]
    available = [s for s in default_slots if s not in used_slots]
    
    if not available:
        await update.message.reply_text("❌ همه بازه‌های زمانی پر هستند.\nلطفاً یک پارت را تکمیل یا حذف کن.")
        return
    
    context.user_data["add_activity_step"] = "time_slot"
    
    keyboard = []
    for slot in available[:6]:
        keyboard.append([f"⏰ {slot}"])
    keyboard.append(["✏️ بازه دلخواه", "🔙 بازگشت"])
    
    await update.message.reply_text(
        "📝 **اضافه کردن فعالیت جدید**\n\n"
        "📍 **مرحله ۱: انتخاب بازه زمانی**\n\n"
        "یکی از بازه‌های زیر رو انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def handle_manual_plan_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پردازش ورودی ساخت دستی برنامه"""
    text = update.message.text.strip()
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    step = context.user_data.get("manual_plan_step")
    
    if step == "time_slots":
        if text == "🔙 بازگشت":
            context.user_data.pop("manual_plan_step", None)
            context.user_data.pop("manual_time_slots", None)
            await update.message.reply_text("🔙 بازگشت به منو", reply_markup=get_main_keyboard())
            return
        
        if text == "تموم":
            time_slots = context.user_data.get("manual_time_slots", [])
            if not time_slots:
                await update.message.reply_text(
                    "❌ حداقل یک بازه زمانی وارد کن.\n"
                    "مثال: ۸-۱۰"
                )
                return
            
            context.user_data["manual_plan_step"] = "activities"
            await update.message.reply_text(
                f"✅ {len(time_slots)} بازه زمانی ثبت شد.\n\n"
                "📍 **مرحله ۲: فعالیت‌ها**\n\n"
                "فعالیت‌های خود را وارد کنید.\n"
                "هر سطر یک فعالیت باشد.\n\n"
                "📝 فرمت: عنوان | مدت (دقیقه) | اولویت\n"
                "مثال:\n"
                "ریاضی - فصل ۴ | ۴۵ | بالا\n"
                "فیزیک - حرکت شناسی | ۶۰ | بالا\n"
                "زیست - گفتار ۱ | ۳۰ | متوسط\n\n"
                "⚠️ تعداد فعالیت‌ها باید با تعداد بازه‌ها برابر باشد.\n"
                "برای اتمام، <b>تموم</b> رو بفرست.",
                reply_markup=ReplyKeyboardMarkup([["تموم", "🔙 بازگشت"]], resize_keyboard=True),
                parse_mode=ParseMode.HTML
            )
            return
        
        # پردازش بازه‌های زمانی
        time_slots = parse_time_slots(text)
        if time_slots:
            context.user_data["manual_time_slots"].extend(time_slots)
            await update.message.reply_text(
                f"✅ {len(time_slots)} بازه زمانی اضافه شد.\n"
                f"📊 مجموع: {len(context.user_data['manual_time_slots'])} بازه\n\n"
                "بازه‌های بعدی رو وارد کن یا <b>تموم</b> رو بفرست.",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text("❌ فرمت بازه زمانی نامعتبر.\nمثال: ۸-۱۰")
    
    elif step == "activities":
        if text == "🔙 بازگشت":
            context.user_data.pop("manual_plan_step", None)
            context.user_data.pop("manual_time_slots", None)
            context.user_data.pop("manual_activities", None)
            await update.message.reply_text("🔙 بازگشت به منو", reply_markup=get_main_keyboard())
            return
        
        if text == "تموم":
            activities = context.user_data.get("manual_activities", [])
            time_slots = context.user_data.get("manual_time_slots", [])
            
            if not activities:
                await update.message.reply_text("❌ حداقل یک فعالیت وارد کن.")
                return
            
            if len(activities) != len(time_slots):
                await update.message.reply_text(
                    f"⚠️ تعداد فعالیت‌ها ({len(activities)}) با تعداد بازه‌ها ({len(time_slots)}) برابر نیست.\n"
                    f"لطفاً {len(time_slots) - len(activities)} فعالیت دیگر وارد کن."
                )
                return
            
            # ساخت برنامه
            session_id = create_manual_plan(user_id, time_slots, activities)
            
            if session_id:
                plan = get_plan_by_date(user_id, get_today_date())
                if plan:
                    context.user_data["current_plan"] = plan
                    context.user_data.pop("manual_plan_step", None)
                    context.user_data.pop("manual_time_slots", None)
                    context.user_data.pop("manual_activities", None)
                    
                    await update.message.reply_text(
                        "✅ **برنامه امروز ساخته شد!**",
                        reply_markup=get_main_keyboard()
                    )
                    await show_parts_initial(update, context, plan["parts"])
                    return
            
            await update.message.reply_text("❌ خطا در ساخت برنامه. لطفاً دوباره تلاش کن.")
            return
        
        # پردازش فعالیت‌ها
        activities = parse_activities(text)
        if activities:
            context.user_data["manual_activities"].extend(activities)
            remaining = len(context.user_data["manual_time_slots"]) - len(context.user_data["manual_activities"])
            await update.message.reply_text(
                f"✅ {len(activities)} فعالیت اضافه شد.\n"
                f"📊 مجموع: {len(context.user_data['manual_activities'])} فعالیت\n"
                f"⏳ {remaining} فعالیت دیگر باقی‌مانده.\n\n"
                "فعالیت‌های بعدی رو وارد کن یا <b>تموم</b> رو بفرست.",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                "❌ فرمت فعالیت نامعتبر.\n"
                "مثال: ریاضی - فصل ۴ | ۴۵ | بالا"
            )

async def handle_add_activity_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پردازش ورودی اضافه کردن فعالیت"""
    text = update.message.text.strip()
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    step = context.user_data.get("add_activity_step")
    
    if step == "time_slot":
        if text == "🔙 بازگشت":
            context.user_data.pop("add_activity_step", None)
            context.user_data.pop("add_activity_time_slot", None)
            plan = context.user_data.get("current_plan", {})
            parts = plan.get("parts", [])
            if parts:
                await show_parts_final(update, context, parts, True)
            else:
                await update.message.reply_text("🔙 بازگشت", reply_markup=get_main_keyboard())
            return
        
        if text.startswith("⏰ "):
            time_slot_str = text.replace("⏰ ", "").strip()
        elif text.startswith("✏️ "):
            await update.message.reply_text(
                "✏️ بازه دلخواه رو وارد کن (مثال: ۱۳-۱۴):"
            )
            context.user_data["add_activity_step"] = "custom_time"
            return
        else:
            time_slot_str = text
        
        # پردازش بازه
        slots = parse_time_slots(time_slot_str)
        if slots:
            context.user_data["add_activity_time_slot"] = slots[0]
            context.user_data["add_activity_step"] = "activity"
            
            await update.message.reply_text(
                f"✅ بازه {time_slot_str} انتخاب شد.\n\n"
                "📍 **مرحله ۲: اطلاعات فعالیت**\n\n"
                "📝 عنوان فعالیت | مدت (دقیقه) | اولویت\n"
                "مثال: شیمی - فصل ۲ | ۴۵ | بالا\n\n"
                "برای لغو، <b>لغو</b> رو بفرست.",
                reply_markup=ReplyKeyboardMarkup([["لغو", "🔙 بازگشت"]], resize_keyboard=True)
            )
        else:
            await update.message.reply_text("❌ بازه نامعتبر. لطفاً دوباره انتخاب کن.")
    
    elif step == "custom_time":
        slots = parse_time_slots(text)
        if slots:
            context.user_data["add_activity_time_slot"] = slots[0]
            context.user_data["add_activity_step"] = "activity"
            
            await update.message.reply_text(
                f"✅ بازه {text} انتخاب شد.\n\n"
                "📍 **مرحله ۲: اطلاعات فعالیت**\n\n"
                "📝 عنوان فعالیت | مدت (دقیقه) | اولویت\n"
                "مثال: شیمی - فصل ۲ | ۴۵ | بالا",
                reply_markup=ReplyKeyboardMarkup([["لغو", "🔙 بازگشت"]], resize_keyboard=True)
            )
        else:
            await update.message.reply_text("❌ بازه نامعتبر. لطفاً دوباره وارد کن.")
    
    elif step == "activity":
        if text == "🔙 بازگشت" or text == "لغو":
            context.user_data.pop("add_activity_step", None)
            context.user_data.pop("add_activity_time_slot", None)
            plan = context.user_data.get("current_plan", {})
            parts = plan.get("parts", [])
            if parts:
                await show_parts_final(update, context, parts, True)
            else:
                await update.message.reply_text("🔙 بازگشت", reply_markup=get_main_keyboard())
            return
        
        # پردازش فعالیت
        activities = parse_activities(text)
        if activities:
            activity = activities[0]
            time_slot = context.user_data.get("add_activity_time_slot")
            plan = context.user_data.get("current_plan", {})
            session_id = plan.get("session_id")
            
            if not session_id:
                await update.message.reply_text("❌ برنامه‌ای وجود ندارد.")
                return
            
            part_id = add_manual_activity(session_id, time_slot, activity)
            if part_id:
                # به‌روزرسانی plan
                plan = get_plan_by_date(user_id, get_today_date())
                if plan:
                    context.user_data["current_plan"] = plan
                    context.user_data.pop("add_activity_step", None)
                    context.user_data.pop("add_activity_time_slot", None)
                    
                    await update.message.reply_text(
                        f"✅ فعالیت جدید اضافه شد!\n\n"
                        f"📖 {activity['title']}\n"
                        f"⏱ {activity.get('duration', 45)} دقیقه\n"
                        f"🕒 {time_slot['start']}-{time_slot['end']}",
                        reply_markup=get_main_keyboard()
                    )
                    await show_parts_final(update, context, plan["parts"], True)
                    return
            
            await update.message.reply_text("❌ خطا در اضافه کردن فعالیت.")
        else:
            await update.message.reply_text("❌ فرمت فعالیت نامعتبر.")

# ==================== چت با AI ====================

async def handle_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ورود به حالت چت AI"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    if not get_user_quota(user_id):
        init_user_quota(user_id)
    
    remaining = get_remaining_messages(user_id)
    
    if remaining <= 0:
        await update.message.reply_text(
            "⛔️ **سقف پیام رایگان امروزت تموم شده!**\n\n"
            "📊 پیام‌های باقی‌مانده: ۰\n"
            "💡 برای ادامه چت با AI، اشتراک تهیه کن.\n\n"
            "💰 هزینه اشتراک یک ماهه: ۵۰۰,۰۰۰ تومان\n"
            "📱 شماره کارت: **۶۲۱۹۸۶۱۸۳۷۵۶۹۶۸۹**\n"
            "👤 به نام: **مصطفی فرخندئی**\n\n"
            "📸 بعد از واریز، عکس رسید رو بفرست تا اشتراکت فعال بشه.",
            parse_mode=ParseMode.HTML
        )
        return
    
    context.user_data["mode"] = "ai_chat"
    
    user_data = get_user_data(str(update.effective_user.id))
    context_summary = ""
    if user_data:
        weak = ", ".join(user_data.get("weak_subjects", [])) or "ندارد"
        context_summary = f"هدف کاربر: {user_data.get('goal', 'نامشخص')} | درس ضعیف: {weak}"
    
    context.user_data["ai_context_summary"] = context_summary
    
    await update.message.reply_text(
        f"💬 **چت با دستیار هوشمند مطالعه**\n\n"
        f"📚 هر سوالی درباره درس و برنامه‌ریزی داری بپرس.\n"
        f"🔄 برای شروع مکالمه جدید از دکمه استفاده کن.\n"
        f"🔙 برای خروج به منو برگرد.\n\n"
        f"📊 **پیام‌های باقی‌مانده امروز**: {remaining}\n"
        f"📌 {context_summary}",
        reply_markup=get_ai_chat_keyboard(),
        parse_mode=ParseMode.HTML
    )

async def handle_ai_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پردازش پیام در حالت چت AI"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        context.user_data["mode"] = None
        return
    
    text = update.message.text.strip()
    
    if text == "🔙 بازگشت به منو":
        context.user_data["mode"] = None
        context.user_data.pop("ai_context_summary", None)
        await update.message.reply_text("🔙 برگشتی به منو 👇", reply_markup=get_main_keyboard())
        return
    
    if text == "🔄 مکالمه جدید":
        clear_chat_history(user_id)
        await update.message.reply_text(
            "🔄 مکالمه جدید شروع شد 🌱\n"
            "حالا سوالت رو بپرس.",
            reply_markup=get_ai_chat_keyboard()
        )
        return
    
    if text == "📊 مصرف امروز":
        remaining = get_remaining_messages(user_id)
        quota = get_user_quota(user_id)
        plan_type = quota.get("plan_type", "trial") if quota else "trial"
        plan_names = {"trial": "آزمایشی", "basic": "پایه", "premium": "پیشرفته"}
        await update.message.reply_text(
            f"📊 **مصرف امروز**\n\n"
            f"📌 نوع اشتراک: {plan_names.get(plan_type, 'آزمایشی')}\n"
            f"💬 پیام‌های باقی‌مانده: {remaining}\n"
            f"📅 تاریخ: {get_today_shamsi()}",
            parse_mode=ParseMode.HTML
        )
        return
    
    if len(text) > 1000:
        await update.message.reply_text("⚠️ لطفاً پیام کوتاه‌تری بفرست (حداکثر ۱۰۰۰ کاراکتر).")
        return
    
    remaining = get_remaining_messages(user_id)
    if remaining <= 0:
        await update.message.reply_text(
            "⛔️ سقف پیام امروزت تموم شده!\n"
            "برای ادامه، اشتراک تهیه کن یا فردا دوباره امتحان کن."
        )
        context.user_data["mode"] = None
        await update.message.reply_text("🔙 برگشتی به منو", reply_markup=get_main_keyboard())
        return
    
    # تشخیص دستورات ویژه
    await process_ai_command(update, context, text, user_id)

async def process_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int):
    """پردازش دستورات ویژه AI"""
    
    # دستورات ساخت برنامه
    if any(word in text for word in ["برنامه بساز", "برنامه امروز", "برنامه جدید", "ساخت برنامه"]):
        await handle_today_plan_ai(update, context)
        return
    
    # دستورات تکمیل
    if any(word in text for word in ["تموم کردم", "انجام شد", "تکمیل شد", "تموم شد"]):
        await handle_complete_from_chat(update, context, text, user_id)
        return
    
    # دستورات اضافه کردن
    if any(word in text for word in ["اضافه کن", "اضافه کردن", "جدید"]):
        await handle_add_from_chat(update, context, text, user_id)
        return
    
    # دستورات تغییر
    if any(word in text for word in ["تغییر بده", "ویرایش کن", "جابه‌جا کن"]):
        await handle_edit_from_chat(update, context, text, user_id)
        return
    
    # دستورات نمایش
    if any(word in text for word in ["نشون بده", "برنامه رو ببین", "برنامه امروز"]):
        await handle_today_plan(update, context)
        return
    
    # اگر دستور خاصی نبود، چت معمولی
    await handle_normal_chat(update, context, text, user_id)

async def handle_normal_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int):
    """پردازش چت معمولی با AI"""
    history = get_chat_history(user_id, limit=10)
    
    messages = [{"role": "system", "content": AI_CHAT_SYSTEM_PROMPT}]
    
    context_summary = context.user_data.get("ai_context_summary", "")
    if context_summary:
        messages[0]["content"] += f"\n\nاطلاعات کاربر: {context_summary}"
    
    messages += history
    messages.append({"role": "user", "content": text})
    
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    
    try:
        completion = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            max_tokens=600,
            temperature=0.6
        )
        
        reply = completion.choices[0].message.content
        
        save_chat_message(user_id, "user", text)
        save_chat_message(user_id, "assistant", reply)
        
        increment_quota(user_id)
        
        remaining_after = get_remaining_messages(user_id)
        
        await update.message.reply_text(
            f"{reply}\n\n"
            f"📊 {remaining_after} پیام امروز باقی مونده",
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"خطا در چت AI: {e}")
        await update.message.reply_text(
            "⚠️ مشکلی در ارتباط با AI پیش اومد.\n"
            "لطفاً چند لحظه بعد دوباره امتحان کن."
        )

async def handle_complete_from_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int):
    """تکمیل پارت از طریق چت"""
    # پیدا کردن عنوان پارت
    title_match = re.search(r'(.+?)\s*(?:رو|را|)\s*(?:تموم|انجام|تکمیل)', text)
    if not title_match:
        await update.message.reply_text(
            "❌ لطفاً عنوان پارت رو مشخص کن.\n"
            "مثال: ریاضی رو تموم کردم"
        )
        return
    
    title = title_match.group(1).strip()
    
    # پیدا کردن پارت در برنامه امروز
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    
    if not parts:
        plan = get_plan_by_date(user_id, get_today_date())
        if plan and plan.get("parts"):
            parts = plan["parts"]
            context.user_data["current_plan"] = plan
        else:
            await update.message.reply_text("❌ برنامه‌ای برای امروز وجود ندارد.")
            return
    
    # پیدا کردن پارت
    found_part = None
    for p in parts:
        if title in p["title"] or p["title"] in title:
            found_part = p
            break
    
    if not found_part:
        await update.message.reply_text(
            f"❌ پارت '{title}' در برنامه امروز پیدا نشد.\n"
            f"📋 پارت‌های موجود:\n" + "\n".join([f"• {p['title']}" for p in parts[:5]])
        )
        return
    
    if found_part.get("completed"):
        await update.message.reply_text(f"✅ {title} قبلاً تکمیل شده.")
        return
    
    # تکمیل پارت
    await handle_done_part(update, context, found_part["part_id"])

async def handle_add_from_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int):
    """اضافه کردن فعالیت از طریق چت"""
    # استخراج اطلاعات
    title_match = re.search(r'(?:اضافه کن|جدید)\s*(.+?)\s*(?:به|با|برای|)', text)
    if not title_match:
        await update.message.reply_text(
            "❌ لطفاً عنوان فعالیت رو مشخص کن.\n"
            "مثال: اضافه کن ریاضی - فصل ۴"
        )
        return
    
    title = title_match.group(1).strip()
    
    # استخراج مدت زمان
    duration_match = re.search(r'(\d+)\s*(?:دقیقه|د|ساعت)', text)
    duration = 45
    if duration_match:
        duration = int(duration_match.group(1))
        if "ساعت" in duration_match.group(0):
            duration *= 60
        duration = max(20, min(90, duration))
    
    # استخراج اولویت
    priority = "medium"
    if "بالا" in text or "مهم" in text:
        priority = "high"
    elif "پایین" in text or "کم" in text:
        priority = "low"
    
    # پیدا کردن بازه خالی
    plan = context.user_data.get("current_plan", {})
    if not plan.get("session_id"):
        await update.message.reply_text("❌ ابتدا برنامه‌ای ایجاد کن.")
        return
    
    parts = plan.get("parts", [])
    used_slots = [p.get("time_slot") for p in parts if p.get("time_slot")]
    
    # پیشنهاد بازه
    default_slots = ["۸-۹", "۹-۱۰", "۱۰-۱۱", "۱۱-۱۲", "۱۳-۱۴", "۱۴-۱۵", "۱۵-۱۶", "۱۶-۱۷", "۱۷-۱۸", "۱۸-۱۹", "۱۹-۲۰"]
    available = [s for s in default_slots if s not in used_slots]
    
    if not available:
        await update.message.reply_text("❌ همه بازه‌ها پر هستند.\nلطفاً یک پارت را تکمیل یا حذف کن.")
        return
    
    time_slot_str = available[0]
    slots = parse_time_slots(time_slot_str)
    
    if slots:
        activity = {"title": title, "duration": duration, "priority": priority}
        part_id = add_manual_activity(plan["session_id"], slots[0], activity)
        
        if part_id:
            plan = get_plan_by_date(user_id, get_today_date())
            if plan:
                context.user_data["current_plan"] = plan
                await update.message.reply_text(
                    f"✅ فعالیت جدید اضافه شد!\n\n"
                    f"📖 {title}\n"
                    f"⏱ {duration} دقیقه\n"
                    f"🕒 {time_slot_str}"
                )
                await show_parts_final(update, context, plan["parts"], True)
                return
    
    await update.message.reply_text("❌ خطا در اضافه کردن فعالیت.")

async def handle_edit_from_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int):
    """تغییر برنامه از طریق چت"""
    # استخراج تغییرات
    changes = []
    
    # تغییر زمان
    time_match = re.search(r'زمان\s*(.+?)\s*(?:رو|را|)\s*(?:به|)\s*(\d+)\s*(?:دقیقه|ساعت)', text)
    if time_match:
        title = time_match.group(1).strip()
        new_time = int(time_match.group(2))
        if "ساعت" in time_match.group(3):
            new_time *= 60
        changes.append({"type": "time", "title": title, "value": new_time})
    
    # تغییر ترتیب
    move_match = re.search(r'(?:انتقال|بردن|جابه‌جا)\s*(.+?)\s*(?:به|قبل|بعد)', text)
    if move_match:
        title = move_match.group(1).strip()
        changes.append({"type": "move", "title": title})
    
    if not changes:
        await update.message.reply_text(
            "❌ تغییر مورد نظر رو مشخص کن.\n"
            "مثال: زمان ریاضی رو به ۶۰ دقیقه افزایش بده"
        )
        return
    
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    
    if not parts:
        await update.message.reply_text("❌ برنامه‌ای برای امروز وجود ندارد.")
        return
    
    applied = []
    for change in changes:
        if change["type"] == "time":
            for p in parts:
                if change["title"] in p["title"] or p["title"] in change["title"]:
                    old_time = p["planned_minutes"]
                    p["planned_minutes"] = max(20, min(90, change["value"]))
                    applied.append(f"⏱ زمان {p['title']}: {old_time}د → {p['planned_minutes']}د")
                    execute_query("UPDATE study_parts SET planned_minutes = %s WHERE part_id = %s", 
                                 (p["planned_minutes"], p["part_id"]))
                    break
        
        elif change["type"] == "move":
            # پیدا کردن پارت و جابه‌جایی
            for i, p in enumerate(parts):
                if change["title"] in p["title"] or p["title"] in change["title"]:
                    if i > 0:
                        parts[i], parts[i-1] = parts[i-1], parts[i]
                        for j, p2 in enumerate(parts):
                            p2["part_number"] = j + 1
                        applied.append(f"🔄 {p['title']} جابه‌جا شد")
                        break
    
    if applied:
        # به‌روزرسانی زمان‌ها
        current_time = 8 * 60
        for p in sorted(parts, key=lambda x: x.get("part_number", 0)):
            duration = p["planned_minutes"]
            start_h = current_time // 60
            start_m = current_time % 60
            end_time = current_time + duration
            end_h = end_time // 60
            end_m = end_time % 60
            p["planned_start_time"] = f"{start_h:02d}:{start_m:02d}"
            p["planned_end_time"] = f"{end_h:02d}:{end_m:02d}"
            p["time_slot"] = f"{p['planned_start_time']}-{p['planned_end_time']}"
            current_time = end_time + 5
        
        for p in parts:
            execute_query(
                """UPDATE study_parts 
                   SET part_number = %s, planned_start_time = %s, planned_end_time = %s, time_slot = %s
                   WHERE part_id = %s""",
                (p["part_number"], p["planned_start_time"], p["planned_end_time"], p["time_slot"], p["part_id"])
            )
        
        plan["parts"] = parts
        context.user_data["current_plan"] = plan
        
        await update.message.reply_text(
            "✅ **تغییرات اعمال شد!**\n\n" + "\n".join(applied)
        )
        await show_parts_final(update, context, parts, True)
    else:
        await update.message.reply_text("❌ تغییری اعمال نشد.")

# ==================== گزارش ====================

async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    today = get_today_date()
    activities = get_today_activities(user_id)
    subject_status = get_subject_status(user_id)
    
    if not activities and not subject_status:
        await update.message.reply_text(
            "📭 هنوز فعالیتی ثبت نکردی.\n"
            "📝 با دکمه <b>برنامه امروز</b> شروع کن.",
            reply_markup=get_main_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return
    
    text = f"📊 **گزارش {get_today_shamsi()}** - ساعت {get_iran_time_str()}\n\n"
    
    if activities:
        total_time = sum(a.get("actual_duration", a.get("planned_duration", 0)) for a in activities)
        done = len([a for a in activities if a.get("status") == "done"])
        scores = [a.get("score") for a in activities if a.get("score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0
        text += f"⏱ زمان کل: {format_time_hours_minutes(total_time)}\n"
        text += f"✅ تکمیل‌شده: {done}/{len(activities)}\n"
        if scores:
            text += f"📊 میانگین نمره: {avg_score:.1f}%\n"
        text += "\n📋 **فعالیت‌ها:**\n"
        for a in activities:
            status = "✅" if a.get("status") == "done" else "⬜"
            text += f"{status} {a['subject']}"
            if a.get('topic'):
                text += f" - {a['topic']}"
            if a.get('score') is not None:
                text += f" ({a['score']:.0f}%)"
            text += f" - {a.get('actual_duration', a.get('planned_duration', 0))} دقیقه\n"
    
    if subject_status:
        text += "\n📚 **وضعیت دروس:**\n"
        for s in subject_status[:5]:
            level_emoji = "🔴" if s.get("level") == "weak" else "🟡" if s.get("level") == "medium" else "🟢"
            text += f"{level_emoji} {s['subject']}: {s.get('avg_score', 0):.0f}%"
            if s.get('progress', 0) > 0:
                text += f" ({s['progress']:.0f}% پیشرفت)"
            text += "\n"
    
    user_data = get_user_data(str(update.effective_user.id))
    if user_data:
        level = user_data.get('plan_level', 0)
        level_name = get_plan_level_name(level)
        level_emoji = get_plan_level_emoji(level)
        text += f"\n📊 سطح برنامه: {level_emoji} {level_name}"
        
        remaining = get_remaining_messages(user_id)
        text += f"\n💬 پیام‌های AI باقی‌مانده: {remaining}"
    
    await update.message.reply_text(text, reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML)

# ==================== خرید اشتراک و پرداخت ====================

async def handle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نمایش اطلاعات اشتراک و خرید"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    quota = get_user_quota(user_id)
    remaining = get_remaining_messages(user_id)
    
    plan_names = {
        "trial": "🌱 آزمایشی",
        "basic": "📘 پایه", 
        "premium": "🚀 پیشرفته"
    }
    
    plan_type = quota.get("plan_type", "trial") if quota else "trial"
    plan_name = plan_names.get(plan_type, "آزمایشی")
    
    text = f"""💰 **اشتراک و خرید**

📌 وضعیت فعلی: {plan_name}
💬 پیام‌های باقی‌مانده: {remaining}

---

🌟 **پلن‌های اشتراک:**

📘 **پایه** - ۵۰۰,۰۰۰ تومان
• ۱۵ پیام AI در روز
• برنامه روزانه هوشمند
• تحلیل عملکرد هفتگی
• پشتیبانی ویژه

🚀 **پیشرفته** - ۱,۰۰۰,۰۰۰ تومان
• ۳۰ پیام AI در روز
• حالت High برای پاسخ‌های دقیق‌تر
• تحلیل عمیق و استراتژی آزمون
• برنامه شخصی‌سازی‌شده روزانه
• اولویت در پشتیبانی

---

💳 **روش پرداخت:**

شماره کارت: **۶۲۱۹۸۶۱۸۳۷۵۶۹۶۸۹**
به نام: **مصطفی فرخندئی**

📸 بعد از واریز، عکس رسید رو بفرست تا اشتراکت فعال بشه.

🔹 اشتراک به مدت **یک ماه** فعال می‌شود.
🔹 تمدید خودکار: یک روز قبل از انقضا یادآوری می‌شود.
"""
    
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دریافت عکس رسید پرداخت و ارسال به ادمین"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    photo = update.message.photo[-1]
    user_data = get_user_data(str(update.effective_user.id))
    
    caption = f"📸 **رسید جدید پرداخت**\n\n"
    caption += f"👤 کاربر: {user_data.get('full_name', 'نامشخص')}\n"
    caption += f"🆔 ID: {user_id}\n"
    caption += f"📱 یوزرنیم: @{user_data.get('username', 'ندارد')}\n"
    caption += f"📅 تاریخ: {get_today_shamsi()}\n"
    caption += f"⏰ ساعت: {get_iran_time_str()}\n\n"
    caption += "برای تایید اشتراک، از دکمه‌های زیر استفاده کن."
    
    keyboard = [
        [InlineKeyboardButton("✅ تایید اشتراک", callback_data=f"approve_sub_{user_id}")],
        [InlineKeyboardButton("❌ رد", callback_data=f"reject_sub_{user_id}")],
        [InlineKeyboardButton("📝 پیام به کاربر", callback_data=f"msg_sub_{user_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo.file_id,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"خطا در ارسال رسید به ادمین {admin_id}: {e}")
    
    await update.message.reply_text(
        "✅ **رسید شما برای ادمین ارسال شد.**\n\n"
        "📌 پس از تایید، اشتراک شما فعال می‌شود.\n"
        "⏱ این فرآیند معمولاً کمتر از ۲۴ ساعت طول می‌کشد.\n\n"
        "🔔 پس از فعال‌سازی به شما اطلاع داده می‌شود."
    )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پردازش callback های اینلاین"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    admin_id = update.effective_user.id
    
    if admin_id not in ADMIN_IDS:
        await query.edit_message_text("❌ شما دسترسی ادمین ندارید.")
        return
    
    if data.startswith("approve_sub_"):
        user_id = int(data.replace("approve_sub_", ""))
        # فعال‌سازی اشتراک
        execute_query(
            "UPDATE user_quota SET plan_type = 'basic', plan_expiry = %s WHERE user_id = %s",
            ((datetime.now(IRAN_TZ) + timedelta(days=30)).date(), user_id)
        )
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🎉 **اشتراک شما فعال شد!**\n\n"
                     "📘 اشتراک پایه به مدت یک ماه فعال شد.\n"
                     "💬 روزانه ۱۵ پیام AI در اختیار دارید.\n\n"
                     "📚 موفق باشید! 🚀",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"خطا در ارسال پیام تایید به کاربر: {e}")
        
        await query.edit_message_text(
            f"✅ اشتراک کاربر {user_id} فعال شد.\n"
            f"📅 تاریخ انقضا: {(datetime.now(IRAN_TZ) + timedelta(days=30)).strftime('%Y-%m-%d')}"
        )
    
    elif data.startswith("reject_sub_"):
        user_id = int(data.replace("reject_sub_", ""))
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ متأسفیم، درخواست اشتراک شما تایید نشد.\n"
                     "لطفاً برای اطلاعات بیشتر با پشتیبانی تماس بگیرید."
            )
        except Exception as e:
            logger.error(f"خطا در ارسال پیام رد به کاربر: {e}")
        
        await query.edit_message_text(f"❌ درخواست اشتراک کاربر {user_id} رد شد.")

# ==================== پروفایل ====================

async def handle_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نمایش پروفایل کاربر"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    user_data = get_user_data(str(update.effective_user.id))
    if not user_data:
        await update.message.reply_text("❌ اطلاعات شما یافت نشد.")
        return
    
    quota = get_user_quota(user_id)
    remaining = get_remaining_messages(user_id)
    
    plan_names = {
        "trial": "🌱 آزمایشی",
        "basic": "📘 پایه", 
        "premium": "🚀 پیشرفته"
    }
    
    plan_type = quota.get("plan_type", "trial") if quota else "trial"
    plan_name = plan_names.get(plan_type, "آزمایشی")
    
    level = user_data.get('plan_level', 0)
    level_name = get_plan_level_name(level)
    level_emoji = get_plan_level_emoji(level)
    
    text = f"""👤 **پروفایل کاربری**

📌 نام: {user_data.get('full_name', 'نامشخص')}
🎯 هدف: {user_data.get('goal', 'نامشخص')}
🎓 پایه: {user_data.get('grade', 'نامشخص')}
🧪 رشته: {user_data.get('field', 'نامشخص')}

📊 سطح برنامه: {level_emoji} {level_name}
💬 پیام‌های AI باقی‌مانده: {remaining}
💰 اشتراک: {plan_name}

📅 تاریخ ثبت‌نام: {user_data.get('created_at', 'نامشخص')}

---

📝 برای تغییر اطلاعات، با ادمین تماس بگیرید.
"""
    
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ==================== دستورات ادمین ====================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    await update.message.reply_text(
        "👨‍💼 **پنل ادمین**\n\n"
        "📝 /advice - ثبت توصیه جدید\n"
        "📊 /stats - آمار کلی\n"
        "🧠 /testai - تست AI\n"
        "📋 /listadvice - لیست توصیه‌ها\n"
        "🗑 /removeadvice [id] - حذف توصیه\n"
        "📊 /aistats - آمار مصرف AI",
        parse_mode=ParseMode.HTML
    )

async def advice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "📝 **ثبت توصیه جدید**\n\n"
            "توصیه خود را به صورت آزاد بنویس:\n"
            "/advice متن توصیه\n\n"
            "مثال:\n"
            "/advice دانش‌آموزان کنکوری باید روزانه ۴۵ دقیقه ریاضی کار کنن"
        )
        return
    
    admin_text = " ".join(context.args)
    await update.message.reply_text("🧠 در حال پردازش توصیه با AI...")
    processed = process_admin_advice_with_ai(admin_text)
    
    if not processed:
        await update.message.reply_text("❌ خطا در پردازش توصیه.")
        return
    
    saved = 0
    for advice in processed:
        advice["created_by"] = user_id
        result = save_advice(advice)
        if result:
            saved += 1
    
    await update.message.reply_text(
        f"✅ {saved} توصیه با موفقیت ثبت شد!\n\n"
        f"📋 برای مشاهده لیست: /listadvice"
    )

async def list_advice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    results = execute_query(
        """SELECT id, topic, label, advice, priority, is_active, usage_count
           FROM advisory_rules 
           ORDER BY priority DESC, created_at DESC
           LIMIT 20""",
        fetchall=True
    )
    
    if not results:
        await update.message.reply_text("📭 هیچ توصیه‌ای ثبت نشده.")
        return
    
    text = "📋 **لیست توصیه‌ها:**\n\n"
    for r in results:
        status = "✅" if r[5] else "❌"
        text += f"{status} #{r[0]} | {r[1]} | {r[2]} | اولویت {r[3]} | {r[6]} بار استفاده\n"
        text += f"   {r[4][:50]}...\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def remove_advice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    if not context.args:
        await update.message.reply_text("❌ لطفاً ID توصیه را وارد کن: /removeadvice 5")
        return
    
    try:
        advice_id = int(context.args[0])
        execute_query("UPDATE advisory_rules SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (advice_id,))
        await update.message.reply_text(f"🗑 توصیه #{advice_id} غیرفعال شد.")
    except:
        await update.message.reply_text("❌ ID نامعتبر.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    users = execute_query("SELECT COUNT(*) FROM users", fetch=True)
    onboarded = execute_query("SELECT COUNT(*) FROM users WHERE is_onboarded = TRUE", fetch=True)
    activities = execute_query("SELECT COUNT(*) FROM activity_log", fetch=True)
    advice = execute_query("SELECT COUNT(*) FROM advisory_rules WHERE is_active = TRUE", fetch=True)
    chat_msgs = execute_query("SELECT COUNT(*) FROM chat_messages", fetch=True)
    
    text = "📊 **آمار کلی**\n\n"
    text += f"👥 کل کاربران: {users[0] if users else 0}\n"
    text += f"✅ ثبت‌نام‌شده: {onboarded[0] if onboarded else 0}\n"
    text += f"📋 فعالیت‌ها: {activities[0] if activities else 0}\n"
    text += f"💡 توصیه‌های فعال: {advice[0] if advice else 0}\n"
    text += f"💬 پیام‌های چت: {chat_msgs[0] if chat_msgs else 0}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def ai_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    results = execute_query(
        """SELECT u.telegram_id, u.full_name, q.plan_type, q.daily_messages, q.last_reset,
                  COUNT(c.id) as total_chats
           FROM user_quota q
           LEFT JOIN users u ON u.id = q.user_id
           LEFT JOIN chat_messages c ON c.user_id = q.user_id AND c.role = 'assistant'
           GROUP BY u.telegram_id, u.full_name, q.plan_type, q.daily_messages, q.last_reset
           ORDER BY q.daily_messages DESC
           LIMIT 20""",
        fetchall=True
    )
    
    if not results:
        await update.message.reply_text("📭 هنوز مصرفی ثبت نشده.")
        return
    
    text = "📊 **آمار مصرف AI**\n\n"
    for r in results:
        text += f"👤 {r[1] or r[0]}: {r[2] or 'trial'} | امروز: {r[3] or 0} | کل: {r[5] or 0}\n"
    
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def test_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی denied.")
        return
    
    await update.message.reply_text("🧠 در حال تست AI...")
    try:
        start = time.time()
        response = await call_ai("سلام، فقط بگو 'AI وصل است' به فارسی", max_tokens=20, temperature=0.1)
        elapsed = time.time() - start
        if response:
            await update.message.reply_text(
                f"✅ **AI وصل است!**\n\n"
                f"⏱ زمان پاسخ: {elapsed:.2f} ثانیه\n"
                f"📝 پاسخ: {response}\n"
                f"📌 مدل: {AI_MODEL}"
            )
        else:
            await update.message.reply_text("❌ AI پاسخ نداد.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {str(e)[:200]}")

def process_admin_advice_with_ai(admin_text: str) -> List[Dict]:
    prompt = f"""توصیه ادمین را به داده‌های ساختاریافته تبدیل کن:
"{admin_text}"
خروجی JSON:
[
  {{
    "topic": "ریاضی",
    "label": "همه",
    "condition": "همیشه",
    "advice": "روزانه ۴۵ دقیقه صبح مطالعه کن",
    "priority": 9,
    "time": "morning",
    "frequency": "daily"
  }}
]"""
    # برای سادگی، یک توصیه ساده برمی‌گردانیم
    # در نسخه کامل، از AI استفاده می‌شود
    return [{
        "topic": "عمومی",
        "label": "همه",
        "condition": "همیشه",
        "advice": admin_text,
        "priority": 5,
        "time": "any",
        "frequency": "daily"
    }]

# ==================== هندلر اصلی ====================

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    
    if text == "📝 برنامه امروز":
        await handle_today_plan(update, context)
    elif text == "📊 گزارش":
        await handle_report(update, context)
    elif text == "📅 تقویم":
        await handle_calendar(update, context)
    elif text == "💬 چت با AI":
        await handle_ai_chat(update, context)
    elif text == "💰 خرید اشتراک":
        await handle_subscription(update, context)
    elif text == "👤 پروفایل":
        await handle_profile(update, context)
    else:
        await update.message.reply_text("❓ لطفاً از دکمه‌های منو استفاده کن.", reply_markup=get_main_keyboard())

async def handle_text_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پردازش پیام‌های آزاد کاربر"""
    text = update.message.text.strip()
    user_id = get_user_id_by_telegram(update.effective_user.id)
    
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    
    # بررسی حالت چت AI
    if context.user_data.get("mode") == "ai_chat":
        await handle_ai_chat_message(update, context)
        return
    
    # بررسی مرحله ساخت دستی برنامه
    if context.user_data.get("manual_plan_step"):
        await handle_manual_plan_input(update, context)
        return
    
    # بررسی مرحله اضافه کردن فعالیت
    if context.user_data.get("add_activity_step"):
        await handle_add_activity_input(update, context)
        return
    
    # بررسی مرحله onboarding
    step = context.user_data.get("onboarding_step")
    if step is not None:
        await onboarding_handler(update, context)
        return
    
    # پیام آزاد = چت با AI
    await handle_ai_chat(update, context)

async def nightly_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = execute_query(
        "SELECT id, telegram_id, full_name FROM users WHERE is_active = TRUE AND is_onboarded = TRUE",
        fetchall=True
    )
    if not users:
        return
    
    today_shamsi = get_today_shamsi()
    for user in users:
        user_id = user[0]
        telegram_id = user[1]
        full_name = user[2] or "کاربر"
        try:
            activities = get_today_activities(user_id)
            if not activities:
                continue
            total_time = sum(a.get("actual_duration", a.get("planned_duration", 0)) for a in activities)
            done = len([a for a in activities if a.get("status") == "done"])
            scores = [a.get("score") for a in activities if a.get("score") is not None]
            avg_score = sum(scores) / len(scores) if scores else 0
            text = f"🌙 **گزارش شبانه - {today_shamsi}**\n\n👤 {full_name}\n\n"
            text += f"⏱ زمان مطالعه: {format_time_hours_minutes(total_time)}\n"
            text += f"✅ تکمیل‌شده: {done}/{len(activities)}\n"
            if scores:
                text += f"📊 میانگین نمره: {avg_score:.1f}%\n"
            text += "\n📋 **فعالیت‌ها:**\n"
            for a in activities[-5:]:
                status = "✅" if a.get("status") == "done" else "⬜"
                text += f"{status} {a['subject']}"
                if a.get('topic'):
                    text += f" - {a['topic']}"
                text += f" ({a.get('actual_duration', a.get('planned_duration', 0))} دقیقه)"
                if a.get('score') is not None:
                    text += f" - {a['score']:.0f}%"
                text += "\n"
            advice = get_active_advice(user_id)
            if advice:
                text += "\n💡 **توصیه فردا:**\n"
                for a in advice[:2]:
                    text += f"• {a['advice']}\n"
            text += "\n🔜 فردا منتظرت هستم! 🌟"
            await context.bot.send_message(telegram_id, text, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"خطا در ارسال گزارش به {telegram_id}: {e}")

# ==================== تابع اصلی ====================

def main() -> None:
    init_db_pool()
    create_tables()
    
    application = Application.builder() \
        .token(TOKEN) \
        .connect_timeout(60.0) \
        .read_timeout(60.0) \
        .write_timeout(60.0) \
        .pool_timeout(60.0) \
        .build()
    
    # هندلرهای دستورات
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("advice", advice_command))
    application.add_handler(CommandHandler("listadvice", list_advice_command))
    application.add_handler(CommandHandler("removeadvice", remove_advice_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("aistats", ai_stats_command))
    application.add_handler(CommandHandler("testai", test_ai_command))
    
    # هندلرهای منو
    application.add_handler(MessageHandler(
        filters.Regex("^(📝 برنامه امروز|📊 گزارش|📅 تقویم|💬 چت با AI|💰 خرید اشتراک|👤 پروفایل)$"),
        handle_main_menu
    ))
    
    # هندلرهای برنامه
    application.add_handler(MessageHandler(
        filters.Regex("^(🧠 ساخت با AI|✏️ ساخت دستی|🔙 برگشت به حالت قبل|✅ تایید تغییرات|❌ لغو تغییرات)$"),
        handle_plan_actions
    ))
    
    application.add_handler(MessageHandler(
        filters.Regex("^(✅ تایید برنامه|✅ اتمام برنامه|✏️ ویرایش برنامه|✏️ ویرایش دستی|💬 ویرایش با AI|➕ اضافه کردن|🔄 بازنشانی|🔙 بازگشت|⏱ تایمر|⏹ توقف|⏱ ادامه|✅ تکمیل|🗑 حذف پارت)$"),
        handle_plan_actions
    ))
    
    # هندلر کلیک روی پارت‌ها
    application.add_handler(MessageHandler(filters.Regex(r".*\[.*\].*"), handle_part_click))
    
    # هندلر تقویم
    application.add_handler(MessageHandler(filters.Regex(r"^📅 \d{4}/\d{2}/\d{2}$"), handle_calendar_date))
    
    # هندلر عکس (رسید)
    application.add_handler(MessageHandler(filters.PHOTO, handle_receipt))
    
    # هندلر پیام‌های آزاد
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_other))
    
    # هندلر callback های اینلاین
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    # Job Queue برای گزارش شبانه
    job_queue = application.job_queue
    if job_queue:
        now = get_iran_now()
        target = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        seconds_until = (target - now).total_seconds()
        job_queue.run_repeating(nightly_report, interval=86400, first=seconds_until)
        logger.info("✅ تسک‌های زمان‌بندی‌شده با زمان ایران تنظیم شدند")
    
    logger.info("🤖 ربات مطالعه هوشمند با سیستم سطوح و چت AI شروع به کار کرد!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
