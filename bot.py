# ==================== bot.py - بخش 1/4 ====================
# ربات مطالعه هوشمند - ایمپورت‌ها، تنظیمات، دیتابیس، KOK، RAG، Role،
# NavStack، Entity Detection، AI، کارت‌ها، سرویس‌های مرکزی (Reschedule/Cleanup)

import asyncio
import json
import logging
import os
import random
import re
import time
import io
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple, Union

import psycopg2
from psycopg2 import pool
import jdatetime
import pytz
import httpx
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

from openai import AsyncOpenAI

# ==================== بارگذاری تنظیمات ====================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_BASE_URL = os.getenv("AI_BASE_URL")
AI_MODEL = os.getenv("AI_MODEL")
AI_MODEL_LIGHT = os.getenv("AI_MODEL_LIGHT", AI_MODEL)
AI_MODEL_ADVANCED = os.getenv("AI_MODEL_ADVANCED", AI_MODEL)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
RAG_ENABLED = os.getenv("RAG_ENABLED", "true").lower() == "true"
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "study_bot_db"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
    "port": os.getenv("DB_PORT", "5432")
}

ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
IRAN_TZ = pytz.timezone('Asia/Tehran')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

client = AsyncOpenAI(
    base_url=AI_BASE_URL,
    api_key=AI_API_KEY,
    timeout=httpx.Timeout(60.0, connect=15.0)
)

# ==================== ثابت‌های محافظت‌شده ====================
PROTECTED_CONTENT_TYPES = ('reminder', 'class')
PROTECTED_ORIGINS = ('routine', 'reminder', 'card')
DAY_END_MIN = 23 * 60 + 59
OVERDUE_GRACE_HOURS = 2

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


# ==================== توابع کمکی زمان/متن ====================
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
    except Exception:
        return date_str


def format_time_hours_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} دقیقه"
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f"{hours} ساعت"
    return f"{hours} ساعت و {mins} دقیقه"


def convert_persian_to_int(text: str) -> Optional[int]:
    persian_to_english = {
        '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4',
        '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9'
    }
    result = text
    for persian, english in persian_to_english.items():
        result = result.replace(persian, english)
    try:
        return int(result)
    except Exception:
        return None


def normalize_time_str(time_str: Optional[str]) -> Optional[str]:
    if not time_str:
        return time_str
    persian_to_english = {
        '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4',
        '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
        '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4',
        '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9'
    }
    result = str(time_str)
    for fa, en in persian_to_english.items():
        result = result.replace(fa, en)
    result = result.replace('：', ':').strip()
    return result


def time_to_minutes(time_str: str) -> int:
    try:
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return 0


def minutes_to_time(minutes: int) -> str:
    if minutes < 0:
        minutes = 0
    if minutes > 23 * 60 + 59:
        minutes = 23 * 60 + 59
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


def get_weekday_fa(dt) -> str:
    weekdays = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]
    return weekdays[dt.weekday()]


def get_shamsi_month_name(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        jdate = jdatetime.datetime.fromgregorian(datetime=dt)
        months = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                  "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
        return months[jdate.month - 1]
    except Exception:
        return ""


# ==================== تشخیص پارت محافظت‌شده ====================
def is_reminder_part(part: Dict) -> bool:
    if not part:
        return False
    return (part.get("content_type") == "reminder"
            or (part.get("metadata") or {}).get("is_reminder")
            or part.get("activity") == "reminder")


def is_protected_part(part: Dict) -> bool:
    """پارت محافظت‌شده — هرگز جابه‌جا/حذف/فشرده/سرریز نشود."""
    if not part:
        return False
    if part.get("completed"):
        return True
    if part.get("is_fixed_time"):
        # اگر fixed است ولی منقضی شده (قبلاً شناور شده) — باز هم محافظت‌شده تلقی نمی‌شود
        if (part.get("metadata") or {}).get("converted_from_fixed"):
            return False
        return True
    ct = (part.get("content_type") or "").lower()
    if ct in PROTECTED_CONTENT_TYPES:
        return True
    origin = (part.get("origin") or "").lower()
    if origin in PROTECTED_ORIGINS:
        return True
    md = part.get("metadata") or {}
    if md.get("is_reminder") or md.get("routine_card_id"):
        return True
    if part.get("matched_card_id") and ct in ("class", "routine", "reminder"):
        return True
    return False


def is_floating_part(part: Dict) -> bool:
    """پارت شناور شده از حالت ثابت منقضی."""
    return bool((part.get("metadata") or {}).get("converted_from_fixed"))


def reset_user_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """پاک کردن همه stateهای موقت کاربر — با حفظ nav_stack نیمه‌کاره"""
    keys_to_clear = [
        "onboarding_step", "onboarding_data", "awaiting_custom",
        "rag_mode", "role_mode", "awaiting_activity_type",
        "feedback_part_id", "feedback_questions", "feedback_index",
        "feedback_answers", "feedback_activity_type",
        "edit_mode", "edit_selected_part", "edit_drafts",
        "build_mode", "build_step", "build_data",
        "add_activity_step", "pending_delete", "pending_card_delete",
        "pending_change", "pending_conflict", "pending_clarify",
        "pending_split", "pending_reschedule", "pending_reminder_plan",
        "pending_card_suggestion", "pending_class_card",
        "pending_routine_card", "pending_routine_cards",
        "pending_project_card", "pending_cards", "pending_cards_raw",
        "pending_card_text", "awaiting_card_text",
        "reminder_mode", "routin_mode", "project_mode",
        "project_provider", "challenge_mode", "card_edit_mode",
        "calendar_page", "selected_date", "selected_shamsi",
        "current_plan", "active_part", "mode", "ai_context_summary",
        "card_edit_target", "is_admin", "last_kok_warning", "awaiting_receipt",
        "back_to_date", "clarify_followup", "pending_routine_clarify",
        "pending_early_complete", "pending_card_update", "pending_project_source",
        "pending_reminder_data", "pending_card_update",
        "rescheduling", "floating_notified_date", "processing",
        "cards_page", "admin_cards_mode", "admin_cards_page",
        "schedule_choice_pending", "pending_calendar_update",
        "pending_overflow_choice", "pending_compaction",
        "clarify_asked_for_text",
    ]
    for k in keys_to_clear:
        context.user_data.pop(k, None)


# ==================== NavStack ====================
def nav_push(context: ContextTypes.DEFAULT_TYPE, page: str, **kwargs) -> None:
    """push یک صفحه به استک ناوبری."""
    stack = context.user_data.setdefault("nav_stack", [])
    entry = {"page": page}
    entry.update(kwargs)
    stack.append(entry)
    if len(stack) > 30:
        stack.pop(0)


def nav_pop(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict]:
    stack = context.user_data.get("nav_stack", [])
    if not stack:
        return None
    return stack.pop()


def nav_reset(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["nav_stack"] = []


def nav_peek(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict]:
    stack = context.user_data.get("nav_stack", [])
    return stack[-1] if stack else None


# ==================== Entity Detection ====================
def detect_entity(text: str, context: ContextTypes.DEFAULT_TYPE,
                   user_id: Optional[int] = None) -> Tuple[Optional[str], Optional[int]]:
    """تشخیص (type, id) از متن — اولویت: پارت > کارت."""
    if not text:
        return None, None
    m = re.search(r'\[(\d+)\]', text)
    if not m:
        return None, None
    eid = int(m.group(1))

    plan = context.user_data.get("current_plan") or {}
    for p in plan.get("parts", []) or []:
        if p.get("part_id") == eid:
            return "part", eid

    if context.user_data.get("cards_page") is not None or context.user_data.get("admin_cards_mode"):
        return "card", eid

    return None, eid


# ==================== KOK ====================
_kok_costs_cache = {}
_kok_costs_cache_time = 0
KOK_CACHE_TTL = 60

DEFAULT_KOK_COSTS = {
    "ai_call_base": 2,
    "detect_topics": 5,
    "manual_plan_build": 7,
    "ai_plan_build": 5,
    "project_setup": 10,
    "reschedule": 6,
    "routine_setup": 4,
    "class_setup": 3,
    "reminder_setup": 2,
    "ai_chat": 2,
    "subject_detect": 1,
    "report_analysis": 3,
    "admin_advice": 2,
    "admin_card": 3,
    "multi_add": 5,
    "clarify": 2,
    "card_edit": 2,
}


def get_kok_cost(action_key: str) -> int:
    global _kok_costs_cache, _kok_costs_cache_time
    now = time.time()
    if now - _kok_costs_cache_time > KOK_CACHE_TTL:
        try:
            rows = execute_query(
                "SELECT action_key, cost FROM kok_costs WHERE is_active = TRUE",
                fetchall=True
            )
            _kok_costs_cache = {r[0]: r[1] for r in (rows or [])}
        except Exception:
            _kok_costs_cache = {}
        _kok_costs_cache_time = now
    if action_key in _kok_costs_cache:
        return _kok_costs_cache[action_key]
    return DEFAULT_KOK_COSTS.get(action_key, 2)


def get_user_kok(user_id: int) -> int:
    if not user_id:
        return 0
    result = execute_query(
        "SELECT kok_balance FROM users WHERE id = %s",
        (user_id,), fetch=True
    )
    return result[0] if result and result[0] is not None else 0


def has_enough_kok(user_id: int, action_key: str) -> Tuple[bool, int, int]:
    cost = get_kok_cost(action_key)
    balance = get_user_kok(user_id)
    return balance >= cost, balance, cost


def consume_kok(user_id: int, action_key: str,
                reference_id: str = None, description: str = None) -> bool:
    cost = get_kok_cost(action_key)
    if cost <= 0:
        return True

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT kok_balance FROM users WHERE id = %s FOR UPDATE",
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return False

        balance = row[0] or 0
        if balance < cost:
            conn.rollback()
            return False

        new_balance = balance - cost
        cursor.execute(
            """UPDATE users SET kok_balance = %s,
               total_kok_used = COALESCE(total_kok_used, 0) + %s
               WHERE id = %s""",
            (new_balance, cost, user_id)
        )
        cursor.execute(
            """INSERT INTO kok_transactions
               (user_id, amount, balance_after, reason, reference_id, description)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, -cost, new_balance, action_key, reference_id, description)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"❌ خطا در consume_kok: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_connection(conn)


def add_kok(user_id: int, amount: int, reason: str,
            reference_id: str = None, description: str = None) -> bool:
    if amount <= 0 or not user_id:
        return False

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT kok_balance FROM users WHERE id = %s FOR UPDATE",
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return False

        new_balance = (row[0] or 0) + amount
        cursor.execute(
            """UPDATE users SET kok_balance = %s,
               total_kok_purchased = COALESCE(total_kok_purchased, 0) + %s
               WHERE id = %s""",
            (new_balance, amount, user_id)
        )
        cursor.execute(
            """INSERT INTO kok_transactions
               (user_id, amount, balance_after, reason, reference_id, description)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, amount, new_balance, reason, reference_id, description)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"❌ خطا در add_kok: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            return_connection(conn)


KOK_ACTION_LABELS = {
    'ai_plan_build': 'ساخت برنامه با AI',
    'detect_topics': 'تشخیص مباحث',
    'manual_plan_build': 'چیدن برنامه دستی',
    'project_setup': 'تنظیم پروژه',
    'reschedule': 'بازچینی برنامه',
    'routine_setup': 'ساخت روتین',
    'class_setup': 'ساخت کلاس',
    'reminder_setup': 'ساخت یادآور',
    'ai_chat': 'چت با AI',
    'ai_call_base': 'فراخوانی AI',
    'subject_detect': 'تشخیص درس',
    'report_analysis': 'تحلیل گزارش',
    'admin_advice': 'توصیه ادمین',
    'admin_card': 'ساخت کارت ادمین',
    'multi_add': 'افزودن چند فعالیت',
    'clarify': 'سوال شفاف‌سازی',
    'card_edit': 'ویرایش کارت',
}


async def check_and_consume_kok(update: Update, user_id: int, action_key: str,
                                 reference_id: str = None,
                                 silent_admin: bool = True) -> bool:
    if not user_id:
        return False
    tg_id = update.effective_user.id if update and update.effective_user else 0
    if silent_admin and tg_id in ADMIN_IDS:
        return True

    enough, balance, cost = has_enough_kok(user_id, action_key)
    if not enough:
        action_label = KOK_ACTION_LABELS.get(action_key, action_key)
        msg = (
            f"⛔️ **موجودی KOK کافی نیست**\n\n"
            f"💎 موجودی: {balance} KOK\n"
            f"💳 نیاز: {cost} KOK برای «{action_label}»\n\n"
            f"برای خرید KOK از دکمه 💰 استفاده کن."
        )
        try:
            await update.message.reply_text(
                msg, parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard()
            )
        except Exception:
            pass
        return False

    return consume_kok(user_id, action_key, reference_id)


def estimate_kok_for_chat(interpretation: Dict) -> str:
    action = interpretation.get("action", "chat")
    return {
        "build_new": "ai_plan_build", "add": "ai_call_base",
        "delete": "ai_call_base", "update": "ai_call_base",
        "clear": "ai_call_base", "reminder": "reminder_setup",
        "suggest_card": "ai_call_base", "add_class": "class_setup",
        "multi_add": "multi_add", "clarify": "clarify", "chat": "ai_chat",
    }.get(action, "ai_chat")


def format_kok_balance(user_id: int) -> str:
    balance = get_user_kok(user_id)
    if balance == 0:
        return "💎 ۰ KOK"
    if balance < 20:
        return f"🔴 {balance} KOK"
    if balance < 50:
        return f"🟡 {balance} KOK"
    return f"🟢 {balance} KOK"


# ==================== دسترسی کارت‌ها ====================
def _access_condition(column: str = "created_by") -> Tuple[str, list]:
    if not ADMIN_IDS:
        return f"({column} = %s OR {column} IS NULL)", []
    placeholders = ",".join(["%s"] * len(ADMIN_IDS))
    return f"({column} = %s OR {column} IN ({placeholders}) OR {column} IS NULL)", list(ADMIN_IDS)


def _admin_only_condition(column: str = "created_by") -> Tuple[str, list]:
    if not ADMIN_IDS:
        return f"({column} IS NULL)", []
    placeholders = ",".join(["%s"] * len(ADMIN_IDS))
    return f"({column} IN ({placeholders}) OR {column} IS NULL)", list(ADMIN_IDS)


# ==================== RAG ====================
async def embed_text(text: str) -> Optional[List[float]]:
    try:
        text = text.strip()[:8000]
        if not text:
            return None
        response = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"❌ خطا در embedding: {e}")
        return None


def save_rag_document(content: str, embedding: List[float], source: str,
                      category: str = "general", created_by: int = None) -> Optional[int]:
    try:
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        result = execute_query(
            """INSERT INTO rag_documents (content, embedding, source, category, created_by)
               VALUES (%s, %s::vector, %s, %s, %s) RETURNING id""",
            (content, embedding_str, source, category, created_by),
            fetch=True
        )
        return result[0] if result else None
    except Exception as e:
        logger.error(f"❌ خطا در ذخیره RAG: {e}")
        return None


async def search_rag(query: str, user_id: int = None, top_k: int = None,
                     category: str = None,
                     admin_only: bool = False) -> List[Dict]:
    if not RAG_ENABLED:
        return []
    top_k = top_k or RAG_TOP_K
    try:
        query_embedding = await embed_text(query)
        if not query_embedding:
            return []
        embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

        conditions = ["embedding IS NOT NULL"]
        params = []

        if admin_only:
            cond, admin_params = _admin_only_condition("created_by")
            conditions.append(cond)
            params.extend(admin_params)
        elif user_id:
            cond, admin_params = _access_condition("created_by")
            conditions.append(cond)
            params.append(user_id)
            params.extend(admin_params)
        else:
            cond, admin_params = _admin_only_condition("created_by")
            conditions.append(cond)
            params.extend(admin_params)

        if category:
            conditions.append("category = %s")
            params.append(category)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT id, content, source, category,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM rag_documents WHERE {where}
            ORDER BY embedding <=> %s::vector LIMIT %s
        """
        final_params = [embedding_str] + params + [embedding_str, top_k]
        results = execute_query(sql, tuple(final_params), fetchall=True)
        return [{
            "id": r[0], "content": r[1], "source": r[2],
            "category": r[3], "similarity": float(r[4]) if r[4] else 0
        } for r in (results or []) if r[4] and float(r[4]) > 0.3]
    except Exception as e:
        logger.error(f"❌ خطا در جستجوی RAG: {e}")
        return []


def build_rag_context(rag_results: List[Dict]) -> str:
    if not rag_results:
        return ""
    context = "\n\n=== 📚 دانش‌نامه تخصصی ===\n"
    for i, r in enumerate(rag_results, 1):
        context += f"\n[{i}] (منبع: {r.get('source', 'نامشخص')}):\n{r.get('content', '')[:800]}\n"
    context += "\n=== پایان دانش‌نامه ===\n"
    return context


def list_rag_documents(limit: int = 20) -> List[Dict]:
    results = execute_query(
        """SELECT id, content, source, category, created_at
           FROM rag_documents ORDER BY created_at DESC LIMIT %s""",
        (limit,), fetchall=True
    )
    return [{
        "id": r[0], "content": r[1][:100] if r[1] else "",
        "source": r[2], "category": r[3], "created_at": r[4]
    } for r in (results or [])]


def delete_rag_document(doc_id: int) -> bool:
    try:
        execute_query("DELETE FROM rag_documents WHERE id = %s", (doc_id,))
        return True
    except Exception:
        return False


def delete_rag_by_source(source: str) -> int:
    try:
        return execute_query("DELETE FROM rag_documents WHERE source = %s", (source,))
    except Exception:
        return 0


# ==================== AI Roles ====================
def save_ai_role(role_text: str, created_by: int = None) -> Optional[int]:
    try:
        result = execute_query(
            """INSERT INTO ai_roles (role_text, is_active, created_by)
               VALUES (%s, FALSE, %s) RETURNING id""",
            (role_text, created_by), fetch=True
        )
        return result[0] if result else None
    except Exception as e:
        logger.error(f"❌ خطا در ذخیره role: {e}")
        return None


def get_active_role() -> Optional[str]:
    result = execute_query(
        "SELECT role_text FROM ai_roles WHERE is_active = TRUE LIMIT 1",
        fetch=True
    )
    return result[0] if result else None


def activate_role(role_id: int) -> bool:
    try:
        execute_query("UPDATE ai_roles SET is_active = FALSE")
        execute_query("UPDATE ai_roles SET is_active = TRUE WHERE id = %s", (role_id,))
        return True
    except Exception:
        return False


def list_ai_roles() -> List[Dict]:
    results = execute_query(
        "SELECT id, role_text, is_active, created_at FROM ai_roles ORDER BY created_at DESC",
        fetchall=True
    )
    return [{
        "id": r[0], "role_text": r[1], "is_active": r[2], "created_at": r[3]
    } for r in (results or [])]


def delete_ai_role(role_id: int) -> bool:
    try:
        execute_query("DELETE FROM ai_roles WHERE id = %s", (role_id,))
        return True
    except Exception:
        return False


def build_role_prefix(user_id: int = None) -> str:
    role_text = None
    if user_id:
        try:
            user_row = execute_query(
                "SELECT role_preference FROM users WHERE id = %s",
                (user_id,), fetch=True
            )
            pref = user_row[0] if user_row else None
            if pref:
                role_text = pref
        except Exception:
            pass

    if not role_text:
        role_text = get_active_role()

    if not role_text:
        return ""
    return f"\n\n=== 🎭 شخصیت AI ===\n{role_text}\n\n⚠️ تمام پاسخ‌ها باید با این شخصیت سازگار باشه.\n"


# ==================== کارت‌ها ====================
def fetch_cards(user_id: int = None, mode: str = "accessible",
                include_inactive: bool = False,
                admin_only: bool = False) -> List[Dict]:
    """
    mode: 'accessible' (پیش‌فرض) | 'own' | 'admin_only'
    include_inactive: برای لیست ادمین با آرشیو
    """
    conditions = []
    params = []

    if mode == "own":
        if not user_id:
            return []
        conditions.append("created_by = %s")
        params.append(user_id)
    elif mode == "admin_only" or admin_only:
        cond, admin_params = _admin_only_condition("created_by")
        conditions.append(cond)
        params.extend(admin_params)
    else:
        if user_id:
            cond, admin_params = _access_condition("created_by")
            conditions.append(cond)
            params.append(user_id)
            params.extend(admin_params)
        else:
            cond, admin_params = _admin_only_condition("created_by")
            conditions.append(cond)
            params.extend(admin_params)

    if not include_inactive:
        conditions.append("is_active = TRUE")

    where = " AND ".join(conditions) if conditions else "TRUE"
    order = "priority DESC, created_at DESC"
    if include_inactive:
        order = "is_active DESC, priority DESC, created_at DESC"

    query = f"""
        SELECT id, card_type, content_type, data, applies_to, priority, created_by, is_active, created_at
        FROM cards WHERE {where} ORDER BY {order}
    """
    results = execute_query(query, tuple(params), fetchall=True)
    return [{
        "id": r[0], "card_type": r[1], "content_type": r[2],
        "data": r[3] or {}, "applies_to": r[4] or {},
        "priority": r[5] or 5, "created_by": r[6],
        "is_active": r[7], "created_at": r[8]
    } for r in (results or [])]


def get_user_cards_direct(user_id: int) -> List[Dict]:
    return fetch_cards(user_id=user_id, mode="own", include_inactive=False)


def get_active_cards(user_id: int = None) -> List[Dict]:
    return fetch_cards(user_id=user_id, mode="accessible", include_inactive=False)


async def get_relevant_admin_cards(user_id: int, query: str, top_k: int = 5) -> List[Dict]:
    if not ADMIN_IDS or not query:
        return []
    try:
        emb = await embed_text(query)
        if not emb:
            return []
        embedding_str = "[" + ",".join(str(x) for x in emb) + "]"
        placeholders = ",".join(["%s"] * len(ADMIN_IDS))
        results = execute_query(
            f"""
            SELECT r.source, 1 - (r.embedding <=> %s::vector) AS similarity
            FROM rag_documents r
            WHERE r.embedding IS NOT NULL
              AND r.source LIKE 'card_%%'
              AND r.created_by IN ({placeholders})
            ORDER BY r.embedding <=> %s::vector LIMIT %s
            """,
            tuple([embedding_str] + list(ADMIN_IDS) + [embedding_str, top_k * 3]),
            fetchall=True
        )
        if not results:
            return []
        card_ids = []
        seen = set()
        for r in results:
            try:
                cid = int(r[0].replace("card_", ""))
                if cid not in seen:
                    seen.add(cid)
                    card_ids.append(cid)
                    if len(card_ids) >= top_k:
                        break
            except Exception:
                continue
        if not card_ids:
            return []
        id_placeholders = ",".join(["%s"] * len(card_ids))
        cards_raw = execute_query(
            f"""SELECT id, card_type, content_type, data, applies_to, priority, created_by
                FROM cards WHERE id IN ({id_placeholders}) AND is_active = TRUE""",
            tuple(card_ids), fetchall=True
        )
        if not cards_raw:
            return []
        by_id = {c[0]: c for c in cards_raw}
        result = []
        for cid in card_ids:
            if cid in by_id:
                c = by_id[cid]
                result.append({
                    "id": c[0], "card_type": c[1], "content_type": c[2],
                    "data": c[3] or {}, "applies_to": c[4] or {},
                    "priority": c[5] or 5, "created_by": c[6]
                })
        return result
    except Exception as e:
        logger.error(f"❌ خطا در get_relevant_admin_cards: {e}")
        return []


def get_active_project_card(user_id: int) -> Optional[Dict]:
    row = execute_query(
        """SELECT id, project_type, title, data FROM user_projects
           WHERE user_id = %s AND is_active = TRUE
           ORDER BY created_at DESC LIMIT 1""",
        (user_id,), fetch=True
    )
    if not row:
        return None
    project_id, ptype, title, data = row
    data = data or {}
    source_card_id = None
    try:
        source_card_id = int(data.get("source_card_id"))
    except Exception:
        source_card_id = None
    if source_card_id:
        card_row = execute_query(
            """SELECT id, card_type, content_type, data, priority, created_by
               FROM cards WHERE id = %s AND is_active = TRUE""",
            (source_card_id,), fetch=True
        )
        if card_row:
            return {
                "id": card_row[0], "card_type": card_row[1],
                "content_type": card_row[2],
                "data": card_row[3] or {}, "priority": card_row[4] or 5,
                "created_by": card_row[5]
            }
    return {
        "id": None, "card_type": ptype, "content_type": ptype,
        "data": {**data, "title": title}, "priority": 8, "created_by": None
    }


def save_card(card_type: str, data: Dict, applies_to: Dict = None,
              priority: int = 5, raw_text: str = None, created_by: int = None,
              content_type: str = None) -> Optional[int]:
    query = """
    INSERT INTO cards (card_type, content_type, data, applies_to, priority, raw_text, created_by)
    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
    """
    result = execute_query(query, (
        card_type, content_type, json.dumps(data), json.dumps(applies_to or {}),
        priority, raw_text, created_by
    ), fetch=True)
    return result[0] if result else None


def update_card_data(card_id: int, new_data: Dict) -> bool:
    try:
        execute_query(
            "UPDATE cards SET data = %s WHERE id = %s",
            (json.dumps(new_data), card_id)
        )
        return True
    except Exception as e:
        logger.error(f"خطا در update_card_data: {e}")
        return False


def deactivate_card(card_id: int) -> bool:
    try:
        execute_query("UPDATE cards SET is_active = FALSE WHERE id = %s", (card_id,))
        return True
    except Exception:
        return False


def hard_delete_card(card_id: int) -> bool:
    """حذف کامل کارت (طبق بخش ۱۴) + پاک‌سازی RAG + null کردن matched_card_id"""
    try:
        execute_query("UPDATE study_parts SET matched_card_id = NULL WHERE matched_card_id = %s",
                      (card_id,))
        execute_query("DELETE FROM card_tracking WHERE card_id = %s", (card_id,))
        execute_query("DELETE FROM cards WHERE id = %s", (card_id,))
        delete_rag_by_source(f"card_{card_id}")
        logger.info(f"hard_delete_card: card_id={card_id}")
        return True
    except Exception as e:
        logger.error(f"خطا در hard_delete_card {card_id}: {e}")
        return False


async def index_card_to_rag(card_id: int) -> Optional[int]:
    result = execute_query(
        "SELECT card_type, content_type, data, created_by FROM cards WHERE id = %s",
        (card_id,), fetch=True
    )
    if not result:
        return None
    card_type, content_type, data, created_by = result
    data = data or {}

    parts = [f"کارت: {card_type}"]
    if card_type == "microtopic":
        parts.append(f"عنوان: {data.get('title', '')}")
        parts.append(f"درس: {data.get('subject', '')}")
        parts.append(f"مبحث: {data.get('topic', '')}")
        parts.append(f"زیرمبحث: {data.get('subtopic', '')}")
        parts.append(f"پایه: {data.get('grade', '')}")
        kw = data.get('keywords', [])
        if kw:
            parts.append(f"کلمات کلیدی: {', '.join(kw)}")
    elif card_type == "syllabus":
        parts.append(f"عنوان: {data.get('title', '')}")
        parts.append(f"درس: {data.get('subject', '')}")
        parts.append(f"پایه: {data.get('grade', '')}")
        syl = data.get('syllabus', [])
        if syl:
            parts.append(f"مباحث: {', '.join(syl)}")
    elif card_type == "resource":
        parts.append(f"عنوان: {data.get('title', '')}")
        parts.append(f"درس: {data.get('subject', '')}")
        parts.append(f"ارائه‌دهنده: {data.get('provider', '')}")
        chapters = data.get('chapters', [])
        if chapters:
            ch_titles = [ch.get('title', '') for ch in chapters if isinstance(ch, dict)]
            parts.append(f"فصل‌ها: {', '.join(ch_titles)}")
    elif card_type == "exam":
        parts.append(f"عنوان: {data.get('title', '')}")
        parts.append(f"ارائه‌دهنده: {data.get('provider', '')}")
        for s in data.get('subjects', []):
            if isinstance(s, dict):
                parts.append(f"درس {s.get('name', '')}: {', '.join(s.get('syllabus', []))}")
    elif card_type == "routine":
        parts.append(f"عنوان: {data.get('title', '')}")
        parts.append(f"نوع: {data.get('routine_type', '')}")
        parts.append(f"تکرار: {data.get('recurrence', '')}")
        parts.append(f"ساعت: {data.get('start_time', '')}-{data.get('end_time', '')}")
    elif card_type == "project":
        parts.append(f"عنوان: {data.get('title', '')}")
        parts.append(f"ارائه‌دهنده: {data.get('provider', '')}")
        subs = data.get('subjects', [])
        if isinstance(subs, list):
            parts.append(f"درس‌ها: {', '.join(subs)}")
    elif card_type == "reminder":
        parts.append(f"عنوان: {data.get('title', '')}")
        parts.append(f"تاریخ: {data.get('date', '')}")
        parts.append(f"ساعت: {data.get('time', '')}")
        parts.append(f"پیام: {data.get('message', '')}")
    else:
        for k, v in data.items():
            if isinstance(v, str) and v:
                parts.append(f"{k}: {v}")

    content = " | ".join([p for p in parts if p])
    if len(content.strip()) < 20:
        return None
    delete_rag_by_source(f"card_{card_id}")
    emb = await embed_text(content)
    if not emb:
        return None
    return save_rag_document(
        content=content, embedding=emb,
        source=f"card_{card_id}", category=card_type, created_by=created_by
    )


# ==================== AI ====================
async def call_ai(prompt: str, max_tokens: int = 1500, temperature: float = 0.3,
                  model_type: str = "light") -> Optional[str]:
    model = AI_MODEL_ADVANCED if model_type == "advanced" else AI_MODEL_LIGHT
    for attempt in range(3):
        try:
            completion = await client.chat.completions.create(
                model=model,
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


def _strip_json_fence(text: str) -> str:
    """حذف ```json ... ``` از پاسخ AI"""
    if not text:
        return text
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)
    return text.strip()


def safe_json_loads(text: str, expect: type = dict) -> Optional[Union[dict, list]]:
    """پارس امن JSON با strip fence و validate type."""
    if not text:
        return None
    try:
        candidate = _strip_json_fence(text)
        # برای list/dict پیدا کردن اولین و آخرین براکت
        if expect is list:
            m = re.search(r'\[.*\]', candidate, re.DOTALL)
            if not m:
                return None
            candidate = m.group()
        else:
            m = re.search(r'\{.*\}', candidate, re.DOTALL)
            if not m:
                return None
            candidate = m.group()
        data = json.loads(candidate)
        if not isinstance(data, expect):
            return None
        return data
    except Exception:
        return None


# ==================== توابع کاربری پایه ====================
def get_user_id_by_telegram(telegram_id: int) -> Optional[int]:
    result = execute_query(
        "SELECT id FROM users WHERE telegram_id = %s",
        (str(telegram_id),), fetch=True
    )
    return result[0] if result else None


def get_user_data(telegram_id: int) -> Optional[Dict]:
    result = execute_query(
        """SELECT id, telegram_id, username, full_name, goal, grade, field,
                  is_onboarded, weak_subjects, strong_subjects,
                  study_hours_per_week, peak_time, learning_style, focus_duration,
                  break_duration, created_at, providers, onboarding_profile,
                  role_preference, kok_balance, total_kok_used, total_kok_purchased
           FROM users WHERE telegram_id = %s""",
        (str(telegram_id),), fetch=True
    )
    if not result:
        return None
    return {
        "id": result[0], "telegram_id": result[1], "username": result[2],
        "full_name": result[3], "goal": result[4], "grade": result[5],
        "field": result[6], "is_onboarded": result[7],
        "weak_subjects": result[8] or [], "strong_subjects": result[9] or [],
        "study_hours_per_week": result[10], "peak_time": result[11],
        "learning_style": result[12], "focus_duration": result[13] or 45,
        "break_duration": result[14] or 10,
        "created_at": result[15], "providers": result[16] or [],
        "onboarding_profile": result[17] or {},
        "role_preference": result[18],
        "kok_balance": result[19] or 0,
        "total_kok_used": result[20] or 0,
        "total_kok_purchased": result[21] or 0,
    }


def get_user_subscription(user_id: int) -> Optional[Dict]:
    result = execute_query(
        """SELECT us.id, us.plan_id, sp.name, us.start_date, us.end_date,
                  us.credits_hourly, us.credits_daily, us.credits_weekly,
                  sp.model_access, us.last_hourly_reset, us.last_daily_reset,
                  us.last_weekly_reset
           FROM user_subscriptions us
           JOIN subscription_plans sp ON sp.id = us.plan_id
           WHERE us.user_id = %s AND us.is_active = TRUE
             AND us.end_date >= CURRENT_DATE
           ORDER BY us.created_at DESC LIMIT 1""",
        (user_id,), fetch=True
    )
    if not result:
        return None
    return {
        "id": result[0], "plan_id": result[1], "plan_name": result[2],
        "start_date": result[3], "end_date": result[4],
        "credits_hourly": result[5], "credits_daily": result[6],
        "credits_weekly": result[7], "model_access": result[8],
        "last_hourly_reset": result[9], "last_daily_reset": result[10],
        "last_weekly_reset": result[11]
    }


def get_user_model_type(user_id: int) -> str:
    sub = get_user_subscription(user_id)
    if sub and sub.get("model_access") == "advanced":
        return "advanced"
    return "light"


def get_available_plans() -> List[Dict]:
    results = execute_query(
        """SELECT id, name, price, kok_amount, model_access, duration_days
           FROM subscription_plans WHERE is_active = TRUE ORDER BY price ASC""",
        fetchall=True
    )
    return [{
        "id": r[0], "name": r[1], "price": r[2],
        "kok_amount": r[3] or 0, "model_access": r[4], "duration_days": r[5]
    } for r in (results or [])]


def activate_subscription(user_id: int, plan_id: int) -> bool:
    try:
        plan = execute_query(
            """SELECT name, duration_days, kok_amount, model_access
               FROM subscription_plans WHERE id = %s""",
            (plan_id,), fetch=True
        )
        if not plan:
            return False
        name, duration_days, kok_amount, model_access = plan
        execute_query(
            "UPDATE user_subscriptions SET is_active = FALSE WHERE user_id = %s",
            (user_id,)
        )
        start = get_iran_now().date()
        end = start + timedelta(days=duration_days or 30)
        execute_query(
            """INSERT INTO user_subscriptions
               (user_id, plan_id, start_date, end_date,
                credits_hourly, credits_daily, credits_weekly,
                last_hourly_reset, last_daily_reset, last_weekly_reset, is_active)
               VALUES (%s, %s, %s, %s, 0, 0, 0, %s, %s, %s, TRUE)""",
            (user_id, plan_id, start, end, get_iran_now(), start, start)
        )
        if kok_amount and kok_amount > 0:
            add_kok(user_id, kok_amount, "subscription",
                    reference_id=str(plan_id), description=f"خرید پلن {name}")
        return True
    except Exception as e:
        logger.error(f"خطا در فعال‌سازی اشتراک: {e}")
        return False


def save_user(user_data: Dict) -> Optional[int]:
    query = """
    INSERT INTO users (telegram_id, username, full_name, goal, grade, field,
                       exam_date, study_hours_per_week, peak_time, learning_style,
                       focus_duration, break_duration, weak_subjects, strong_subjects,
                       daily_schedule, providers, onboarding_profile, role_preference,
                       is_active, is_onboarded, kok_balance)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (telegram_id) DO UPDATE SET
        username = EXCLUDED.username, full_name = EXCLUDED.full_name,
        goal = EXCLUDED.goal, grade = EXCLUDED.grade, field = EXCLUDED.field,
        exam_date = EXCLUDED.exam_date, study_hours_per_week = EXCLUDED.study_hours_per_week,
        peak_time = EXCLUDED.peak_time, learning_style = EXCLUDED.learning_style,
        focus_duration = EXCLUDED.focus_duration, break_duration = EXCLUDED.break_duration,
        weak_subjects = EXCLUDED.weak_subjects, strong_subjects = EXCLUDED.strong_subjects,
        daily_schedule = EXCLUDED.daily_schedule, providers = EXCLUDED.providers,
        onboarding_profile = EXCLUDED.onboarding_profile,
        role_preference = EXCLUDED.role_preference,
        is_onboarded = EXCLUDED.is_onboarded,
        updated_at = CURRENT_TIMESTAMP
    RETURNING id
    """
    result = execute_query(query, (
        user_data["telegram_id"], user_data.get("username"), user_data.get("full_name"),
        user_data.get("goal"), user_data.get("grade"), user_data.get("field"),
        user_data.get("exam_date"), user_data.get("study_hours_per_week"),
        user_data.get("peak_time"), user_data.get("learning_style"),
        user_data.get("focus_duration", 45), user_data.get("break_duration", 10),
        json.dumps(user_data.get("weak_subjects", [])),
        json.dumps(user_data.get("strong_subjects", [])),
        json.dumps(user_data.get("daily_schedule", {})),
        json.dumps(user_data.get("providers", [])),
        json.dumps(user_data.get("onboarding_profile", {})),
        user_data.get("role_preference"),
        user_data.get("is_active", True), user_data.get("is_onboarded", False),
        user_data.get("kok_balance", 0)
    ), fetch=True)
    return result[0] if result else None


# ==================== Chat ====================
def save_chat_message(user_id: int, role: str, content: str) -> None:
    try:
        execute_query(
            "INSERT INTO chat_history (user_id, role, content) VALUES (%s, %s, %s)",
            (user_id, role, content)
        )
    except Exception as e:
        logger.error(f"خطا در ذخیره پیام چت: {e}")


def get_chat_history(user_id: int, limit: int = 10) -> List[Dict]:
    results = execute_query(
        """SELECT role, content FROM chat_history
           WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
        (user_id, limit * 2), fetchall=True
    )
    if not results:
        return []
    return [{"role": r[0], "content": r[1]} for r in reversed(results)]


def clear_chat_history(user_id: int) -> None:
    execute_query("DELETE FROM chat_history WHERE user_id = %s", (user_id,))


# ==================== تشخیص زمان ====================
def analyze_time_slots(text: str) -> List[str]:
    text = normalize_time_str(text) or ""
    text = text.strip()
    time_slots = []

    pattern1 = r'(\d{1,2}):(\d{2})\s*(?:[-–—]|تا|الی)\s*(\d{1,2}):(\d{2})'
    for match in re.findall(pattern1, text):
        sh, sm, eh, em = match
        if 0 <= int(sh) <= 23 and 0 <= int(eh) <= 23 and 0 <= int(sm) <= 59 and 0 <= int(em) <= 59:
            time_slots.append(f"{int(sh):02d}:{int(sm):02d}-{int(eh):02d}:{int(em):02d}")

    pattern2 = r'(\d{1,2}):(\d{2})_(\d{1,2}):(\d{2})'
    for match in re.findall(pattern2, text):
        sh, sm, eh, em = match
        if 0 <= int(sh) <= 23 and 0 <= int(eh) <= 23:
            time_slots.append(f"{int(sh):02d}:{int(sm):02d}-{int(eh):02d}:{int(em):02d}")

    pattern3 = r'(?<!\d)(\d{1,2})\s*(?:[-–—]|تا|الی)\s*(\d{1,2})(?!\d)'
    for match in re.findall(pattern3, text):
        s, e = match
        if 0 <= int(s) <= 23 and 0 <= int(e) <= 23:
            time_slots.append(f"{int(s):02d}:00-{int(e):02d}:00")

    pattern4 = r'(?<!\d)(\d{1,2})\.(\d)\s*[-–—]\s*(\d{1,2})\.(\d)(?!\d)'
    for match in re.findall(pattern4, text):
        sh, sm, eh, em = match
        sm_min = int(sm) * 6
        em_min = int(em) * 6
        if 0 <= int(sh) <= 23 and 0 <= int(eh) <= 23:
            time_slots.append(f"{int(sh):02d}:{sm_min:02d}-{int(eh):02d}:{em_min:02d}")

    time_slots = list(dict.fromkeys(time_slots))
    try:
        time_slots.sort(key=lambda x: (
            int(x.split('-')[0].split(':')[0]),
            int(x.split('-')[0].split(':')[1])
        ))
    except Exception:
        pass
    return time_slots


def total_slot_minutes(time_slots: List[str]) -> int:
    total = 0
    for slot in time_slots:
        try:
            start_str, end_str = slot.split("-")
            s = time_to_minutes(start_str)
            e = time_to_minutes(end_str)
            if e > s:
                total += (e - s)
        except Exception:
            continue
    return total


def get_largest_slot_minutes(time_slots: List[str]) -> int:
    largest = 0
    for slot in time_slots:
        try:
            s_str, e_str = slot.split("-")
            s = time_to_minutes(s_str)
            e = time_to_minutes(e_str)
            if e > s and (e - s) > largest:
                largest = e - s
        except Exception:
            continue
    return largest


# ==================== compute_effective_start (بخش ۲۰.۴) ====================
def compute_effective_start(time_slots: List[str], now_min: int,
                            source_date: str) -> Optional[int]:
    """
    محاسبه‌ی نقطه شروع مؤثر بر اساس بازه‌های آزاد و زمان فعلی.
    """
    if not time_slots:
        return max(now_min, 8 * 60) if source_date == get_today_date() else 8 * 60

    today_str = get_today_date()
    ranges = []
    for slot in time_slots:
        try:
            s_str, e_str = slot.split("-")
            s = time_to_minutes(s_str)
            e = time_to_minutes(e_str)
            if e > s:
                ranges.append((s, e))
        except Exception:
            continue
    if not ranges:
        return None

    ranges.sort()
    if source_date < today_str or source_date > today_str:
        return ranges[0][0]

    # today
    for (s, e) in ranges:
        if now_min < s:
            return s
        if s <= now_min < e:
            return max(s, now_min)
    return None


# ==================== سرویس مرکزی بازچینی (run_reschedule) ====================
async def run_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          user_id: int, target_date: str,
                          trigger: str = "manual",
                          ask_calendar: bool = False,
                          show_final: bool = True) -> bool:
    """
    مسیر مرکزی بازچینی.
    trigger: 'manual' | 'complete' | 'apply_change' | 'rebuild_week'
    ask_calendar: پرسش به‌روزرسانی روزهای بعد.
    KOK فقط در trigger='manual' consume می‌شود.
    """
    if context.user_data.get("rescheduling"):
        try:
            await update.message.reply_text("⏳ در حال بازچینی... لطفاً صبر کن.")
        except Exception:
            pass
        return False

    context.user_data["rescheduling"] = True
    try:
        if trigger == "manual":
            if not await check_and_consume_kok(update, user_id, "reschedule"):
                context.user_data.pop("rescheduling", None)
                return False

        plan = get_plan_by_date(user_id, target_date)
        if not plan or not plan.get("parts"):
            context.user_data.pop("rescheduling", None)
            return False

        before_parts = plan["parts"]
        before_count = len(before_parts)

        new_parts = reschedule_parts(
            before_parts,
            base_start_time=get_iran_now(),
            session_id=plan.get("session_id"),
            user_id=user_id,
            source_date=target_date
        )

        after_plan = get_plan_by_date(user_id, target_date)
        if after_plan:
            context.user_data["current_plan"] = after_plan
            new_parts = after_plan["parts"]

        overflow_count = max(0, before_count - len(new_parts))
        if overflow_count == 0 and after_plan:
            overflow_count = sum(
                1 for p in new_parts
                if (p.get("metadata") or {}).get("is_overflow")
            )

        logger.info(
            f"reschedule: user={user_id} date={target_date} trigger={trigger} "
            f"parts_before={before_count} parts_after={len(new_parts) if new_parts else 0} "
            f"overflow={overflow_count}"
        )

        if ask_calendar and overflow_count > 0:
            context.user_data["pending_calendar_update"] = {
                "user_id": user_id,
                "target_date": target_date,
                "overflow_count": overflow_count,
                "trigger": trigger,
            }
            try:
                await update.message.reply_text(
                    f"🔄 **بازچینی انجام شد.**\n\n"
                    f"📊 {len(new_parts)} پارت در برنامه، "
                    f"{overflow_count} پارت سرریز شد (کمبود وقت).\n\n"
                    f"تقویم روزهای بعد هم به‌روزرسانی شود؟",
                    reply_markup=ReplyKeyboardMarkup(
                        [["✅ بله، روزهای بعد را هم به‌روزرسانی کن"],
                         ["❌ نه، فقط امروز"]],
                        resize_keyboard=True, one_time_keyboard=True
                    ),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            context.user_data.pop("rescheduling", None)
            return True

        if show_final and new_parts:
            try:
                await show_parts_final(update, context, new_parts,
                                       show_date=(target_date != get_today_date()))
            except Exception as e:
                logger.error(f"خطا در show_parts_final از run_reschedule: {e}")

        context.user_data.pop("rescheduling", None)
        return True
    except Exception as e:
        logger.error(f"خطا در run_reschedule (trigger={trigger}): {e}")
        context.user_data.pop("rescheduling", None)
        return False


# ==================== Cleanup: یادآور منقضی + ثابت منقضی ====================
async def cleanup_expired_parts_job(context_or_none=None, user_id: Optional[int] = None) -> Dict:
    """
    پاک‌سازی:
    - یادآورهای منقضی (content_type='reminder' یا metadata.is_reminder=True، completed=FALSE و از پایان +2h گذشته): DELETE
    - پارت‌های ثابت منقضی (is_fixed_time=TRUE، completed=FALSE، پایان +2h گذشته، و نه reminder): شناور
    """
    now = get_iran_now()
    now_naive = now.replace(tzinfo=None)
    today_str = get_today_date()

    deleted_reminders = 0
    converted_fixed = 0

    try:
        user_clause = ""
        params = []
        if user_id:
            user_clause = " AND user_id = %s"
            params.append(user_id)

        # یادآورهای منقضی
        rows = execute_query(
            f"""SELECT part_id, session_id, matched_card_id, metadata
                FROM study_parts
                WHERE completed = FALSE
                  AND (
                      content_type = 'reminder'
                      OR COALESCE(metadata->>'is_reminder', 'false') = 'true'
                  )
                  AND (
                      (planned_end_time IS NOT NULL AND planned_end_time < (CURRENT_TIME - INTERVAL '{OVERDUE_GRACE_HOURS} hours'))
                      OR (planned_start_time IS NOT NULL AND planned_start_time < (CURRENT_TIME - INTERVAL '{OVERDUE_GRACE_HOURS} hours'))
                  )
                  AND EXISTS (
                      SELECT 1 FROM study_sessions s
                      WHERE s.session_id = study_parts.session_id
                        AND s.date <= %s
                  )
                  {user_clause}""",
            tuple([today_str] + params),
            fetchall=True
        ) or []

        for r in rows:
            part_id = r[0]
            card_id = r[2]
            try:
                execute_query("DELETE FROM reminder_notifications WHERE part_id = %s", (part_id,))
                execute_query("DELETE FROM study_parts WHERE part_id = %s", (part_id,))
                execute_query(
                    "UPDATE study_sessions SET total_parts = GREATEST(0, total_parts - 1) WHERE session_id = %s",
                    (r[1],)
                )
                if card_id:
                    execute_query("UPDATE cards SET is_active = FALSE WHERE id = %s", (card_id,))
                    delete_rag_by_source(f"card_{card_id}")
                deleted_reminders += 1
            except Exception as e:
                logger.error(f"cleanup: خطا در حذف یادآور {part_id}: {e}")

        # پارهای ثابت منقضی → شناور
        fixed_rows = execute_query(
            f"""SELECT part_id, planned_start_time, planned_end_time, reason, metadata
                FROM study_parts
                WHERE is_fixed_time = TRUE
                  AND completed = FALSE
                  AND (
                      content_type IS NULL
                      OR content_type NOT IN ('reminder', 'class')
                  )
                  AND (
                      origin IS NULL
                      OR origin NOT IN ('routine', 'reminder', 'card')
                  )
                  AND COALESCE(metadata->>'is_reminder', 'false') != 'true'
                  AND COALESCE(metadata->>'converted_from_fixed', 'false') != 'true'
                  AND planned_end_time IS NOT NULL
                  AND planned_end_time < (CURRENT_TIME - INTERVAL '{OVERDUE_GRACE_HOURS} hours')
                  AND EXISTS (
                      SELECT 1 FROM study_sessions s
                      WHERE s.session_id = study_parts.session_id
                        AND s.date <= %s
                  )
                  {user_clause}""",
            tuple([today_str] + params),
            fetchall=True
        ) or []

        for r in fixed_rows:
            part_id, ps, pe, reason, md = r
            md = md or {}
            orig = {
                "start": str(ps) if ps else None,
                "end": str(pe) if pe else None,
            }
            md["converted_from_fixed"] = True
            md["original_time"] = orig
            try:
                execute_query(
                    """UPDATE study_parts SET
                       is_fixed_time = FALSE,
                       planned_start_time = NULL,
                       planned_end_time = NULL,
                       time_slot = '',
                       explicit_start = NULL,
                       explicit_end = NULL,
                       reason = %s,
                       metadata = %s
                       WHERE part_id = %s""",
                    ("زمان ثابت گذشته؛ شناور شد", json.dumps(md), part_id)
                )
                converted_fixed += 1
            except Exception as e:
                logger.error(f"cleanup: خطا در شناورسازی {part_id}: {e}")

        # session خالی → archive
        execute_query(
            """UPDATE study_sessions SET archived = TRUE
               WHERE total_parts = 0 AND archived = FALSE AND confirmed = TRUE"""
        )

        logger.info(
            f"cleanup_expired: deleted_reminders={deleted_reminders} "
            f"converted_fixed={converted_floating if False else converted_fixed} "
            f"user={user_id or 'all'}"
        )
        return {
            "deleted_reminders": deleted_reminders,
            "converted_fixed": converted_fixed,
        }
    except Exception as e:
        logger.error(f"cleanup_expired_parts_job خطا: {e}")
        return {"deleted_reminders": 0, "converted_fixed": 0}


# legacy alias
async def purge_expired_reminders(user_id: Optional[int] = None) -> Dict:
    return await cleanup_expired_parts_job(user_id=user_id)


async def convert_expired_fixed_parts(user_id: Optional[int] = None) -> int:
    res = await cleanup_expired_parts_job(user_id=user_id)
    return res.get("converted_fixed", 0)
# ==================== bot.py - بخش 2/4 ====================
# کیبوردها + تقویم + منطق برنامه‌ریزی + scheduling (بازه‌های ناپیوسته) +
# conflict + گزارش + یادآور + AI Plan + Clarify Gate + Schema

# ==================== کیبوردها ====================
def get_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["📝 برنامه امروز", "📊 گزارش"],
        ["📅 تقویم", "💰 KOK و اشتراک"],
        ["📋 کارت‌های من"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_activity_type_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["📖 مطالعه", "📝 تست"],
        ["🔁 مرور", "🏫 کلاس"],
        ["🎥 ویدئو", "✏️ تمرین"],
        ["📌 سایر"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def get_admin_panel_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["📚 RAG", "🎭 Role"],
        ["🃏 Cards", "📊 آمار"],
        ["💳 پرداخت‌ها", "🔙 بازگشت"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_rag_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["📤 ارسال متن", "📁 ارسال فایل"],
        ["📋 لیست اسناد", "🗑 حذف سند"],
        ["🔙 بازگشت"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_role_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["✏️ شخصیت جدید", "📋 لیست شخصیت‌ها"],
        ["✅ فعال‌سازی", "🗑 حذف"],
        ["🔙 بازگشت"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_part_buttons_initial(parts: List[Dict]) -> ReplyKeyboardMarkup:
    keyboard = []
    sorted_parts = sorted(parts, key=lambda x: x.get("part_number", 0))
    for part in sorted_parts:
        status = "⬜" if not part.get("completed") else "✅"
        title = part.get("title", "بدون عنوان")
        planned_start = part.get("planned_start_time") or part.get("planned_start") or ""
        planned_end = part.get("planned_end_time") or part.get("planned_end") or ""
        time_info = f" {planned_start}-{planned_end}" if planned_start and planned_end else (
            f" {part['time_slot']}" if part.get("time_slot") else "")
        fixed_tag = " 🔒" if part.get("is_fixed_time", False) else ""
        floating_tag = " 🕊" if is_floating_part(part) else ""
        reminder_tag = " 🔔" if is_reminder_part(part) else ""
        text = (f"{status} {title} ({part.get('planned_minutes', 0)}د)"
                f"{time_info}{fixed_tag}{floating_tag}{reminder_tag} "
                f"↕️ [{part.get('part_id', 0)}]")
        keyboard.append([text])
    keyboard.append(["✅ تایید برنامه"])
    keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_part_buttons_final(parts: List[Dict], show_date: bool = False,
                            back_to_calendar: bool = False,
                            is_past: bool = False,
                            is_future: bool = False) -> ReplyKeyboardMarkup:
    keyboard = []
    if not parts:
        keyboard.append(["🔙 بازگشت"])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def sort_key(p):
        st = p.get("planned_start_time") or p.get("planned_start") or "00:00"
        try:
            return (0, time_to_minutes(st))
        except Exception:
            return (1, p.get("part_number", 0))

    sorted_parts = sorted(parts, key=sort_key)
    for part in sorted_parts:
        part_id = part.get("part_id")
        if not part_id:
            continue
        status = "✅" if part.get("completed", False) else "⬜"
        title = part.get("title", "بدون عنوان")
        planned_start = part.get("planned_start_time") or part.get("planned_start") or ""
        planned_end = part.get("planned_end_time") or part.get("planned_end") or ""
        time_info = f" {planned_start}-{planned_end}" if planned_start and planned_end else (
            f" {part['time_slot']}" if part.get("time_slot") else "")
        fixed_tag = " 🔒" if part.get("is_fixed_time", False) else ""
        floating_tag = " 🕊" if is_floating_part(part) else ""
        reminder_tag = " 🔔" if is_reminder_part(part) else ""
        overflow_tag = " ⚠️" if (part.get("metadata") or {}).get("is_overflow") else ""
        text = (f"{status} {title} ({part.get('planned_minutes', 0)}د)"
                f"{time_info}{fixed_tag}{floating_tag}{reminder_tag}{overflow_tag} [{part_id}]")
        keyboard.append([text])

    if not is_past and not is_future:
        keyboard.append(["✏️ ویرایش برنامه", "🔄 بازچینی برنامه"])
        keyboard.append(["✅ اتمام برنامه"])
    elif is_future:
        keyboard.append(["✏️ ویرایش برنامه", "🔄 بازچینی برنامه"])

    if back_to_calendar:
        keyboard.append(["🔙 بازگشت به تقویم"])
    else:
        keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_part_detail_buttons(part_id: int, is_timer_running: bool = False,
                             elapsed_seconds: int = 0,
                             is_reminder: bool = False) -> ReplyKeyboardMarkup:
    keyboard = []
    if is_reminder:
        keyboard.append(["✅ انجامش دادم"])
        keyboard.append(["🔙 بازگشت"])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    if is_timer_running:
        keyboard.append(["⏹ توقف", "✅ تکمیل"])
    else:
        if elapsed_seconds > 0:
            keyboard.append(["▶️ ادامه تایمر", "✅ تکمیل"])
        else:
            keyboard.append(["⏱ تایمر", "✅ تکمیل"])
    keyboard.append(["🗑 حذف پارت"])
    keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_edit_order_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["✅ تایید ترتیب و رفتن به ویرایش زمان"],
        ["❌ لغو ویرایش"]
    ], resize_keyboard=True)


def get_edit_time_list_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["✅ اتمام ویرایش زمان"],
        ["🔙 برگشت به ویرایش ترتیب"],
        ["❌ لغو کل ویرایش"]
    ], resize_keyboard=True)


def get_edit_part_time_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["➕ ۱۵ دقیقه", "➖ ۱۵ دقیقه"],
        ["➕ ۳۰ دقیقه", "➖ ۳۰ دقیقه"],
        ["➕ ۱ ساعت", "➖ ۱ ساعت"],
        ["✅ تایید این پارت"],
        ["🔙 برگشت به لیست"]
    ], resize_keyboard=True)


def get_duration_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["⏱ ۲۰ دقیقه", "⏱ ۳۰ دقیقه"],
        ["⏱ ۴۵ دقیقه", "⏱ ۶۰ دقیقه"],
        ["✏️ دلخواه"]
    ], resize_keyboard=True)


def get_ai_chat_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["🔄 مکالمه جدید", "🔙 بازگشت به منو"]
    ], resize_keyboard=True)


def get_build_plan_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["🧠 ساخت با AI", "✏️ خودم می‌نویسم"],
        ["🔙 بازگشت"]
    ], resize_keyboard=True)


def get_confirm_change_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["✅ تایید تغییر", "❌ رد تغییر"],
        ["🔙 بازگشت"]
    ], resize_keyboard=True)


def get_confirm_clear_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["🗑 بله، همه را پاک کن"],
        ["❌ نه، لغو"]
    ], resize_keyboard=True)


def get_confirm_delete_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["✅ بله، حذف کن"],
        ["❌ نه، لغو"]
    ], resize_keyboard=True)


def get_feedback_keyboard(question_key: str, options: List[str]) -> ReplyKeyboardMarkup:
    keyboard = []
    row = []
    for opt in options:
        row.append(opt)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_project_keyboard(project_cards: List[Dict], active_card_id: int = None) -> ReplyKeyboardMarkup:
    keyboard = []
    for card in project_cards:
        data = card.get("data", {})
        title = data.get("title", "پروژه")
        exam_date = data.get("exam_date_shamsi")
        provider = data.get("provider", "")
        card_id = card.get("id")

        label = title
        if exam_date:
            label += f" ({exam_date})"
        elif provider:
            label += f" — {provider}"
        label = label[:40]
        mark = "✅ " if active_card_id and card_id == active_card_id else ""
        keyboard.append([f"🎯 {mark}{label} [{card_id}]"])

    keyboard.append(["✏️ پروژه دلخواه"])
    keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_project_summary_keyboard(is_active: bool) -> ReplyKeyboardMarkup:
    """خلاصه پروژه — طبق بخش ۱۳."""
    if is_active:
        return ReplyKeyboardMarkup([
            ["🔄 بازسازی هفته", "🗑 حذف پروژه"],
            ["🔙 بازگشت"]
        ], resize_keyboard=True)
    return ReplyKeyboardMarkup([
        ["✅ تایید", "🔙 بازگشت"]
    ], resize_keyboard=True)


def get_routin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True)


def get_reminder_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True)


def get_subscription_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["💳 ۵۰ KOK — ۱۰۰,۰۰۰", "💳 ۲۰۰ KOK — ۳۵۰,۰۰۰"],
        ["💳 ۴۰۰ KOK — ۶۵۰,۰۰۰"],
        ["📊 تاریخچه KOK"],
        ["🔙 بازگشت"]
    ], resize_keyboard=True)


def get_calendar_nav_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["⬅️ صفحه قبل", "➡️ صفحه بعد"],
        ["🔙 بازگشت"]
    ], resize_keyboard=True)


def get_card_confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["✅ ذخیره کارت", "✏️ ویرایش"],
        ["❌ لغو"]
    ], resize_keyboard=True)


def get_reminder_suggest_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["📚 بله، برنامه بچین", "❌ نه، فقط یادآور"]
    ], resize_keyboard=True, one_time_keyboard=True)


def get_reminder_action_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["✅ انجامش دادم", "⏰ ۳۰ دقیقه دیگه"]
    ], resize_keyboard=True, one_time_keyboard=True)


def get_reschedule_confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["✅ بله، بازچینی کن", "❌ نه، دست نزن"]
    ], resize_keyboard=True, one_time_keyboard=True)


def get_reschedule_apply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["✅ اعمال کن", "❌ لغو"]
    ], resize_keyboard=True, one_time_keyboard=True)


def get_cards_list_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["🔄 بروزرسانی وضعیت"],
        ["🔙 بازگشت"]
    ], resize_keyboard=True)


def get_yes_no_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["✅ بله", "❌ نه"]], resize_keyboard=True, one_time_keyboard=True)


def get_split_question_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["1️⃣ ۱ پارت (تقسیم نکن)"],
        ["2️⃣ ۲ پارت", "3️⃣ ۳ پارت"],
        ["4️⃣ ۴ پارت", "5️⃣ ۵ پارت"],
        ["✏️ خودم تعیین می‌کنم"],
        ["🔙 لغو"]
    ], resize_keyboard=True, one_time_keyboard=True)


def get_calendar_update_keyboard() -> ReplyKeyboardMarkup:
    """بخش ۸.۲ — پرسش تقویم."""
    return ReplyKeyboardMarkup([
        ["✅ بله، روزهای بعد را هم به‌روزرسانی کن"],
        ["❌ نه، فقط امروز"]
    ], resize_keyboard=True, one_time_keyboard=True)


def get_schedule_choice_keyboard() -> ReplyKeyboardMarkup:
    """بخش ۱۷ — ساعت‌گذاری پارت جدید."""
    return ReplyKeyboardMarkup([
        ["▶️ همین الان شروع کنم"],
        ["🕐 ساعت مشخص"],
        ["🧠 AI در جای مناسب بگذارد"]
    ], resize_keyboard=True, one_time_keyboard=True)


def get_fixed_or_floating_keyboard() -> ReplyKeyboardMarkup:
    """بخش ۱۶ — ثابت/شناور."""
    return ReplyKeyboardMarkup([
        ["🔒 ثابت", "🕊 شناور"]
    ], resize_keyboard=True, one_time_keyboard=True)


def get_compaction_keyboard() -> ReplyKeyboardMarkup:
    """بخش ۹.۱.C — فشرده‌سازی / حذف."""
    return ReplyKeyboardMarkup([
        ["✂️ فشرده‌سازی"],
        ["🗑 حذف پارت‌های اضافی"],
        ["🔙 بازگشت"]
    ], resize_keyboard=True, one_time_keyboard=True)


# ==================== بانک بازخورد ====================
FEEDBACK_BANK = {
    "test_percent": {
        "question": "📝 از این تست چند درصد درست زدی؟",
        "options": ["زیر ۲۰٪", "۲۰-۴۰٪", "۴۰-۶۰٪", "۶۰-۸۰٪", "بالای ۸۰٪"],
    },
    "test_why_wrong": {
        "question": "❓ بیشتر غلط‌هات به‌خاطر چی بود؟",
        "options": ["اصلاً بلد نبودم", "بلد بودم ولی اشتباه کردم", "وقت کم آوردم", "بی‌دقتی کردم"],
    },
    "study_distraction_where": {
        "question": "❓ بیشتر حواست کجا پرت شد؟",
        "options": ["گوشی/شبکه‌های اجتماعی", "فکر و نگرانی ذهنی", "سروصدای اطراف", "خستگی/خواب‌آلودگی", "گشنگی"],
    },
    "study_where": {
        "question": "📍 کجا داشتی می‌خوندی؟",
        "options": ["اتاق خودم", "جای دیگه‌ی خونه", "کتابخانه", "بیرون از خونه"],
    },
    "review_method": {
        "question": "🔁 با چه روشی مرور کردی؟",
        "options": ["خلاصه‌نویسی", "فلش‌کارت", "تست‌زنی", "بازخوانی", "تدریس به دیگران"],
    },
    "review_hard_part": {
        "question": "❓ کدوم بخشش برات سخت بود؟",
        "options": ["مفاهیم پایه", "فرمول‌ها", "مسائل ترکیبی", "حفظیات", "همه‌ش"],
    },
    "class_unsolved": {
        "question": "❓ چه مبحثی رو نفهمیدی؟",
        "options": ["مفهوم کلی", "مثال‌ها", "روش حل", "همه‌ش"],
    },
    "class_solved_percent": {
        "question": "📊 چند درصد سوالات سر کلاس حل کردی؟",
        "options": ["زیر ۵۰٪", "۵۰-۷۰٪", "۷۰-۹۰٪", "بالای ۹۰٪"],
    },
    "energy_after": {
        "question": "⚡ الان بعد از این فعالیت چه حسی داری؟",
        "options": ["💪 پرانرژی‌ام", "🙂 عادی‌ام", "😴 خسته‌ام"],
    },
    "need_review": {
        "question": "🔄 فکر می‌کنی این مبحث رو باید زود مرور کنی؟",
        "options": ["آره، زود فراموش می‌کنم", "نه، خوب جا افتاد"],
    },
    "speed": {
        "question": "⏱ سرعتت چطور بود؟",
        "options": ["کند", "متوسط", "سریع"],
    },
}


def get_feedback_questions(activity_type: str) -> List[str]:
    if activity_type == "test":
        return ["test_percent", "test_why_wrong"]
    elif activity_type == "study":
        return ["study_distraction_where", "study_where"]
    elif activity_type == "review":
        return ["review_method", "review_hard_part"]
    elif activity_type == "class":
        return ["class_unsolved", "class_solved_percent"]
    elif activity_type == "video":
        return ["need_review", "energy_after"]
    elif activity_type == "exercise":
        return ["energy_after", "speed"]
    return ["need_review", "energy_after"]


# ==================== تقویم ====================
def get_calendar_dates(user_id: int, page: int = 0) -> List[Dict]:
    today = get_iran_now().date()
    if page == 0:
        start = today
        end = today + timedelta(days=7)
    elif page < 0:
        end = today + timedelta(days=page * 7)
        start = end - timedelta(days=6)
    else:
        start = today + timedelta(days=(page - 1) * 8 + 8)
        end = start + timedelta(days=7)

    result = []
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        plan_info = execute_query(
            """SELECT s.session_id, s.total_parts, s.completed_parts,
                  COUNT(CASE WHEN p.content_type = 'reminder'
                              OR p.metadata->>'is_reminder' = 'true'
                         THEN 1 END) as reminder_count,
                  COUNT(CASE WHEN p.metadata->>'is_overflow' = 'true' THEN 1 END) as overflow_count
               FROM study_sessions s
               LEFT JOIN study_parts p ON p.session_id = s.session_id
               WHERE s.user_id = %s AND s.date = %s AND s.archived = FALSE
               GROUP BY s.session_id, s.total_parts, s.completed_parts
               ORDER BY s.created_at DESC LIMIT 1""",
            (user_id, date_str), fetch=True
        )
        has_plan = bool(plan_info and plan_info[1] and plan_info[1] > 0)
        total_parts = plan_info[1] if plan_info else 0
        completed_parts = plan_info[2] if plan_info else 0
        reminder_count = plan_info[3] if plan_info and plan_info[3] else 0
        overflow_count = plan_info[4] if plan_info and plan_info[4] else 0

        is_today = (current == today)
        is_past = (current < today)
        result.append({
            "date": date_str, "shamsi": get_shamsi_date(date_str),
            "weekday": get_weekday_fa(current),
            "is_today": is_today, "is_past": is_past, "is_future": (current > today),
            "has_plan": has_plan, "total_parts": total_parts,
            "completed_parts": completed_parts,
            "reminder_count": reminder_count, "overflow_count": overflow_count,
        })
        current += timedelta(days=1)
    return result


def format_calendar_date(item: Dict) -> str:
    shamsi = item["shamsi"]
    parts = shamsi.split("/")
    if len(parts) == 3:
        month_name = get_shamsi_month_name(item["date"])
        display = f"{parts[2]} {month_name}"
    else:
        display = shamsi

    prefix = "🌟 " if item["is_today"] else ""
    line = f"{prefix}{item['weekday']} {display}"
    if item["has_plan"]:
        if item["completed_parts"] == item["total_parts"] and item["total_parts"] > 0:
            line += " ✅"
        elif item["completed_parts"] > 0:
            line += f" 📖({item['completed_parts']}/{item['total_parts']})"
        else:
            line += " 📖"
    if item.get("reminder_count", 0) > 0:
        line += f" 🔔×{item['reminder_count']}"
    if item.get("overflow_count", 0) > 0:
        line += " ⚠️"
    return line


# ==================== سشن ====================
def get_or_create_session(user_id: int, target_date: str) -> Optional[int]:
    try:
        result = execute_query(
            """INSERT INTO study_sessions
               (user_id, date, total_parts, completed_parts,
                time_slots, topics, confirmed, archived, meta)
               VALUES (%s, %s, 0, 0, '[]'::jsonb, '[]'::jsonb, FALSE, FALSE, '{}'::jsonb)
               ON CONFLICT (user_id, date) DO UPDATE
               SET archived = FALSE, is_finished = FALSE, confirmed = FALSE
               RETURNING session_id""",
            (user_id, target_date), fetch=True
        )
        return result[0] if result else None
    except Exception as e:
        logger.error(f"خطا در get_or_create_session: {e}")
        return None


def save_session_with_parts(user_id: int, parts: List[Dict], time_slots: List[str],
                           topics: List[Dict], meta: Dict = None) -> Optional[int]:
    return save_session_with_parts_for_date(
        user_id, parts, time_slots, topics, get_today_date(), meta
    )


def save_session_with_parts_for_date(user_id: int, parts: List[Dict],
                                      time_slots: List[str], topics: List[Dict],
                                      target_date: str, meta: Dict = None) -> Optional[int]:
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT session_id FROM study_sessions
               WHERE user_id = %s AND date = %s
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, target_date)
        )
        existing = cursor.fetchone()

        if existing:
            session_id = existing[0]
            # بخش ۱۰.۸ — حفاظت پارهای محافظت‌شده
            cursor.execute(
                """SELECT COUNT(*) FROM study_parts
                   WHERE session_id = %s AND (
                       is_fixed_time = TRUE
                       OR content_type IN ('reminder', 'class')
                       OR origin IN ('routine', 'reminder', 'card')
                       OR COALESCE(metadata->>'is_reminder', 'false') = 'true'
                       OR completed = TRUE
                   )""",
                (session_id,)
            )
            protected_count = cursor.fetchone()[0] or 0
            if protected_count > 0:
                # حذف فقط پارهای غیرمحافظت‌شده
                cursor.execute(
                    """DELETE FROM study_parts
                       WHERE session_id = %s AND NOT (
                           is_fixed_time = TRUE
                           OR content_type IN ('reminder', 'class')
                           OR origin IN ('routine', 'reminder', 'card')
                           OR COALESCE(metadata->>'is_reminder', 'false') = 'true'
                           OR completed = TRUE
                       )""",
                    (session_id,)
                )
            else:
                cursor.execute("DELETE FROM study_parts WHERE session_id = %s", (session_id,))

            cursor.execute("""
                UPDATE study_sessions
                SET total_parts = %s, completed_parts = 0,
                    time_slots = %s, topics = %s, meta = %s,
                    confirmed = FALSE, is_finished = FALSE, archived = FALSE,
                    created_at = CURRENT_TIMESTAMP
                WHERE session_id = %s
            """, (len(parts), json.dumps(time_slots), json.dumps(topics),
                  json.dumps(meta or {}), session_id))
        else:
            cursor.execute("""
                INSERT INTO study_sessions
                (user_id, date, total_parts, time_slots, topics, archived, meta)
                VALUES (%s, %s, %s, %s, %s, FALSE, %s) RETURNING session_id
            """, (user_id, target_date, len(parts), json.dumps(time_slots),
                  json.dumps(topics), json.dumps(meta or {})))
            result = cursor.fetchone()
            if not result:
                conn.rollback()
                return None
            session_id = result[0]

        for part in parts:
            planned_start = normalize_time_str(part.get("planned_start_time"))
            planned_end = normalize_time_str(part.get("planned_end_time"))
            if not planned_start and part.get("time_slot"):
                try:
                    start_str, end_str = part["time_slot"].split("-")
                    planned_start = start_str
                    planned_end = end_str
                except Exception:
                    pass

            cursor.execute("""
                INSERT INTO study_parts (
                    session_id, user_id, part_number, title, activity_type, content_type,
                    planned_minutes, time_slot, pages,
                    planned_start_time, planned_end_time, is_fixed_time, reason,
                    explicit_start, explicit_end, depends_on, metadata,
                    origin, subject, topic_resolved, detection_confidence, matched_card_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s)
            """, (
                session_id, user_id, part["part_number"], part["title"],
                part.get("activity", "study"), part.get("content_type", "book"),
                part["planned_minutes"], part.get("time_slot", ""), part.get("pages", 0),
                planned_start, planned_end, part.get("is_fixed_time", False),
                part.get("reason", ""), part.get("explicit_start"),
                part.get("explicit_end"), part.get("depends_on"),
                json.dumps(part.get("metadata", {})), part.get("origin", "ai"),
                part.get("subject"), part.get("topic_resolved"),
                part.get("detection_confidence"), part.get("matched_card_id")
            ))

        conn.commit()
        return session_id
    except Exception as e:
        logger.error(f"❌ خطا در save_session_with_parts_for_date: {e}")
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
        user_id = part_data.get("user_id")

        cursor.execute("""
            INSERT INTO study_parts (
                session_id, user_id, part_number, title, activity_type, content_type,
                planned_minutes, time_slot, pages, completed,
                planned_start_time, planned_end_time, is_fixed_time, reason,
                explicit_start, explicit_end, depends_on, metadata,
                origin, subject, topic_resolved, detection_confidence, matched_card_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s) RETURNING part_id
        """, (
            session_id, user_id, new_part_number, part_data["title"],
            part_data.get("activity", "study"), part_data.get("content_type", "book"),
            part_data["planned_minutes"], part_data.get("time_slot", ""),
            part_data.get("pages", 0), False, part_data.get("planned_start_time"),
            part_data.get("planned_end_time"), part_data.get("is_fixed_time", False),
            part_data.get("reason", ""), part_data.get("explicit_start"),
            part_data.get("explicit_end"), part_data.get("depends_on"),
            json.dumps(part_data.get("metadata", {})), part_data.get("origin", "user"),
            part_data.get("subject"), part_data.get("topic_resolved"),
            part_data.get("detection_confidence"), part_data.get("matched_card_id")
        ))
        result = cursor.fetchone()
        if not result:
            conn.rollback()
            return None
        part_id = result[0]
        cursor.execute(
            "UPDATE study_sessions SET total_parts = total_parts + 1 WHERE session_id = %s",
            (session_id,)
        )
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
    execute_query("UPDATE study_sessions SET confirmed = TRUE WHERE session_id = %s", (session_id,))


def finish_session(session_id: int) -> None:
    execute_query("UPDATE study_sessions SET is_finished = TRUE, archived = TRUE WHERE session_id = %s", (session_id,))


def get_plan_by_date(user_id: int, date_str: str) -> Optional[Dict]:
    """پارت‌های یادآور منقضی نمایش داده نمی‌شوند (بخش ۱۱.۵)."""
    query = """
    SELECT s.session_id, s.total_parts, s.completed_parts,
           s.confirmed, s.time_slots, s.topics, s.is_finished, s.archived, s.meta
    FROM study_sessions s
    WHERE s.user_id = %s AND s.date = %s AND s.archived = FALSE
    ORDER BY s.created_at DESC LIMIT 1
    """
    result = execute_query(query, (user_id, date_str), fetch=True)
    if not result:
        return None
    (session_id, total_parts, completed_parts, confirmed, time_slots, topics,
     is_finished, archived, meta) = result

    query_parts = """
    SELECT part_id, part_number, title, planned_minutes, actual_minutes,
           time_slot, completed, pages,
           to_char(planned_start_time, 'HH24:MI'), to_char(planned_end_time, 'HH24:MI'),
           actual_start_time, actual_end_time, is_fixed_time,
           reason, explicit_start, explicit_end, depends_on,
           activity_type, content_type, metadata, activity_type_confirmed,
           origin, subject, topic_resolved, detection_confidence, matched_card_id
    FROM study_parts
    WHERE session_id = %s
    ORDER BY part_number
    """
    parts_result = execute_query(query_parts, (session_id,), fetchall=True)

    parts = []
    for row in parts_result:
        parts.append({
            "part_id": row[0], "part_number": row[1], "title": row[2],
            "planned_minutes": row[3], "actual_minutes": row[4] or 0,
            "time_slot": row[5] or "", "completed": row[6], "pages": row[7] or 0,
            "planned_start_time": row[8] or "", "planned_end_time": row[9] or "",
            "planned_start": row[8] or "", "planned_end": row[9] or "",
            "actual_start": row[10], "actual_end": row[11],
            "is_fixed_time": row[12] or False, "reason": row[13] or "",
            "explicit_start": row[14], "explicit_end": row[15],
            "depends_on": row[16], "activity_type": row[17] or "study",
            "content_type": row[18] or "book", "metadata": row[19] or {},
            "activity_type_confirmed": row[20], "origin": row[21] or "ai",
            "subject": row[22], "topic_resolved": row[23],
            "detection_confidence": row[24], "matched_card_id": row[25]
        })

    if isinstance(time_slots, str):
        try:
            time_slots = json.loads(time_slots)
        except Exception:
            time_slots = []
    if isinstance(topics, str):
        try:
            topics = json.loads(topics)
        except Exception:
            topics = []

    return {
        "session_id": session_id, "total_parts": total_parts,
        "completed_parts": completed_parts, "confirmed": confirmed,
        "time_slots": time_slots, "topics": topics, "parts": parts,
        "date": date_str, "is_finished": is_finished,
        "archived": archived, "meta": meta or {}
    }


# ==================== فعالیت‌ها ====================
def get_today_activities(user_id: int) -> List[Dict]:
    today = get_today_date()
    results = execute_query(
        """SELECT id, subject, topic, activity_type, planned_duration, actual_duration,
                  start_time, end_time, score, status, part_order, created_at
           FROM activity_log
           WHERE user_id = %s AND date = %s AND is_archived = FALSE
           ORDER BY part_order ASC, created_at ASC""",
        (user_id, today), fetchall=True
    )
    return [{
        "id": r[0], "subject": r[1], "topic": r[2], "activity_type": r[3],
        "planned_duration": r[4] or 0, "actual_duration": r[5] or 0,
        "start_time": r[6], "end_time": r[7], "score": r[8],
        "status": r[9] or "pending", "part_order": r[10] or 0, "created_at": r[11]
    } for r in results] if results else []


def get_activities_by_date_range(user_id: int, start_date: str, end_date: str) -> List[Dict]:
    results = execute_query(
        """SELECT id, subject, topic, activity_type, planned_duration, actual_duration,
                  date, score, status, part_order
           FROM activity_log
           WHERE user_id = %s AND date BETWEEN %s AND %s AND is_archived = FALSE
           ORDER BY date DESC, part_order ASC""",
        (user_id, start_date, end_date), fetchall=True
    )
    return [{
        "id": r[0], "subject": r[1], "topic": r[2], "activity_type": r[3],
        "planned_duration": r[4] or 0, "actual_duration": r[5] or 0,
        "date": str(r[6]), "score": r[7], "status": r[8] or "pending",
        "part_order": r[9] or 0
    } for r in results] if results else []


def get_yesterday_activities(user_id: int) -> List[Dict]:
    yesterday = (get_iran_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return get_activities_by_date_range(user_id, yesterday, yesterday)


def save_activity(activity_data: Dict) -> Optional[int]:
    query = """
    INSERT INTO activity_log (
        user_id, date, subject, topic, activity_type, planned_duration,
        actual_duration, start_time, end_time, score, status,
        pages_count, test_count, part_order
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
    """
    result = execute_query(query, (
        activity_data["user_id"], activity_data["date"],
        activity_data["subject"], activity_data.get("topic"),
        activity_data.get("activity_type"), activity_data.get("planned_duration"),
        activity_data.get("actual_duration"), activity_data.get("start_time"),
        activity_data.get("end_time"), activity_data.get("score"),
        activity_data.get("status", "pending"),
        activity_data.get("pages_count", 0), activity_data.get("test_count", 0),
        activity_data.get("part_order", 0)
    ), fetch=True)
    return result[0] if result else None


def save_part_feedback(part_id: int, user_id: int, question_key: str, answer_value: str) -> None:
    try:
        execute_query(
            """INSERT INTO part_feedback (part_id, user_id, question_key, answer_value)
               VALUES (%s, %s, %s, %s)""",
            (part_id, user_id, question_key, answer_value)
        )
    except Exception as e:
        logger.error(f"خطا در ذخیره بازخورد: {e}")


def update_subject_status(user_id: int, subject: str, activity_data: Dict) -> None:
    current = execute_query(
        "SELECT total_sessions, completed_sessions, total_study_minutes, avg_score FROM subject_status WHERE user_id = %s AND subject = %s",
        (user_id, subject), fetch=True
    )
    if current:
        total_sessions = (current[0] or 0) + 1
        completed = (current[1] or 0) + (1 if activity_data.get("status") == "done" else 0)
        total_minutes = (current[2] or 0) + (activity_data.get("actual_duration") or 0)
        old_avg = current[3] or 0
        new_score = activity_data.get("score")
        avg_score = (old_avg * (total_sessions - 1) + new_score) / total_sessions if new_score is not None else old_avg
        execute_query(
            """UPDATE subject_status SET total_sessions = %s, completed_sessions = %s,
               total_study_minutes = %s, avg_score = %s, last_studied = %s, last_score = %s,
               updated_at = CURRENT_TIMESTAMP WHERE user_id = %s AND subject = %s""",
            (total_sessions, completed, total_minutes, avg_score,
             activity_data["date"], new_score, user_id, subject)
        )
    else:
        execute_query(
            """INSERT INTO subject_status (user_id, subject, total_sessions,
               completed_sessions, total_study_minutes, avg_score, last_studied, last_score)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, subject, 1, 1 if activity_data.get("status") == "done" else 0,
             activity_data.get("actual_duration") or 0, activity_data.get("score"),
             activity_data["date"], activity_data.get("score"))
        )


# ==================== آمار و گزارش ====================
def get_user_study_summary(user_id: int, days: int = 7) -> Dict:
    end_date = get_today_date()
    start_date = (get_iran_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    stats = execute_query(
        """SELECT COUNT(*), COUNT(CASE WHEN status = 'done' THEN 1 END),
                  COALESCE(SUM(actual_duration), 0),
                  COALESCE(AVG(CASE WHEN score IS NOT NULL THEN score END), 0),
                  COUNT(DISTINCT date)
           FROM activity_log
           WHERE user_id = %s AND date BETWEEN %s AND %s AND is_archived = FALSE""",
        (user_id, start_date, end_date), fetch=True
    )
    return {
        "days": days, "start_date": start_date, "end_date": end_date,
        "total_sessions": stats[0] if stats else 0,
        "completed_sessions": stats[1] if stats else 0,
        "total_minutes": stats[2] if stats else 0,
        "avg_score": float(stats[3]) if stats and stats[3] else 0,
        "active_days": stats[4] if stats else 0,
    }


def get_recent_dates(user_id: int, days: int = 10) -> List[str]:
    results = execute_query(
        """SELECT DISTINCT date FROM study_sessions
           WHERE user_id = %s ORDER BY date DESC LIMIT %s""",
        (user_id, days), fetchall=True
    )
    return [r[0] for r in results] if results else []


def get_last_n_days_data(user_id: int, days: int = 7) -> List[Dict]:
    results = execute_query(
        """SELECT date, total_parts, completed_parts FROM study_sessions
           WHERE user_id = %s ORDER BY date DESC LIMIT %s""",
        (user_id, days), fetchall=True
    )
    return [{"date": r[0], "total_parts": r[1] or 0, "completed_parts": r[2] or 0}
            for r in results] if results else []


def calc_delay_percent(user_id: int, start_date: str, end_date: str) -> Dict:
    today = get_today_date()
    stats = execute_query(
        """SELECT COUNT(*),
              COUNT(CASE WHEN p.completed = FALSE AND s.date < %s THEN 1 END),
              COUNT(CASE WHEN p.completed = FALSE AND s.date = %s
                         AND p.planned_end_time IS NOT NULL
                         AND p.planned_end_time < CURRENT_TIME THEN 1 END)
           FROM study_parts p
           JOIN study_sessions s ON s.session_id = p.session_id
           WHERE s.user_id = %s AND s.date BETWEEN %s AND %s
             AND p.content_type != 'reminder' AND s.archived = FALSE""",
        (today, today, user_id, start_date, end_date), fetch=True
    )
    if not stats or not stats[0]:
        return {"total": 0, "delayed": 0, "percent": 0}
    total = stats[0] or 0
    delayed_past = stats[1] or 0
    delayed_today = stats[2] or 0
    delayed = delayed_past + delayed_today
    if total == 0:
        return {"total": 0, "delayed": 0, "percent": 0}
    percent = (delayed / total * 100)
    return {"total": total, "delayed": delayed, "delayed_past": delayed_past,
            "delayed_today": delayed_today, "percent": round(percent, 1)}


def calc_subject_breakdown(user_id: int, start_date: str, end_date: str) -> List[Dict]:
    results = execute_query(
        """SELECT COALESCE(p.subject, 'نامشخص'),
              SUM(CASE WHEN p.completed THEN p.actual_minutes ELSE 0 END),
              SUM(p.planned_minutes), COUNT(*),
              COUNT(CASE WHEN p.completed THEN 1 END)
           FROM study_parts p
           JOIN study_sessions s ON s.session_id = p.session_id
           WHERE s.user_id = %s AND s.date BETWEEN %s AND %s
             AND p.content_type != 'reminder'
           GROUP BY p.subject ORDER BY 2 DESC""",
        (user_id, start_date, end_date), fetchall=True
    )
    total_done = sum([(r[1] or 0) for r in (results or [])])
    output = []
    for r in (results or []):
        subj, done_min, planned_min, total_parts, done_parts = r
        done_min = done_min or 0
        planned_min = planned_min or 0
        time_percent = (done_min / total_done * 100) if total_done > 0 else 0
        commitment = (done_min / planned_min * 100) if planned_min > 0 else 0
        output.append({
            "subject": subj, "done_minutes": done_min,
            "planned_minutes": planned_min, "total_parts": total_parts,
            "completed_parts": done_parts or 0,
            "time_percent": round(time_percent, 1),
            "commitment_percent": round(commitment, 1)
        })
    return output


def calc_time_totals(user_id: int) -> Dict:
    today = get_iran_now().date()

    def sum_minutes(start_d, end_d):
        result = execute_query(
            """SELECT COALESCE(SUM(actual_minutes), 0),
                      COUNT(DISTINCT s.date),
                      COUNT(CASE WHEN p.completed THEN 1 END),
                      COUNT(*)
               FROM study_parts p
               JOIN study_sessions s ON s.session_id = p.session_id
               WHERE s.user_id = %s AND s.date BETWEEN %s AND %s
                 AND p.content_type != 'reminder'""",
            (user_id, start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d")),
            fetch=True
        )
        if not result:
            return {"minutes": 0, "active_days": 0, "completed": 0, "total": 0}
        return {"minutes": result[0] or 0, "active_days": result[1] or 0,
                "completed": result[2] or 0, "total": result[3] or 0}

    day_stats = sum_minutes(today, today)
    week_start = today - timedelta(days=today.weekday())
    week_stats = sum_minutes(week_start, today)
    month_start = today.replace(day=1)
    month_stats = sum_minutes(month_start, today)
    return {"day": day_stats, "week": week_stats, "month": month_stats}


def calc_hourly_breakdown(user_id: int, start_date: str, end_date: str) -> Dict:
    results = execute_query(
        """SELECT p.planned_start_time, SUM(p.actual_minutes)
           FROM study_parts p
           JOIN study_sessions s ON s.session_id = p.session_id
           WHERE s.user_id = %s AND s.date BETWEEN %s AND %s
             AND p.completed = TRUE AND p.planned_start_time IS NOT NULL
             AND p.content_type != 'reminder'
           GROUP BY p.planned_start_time""",
        (user_id, start_date, end_date), fetchall=True
    )
    buckets = {"morning": 0, "noon": 0, "evening": 0, "night": 0}
    for r in (results or []):
        if not r[0]:
            continue
        hour = r[0].hour
        minutes = r[1] or 0
        if 6 <= hour < 12:
            buckets["morning"] += minutes
        elif 12 <= hour < 17:
            buckets["noon"] += minutes
        elif 17 <= hour < 21:
            buckets["evening"] += minutes
        else:
            buckets["night"] += minutes
    total = sum(buckets.values())
    if total > 0:
        for k in buckets:
            buckets[k] = round(buckets[k] / total * 100, 1)
    return buckets


def update_daily_stats(user_id: int, target_date: str) -> None:
    try:
        stats = execute_query(
            """SELECT COUNT(*), COUNT(CASE WHEN completed THEN 1 END),
                  COUNT(CASE WHEN completed = FALSE
                             AND planned_end_time < CURRENT_TIME
                             AND s.date = CURRENT_DATE::text THEN 1 END),
                  COALESCE(SUM(planned_minutes), 0), COALESCE(SUM(actual_minutes), 0)
               FROM study_parts p
               JOIN study_sessions s ON s.session_id = p.session_id
               WHERE s.user_id = %s AND s.date = %s
                 AND p.content_type != 'reminder'""",
            (user_id, target_date), fetch=True
        )
        if not stats:
            return
        total, completed, delayed, planned, actual = stats
        delay_percent = (delayed / total * 100) if total > 0 else 0
        commitment_percent = (actual / planned * 100) if planned > 0 else 0
        subjects = calc_subject_breakdown(user_id, target_date, target_date)
        hourly = calc_hourly_breakdown(user_id, target_date, target_date)
        execute_query(
            """INSERT INTO daily_stats
               (user_id, date, total_parts, completed_parts, delayed_parts,
                planned_minutes, actual_minutes, subjects_breakdown,
                hourly_breakdown, delay_percent, commitment_percent)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id, date) DO UPDATE SET
               total_parts = EXCLUDED.total_parts,
               completed_parts = EXCLUDED.completed_parts,
               delayed_parts = EXCLUDED.delayed_parts,
               planned_minutes = EXCLUDED.planned_minutes,
               actual_minutes = EXCLUDED.actual_minutes,
               subjects_breakdown = EXCLUDED.subjects_breakdown,
               hourly_breakdown = EXCLUDED.hourly_breakdown,
               delay_percent = EXCLUDED.delay_percent,
               commitment_percent = EXCLUDED.commitment_percent,
               updated_at = CURRENT_TIMESTAMP""",
            (user_id, target_date, total, completed, delayed,
             planned, actual, json.dumps(subjects),
             json.dumps(hourly), delay_percent, commitment_percent)
        )
    except Exception as e:
        logger.error(f"خطا در update_daily_stats: {e}")


# ==================== time scheduling — بازه‌های ناپیوسته ====================
def _sort_parts_by_priority(parts: List[Dict]) -> List[Dict]:
    return sorted(parts, key=lambda x: (
        0 if x.get("priority") == "high" else (1 if x.get("priority") == "normal" else 2),
        x.get("part_number", 0)
    ))


def _free_ranges_from_slots(time_slots: List[str]) -> List[Tuple[int, int]]:
    ranges = []
    for slot in time_slots or []:
        try:
            s_str, e_str = slot.split("-")
            s = time_to_minutes(s_str)
            e = time_to_minutes(e_str)
            if e > s:
                ranges.append((s, e))
        except Exception:
            continue
    ranges.sort()
    return ranges


def schedule_parts_with_time_slots(parts: List[Dict], time_slots: List[str],
                                     source_date: str = None) -> List[Dict]:
    """
    بازنویسی طبق بخش ۱۹ و ۲۰ — بازه‌های ناپیوسته، شروع از effective_start،
    هیچ پارتی در شکاف بین بازه‌ها قرار نگیرد.
    پارهای محافظت‌شده قفل می‌مانند.
    """
    if not time_slots or not parts:
        return parts

    if source_date is None:
        source_date = get_today_date()

    slot_ranges = _free_ranges_from_slots(time_slots)
    if not slot_ranges:
        return parts

    now_min = get_iran_now().hour * 60 + get_iran_now().minute
    effective_start = compute_effective_start(time_slots, now_min, source_date)

    protected = [p for p in parts if is_protected_part(p)]
    free_parts = [p for p in parts if not is_protected_part(p)]

    for p in protected:
        st = p.get("explicit_start") or p.get("planned_start_time")
        en = p.get("explicit_end") or p.get("planned_end_time")
        if st and en:
            p["planned_start_time"] = st
            p["planned_end_time"] = en
            p["time_slot"] = f"{st}-{en}"
            try:
                p["planned_minutes"] = max(15, time_to_minutes(en) - time_to_minutes(st))
            except Exception:
                pass

    occupied_ranges = []
    for p in protected:
        st = p.get("planned_start_time")
        en = p.get("planned_end_time")
        if st and en:
            try:
                occupied_ranges.append((time_to_minutes(st), time_to_minutes(en)))
            except Exception:
                pass
    occupied_ranges.sort()

    if effective_start is None:
        # بعد از همه بازه‌ها هستیم — همه سرریز
        for p in free_parts:
            p["metadata"] = {**(p.get("metadata") or {}), "is_overflow": True}
        return protected + free_parts

    # محاسبه بازه‌های آزاد
    free_ranges = []
    for (slot_s, slot_e) in slot_ranges:
        cursor = max(slot_s, effective_start) if slot_s <= effective_start <= slot_e else slot_s
        if slot_e <= effective_start:
            continue
        cursor = max(cursor, effective_start)
        # کم کردن occupiedهای داخل بازه
        segments = [(cursor, slot_e)]
        for (os_, oe) in occupied_ranges:
            new_segments = []
            for (s, e) in segments:
                if oe <= s or os_ >= e:
                    new_segments.append((s, e))
                else:
                    if s < os_:
                        new_segments.append((s, os_))
                    if oe < e:
                        new_segments.append((oe, e))
            segments = new_segments
        free_ranges.extend([seg for seg in segments if seg[1] > seg[0]])

    free_ranges.sort()

    ordered_free = _sort_parts_by_priority(free_parts)
    placed = []
    overflow = []

    fr_idx = 0
    current_cursor = free_ranges[0][0] if free_ranges else None

    for p in ordered_free:
        dur = p.get("planned_minutes", 45)
        placed_ok = False

        while fr_idx < len(free_ranges):
            s, e = free_ranges[fr_idx]
            if current_cursor is None or current_cursor < s:
                current_cursor = s
            if current_cursor + dur <= e:
                p["planned_start_time"] = minutes_to_time(current_cursor)
                p["planned_end_time"] = minutes_to_time(current_cursor + dur)
                p["time_slot"] = f"{p['planned_start_time']}-{p['planned_end_time']}"
                p["metadata"] = {**(p.get("metadata") or {}), "is_overflow": False}
                current_cursor += dur
                placed.append(p)
                placed_ok = True
                break
            fr_idx += 1
            current_cursor = None

        if not placed_ok:
            p["metadata"] = {**(p.get("metadata") or {}), "is_overflow": True}
            overflow.append(p)

    result = protected + placed + overflow
    result.sort(key=lambda x: (
        0 if x.get("planned_start_time") else 1,
        time_to_minutes(x.get("planned_start_time") or "00:00") if x.get("planned_start_time") else 0,
        x.get("part_number", 0)
    ))
    for i, p in enumerate(result):
        p["part_number"] = i + 1
    return result


def split_by_time(parts: List[Dict], base_start_time: datetime,
                  start_limit: int = None,
                  source_date: str = None) -> Tuple[List[Dict], List[Dict]]:
    """
    بخش ۱۰ و ۲۰ — بازچینی بر اساس بازه‌های آزاد روزانه.
    پارهای محافظت‌شده دست‌نخورده.
    شروع از max(now, start_limit) در امروز.
    """
    if not parts:
        return [], []

    if source_date is None:
        source_date = get_today_date()

    now_min = get_iran_now().hour * 60 + get_iran_now().minute
    if start_limit is not None:
        start_min = start_limit
    else:
        start_min = base_start_time.hour * 60 + base_start_time.minute if base_start_time else now_min

    if source_date == get_today_date():
        start_min = max(start_min, now_min)

    protected = [p for p in parts if is_protected_part(p)]
    free = [p for p in parts if not is_protected_part(p)]

    for p in protected:
        st = p.get("planned_start_time") or p.get("explicit_start")
        en = p.get("planned_end_time") or p.get("explicit_end")
        if st and en:
            p["planned_start_time"] = st
            p["planned_end_time"] = en
            p["time_slot"] = f"{st}-{en}"
            try:
                p["planned_minutes"] = max(15, time_to_minutes(en) - time_to_minutes(st))
            except Exception:
                pass

    occupied = []
    for p in protected:
        st = p.get("planned_start_time")
        en = p.get("planned_end_time")
        if st and en:
            try:
                occupied.append((time_to_minutes(st), time_to_minutes(en)))
            except Exception:
                pass
    occupied.sort()

    gaps = []
    cursor = start_min
    for (s, e) in occupied:
        if e <= cursor:
            continue
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < DAY_END_MIN:
        gaps.append((cursor, DAY_END_MIN))

    ordered_free = _sort_parts_by_priority(free)
    placed = []
    overflow = []

    gi = 0
    current_cursor = gaps[0][0] if gaps else None

    for p in ordered_free:
        dur = p.get("planned_minutes", 45)
        ok = False
        while gi < len(gaps):
            gs, ge = gaps[gi]
            if current_cursor is None or current_cursor < gs:
                current_cursor = gs
            if current_cursor + dur <= ge:
                p["planned_start_time"] = minutes_to_time(current_cursor)
                p["planned_end_time"] = minutes_to_time(current_cursor + dur)
                p["time_slot"] = f"{p['planned_start_time']}-{p['planned_end_time']}"
                p["metadata"] = {**(p.get("metadata") or {}), "is_overflow": False}
                current_cursor += dur
                placed.append(p)
                ok = True
                break
            gi += 1
            current_cursor = None
        if not ok:
            p["metadata"] = {**(p.get("metadata") or {}), "is_overflow": True}
            overflow.append(p)

    scheduled = protected + placed
    scheduled.sort(key=lambda x: (
        0 if x.get("planned_start_time") else 1,
        time_to_minutes(x.get("planned_start_time") or "00:00") if x.get("planned_start_time") else 0,
        x.get("part_number", 0)
    ))
    for i, p in enumerate(scheduled):
        p["part_number"] = i + 1
    return scheduled, overflow


def add_parts_to_next_day(user_id: int, overflow: List[Dict],
                          source_date: str, max_days: int = 7) -> Dict:
    """بخش ۹.۱.B — انتقال سرریز به روزهای بعد."""
    if not overflow:
        return {"moved": 0, "dates": [], "not_fit": []}

    moved = 0
    dates_used = []
    remaining = [p for p in overflow if not is_protected_part(p)]

    try:
        base_date = datetime.strptime(source_date, "%Y-%m-%d").date()
    except Exception:
        base_date = get_iran_now().date()

    for day_offset in range(1, max_days + 1):
        if not remaining:
            break
        target_date = (base_date + timedelta(days=day_offset)).strftime("%Y-%m-%d")
        target_session_id = get_or_create_session(user_id, target_date)
        if not target_session_id:
            continue

        existing_plan = get_plan_by_date(user_id, target_date)
        existing_parts = existing_plan["parts"] if existing_plan else []

        occupied = []
        for ep in existing_parts:
            st = ep.get("planned_start_time") or ""
            en = ep.get("planned_end_time") or ""
            if st and en and not ep.get("completed"):
                try:
                    occupied.append((time_to_minutes(st), time_to_minutes(en)))
                except Exception:
                    pass
        occupied.sort()

        gaps = []
        cursor = 8 * 60
        for (s, e) in occupied:
            if e <= cursor:
                continue
            if s > cursor:
                gaps.append((cursor, s))
            cursor = max(cursor, e)
        if cursor < DAY_END_MIN:
            gaps.append((cursor, DAY_END_MIN))

        day_moved_any = False
        new_idx = 0
        for (gs, ge) in gaps:
            if new_idx >= len(remaining):
                break
            cursor = gs
            while new_idx < len(remaining) and cursor < ge:
                p = remaining[new_idx]
                dur = p.get("planned_minutes", 45)
                if cursor + dur <= ge:
                    add_part_to_session(target_session_id, {
                        "title": p.get("title"),
                        "planned_minutes": dur,
                        "time_slot": f"{minutes_to_time(cursor)}-{minutes_to_time(cursor + dur)}",
                        "planned_start_time": minutes_to_time(cursor),
                        "planned_end_time": minutes_to_time(cursor + dur),
                        "is_fixed_time": False,
                        "reason": p.get("reason", ""),
                        "pages": p.get("pages", 0),
                        "user_id": user_id,
                        "activity": p.get("activity", "study"),
                        "content_type": p.get("content_type", "book"),
                        "origin": p.get("origin", "ai"),
                        "subject": p.get("subject"),
                        "topic_resolved": p.get("topic_resolved"),
                        "detection_confidence": p.get("detection_confidence"),
                        "matched_card_id": p.get("matched_card_id"),
                        "metadata": {
                            "is_overflow": True,
                            "overflow_from_date": source_date,
                        }
                    })
                    cursor += dur
                    new_idx += 1
                    day_moved_any = True
                else:
                    break

        if day_moved_any:
            dates_used.append(target_date)
            moved += new_idx
            remaining = remaining[new_idx:]

    return {"moved": moved, "dates": dates_used, "not_fit": remaining}


def reschedule_parts(parts: List[Dict], base_start_time: datetime = None,
                     session_id: int = None,
                     user_id: int = None,
                     source_date: str = None,
                     time_slots: List[str] = None) -> List[Dict]:
    """بخش ۱۰ و ۱۹ و ۲۰ — بازچینی مرکزی."""
    if not parts:
        return parts

    if source_date is None:
        source_date = get_today_date()

    if time_slots:
        scheduled = schedule_parts_with_time_slots(parts, time_slots, source_date)
        overflow = [p for p in scheduled if (p.get("metadata") or {}).get("is_overflow")]
        scheduled = [p for p in scheduled if not (p.get("metadata") or {}).get("is_overflow")]
    else:
        scheduled, overflow = split_by_time(parts, base_start_time, source_date=source_date)

    if session_id:
        for p in scheduled:
            if p.get("completed") or is_protected_part(p):
                if not is_protected_part(p):
                    continue
            try:
                execute_query(
                    """UPDATE study_parts
                       SET planned_start_time = %s, planned_end_time = %s,
                           time_slot = %s, part_number = %s
                       WHERE part_id = %s""",
                    (p.get("planned_start_time"), p.get("planned_end_time"),
                     p.get("time_slot"), p.get("part_number"), p.get("part_id"))
                )
            except Exception as e:
                logger.error(f"خطا در ذخیره زمان‌بندی: {e}")

    if overflow and user_id and source_date:
        result = add_parts_to_next_day(user_id, overflow, source_date)
        logger.info(f"سرریز: {result['moved']} پارت به {result['dates']}")
        # اطلاع‌رسانی
        if result["moved"] > 0 and user_id:
            try:
                shamsi_list = [get_shamsi_date(d) for d in result["dates"]]
                logger.info(f"overflow_notice user={user_id} dates={shamsi_list}")
            except Exception:
                pass

    return scheduled


# ==================== تداخل ====================
def detect_conflicts(user_id: int, target_date: str, new_parts: List[Dict]) -> List[Dict]:
    existing = get_plan_by_date(user_id, target_date)
    if not existing:
        return []
    conflicts = []
    for new_part in new_parts:
        new_start = new_part.get("planned_start_time") or new_part.get("explicit_start")
        new_end = new_part.get("planned_end_time") or new_part.get("explicit_end")
        if not new_start or not new_end:
            continue
        try:
            ns = time_to_minutes(new_start)
            ne = time_to_minutes(new_end)
        except Exception:
            continue
        for old_part in existing["parts"]:
            os_ = old_part.get("planned_start_time")
            oe = old_part.get("planned_end_time")
            if not os_ or not oe:
                continue
            try:
                o_s = time_to_minutes(os_)
                o_e = time_to_minutes(oe)
            except Exception:
                continue
            overlap_start = max(ns, o_s)
            overlap_end = min(ne, o_e)
            if overlap_start < overlap_end:
                overlap = overlap_end - overlap_start
                if is_reminder_part(old_part):
                    ctype = "reminder"
                elif is_protected_part(old_part):
                    ctype = "fixed"
                elif old_part.get("completed"):
                    ctype = "completed"
                else:
                    ctype = "normal"
                conflicts.append({
                    "type": ctype, "old_part": old_part,
                    "new_part": new_part, "overlap_minutes": overlap,
                })
    return conflicts


def categorize_conflicts(conflicts: List[Dict]) -> Dict:
    return {
        "critical": [c for c in conflicts if c["type"] == "fixed"],
        "important": [c for c in conflicts if c["type"] == "reminder"],
        "completed": [c for c in conflicts if c["type"] == "completed"],
        "auto": [c for c in conflicts if c["type"] == "normal"],
    }


def log_conflict(user_id: int, conflict: Dict, resolution: str) -> None:
    try:
        execute_query(
            """INSERT INTO conflict_log
               (user_id, date, conflict_type, part_id, related_part_id,
                overlap_minutes, resolution)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (user_id, get_today_date(), conflict.get("type"),
             conflict.get("old_part", {}).get("part_id"),
             conflict.get("new_part", {}).get("part_id"),
             conflict.get("overlap_minutes"), resolution)
        )
    except Exception as e:
        logger.error(f"خطا در log_conflict: {e}")


# ==================== یادآورها ====================
def schedule_reminder_notifications(part_id: int, user_id: int,
                                     remind_at: datetime) -> None:
    try:
        now = get_iran_now()
        if remind_at <= now:
            execute_query(
                """INSERT INTO reminder_notifications
                   (part_id, user_id, notify_type, scheduled_at, status)
                   VALUES (%s, %s, 'overdue', %s, 'pending')
                   ON CONFLICT (part_id, notify_type) DO NOTHING""",
                (part_id, user_id, now.replace(tzinfo=None))
            )
            return
        notifications = []
        day_before = (remind_at - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
        if day_before > now:
            notifications.append(("day_before", day_before))
        hb = remind_at - timedelta(hours=3)
        if hb > now:
            notifications.append(("hours_before", hb))
        ob = remind_at - timedelta(hours=1)
        if ob > now:
            notifications.append(("hour_before", ob))
        if remind_at > now:
            notifications.append(("at_time", remind_at))
        overdue = remind_at + timedelta(hours=OVERDUE_GRACE_HOURS)
        notifications.append(("overdue", overdue))

        for ntype, scheduled in notifications:
            execute_query(
                """INSERT INTO reminder_notifications
                   (part_id, user_id, notify_type, scheduled_at, status)
                   VALUES (%s, %s, %s, %s, 'pending')
                   ON CONFLICT (part_id, notify_type) DO NOTHING""",
                (part_id, user_id, ntype, scheduled.replace(tzinfo=None))
            )
        logger.info(f"✅ {len(notifications)} یادآوری برای پارت {part_id} زمان‌بندی شد")
    except Exception as e:
        logger.error(f"❌ خطا در schedule_reminder_notifications: {e}")


def save_user_reminder_from_ai(user_id: int, reminder_data: Dict) -> Optional[Dict]:
    try:
        title = reminder_data.get("title", "یادآور")
        message = reminder_data.get("message") or title
        time_str = reminder_data.get("time")
        date_gregorian = reminder_data.get("date_gregorian")
        date_shamsi = reminder_data.get("date")

        target_date = None
        if date_gregorian:
            try:
                target_date = datetime.strptime(date_gregorian, "%Y-%m-%d").date()
            except Exception:
                pass
        if not target_date and date_shamsi:
            try:
                jd = jdatetime.datetime.strptime(date_shamsi, "%Y/%m/%d")
                target_date = jd.togregorian().date()
            except Exception:
                pass
        if not target_date:
            target_date = get_iran_now().date() + timedelta(days=1)

        date_str = target_date.strftime("%Y-%m-%d")
        session_id = get_or_create_session(user_id, date_str)
        if not session_id:
            return None

        planned_start = None
        planned_end = None
        time_slot = ""
        if time_str:
            try:
                h, m = map(int, normalize_time_str(time_str).split(":"))
                planned_start = f"{h:02d}:{m:02d}"
                end_min = min(h * 60 + m + 15, DAY_END_MIN)
                planned_end = minutes_to_time(end_min)
                time_slot = f"{planned_start}-{planned_end}"
            except Exception:
                pass

        part_data = {
            "title": f"🔔 {title}",
            "planned_minutes": 15,
            "time_slot": time_slot,
            "planned_start_time": planned_start,
            "planned_end_time": planned_end,
            "is_fixed_time": bool(planned_start),
            "explicit_start": planned_start,
            "explicit_end": planned_end,
            "reason": message, "pages": 0, "user_id": user_id,
            "activity": "reminder", "content_type": "reminder",
            "origin": "reminder",
            "metadata": {"is_reminder": True, "message": message, "ai_generated": True}
        }
        part_id = add_part_to_session(session_id, part_data)
        if not part_id:
            return None

        card_data = {"title": title, "message": message,
                     "date": date_str, "time": planned_start or "", "part_id": part_id}
        card_id = save_card(
            card_type="reminder", content_type="reminder",
            data=card_data, priority=7, raw_text=message, created_by=user_id
        )
        if card_id:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(index_card_to_rag(card_id))
                else:
                    loop.run_until_complete(index_card_to_rag(card_id))
            except Exception:
                pass

        if planned_start:
            try:
                h, m = map(int, planned_start.split(":"))
                remind_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=h, minute=m)
                remind_dt = IRAN_TZ.localize(remind_dt)
                schedule_reminder_notifications(part_id, user_id, remind_dt)
            except Exception as e:
                logger.error(f"خطا در زمان‌بندی یادآور: {e}")

        return {"session_id": session_id, "part_id": part_id, "card_id": card_id,
                "date": date_str, "date_shamsi": get_shamsi_date(date_str)}
    except Exception as e:
        logger.error(f"❌ خطا در ذخیره یادآور: {e}")
        return None


def build_reminder_message(notify_type: str, title: str,
                            planned_start: str, planned_end: str,
                            reason: str, metadata: Dict,
                            session_date) -> Optional[str]:
    clean_title = (title or "").replace("🔔 ", "").strip()
    msg_text = (metadata or {}).get("message") or reason or ""
    if msg_text == clean_title:
        msg_text = ""

    time_info = ""
    if planned_start:
        time_info = f"⏰ ساعت {planned_start}"
        if planned_end:
            time_info += f" - {planned_end}"

    date_info = ""
    if session_date:
        try:
            shamsi = get_shamsi_date(str(session_date))
            parts = shamsi.split("/")
            if len(parts) == 3:
                date_info = f"📅 {parts[2]} {get_shamsi_month_name(str(session_date))}"
        except Exception:
            pass

    if notify_type == "day_before":
        t = f"🔔 **یادآوری فردا**\n\n📌 {clean_title}\n"
        if time_info:
            t += f"{time_info}\n"
        if msg_text:
            t += f"\n💬 {msg_text}\n"
        return t + "\nفردا این رو یادت نره! 🌟"
    if notify_type == "hours_before":
        t = f"🔔 **۳ ساعت تا {clean_title}**\n\n"
        if time_info:
            t += f"{time_info}\n"
        if date_info:
            t += f"{date_info}\n"
        if msg_text:
            t += f"\n💬 {msg_text}\n"
        return t + "\nیه آماده‌سازی کوچیک بد نیست. 💪"
    if notify_type == "hour_before":
        t = f"⏰ **۱ ساعت تا {clean_title}**\n\n"
        if time_info:
            t += f"{time_info}\n"
        if msg_text:
            t += f"\n💬 {msg_text}\n"
        return t + "\nآماده‌ای؟ 🚀"
    if notify_type == "at_time":
        t = f"🔔 **الان وقتشه!**\n\n📌 {clean_title}\n"
        if time_info:
            t += f"{time_info}\n"
        if msg_text:
            t += f"\n💬 {msg_text}\n"
        return t + "\nموفق باشی! 💚"
    if notify_type == "overdue":
        t = f"⚠️ **یادآور انجام نشده!**\n\n📌 {clean_title}\n"
        if time_info:
            t += f"{time_info}\n"
        if msg_text:
            t += f"\n💬 {msg_text}\n"
        return t + "\nمی‌خوای الان انجامش بدی یا بندازیش بعد؟"
    if notify_type == "snooze":
        t = f"⏰ **یادآوری مجدد**\n\n📌 {clean_title}\n"
        if msg_text:
            t += f"\n💬 {msg_text}\n"
        return t
    if notify_type == "class_reminder":
        t = f"🔔 **کلاس نزدیکه**\n\n🏫 {clean_title}\n"
        if time_info:
            t += f"{time_info}\n"
        if msg_text:
            t += f"\n💬 {msg_text}\n"
        return t
    return None


# ==================== تشخیص موضوع (RAG-based) ====================
def normalize_title_for_cache(title: str) -> str:
    t = (title or "").strip().lower()
    t = re.sub(r'[۰-۹]', lambda m: str(ord(m.group()) - ord('۰')), t)
    t = re.sub(r'[0-9]+', '', t)
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r'[^\w\s\u0600-\u06FF]', '', t)
    return t.strip()[:200]


def get_cached_detection(title: str) -> Optional[Dict]:
    normalized = normalize_title_for_cache(title)
    if not normalized:
        return None
    result = execute_query(
        """SELECT subject, topic, activity_type, confidence, matched_card_id
           FROM subject_detection_cache WHERE normalized_title = %s""",
        (normalized,), fetch=True
    )
    if not result:
        return None
    execute_query(
        "UPDATE subject_detection_cache SET hit_count = hit_count + 1, updated_at = CURRENT_TIMESTAMP WHERE normalized_title = %s",
        (normalized,)
    )
    return {"subject": result[0], "topic": result[1], "activity_type": result[2],
            "confidence": result[3] or 0, "matched_card_id": result[4]}


def save_detection_cache(title: str, subject: str, topic: str,
                          activity_type: str, confidence: float,
                          matched_card_id: int = None) -> None:
    normalized = normalize_title_for_cache(title)
    if not normalized:
        return
    try:
        execute_query(
            """INSERT INTO subject_detection_cache
               (normalized_title, subject, topic, activity_type, confidence, matched_card_id)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (normalized_title) DO UPDATE SET
               subject = EXCLUDED.subject, topic = EXCLUDED.topic,
               activity_type = EXCLUDED.activity_type,
               confidence = EXCLUDED.confidence,
               matched_card_id = EXCLUDED.matched_card_id,
               hit_count = subject_detection_cache.hit_count + 1,
               updated_at = CURRENT_TIMESTAMP""",
            (normalized, subject, topic, activity_type, confidence, matched_card_id)
        )
    except Exception as e:
        logger.error(f"خطا در ذخیره کش تشخیص: {e}")


async def detect_subject_for_part(title: str, activity_hint: str = None,
                                    user_data: Dict = None) -> Dict:
    result = {"subject": None, "topic": None,
              "activity_type": activity_hint or "study",
              "confidence": 0.0, "matched_card_id": None}
    if not title or len(title.strip()) < 2:
        return result
    user_id = user_data.get("id") if user_data else None

    cached = get_cached_detection(title)
    if cached and cached.get("confidence", 0) >= 0.7:
        return cached

    try:
        rag_results = await search_rag(title, user_id=user_id, top_k=5)
    except Exception:
        rag_results = []

    card_results = [r for r in rag_results if r.get("source", "").startswith("card_")]
    if card_results:
        best = card_results[0]
        if best.get("similarity", 0) >= 0.75:
            card_id = None
            try:
                card_id = int(best["source"].replace("card_", ""))
            except Exception:
                pass
            card_data = execute_query("SELECT data FROM cards WHERE id = %s",
                                       (card_id,), fetch=True)
            if card_data:
                data = card_data[0] or {}
                result["subject"] = data.get("subject")
                result["topic"] = data.get("topic") or data.get("title")
                result["confidence"] = best.get("similarity", 0)
                result["matched_card_id"] = card_id
                save_detection_cache(title, result["subject"], result["topic"],
                                     result["activity_type"], result["confidence"], card_id)
                return result

    cards_context = ""
    if card_results:
        cards_context = "\n=== کارت‌های مرتبط ===\n"
        for i, r in enumerate(card_results[:3], 1):
            cards_context += f"\n[{i}] ({int(r.get('similarity', 0)*100)}%):\n{r.get('content', '')[:400]}\n"

    if user_id:
        cond, admin_params = _access_condition("created_by")
        subjects_list = execute_query(
            f"""SELECT DISTINCT data->>'subject' FROM cards
                WHERE card_type IN ('microtopic', 'syllabus', 'resource')
                  AND data->>'subject' IS NOT NULL AND is_active = TRUE AND {cond}
                ORDER BY 1 LIMIT 30""",
            tuple([user_id] + admin_params), fetchall=True
        )
    else:
        subjects_list = []
    subjects_text = ", ".join([s[0] for s in (subjects_list or []) if s[0]]) or "نامشخص"

    profile_text = ""
    if user_data:
        profile_text = f"\nپایه: {user_data.get('grade', 'نامشخص')}\nرشته: {user_data.get('field', 'نامشخص')}"

    prompt = f"""تو یک تشخیص‌دهنده درس و نوع فعالیت هستی.

=== عنوان پارت ===
"{title}"

{f"=== راهنمای نوع فعالیت ==={chr(10)}کاربر گفته: {activity_hint}" if activity_hint else ""}
{cards_context}

=== لیست دروس موجود ===
{subjects_text}

=== پروفایل ==={profile_text}

=== وظیفه ===
1. درس رو از کارت‌های بالا استخراج کن
2. مبحث دقیق رو انتخاب کن
3. اگر راهنما دادی استفاده کن، وگرنه از عنوان تشخیص بده
4. اگر هیچ کارتی نمی‌خونه، subject=null

=== خروجی JSON ===
{{
  "subject": "نام درس" یا null,
  "topic": "مبحث" یا null,
  "activity_type": "study|test|review|class|video|exercise|rest|fun|other",
  "confidence": 0.0 تا 1.0,
  "reasoning": "توضیح کوتاه"
}}
⚠️ فقط JSON خالص."""
    try:
        model_type = get_user_model_type(user_data.get("id", 0)) if user_data else "light"
        response = await call_ai(prompt, max_tokens=500, temperature=0.1, model_type=model_type)
        if response:
            parsed = safe_json_loads(response, dict)
            if parsed:
                result["subject"] = parsed.get("subject")
                result["topic"] = parsed.get("topic")
                if not activity_hint and parsed.get("activity_type"):
                    result["activity_type"] = parsed.get("activity_type")
                result["confidence"] = parsed.get("confidence", 0.5)
                if result["subject"]:
                    save_detection_cache(title, result["subject"], result["topic"],
                                         result["activity_type"], result["confidence"],
                                         result["matched_card_id"])
    except Exception as e:
        logger.error(f"خطا در تشخیص با AI: {e}")
    return result


# ==================== Fallback تشخیص مباحث ====================
def detect_topics_fallback_regex(text: str, user_data: Dict) -> List[Dict]:
    topics = []
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    for line in lines:
        if re.search(r'^(مجموعا:|من مبحثی|focus)', line, re.IGNORECASE):
            continue
        line = re.sub(r'^[✅️🔬⚡📘📖🟢🔵🟣]+\s*', '', line)

        topic = {
            "title": "", "subject": "", "topic": None, "grade_num": None,
            "pages": 0, "test_count": 0, "hours": 0, "minutes": 0,
            "activity": "study", "source": None, "priority": "normal",
            "explicit_start": None, "explicit_end": None, "is_fixed_time": False,
            "depends_on": None, "time_hint": None, "reason": "regex",
            "parts_count": 1,
        }

        # بخش ۱۸.۲ — تشخیص «در N بخش» و ...
        pc_match = re.search(
            r'(?:در\s*)?([۰-۹0-9]+)\s*(?:بخش|جلسه|بار|قسمت)',
            line
        )
        if pc_match:
            pc = convert_persian_to_int(pc_match.group(1))
            if pc and 1 <= pc <= 10:
                topic["parts_count"] = pc
                line = re.sub(
                    r'(?:در\s*)?[۰-۹0-9]+\s*(?:بخش|جلسه|بار|قسمت)',
                    '', line, count=1
                )
        also_split = re.search(r'تقسیم\s*به\s*([۰-۹0-9]+)', line)
        if also_split:
            pc = convert_persian_to_int(also_split.group(1))
            if pc and 1 <= pc <= 10:
                topic["parts_count"] = pc
                line = re.sub(r'تقسیم\s*به\s*[۰-۹0-9]+', '', line, count=1)

        page_range = re.search(r'(?:از\s*)?([۰-۹0-9]+)\s*(?:تا|الی)\s*([۰-۹0-9]+)', line)
        if page_range:
            start = convert_persian_to_int(page_range.group(1))
            end = convert_persian_to_int(page_range.group(2))
            if start and end:
                topic["pages"] = end - start + 1
                line = re.sub(r'(?:از\s*)?[۰-۹0-9]+\s*(?:تا|الی)\s*[۰-۹0-9]+', '', line)

        grade_patterns = {"دهم": r'(دهم|۱۰|10)', "یازدهم": r'(یازدهم|۱۱|11)',
                          "دوازدهم": r'(دوازدهم|۱۲|12)'}
        for gname, pattern in grade_patterns.items():
            if re.search(pattern, line):
                topic["grade_num"] = gname
                line = re.sub(pattern, '', line)
                break

        explicit = re.search(r'(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})', line)
        if explicit:
            topic["explicit_start"] = f"{int(explicit.group(1)):02d}:{explicit.group(2)}"
            topic["explicit_end"] = f"{int(explicit.group(3)):02d}:{explicit.group(4)}"
            topic["is_fixed_time"] = True
            line = re.sub(r'\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}', '', line)

        h_match = re.search(r'(\d+)\s*(ساعت|h)', line)
        if h_match:
            topic["hours"] = int(h_match.group(1))
            line = re.sub(r'\d+\s*(ساعت|h)', '', line)
        m_match = re.search(r'(\d+)\s*(دقیقه|د)', line)
        if m_match:
            topic["minutes"] = int(m_match.group(1))
            line = re.sub(r'\d+\s*(دقیقه|د)', '', line)

        test_match = re.search(r'(\d+)\s*(تست|سوال|تمرین)', line)
        if test_match:
            topic["test_count"] = int(test_match.group(1))
            line = re.sub(r'\d+\s*(تست|سوال|تمرین)', '', line)
            topic["activity"] = "test"

        if topic["pages"] == 0:
            p_match = re.search(r'(\d+)\s*(صفحه|برگ|ص)(?!\w)', line)
            if p_match:
                topic["pages"] = int(p_match.group(1))
                line = re.sub(r'\d+\s*(صفحه|برگ|ص)', '', line)

        source_patterns = [(r'(خیلی\s*سبز)', 'خیلی سبز'), (r'(مبتکران)', 'مبتکران'),
                           (r'(الگو)', 'الگو'), (r'(گاج)', 'گاج'), (r'(جزوه)', 'جزوه'),
                           (r'(ماز)', 'ماز'), (r'(قلمچی)', 'قلمچی')]
        for pattern, sname in source_patterns:
            if re.search(pattern, line):
                topic["source"] = sname
                line = re.sub(pattern, '', line)
                break

        activity_patterns = [
            (r'(تست|سوال|تمرین|مسئله)', 'test'),
            (r'(مرور|بازخوانی|تکرار|روتین|روزانه)', 'review'),
            (r'(تشریحی|تحلیلی)', 'study'),
            (r'(ورزش|باشگاه|gym|پیاده‌روی|شنا)', 'exercise'),
            (r'(استراحت|خواب|ناهار|شام)', 'rest'),
            (r'(تفریح|بازی|فیلم)', 'fun'),
            (r'(کلاس)', 'class'),
        ]
        for pattern, aname in activity_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                topic["activity"] = aname
                break

        paren_match = re.search(r'[\(（]([^\)）]+)[\)）]', line)
        if paren_match:
            topic["topic"] = paren_match.group(1).strip()
            line = re.sub(r'[\(（][^\)）]+[\)）]', '', line)

        title = re.sub(r'[:：،,\-–—]', ' ', line).strip()
        title = re.sub(r'\s+', ' ', title).strip()
        if not title or len(title) < 2:
            continue
        topic["title"] = title
        topic["subject"] = title
        topics.append(topic)
    return topics


# ==================== AI Plan Prompts ====================
def build_ai_detection_prompt(text: str, user_data: Dict, time_slots: List[str]) -> str:
    grade = user_data.get('grade', 'نامشخص') if user_data else 'نامشخص'
    field = user_data.get('field', 'نامشخص') if user_data else 'نامشخص'
    goal = user_data.get('goal', 'نامشخص') if user_data else 'نامشخص'
    weak = ", ".join(user_data.get('weak_subjects', [])) if user_data else "ندارد"
    strong = ", ".join(user_data.get('strong_subjects', [])) if user_data else "ندارد"
    providers = ", ".join(user_data.get('providers', [])) if user_data else "ندارد"

    time_context = ""
    if time_slots:
        total_min = total_slot_minutes(time_slots)
        time_context = "\n\n⚠️ بازه‌های زمانی:\n"
        time_context += "\n".join([f"  - {s}" for s in time_slots])
        time_context += f"\n\n⏱ مجموع: {total_min} دقیقه (بازه‌ها ناپیوسته‌اند)"

    role_prefix = build_role_prefix(user_data.get("id") if user_data else None)

    return f"""{role_prefix}
شما یک متخصص تحلیل متن و تشخیص مباحث مطالعه هستید.

=== کاربر ===
پایه: {grade} | رشته: {field} | هدف: {goal}
ضعف: {weak} | قوت: {strong} | منابع: {providers}
{time_context}

=== متن کاربر ===
"{text}"

=== وظیفه ===
هر خط را دقیق تحلیل کن و JSON بده.

⚠️ فیلد جدید `parts_count`:
- "در N بخش" / "N جلسه" / "N بار" / "تقسیم به N" → parts_count = N
- پیش‌فرض = 1
- این عبارت‌ها از title حذف شوند

=== قوانین ===
✅ هر خط = یک آیتم
✅ "ورزش"/"استراحت" را درس بگذار
✅ مباحث جدا با + یا کاما = آیتم جدا
✅ عنوان هرگز "N جلسه" ندارد

=== خروجی JSON ===
[
  {{
    "title": "عنوان تمیز",
    "subject": "درس",
    "topic": null,
    "grade_num": null,
    "pages": 0,
    "test_count": 0,
    "hours": 0,
    "minutes": 0,
    "activity": "study",
    "source": null,
    "priority": "normal",
    "explicit_start": null,
    "explicit_end": null,
    "is_fixed_time": false,
    "depends_on": null,
    "time_hint": null,
    "parts_count": 1,
    "reason": ""
  }}
]

⚠️ فقط JSON خالص برگردان."""


async def detect_topics_with_ai(text: str, user_data: Dict, time_slots: List[str]) -> Optional[List[Dict]]:
    prompt = build_ai_detection_prompt(text, user_data, time_slots)
    try:
        model_type = get_user_model_type(user_data.get("id", 0)) if user_data else "light"
        response = await call_ai(prompt, max_tokens=2000, temperature=0.2, model_type=model_type)
        if not response:
            return None
        topics = safe_json_loads(response, list)
        if not topics:
            return None
        return [t for t in topics if isinstance(t, dict)]
    except Exception as e:
        logger.error(f"❌ خطا در تشخیص AI: {e}")
        return None


# ==================== finalize_detected_topics با parts_count ====================
def finalize_detected_topics(
    topics: List[Dict], user_data: Dict, default_duration: int = 45,
    time_slots: List[str] = None, total_slot_min: int = 0
) -> List[Dict]:
    result = []
    weak = user_data.get('weak_subjects', []) if user_data else []
    use_slot_mode = bool(time_slots) and total_slot_min > 0
    largest_slot = get_largest_slot_minutes(time_slots or [])

    # اعتبارسنجی
    clean_topics = [t for t in (topics or []) if isinstance(t, dict)]
    if not clean_topics:
        return []

    # اعمال parts_count در ابتدا
    expanded = []
    for t in clean_topics:
        try:
            pc = int(t.get("parts_count") or 1)
        except (ValueError, TypeError):
            pc = 1
        pc = max(1, min(10, pc))

        base_title = (t.get("title") or t.get("subject") or "مطالعه").strip()
        base_title = re.sub(r'\s*[\(\（]?(?:در\s*)?[۰-۹0-9]+\s*(?:بخش|جلسه|بار|قسمت)[\)\）]?\s*', ' ', base_title).strip()
        base_title = re.sub(r'\s*تقسیم\s*به\s*[۰-۹0-9]+\s*', ' ', base_title).strip()
        if not base_title:
            base_title = t.get("subject") or "مطالعه"

        if pc > 1:
            total_min = (t.get('hours', 0) or 0) * 60 + (t.get('minutes', 0) or 0)
            if total_min == 0 and (t.get('pages', 0) or 0) > 0:
                total_min = (t['pages'] or 0) * 6
            if total_min == 0 and (t.get('test_count', 0) or 0) > 0:
                total_min = int((t['test_count'] or 0) * 1.5)
            if total_min == 0:
                total_min = default_duration * pc
            per_min = max(15, total_min // pc)
            pages = (t.get('pages', 0) or 0)
            tests = (t.get('test_count', 0) or 0)
            for i in range(pc):
                newt = dict(t)
                newt["title"] = f"{base_title} (بخش {i+1}/{pc})"
                newt["hours"] = 0
                newt["minutes"] = per_min
                newt["parts_count"] = 1
                if pages > 0:
                    newt["pages"] = max(1, pages // pc)
                if tests > 0:
                    newt["test_count"] = max(1, tests // pc)
                expanded.append(newt)
        else:
            t["title"] = base_title
            t["parts_count"] = 1
            expanded.append(t)

    # اگر در حالت slot و پارت از بزرگ‌ترین بازه بزرگ‌تره → تقسیم خودکار
    if use_slot_mode and largest_slot > 0:
        final_expanded = []
        for t in expanded:
            total_min = (t.get('hours', 0) or 0) * 60 + (t.get('minutes', 0) or 0)
            if total_min == 0:
                if (t.get('pages', 0) or 0) > 0:
                    total_min = (t['pages'] or 0) * 6
                elif (t.get('test_count', 0) or 0) > 0:
                    total_min = int((t['test_count'] or 0) * 1.5)
                else:
                    total_min = default_duration
            if total_min > largest_slot and (t.get("parts_count", 1) == 1):
                # خودکار تقسیم به بخش‌های <= 90 دقیقه
                target_chunk = min(90, largest_slot - 5 if largest_slot > 95 else 60)
                n = max(2, (total_min + target_chunk - 1) // target_chunk)
                base = t["title"]
                base = re.sub(r'\s*\(بخش \d+/\d+\)\s*', '', base).strip()
                per = max(15, total_min // n)
                pages = (t.get('pages', 0) or 0)
                tests = (t.get('test_count', 0) or 0)
                for i in range(n):
                    nt = dict(t)
                    nt["title"] = f"{base} (بخش {i+1}/{n})"
                    nt["hours"] = 0
                    nt["minutes"] = per
                    nt["parts_count"] = 1
                    if pages > 0:
                        nt["pages"] = max(1, pages // n)
                    if tests > 0:
                        nt["test_count"] = max(1, tests // n)
                    final_expanded.append(nt)
            else:
                final_expanded.append(t)
        expanded = final_expanded

    # محاسبه مدت و آپشن‌ها
    if use_slot_mode:
        fixed_minutes = 0
        weights = []
        for t in expanded:
            pages = t.get('pages', 0) or 0
            tests = t.get('test_count', 0) or 0
            hours = t.get('hours', 0) or 0
            minutes = t.get('minutes', 0) or 0
            explicit_min = hours * 60 + minutes
            if explicit_min > 0:
                w = explicit_min
            elif pages > 0:
                w = pages * 6
            elif tests > 0:
                w = int(tests * 1.5)
            else:
                w = default_duration
            weights.append(max(20, w))

        for i, t in enumerate(expanded):
            if t.get('is_fixed_time') and t.get('explicit_start') and t.get('explicit_end'):
                try:
                    s = time_to_minutes(t['explicit_start'])
                    e = time_to_minutes(t['explicit_end'])
                    fixed_minutes += (e - s)
                    weights[i] = e - s
                except Exception:
                    pass

        remaining_min = max(0, total_slot_min - fixed_minutes)
        free_indices = [i for i, t in enumerate(expanded)
                        if not (t.get('is_fixed_time') and t.get('explicit_start'))]
        total_free_weight = sum(weights[i] for i in free_indices) if free_indices else 0

        if free_indices and total_free_weight > 0:
            for i in free_indices:
                ratio = weights[i] / total_free_weight
                weights[i] = max(20, int(remaining_min * ratio))

        for i, t in enumerate(expanded):
            duration = max(20, weights[i])
            subject = t.get('subject') or t.get('title') or 'مطالعه'
            priority = t.get('priority', 'normal')
            if any(w in subject for w in weak):
                priority = 'high'
            result.append({
                "title": t.get('title', 'مطالعه'),
                "subject": subject,
                "topic": t.get('topic'),
                "grade_num": t.get('grade_num'),
                "pages": t.get('pages', 0) or 0,
                "test_count": t.get('test_count', 0) or 0,
                "total_minutes": duration,
                "part_duration": duration,
                "parts_count": 1,
                "priority": priority,
                "activity": t.get('activity', 'study'),
                "source": t.get('source') or 'کتاب',
                "reason": t.get('reason', ''),
                "time_hint": t.get('time_hint'),
                "explicit_start": t.get('explicit_start'),
                "explicit_end": t.get('explicit_end'),
                "is_fixed_time": bool(t.get('is_fixed_time')),
                "depends_on": t.get('depends_on')
            })
    else:
        for t in expanded:
            pages = t.get('pages', 0) or 0
            tests = t.get('test_count', 0) or 0
            hours = t.get('hours', 0) or 0
            minutes = t.get('minutes', 0) or 0
            total_min = hours * 60 + minutes
            if total_min == 0 and pages > 0:
                total_min = pages * 6
            if total_min == 0 and tests > 0:
                total_min = int(tests * 1.5)
            if total_min == 0:
                total_min = default_duration
            total_min = max(20, total_min)
            subject = t.get('subject') or t.get('title') or 'مطالعه'
            priority = t.get('priority', 'normal')
            if any(w in subject for w in weak):
                priority = 'high'
            result.append({
                "title": t.get('title', 'مطالعه'),
                "subject": subject,
                "topic": t.get('topic'),
                "grade_num": t.get('grade_num'),
                "pages": pages,
                "test_count": tests,
                "total_minutes": total_min,
                "part_duration": total_min,
                "parts_count": 1,
                "priority": priority,
                "activity": t.get('activity', 'study'),
                "source": t.get('source') or 'کتاب',
                "reason": t.get('reason', ''),
                "time_hint": t.get('time_hint'),
                "explicit_start": t.get('explicit_start'),
                "explicit_end": t.get('explicit_end'),
                "is_fixed_time": bool(t.get('is_fixed_time')),
                "depends_on": t.get('depends_on')
            })
    return result


# ==================== AI Plan Prompt ====================
def build_plan_prompt(user_data: Dict, user_id: int, cards: List[Dict],
                      history_data: List[Dict], user_request: str = "",
                      rag_context: str = "",
                      active_project: Optional[Dict] = None,
                      target_date: str = None) -> str:
    weak = ", ".join(user_data.get("weak_subjects", [])) or "ندارد"
    strong = ", ".join(user_data.get("strong_subjects", [])) or "ندارد"
    providers = ", ".join(user_data.get("providers", [])) or "ندارد"
    role_prefix = build_role_prefix(user_id)

    today_shamsi = get_today_shamsi()
    today_greg = get_today_date()
    today_weekday = get_weekday_fa(get_iran_now().date())
    target_shamsi = get_shamsi_date(target_date or today_greg)

    project_block = ""
    if active_project:
        pd = active_project.get("data", {}) or {}
        subs = pd.get("subjects", [])
        if isinstance(subs, list):
            subs_text = ", ".join(subs)
        else:
            subs_text = str(subs)
        project_block = f"""
=== 🎯 پروژه فعال کاربر (اولویت اول) ===
عنوان: {pd.get('title', '')}
ارائه‌دهنده: {pd.get('provider', '')}
درس‌ها: {subs_text}
تاریخ آزمون: {pd.get('exam_date_shamsi', '')}
goals: {', '.join(pd.get('goals', []) if isinstance(pd.get('goals'), list) else [])}
commitment: {pd.get('commitment', {})}
schedule_pattern: {pd.get('schedule_pattern', {})}
⚠️ بیش از ۷۰٪ پارتها باید حول این پروژه باشند. کارت‌های ادمین فقط تکمیل جزئیات.
"""

    cards_text = ""
    for card in cards:
        card_type = card.get("card_type", "")
        data = card.get("data", {})
        created_by = card.get("created_by")
        origin_tag = "🔵 کارت شخصی" if created_by == user_id else "🟢 کارت سیستمی/ادمین"

        if card_type == "exam":
            subjects = data.get("subjects", [])
            sy = ""
            for s in subjects:
                if isinstance(s, dict):
                    sy += f"    - {s.get('name', '')}: {', '.join(s.get('syllabus', []))}\n"
            cards_text += f"\n📌 آزمون #{card['id']} [{origin_tag}]:\n"
            cards_text += f"  عنوان: {data.get('title', '')}\n"
            cards_text += f"  تاریخ: {data.get('exam_date_shamsi', '')}\n"
            cards_text += f"  ارائه‌دهنده: {data.get('provider', '')}\n  مباحث:\n{sy}\n"
        elif card_type == "class":
            sch = ""
            for s in data.get("schedule", []):
                if isinstance(s, dict):
                    sch += f"    - {s.get('day_of_week', '')} {s.get('start_time', '')}-{s.get('end_time', '')}\n"
            cards_text += (
                f"\n📌 کلاس #{card['id']} [{origin_tag}]:\n"
                f"  عنوان: {data.get('title', '')}\n"
                f"  ارائه‌دهنده: {data.get('provider', '')}\n"
                f"  درس: {data.get('subject', '')}\n"
                f"  ⚠️ زمان ثابت — با explicit_start/end و is_fixed_time=true.\n"
                f"  برنامه:\n{sch}\n"
            )
        elif card_type == "rule":
            trigger = data.get("trigger", {})
            action = data.get("action", {})
            cards_text += (
                f"\n📌 قانون #{card['id']} [{origin_tag}]:\n"
                f"  {data.get('title', '')} | محرک: {trigger.get('subjects', [])} | "
                f"دستور: {action.get('instruction', '')}\n"
            )
        elif card_type == "resource":
            ch = ""
            for c in data.get("chapters", []):
                if isinstance(c, dict):
                    ch += f"    - {c.get('title', '')}\n"
            cards_text += (
                f"\n📌 منبع #{card['id']} [{origin_tag}]:\n"
                f"  {data.get('title', '')} | {data.get('subject', '')} | "
                f"{data.get('provider', '')}\n  فصل‌ها:\n{ch}\n"
            )
        elif card_type == "syllabus":
            syl = data.get("syllabus", [])
            cards_text += (
                f"\n📌 سرفصل #{card['id']} [{origin_tag}]:\n"
                f"  {data.get('title', '')} | {data.get('subject', '')} | "
                f"{data.get('grade', '')} | مباحث: {', '.join(syl)}\n"
            )
        elif card_type == "microtopic":
            cards_text += (
                f"\n📌 ریزمبحث #{card['id']} [{origin_tag}]:\n"
                f"  {data.get('title', '')} | {data.get('subject', '')} | "
                f"{data.get('topic', '')} | {data.get('subtopic', '')}\n"
            )
        elif card_type == "project":
            cards_text += (
                f"\n📌 پروژه #{card['id']} [{origin_tag}]:\n"
                f"  {data.get('title', '')} | {data.get('project_type', '')} | "
                f"{data.get('provider', '')}\n"
            )
        elif card_type == "routine":
            cards_text += (
                f"\n📌 روتین #{card['id']} [{origin_tag}]:\n"
                f"  {data.get('title', '')} | تکرار: {data.get('recurrence', '')} | "
                f"{data.get('start_time', '')}-{data.get('end_time', '')}\n"
            )
        elif card_type == "reminder":
            cards_text += (
                f"\n📌 یادآور #{card['id']} [{origin_tag}]:\n"
                f"  {data.get('title', '')} | {data.get('date', '')} | {data.get('time', '')}\n"
                f"  ⚠️ حفظ با زمان ثابت.\n"
            )

    if not cards_text.strip():
        cards_text = (
            "کارتی موجود نیست. ۳ تا ۴ پارت نمونه بساز:\n"
            "  • مرور درس ضعیف ۴۵ دقیقه\n"
            "  • حل تست ۳۰ دقیقه\n"
            "  • مطالعه درس قوی ۴۵ دقیقه\n"
        )

    history_text = "📊 تاریخچه‌ای موجود نیست"
    if history_data:
        history_text = "📊 تاریخچه ۳۰ روز اخیر:\n"
        for h in history_data[:30]:
            history_text += f"  {h.get('date', '')}: {h.get('completed_parts', 0)}/{h.get('total_parts', 0)}\n"

    request_instruction = ""
    if user_request:
        request_instruction = f"\n\n⚠️ درخواست ویژه کاربر: {user_request}\nاین را در اولویت قرار بده."

    return f"""{role_prefix}
تو یک متخصص برنامه‌ریزی مطالعه هستی.

=== پروفایل ===
{user_data.get('full_name', 'کاربر')} | پایه: {user_data.get('grade', 'نامشخص')} | رشته: {user_data.get('field', 'نامشخص')}
هدف: {user_data.get('goal', 'نامشخص')} | ضعیف: {weak} | قوی: {strong}
منابع: {providers} | تمرکز: {user_data.get('focus_duration', 45)} دقیقه | استراحت: {user_data.get('break_duration', 10)} دقیقه

=== تاریخ امروز ===
شمسی: {today_shamsi} | میلادی: {today_greg} | روز هفته: {today_weekday}
📅 این برنامه برای تاریخ: {target_shamsi} ({target_date or today_greg})

{project_block}

=== کارت‌های فعال ===
{cards_text if cards_text else 'کارتی موجود نیست.'}
{rag_context}

=== تاریخچه ===
{history_text}
{request_instruction}

=== وظیفه ===
برنامه مطالعه روزانه بساز. **حداقل ۳ پارت تولید کن. parts هرگز خالی نباشد.**

### قوانین سخت:
1. از providers کاربر، ارائه‌دهنده را تشخیص بده.
2. کارت آزمون نزدیک را بگنجان.
3. کارت کلاس → پارت ثابت با explicit_start/end و is_fixed_time=true.
4. کارت قانون → اعمال رفتاری.
5. کارت منبع → پارت مطالعه.
6. کارت پروژه → اولویت بالا.
7. کارت روتین → پارت ثابت تکرارشونده.
8. کارت یادآور → پارت ثابت در همان تاریخ.
9. «X جلسه Y دقیقه‌ای» → sessions=X، session_duration=Y.
10. ۳ تا ۸ پارت بساز.
11. زمان صریح کاربر را رعایت کن.
12. درس‌های ضعیف در بهترین زمان.

### خروجی JSON:
{{
  "analysis": {{
    "user_providers": [],
    "selected_cards": {{"exams": [], "classes": [], "rules": [], "resources": [], "syllabus": [], "projects": [], "routines": [], "reminders": []}},
    "ignored_reason": ""
  }},
  "parts": [
    {{
      "title": "عنوان",
      "subject": "نام درس",
      "activity_type": "study",
      "content_type": "book",
      "duration": 45,
      "sessions": 1,
      "session_duration": 45,
      "priority": "normal",
      "explicit_start": null,
      "explicit_end": null,
      "is_fixed_time": false,
      "source": null,
      "from_card": null,
      "reason": ""
    }}
  ],
  "recommendations": [],
  "applied_cards": []
}}

⚠️ فقط JSON خالص برگردان. همه فیلدها با type درست. parts آرایه غیرخالی."""


# ==================== یکپارچه: generate_plan ====================
async def generate_plan_for_date(user_id: int, user_data: Dict,
                                  target_date: str, user_request: str = "") -> Dict:
    """تابع یکپارچه ساخت برنامه با اولویت پروژه فعال (بخش ۲۱)."""
    user_cards = get_user_cards_direct(user_id)
    active_project = get_active_project_card(user_id)

    rag_query = user_request
    if not rag_query and active_project:
        pd = active_project.get("data", {}) or {}
        rag_query = f"{pd.get('title', '')} {' '.join(pd.get('subjects', []) if isinstance(pd.get('subjects'), list) else [])}"
    if not rag_query:
        rag_query = f"{user_data.get('grade', '')} {user_data.get('field', '')} برنامه مطالعه"

    top_k = 3 if active_project else 5
    admin_cards = await get_relevant_admin_cards(user_id, rag_query, top_k=top_k)

    seen_ids = set()
    cards = []
    # پروژه فعال اول
    if active_project and active_project.get("id"):
        seen_ids.add(active_project["id"])
        cards.append(active_project)
    for c in (admin_cards + user_cards):
        if c["id"] not in seen_ids:
            seen_ids.add(c["id"])
            cards.append(c)

    history_data = get_last_n_days_data(user_id, 30)
    model_type = get_user_model_type(user_id)

    prompt = build_plan_prompt(
        user_data, user_id, cards, history_data,
        user_request, rag_context="",
        active_project=active_project, target_date=target_date
    )

    response = await call_ai(prompt, max_tokens=3500, temperature=0.3, model_type=model_type)

    parsed = safe_json_loads(response, dict) if response else None
    if not parsed or not parsed.get("parts"):
        # retry
        logger.warning(
            f"plan_retry: user={user_id} target={target_date} first_parse_empty=True "
            f"raw_len={len(response) if response else 0}"
        )
        retry_prompt = prompt + "\n\n⚠️ فقط JSON خالص. parts هرگز خالی نباشد. حداقل ۳ پارت."
        retry_model = AI_MODEL_ADVANCED if model_type != "advanced" else AI_MODEL_LIGHT
        response2 = await call_ai(retry_prompt, max_tokens=4000, temperature=0.2, model_type=retry_model)
        parsed = safe_json_loads(response2, dict) if response2 else None
        if not parsed or not parsed.get("parts"):
            logger.error(
                f"plan_failed: user={user_id} target={target_date} "
                f"raw1='{(response or '')[:200]}' raw2='{(response2 or '')[:200]}'"
            )
            return {}

    logger.info(
        f"plan_source: user={user_id} target={target_date} "
        f"project={(active_project or {}).get('data', {}).get('title', 'none')} "
        f"admin_cards={len(admin_cards)} parts={len(parsed.get('parts', []))}"
    )
    return parsed


async def generate_plan_with_ai(user_id: int, user_data: Dict, user_request: str = "") -> Dict:
    return await generate_plan_for_date(user_id, user_data, get_today_date(), user_request)


def create_plan_for_date(user_id: int, user_data: Dict, ai_response: Dict,
                          target_date: str = None) -> Optional[int]:
    """بخش ۱۰.۸ و ۱۵.۴ — با حفاظت پارهای محافظت‌شده."""
    if target_date is None:
        target_date = get_today_date()
    parts_data = ai_response.get('parts', [])
    if not parts_data:
        return None

    analysis = ai_response.get('analysis', {})
    session_id_existing = get_or_create_session(user_id, target_date)

    preserved_parts = []
    if session_id_existing:
        preserved_rows = execute_query(
            """SELECT part_number, title, planned_minutes, time_slot,
                      planned_start_time, planned_end_time, is_fixed_time,
                      explicit_start, explicit_end, reason, metadata,
                      activity_type, content_type, origin, subject, topic_resolved
               FROM study_parts
               WHERE session_id = %s AND (
                   content_type IN ('reminder', 'class')
                   OR metadata->>'is_reminder' = 'true'
                   OR is_fixed_time = TRUE
                   OR origin IN ('user', 'reminder', 'routine', 'card')
               )
               ORDER BY part_number""",
            (session_id_existing,), fetchall=True
        ) or []

        for r in preserved_rows:
            preserved_parts.append({
                "part_number": 0, "title": r[1],
                "planned_minutes": r[2] or 45, "time_slot": r[3] or "",
                "planned_start_time": str(r[4]) if r[4] else None,
                "planned_end_time": str(r[5]) if r[5] else None,
                "is_fixed_time": r[6] or False,
                "explicit_start": r[7], "explicit_end": r[8],
                "reason": r[9] or "", "metadata": r[10] or {},
                "activity": r[11] or "study",
                "content_type": r[12] or "book",
                "origin": r[13] or "ai",
                "subject": r[14], "topic_resolved": r[15],
                "completed": False
            })

    new_parts = []
    for idx, subj in enumerate(parts_data):
        if not isinstance(subj, dict):
            continue
        duration = subj.get('duration', user_data.get('focus_duration', 45))
        try:
            duration = max(20, int(duration))
        except (ValueError, TypeError):
            duration = 45

        explicit_start = subj.get('explicit_start')
        explicit_end = subj.get('explicit_end')
        content_type = subj.get('content_type', 'book')
        activity = subj.get('activity_type', 'study')

        if content_type == 'class' or activity == 'class':
            if not explicit_start and subj.get('start_time'):
                explicit_start = normalize_time_str(subj.get('start_time'))
            if not explicit_end and subj.get('end_time'):
                explicit_end = normalize_time_str(subj.get('end_time'))
            if explicit_start and explicit_end:
                try:
                    duration = max(15, time_to_minutes(explicit_end) - time_to_minutes(explicit_start))
                except Exception:
                    pass

        is_fixed = bool(subj.get('is_fixed_time')) or bool(explicit_start and explicit_end)
        metadata = {
            "source": subj.get('source'),
            "from_card": subj.get('from_card'),
            "reason": subj.get('reason', '')
        }
        new_parts.append({
            "part_number": idx + 1, "title": subj.get('title', 'مطالعه'),
            "topic": subj.get('topic', ''), "activity": activity,
            "content_type": content_type, "planned_minutes": duration,
            "pages": subj.get('pages', 0),
            "time_slot": f"{explicit_start}-{explicit_end}" if explicit_start and explicit_end else "",
            "planned_start_time": explicit_start, "planned_end_time": explicit_end,
            "completed": False, "is_fixed_time": is_fixed,
            "explicit_start": explicit_start, "explicit_end": explicit_end,
            "depends_on": subj.get('depends_on'), "reason": subj.get('reason', ''),
            "origin": "ai", "subject": subj.get('subject'), "metadata": metadata
        })

    all_parts = new_parts + preserved_parts
    all_parts = reschedule_parts(
        all_parts, base_start_time=get_iran_now(),
        user_id=user_id, source_date=target_date
    )

    all_parts.sort(key=lambda p: (
        0 if p.get("planned_start_time") else 1,
        time_to_minutes(p.get("planned_start_time") or "00:00") if p.get("planned_start_time") else 0,
        p.get("part_number", 0)
    ))
    for i, p in enumerate(all_parts):
        p["part_number"] = i + 1

    meta = {"analysis": analysis, "recommendations": ai_response.get('recommendations', [])}
    return save_session_with_parts_for_date(
        user_id, all_parts, [], parts_data, target_date, meta
    )


def create_plan_from_ai_response(user_id: int, user_data: Dict, ai_response: Dict) -> Optional[int]:
    return create_plan_for_date(user_id, user_data, ai_response, get_today_date())


# ==================== Plan Change Prompt ====================
def build_plan_change_prompt(user_id: int, user_data: Dict,
                              user_text: str, parts_summary: str,
                              active_project: Optional[Dict] = None) -> str:
    role_prefix = build_role_prefix(user_id)
    project_line = ""
    if active_project:
        pd = active_project.get("data", {}) or {}
        project_line = f"\n=== پروژه فعال ===\n{pd.get('title', '')} | {pd.get('provider', '')}\n"

    return f"""{role_prefix}
تو یک دستیار هوشمند مدیریت برنامه روزانه هستی.

=== برنامه فعلی امروز ===
{parts_summary}
{project_line}
=== درخواست کاربر ===
{user_text}

=== اطلاعات ===
هدف: {user_data.get('goal', 'نامشخص') if user_data else 'نامشخص'} | پایه: {user_data.get('grade', 'نامشخص') if user_data else 'نامشخص'}
امروز شمسی: {get_today_shamsi()} | میلادی: {get_today_date()}

=== وظیفه ===
تشخیص بده درخواست مربوط به برنامه است یا chat.

## قانون طلایی: پیش‌فرض add
فقط update وقتی صریح گفت «عوض کن/تغییر بده/اصلاح کن».

### انواع action:
chat, add, multi_add, update, delete, clear, build_new, reminder, add_class, suggest_card, clarify

### قوانین سخت:
1. زمان صریح («۱۳:۱۵ تا ۱۴:۳۰») → explicit_start/end و is_fixed_time=true.
2. «X جلسه Y دقیقه‌ای» → sessions=X، session_duration=Y، title بدون این عبارت.
3. تاریخ («فردا/امروز/پس‌فردا» یا تاریخ) → target_date میلادی. کلمات را از title حذف کن.
4. «صبح/عصر/شب» → time_hint (morning/noon/evening/night).
5. پاسخ شفاف‌سازی فقط عدد/زمان → title از متن اصلی، در target.
6. title هرگز خالی/«فعالیت» نباشد.
7. **همه فیلدها با type درست**: subjects آرایه‌ای از dict، نه رشته. target dict، نه رشته.

### multi_add:
- هر فعالیت یک آیتم. حداکثر ۵.
- هر آیتم: title, subject, duration, sessions, session_duration, target_date, time_hint, activity_type, explicit_start, explicit_end, is_fixed_time.

### reminder:
- تاریخ شمسی + میلادی. اگر ساعت نبود time=null.

### خروجی JSON:
{{
  "action": "chat|add|multi_add|update|delete|clear|build_new|reminder|add_class|suggest_card|clarify",
  "confidence": 0.0-1.0,
  "subjects": [
    {{
      "title": "عنوان تمیز",
      "subject": "درس",
      "duration": 45,
      "sessions": 1,
      "session_duration": 45,
      "pages": 0,
      "test_count": 0,
      "target_date": "2026-10-19",
      "time_hint": "morning|noon|evening|night|null",
      "explicit_start": null,
      "explicit_end": null,
      "activity_type": "study",
      "is_fixed_time": false
    }}
  ],
  "target": {{
    "title": "عنوان",
    "duration": 45,
    "sessions": 1,
    "session_duration": 45,
    "target_date": "2026-10-19",
    "start_time": "09:00",
    "end_time": "09:45",
    "explicit_start": null,
    "explicit_end": null,
    "is_fixed_time": false,
    "depends_on": null
  }},
  "reminder": {{}},
  "card_suggestion": {{}},
  "classes": [],
  "question": "",
  "options": [],
  "context": {{"pending_action": null, "original_text": null}},
  "reason": ""
}}

⚠️ فقط JSON خالص. subjects آرایه آبجکت، نه رشته."""


# ==================== Clarify Gate — بخش ۱۶ ====================
CLARIFY_ACTIONS = {"add", "multi_add", "update", "delete", "clear",
                   "build_new", "reminder", "add_class"}


def _clarify_fingerprint(user_text: str) -> str:
    return (user_text or "").strip()[:80]


def _decide_clarify_question(action: str, interpretation: Dict,
                              user_text: str) -> Optional[Tuple[str, List[str]]]:
    """
    بر اساس action و پر بودن فیلدها، سوال شفاف‌سازی + دکمه‌ها.
    خروجی: (question_text, button_labels) یا None.
    """
    target = interpretation.get("target") if isinstance(interpretation.get("target"), dict) else {}
    subjects = interpretation.get("subjects") if isinstance(interpretation.get("subjects"), list) else []
    reminder = interpretation.get("reminder") if isinstance(interpretation.get("reminder"), dict) else {}
    classes = interpretation.get("classes") if isinstance(interpretation.get("classes"), list) else []

    if action in ("add", "multi_add"):
        has_time = bool(
            (target.get("explicit_start") and target.get("explicit_end"))
            or any(isinstance(s, dict) and s.get("explicit_start") and s.get("explicit_end")
                   for s in subjects)
        )
        has_title = bool(
            (target.get("title") or "").strip() not in ("", "فعالیت")
            or any(isinstance(s, dict) and (s.get("title") or "").strip() not in ("", "فعالیت")
                   for s in subjects)
        )
        if not has_title:
            return ("می‌خوای چه چیزی به برنامه اضافه کنم؟ یک عنوان کوتاه بگو.",
                    ["📚 درس را می‌گویم", "✏️ عنوان دیگر", "🔙 بازگشت"])
        if not has_time:
            return ("همین الان شروع کنم، ساعت مشخص بدم، یا AI در جای مناسب بگذارد؟",
                    ["▶️ همین الان شروع کنم", "🕐 ساعت مشخص",
                     "🧠 AI در جای مناسب بگذارد"])
        return ("ثابت ثبت کنم یا شناور بگذارم تا AI بچیند؟",
                ["🔒 ثابت", "🕊 شناور"])
    if action == "build_new":
        return ("چند ساعت در روز و چه درس‌هایی اولویت داشته باشند؟ (یا بگو AI تصمیم بگیرد)",
                ["🧠 AI تصمیم بگیرد", "🕐 من می‌گویم", "🔙 بازگشت"])
    if action == "reminder":
        if reminder.get("time"):
            return ("همین ساعت باشد یا تغییرش بدم؟",
                    ["✅ همین خوبه", "🕐 تغییر بدم", "🔙 بازگشت"])
        return ("ساعت مشخصی برای یادآور داری؟",
                ["🕐 ساعت مشخص", "⏰ صبح", "🌙 شب", "🔙 بازگشت"])
    if action in ("delete", "update"):
        return ("مطمئنی این پارت را می‌خواهی تغییر بدهی؟",
                ["✅ بله", "❌ نه", "🔙 بازگشت"])
    if action == "clear":
        return ("همه پارت‌ها را پاک کنم؟ (پارت‌های ثابت/یادآور محافظت‌شده حذف نمی‌شوند)",
                ["✅ بله", "❌ نه", "🔙 بازگشت"])
    if action == "add_class":
        if not classes:
            return ("کلاس چه روز و چه ساعتی است؟",
                    ["✏️ می‌نویسم", "🔙 بازگشت"])
        return ("کلاس ثابت هفتگی است یا فقط یک بار؟",
                ["🔒 ثابت هفتگی", "1️⃣ فقط یک بار", "🔙 بازگشت"])
    return None


# ==================== Schema ====================
def create_tables():
    queries = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        """CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id VARCHAR(50) UNIQUE NOT NULL,
            username VARCHAR(100), full_name VARCHAR(200),
            goal VARCHAR(200), grade VARCHAR(50), field VARCHAR(50),
            exam_date DATE, study_hours_per_week INTEGER,
            peak_time VARCHAR(20), learning_style VARCHAR(30),
            focus_duration INTEGER DEFAULT 45, break_duration INTEGER DEFAULT 10,
            weak_subjects JSONB, strong_subjects JSONB, daily_schedule JSONB,
            providers JSONB, onboarding_profile JSONB, role_preference VARCHAR(50),
            is_active BOOLEAN DEFAULT TRUE, is_onboarded BOOLEAN DEFAULT FALSE,
            kok_balance INTEGER DEFAULT 0, total_kok_used INTEGER DEFAULT 0,
            total_kok_purchased INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity_date DATE
        )""",
        """CREATE TABLE IF NOT EXISTS subject_status (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            subject VARCHAR(50) NOT NULL,
            total_sessions INTEGER DEFAULT 0, completed_sessions INTEGER DEFAULT 0,
            total_study_minutes INTEGER DEFAULT 0, avg_score FLOAT,
            last_studied DATE, last_score FLOAT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, subject)
        )""",
        """CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            date DATE NOT NULL, subject VARCHAR(200) NOT NULL, topic VARCHAR(200),
            activity_type VARCHAR(30), planned_duration INTEGER,
            actual_duration INTEGER, start_time TIME, end_time TIME,
            score FLOAT, status VARCHAR(20), part_order INTEGER DEFAULT 0,
            pages_count INTEGER, test_count INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_archived BOOLEAN DEFAULT FALSE
        )""",
        """CREATE TABLE IF NOT EXISTS advisory_rules (
            id SERIAL PRIMARY KEY, topic VARCHAR(50) NOT NULL,
            label VARCHAR(50), condition TEXT, advice TEXT NOT NULL,
            priority INTEGER DEFAULT 5, time VARCHAR(20),
            frequency VARCHAR(30), subjects JSONB,
            is_active BOOLEAN DEFAULT TRUE, created_by BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS study_sessions (
            session_id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            date VARCHAR(20), total_parts INT, completed_parts INT DEFAULT 0,
            confirmed BOOLEAN DEFAULT FALSE, is_finished BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            time_slots TEXT, topics TEXT, archived BOOLEAN DEFAULT FALSE,
            meta JSONB, UNIQUE(user_id, date)
        )""",
        """CREATE TABLE IF NOT EXISTS study_parts (
            part_id SERIAL PRIMARY KEY,
            session_id INT REFERENCES study_sessions(session_id),
            user_id INTEGER, part_number INT,
            title VARCHAR(200), activity_type VARCHAR(30) DEFAULT 'study',
            activity_type_confirmed VARCHAR(30),
            content_type VARCHAR(30) DEFAULT 'book',
            planned_minutes INT, actual_minutes INT DEFAULT 0,
            time_slot VARCHAR(50), completed BOOLEAN DEFAULT FALSE,
            pages INT DEFAULT 0, planned_start_time TIME, planned_end_time TIME,
            actual_start_time TIMESTAMP, actual_end_time TIMESTAMP,
            is_fixed_time BOOLEAN DEFAULT FALSE, reason TEXT,
            explicit_start VARCHAR(10), explicit_end VARCHAR(10),
            depends_on VARCHAR(200), metadata JSONB, origin VARCHAR(30) DEFAULT 'ai',
            subject VARCHAR(100), topic_resolved VARCHAR(200),
            detection_confidence FLOAT, matched_card_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS part_feedback (
            id SERIAL PRIMARY KEY,
            part_id INTEGER REFERENCES study_parts(part_id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            question_key VARCHAR(50), answer_value VARCHAR(200),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS cards (
            id SERIAL PRIMARY KEY, card_type VARCHAR(20) NOT NULL,
            content_type VARCHAR(30), data JSONB NOT NULL,
            applies_to JSONB, priority INTEGER DEFAULT 5,
            raw_text TEXT, is_active BOOLEAN DEFAULT TRUE,
            created_by BIGINT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS reminders (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            ref_card_id INTEGER REFERENCES cards(id),
            remind_at TIMESTAMP, remind_type VARCHAR(30), message TEXT,
            sent BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS user_quota (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE PRIMARY KEY,
            daily_messages INTEGER DEFAULT 0, last_reset DATE DEFAULT CURRENT_DATE,
            plan_type VARCHAR(20) DEFAULT 'trial', plan_expiry DATE
        )""",
        """CREATE TABLE IF NOT EXISTS chat_history (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            role VARCHAR(20) NOT NULL, content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS pending_payments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            photo_file_id VARCHAR(200), caption TEXT, plan_id INTEGER,
            status VARCHAR(20) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS rag_documents (
            id SERIAL PRIMARY KEY, content TEXT NOT NULL,
            embedding vector(1536), source VARCHAR(200),
            category VARCHAR(50) DEFAULT 'general', created_by BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS ai_roles (
            id SERIAL PRIMARY KEY, role_text TEXT NOT NULL,
            is_active BOOLEAN DEFAULT FALSE, created_by BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS subscription_plans (
            id SERIAL PRIMARY KEY, name VARCHAR(50) NOT NULL,
            price INTEGER DEFAULT 0, kok_amount INTEGER DEFAULT 0,
            model_access VARCHAR(20) DEFAULT 'light',
            duration_days INTEGER DEFAULT 30, is_active BOOLEAN DEFAULT TRUE
        )""",
        """CREATE TABLE IF NOT EXISTS user_subscriptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            plan_id INTEGER REFERENCES subscription_plans(id),
            start_date DATE NOT NULL, end_date DATE NOT NULL,
            credits_hourly INTEGER DEFAULT 0, credits_daily INTEGER DEFAULT 0,
            credits_weekly INTEGER DEFAULT 0,
            last_hourly_reset TIMESTAMP, last_daily_reset DATE, last_weekly_reset DATE,
            is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS user_projects (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            project_type VARCHAR(50), title VARCHAR(200),
            data JSONB, is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS user_reminders (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            title VARCHAR(200), message TEXT, remind_at TIMESTAMP,
            is_recurring BOOLEAN DEFAULT FALSE, recurrence VARCHAR(50),
            is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS user_rules (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            rule_type VARCHAR(20) DEFAULT 'friend', settings JSONB,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS user_routines (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            routine_type VARCHAR(30), time_value VARCHAR(20),
            data JSONB, is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS performance_metrics (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            date DATE NOT NULL, metric_type VARCHAR(50),
            value FLOAT, data JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS reminder_notifications (
            id SERIAL PRIMARY KEY,
            part_id INTEGER REFERENCES study_parts(part_id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            notify_type VARCHAR(30) NOT NULL, scheduled_at TIMESTAMP NOT NULL,
            sent_at TIMESTAMP, status VARCHAR(20) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(part_id, notify_type)
        )""",
        """CREATE TABLE IF NOT EXISTS subject_detection_cache (
            id SERIAL PRIMARY KEY, normalized_title VARCHAR(200) UNIQUE,
            subject VARCHAR(100), topic VARCHAR(200),
            activity_type VARCHAR(30), confidence FLOAT,
            matched_card_id INTEGER, hit_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS subject_feedback (
            id SERIAL PRIMARY KEY, part_id INTEGER, user_id INTEGER,
            original_subject VARCHAR(100), corrected_subject VARCHAR(100),
            title VARCHAR(200), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS card_tracking (
            id SERIAL PRIMARY KEY,
            card_id INTEGER REFERENCES cards(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            date DATE NOT NULL, status VARCHAR(20), part_id INTEGER,
            notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(card_id, user_id, date)
        )""",
        """CREATE TABLE IF NOT EXISTS daily_stats (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            date DATE NOT NULL, total_parts INTEGER DEFAULT 0,
            completed_parts INTEGER DEFAULT 0, delayed_parts INTEGER DEFAULT 0,
            planned_minutes INTEGER DEFAULT 0, actual_minutes INTEGER DEFAULT 0,
            subjects_breakdown JSONB, activity_breakdown JSONB,
            hourly_breakdown JSONB, delay_percent FLOAT,
            commitment_percent FLOAT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, date)
        )""",
        """CREATE TABLE IF NOT EXISTS kok_transactions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            amount INTEGER NOT NULL, balance_after INTEGER NOT NULL,
            reason VARCHAR(50) NOT NULL, reference_id VARCHAR(100),
            description TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS kok_costs (
            action_key VARCHAR(50) PRIMARY KEY, cost INTEGER NOT NULL,
            label VARCHAR(100), is_active BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS conflict_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            date DATE NOT NULL, conflict_type VARCHAR(30),
            part_id INTEGER, related_part_id INTEGER,
            overlap_minutes INTEGER, resolution VARCHAR(30),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
    ]
    for query in queries:
        try:
            execute_query(query)
        except Exception as e:
            logger.warning(f"خطا در ایجاد جدول: {e}")

    try:
        execute_query("UPDATE subscription_plans SET is_active = FALSE WHERE kok_amount = 0 AND price > 0")
    except Exception:
        pass

    plan_inserts = [
        ("رایگان", 0, 30, "light", 9999),
        ("پایه", 100000, 50, "light", 30),
        ("حرفه‌ای", 350000, 200, "advanced", 30),
        ("ویژه", 650000, 400, "advanced", 30),
    ]
    for name, price, kok, access, days in plan_inserts:
        try:
            existing = execute_query(
                "SELECT id FROM subscription_plans WHERE name = %s", (name,), fetch=True
            )
            if existing:
                execute_query(
                    """UPDATE subscription_plans SET price = %s, kok_amount = %s,
                       model_access = %s, duration_days = %s, is_active = TRUE
                       WHERE name = %s""",
                    (price, kok, access, days, name)
                )
            else:
                execute_query(
                    """INSERT INTO subscription_plans
                       (name, price, kok_amount, model_access, duration_days, is_active)
                       VALUES (%s, %s, %s, %s, %s, TRUE)""",
                    (name, price, kok, access, days)
                )
        except Exception as e:
            logger.warning(f"خطا در insert پلن {name}: {e}")

    for key, cost in DEFAULT_KOK_COSTS.items():
        try:
            execute_query(
                """INSERT INTO kok_costs (action_key, cost, label)
                   VALUES (%s, %s, %s) ON CONFLICT (action_key) DO NOTHING""",
                (key, cost, KOK_ACTION_LABELS.get(key, key))
            )
        except Exception:
            pass

    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS kok_balance INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_kok_used INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_kok_purchased INTEGER DEFAULT 0",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS explicit_start VARCHAR(10)",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS explicit_end VARCHAR(10)",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS depends_on VARCHAR(200)",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS user_id INTEGER",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS metadata JSONB",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS activity_type_confirmed VARCHAR(30)",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS origin VARCHAR(30) DEFAULT 'ai'",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS subject VARCHAR(100)",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS topic_resolved VARCHAR(200)",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS detection_confidence FLOAT",
        "ALTER TABLE study_parts ADD COLUMN IF NOT EXISTS matched_card_id INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS providers JSONB",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_profile JSONB",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role_preference VARCHAR(50)",
        "ALTER TABLE cards ADD COLUMN IF NOT EXISTS content_type VARCHAR(30)",
        "ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS kok_amount INTEGER DEFAULT 0",
        "ALTER TABLE pending_payments ADD COLUMN IF NOT EXISTS plan_id INTEGER",
    ]
    for migration in migrations:
        try:
            execute_query(migration)
        except Exception as e:
            logger.warning(f"خطا در migration: {e}")

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)",
        "CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_log(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_activity_date ON activity_log(date)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON study_sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_parts_session ON study_parts(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_parts_origin ON study_parts(origin)",
        "CREATE INDEX IF NOT EXISTS idx_parts_subject ON study_parts(subject)",
        "CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_payments_user ON pending_payments(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_cards_type ON cards(card_type)",
        "CREATE INDEX IF NOT EXISTS idx_feedback_part ON part_feedback(part_id)",
        "CREATE INDEX IF NOT EXISTS idx_rag_category ON rag_documents(category)",
        "CREATE INDEX IF NOT EXISTS idx_rag_created_by ON rag_documents(created_by)",
        "CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON user_subscriptions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_reminder_notif_status ON reminder_notifications(status, scheduled_at)",
        "CREATE INDEX IF NOT EXISTS idx_reminder_notif_part ON reminder_notifications(part_id)",
        "CREATE INDEX IF NOT EXISTS idx_card_tracking_card ON card_tracking(card_id, user_id)",
        "CREATE INDEX IF NOT EXISTS idx_daily_stats_user ON daily_stats(user_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_kok_tx_user ON kok_transactions(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_cards_created_by ON cards(created_by)",
        "CREATE INDEX IF NOT EXISTS idx_conflict_log_user ON conflict_log(user_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_parts_fixed ON study_parts(is_fixed_time, completed)",
        "CREATE INDEX IF NOT EXISTS idx_parts_content_type ON study_parts(content_type)",
    ]
    for idx in indexes:
        try:
            execute_query(idx)
        except Exception:
            pass

    logger.info("✅ جداول دیتابیس ایجاد شدند")
# ==================== bot.py - بخش 3/4 ====================
# هندلرها: onboarding + chat + plan + calendar + timer + feedback +
# routine + reminder + project + cards + report + subscription +
# nav_back مرکزی + process_ai_plan_change + apply_pending_change

# ==================== تایمر ====================
active_timers = {}
timer_data = {}


def get_current_elapsed(part_id: int) -> int:
    data = timer_data.get(part_id, {})
    offset = data.get("elapsed_offset", 0)
    start_time = data.get("start_time")
    if start_time and part_id in active_timers:
        return offset + int((datetime.now(IRAN_TZ) - start_time).total_seconds())
    return offset


async def update_timer(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    chat_id = job_data.get("chat_id")
    part_id = job_data.get("part_id")
    start_time = job_data.get("start_time")
    timer_message_id = job_data.get("timer_message_id")
    total_minutes = job_data.get("total_minutes", 0)
    elapsed_offset = job_data.get("elapsed_offset", 0)

    elapsed = elapsed_offset + int((datetime.now(IRAN_TZ) - start_time).total_seconds())
    minutes = elapsed // 60
    seconds = elapsed % 60

    result = execute_query(
        "SELECT title, completed FROM study_parts WHERE part_id = %s",
        (part_id,), fetch=True
    )
    if not result or result[1]:
        context.job.schedule_removal()
        active_timers.pop(part_id, None)
        timer_data.pop(part_id, None)
        return

    title = result[0]

    # پایان تایمر — بخش ۶
    if elapsed >= total_minutes * 60:
        context.job.schedule_removal()
        active_timers.pop(part_id, None)
        timer_data.pop(part_id, None)
        try:
            execute_query(
                "UPDATE study_parts SET actual_minutes = %s WHERE part_id = %s",
                (total_minutes, part_id)
            )
        except Exception:
            pass

        # ۱. ویرایش پیام زنده به حالت نهایی
        try:
            if timer_message_id:
                await context.bot.edit_message_text(
                    f"⏱ تایمر پایان یافت ✅\n\n📖 {title}\n📊 ۱۰۰٪",
                    chat_id=chat_id, message_id=timer_message_id
                )
        except Exception:
            pass

        # ۲. ارسال پیام کاملاً جدید
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"✅ **تایمر {title} به پایان رسید!**\n\n"
                      f"⏱ {total_minutes} دقیقه\n\n"
                      f"الان می‌خوای تکمیلش کنی؟"),
                reply_markup=ReplyKeyboardMarkup(
                    [["✅ تکمیل", "▶️ ادامه ۵ دقیقه"],
                     ["🗑 حذف"]],
                    resize_keyboard=True
                ),
                parse_mode=ParseMode.HTML
            )
            # ذخیره برای پیگیری
            context.bot_data.setdefault("timer_ended_parts", {})[str(chat_id)] = part_id
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
    message_text += f"⏳ سپری: {minutes:02d}:{seconds:02d}\n"
    message_text += f"⏳ باقی: {remaining_minutes:02d}:{remaining_secs:02d}\n"
    message_text += f"📊 {progress}%\n"
    message_text += f"`{bar}`\n🎯 {total_minutes} دقیقه"

    if remaining_minutes <= 2:
        message_text += f"\n\n⚠️ **{remaining_minutes} دقیقه تا پایان!**"

    try:
        if timer_message_id:
            await context.bot.edit_message_text(
                message_text, chat_id=chat_id,
                message_id=timer_message_id, parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"خطا در آپدیت تایمر: {e}")


async def start_timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    chat_id = update.effective_chat.id
    if part_id in active_timers:
        await update.message.reply_text("⏱ تایمر در حال اجراست!")
        return

    result = execute_query(
        "SELECT title, planned_minutes, completed FROM study_parts WHERE part_id = %s",
        (part_id,), fetch=True
    )
    if not result:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    title, total_minutes, completed = result
    if completed:
        await update.message.reply_text("❌ این پارت قبلاً انجام شده.")
        return

    if part_id in timer_data and timer_data[part_id].get("elapsed_offset", 0) > 0:
        elapsed_offset = timer_data[part_id]["elapsed_offset"]
        start_time = datetime.now(IRAN_TZ)
        await update.message.reply_text(f"▶️ ادامه تایمر: {title}")
    else:
        elapsed_offset = 0
        start_time = datetime.now(IRAN_TZ)
        await update.message.reply_text(f"⏱ شروع تایمر: {title} ({total_minutes} دقیقه)")

    msg = await update.message.reply_text(
        f"⏱ **تایمر: {title}**\n\n⏳ در حال اجرا...",
        parse_mode=ParseMode.HTML
    )
    job_data = {
        "chat_id": chat_id, "part_id": part_id, "start_time": start_time,
        "timer_message_id": msg.message_id, "total_minutes": total_minutes,
        "elapsed_offset": elapsed_offset
    }
    if context.job_queue:
        job = context.job_queue.run_repeating(update_timer, interval=10, first=10, data=job_data)
        active_timers[part_id] = job
        timer_data[part_id] = {"elapsed_offset": elapsed_offset, "start_time": start_time}


async def stop_timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    if part_id not in active_timers:
        await update.message.reply_text("❌ تایمر فعالی نیست.")
        return
    job = active_timers[part_id]
    job_data = job.data
    start_time = job_data.get("start_time")
    elapsed_offset = job_data.get("elapsed_offset", 0)
    total_minutes = job_data.get("total_minutes", 0)
    elapsed = elapsed_offset + int((datetime.now(IRAN_TZ) - start_time).total_seconds())
    timer_data[part_id] = {"elapsed_offset": elapsed}
    active_timers[part_id].schedule_removal()
    del active_timers[part_id]
    try:
        execute_query(
            "UPDATE study_parts SET actual_minutes = %s WHERE part_id = %s",
            (elapsed // 60, part_id)
        )
    except Exception:
        pass
    remaining = max(0, total_minutes * 60 - elapsed)
    await update.message.reply_text(
        f"⏹ تایمر متوقف شد.\n⏱ سپری: {elapsed // 60:02d}:{elapsed % 60:02d}\n"
        f"⏳ باقی: {remaining // 60:02d}:{remaining % 60:02d}",
    )
    await show_part_detail(update, context, part_id)


# ==================== بازخورد ====================
async def ask_activity_type(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    context.user_data["awaiting_activity_type"] = part_id
    result = execute_query("SELECT title FROM study_parts WHERE part_id = %s", (part_id,), fetch=True)
    title = result[0] if result else "پارت"
    await update.message.reply_text(
        f"✅ **{title} تکمیل شد!**\n\n📌 **نوع این فعالیت چی بود؟**",
        reply_markup=get_activity_type_keyboard(), parse_mode=ParseMode.HTML
    )


async def handle_activity_type(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    part_id = context.user_data.get("awaiting_activity_type")
    if not part_id:
        return
    type_map = {
        "📖 مطالعه": "study", "📝 تست": "test", "🔁 مرور": "review",
        "🏫 کلاس": "class", "🎥 ویدئو": "video",
        "✏️ تمرین": "exercise", "📌 سایر": "other"
    }
    activity_type = type_map.get(text)
    if not activity_type:
        await update.message.reply_text("❌ از گزینه‌های داده شده انتخاب کن.")
        return
    execute_query(
        "UPDATE study_parts SET activity_type_confirmed = %s WHERE part_id = %s",
        (activity_type, part_id)
    )
    context.user_data.pop("awaiting_activity_type", None)
    context.user_data["feedback_activity_type"] = activity_type
    await update.message.reply_text(f"✅ نوع: {text}", parse_mode=ParseMode.HTML)
    await ask_feedback(update, context, part_id, activity_type)


async def ask_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       part_id: int, activity_type: str) -> None:
    questions = get_feedback_questions(activity_type)
    context.user_data["feedback_part_id"] = part_id
    context.user_data["feedback_questions"] = questions
    context.user_data["feedback_index"] = 0
    context.user_data["feedback_answers"] = {}
    await ask_next_feedback(update, context)


async def ask_next_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    questions = context.user_data.get("feedback_questions", [])
    index = context.user_data.get("feedback_index", 0)
    if index >= len(questions):
        await finish_feedback(update, context)
        return
    q_key = questions[index]
    q_data = FEEDBACK_BANK.get(q_key)
    if not q_data:
        context.user_data["feedback_index"] = index + 1
        await ask_next_feedback(update, context)
        return
    total = len(questions)
    text = f"📝 **سوال {index + 1} از {total}**\n\n{q_data['question']}"
    await update.message.reply_text(
        text, reply_markup=get_feedback_keyboard(q_key, q_data["options"]),
        parse_mode=ParseMode.HTML
    )


async def handle_feedback_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    questions = context.user_data.get("feedback_questions", [])
    index = context.user_data.get("feedback_index", 0)
    if index >= len(questions):
        return
    q_key = questions[index]
    q_data = FEEDBACK_BANK.get(q_key)
    if not q_data:
        context.user_data["feedback_index"] = index + 1
        await ask_next_feedback(update, context)
        return
    if text == "🔙 بازگشت":
        for k in ["feedback_part_id", "feedback_questions", "feedback_index", "feedback_answers"]:
            context.user_data.pop(k, None)
        plan = context.user_data.get("current_plan", {})
        if plan.get("parts"):
            await show_parts_final(update, context, plan["parts"])
        return
    if text not in q_data["options"]:
        await update.message.reply_text("❌ از گزینه‌های داده شده انتخاب کن.")
        return
    context.user_data["feedback_answers"][q_key] = text
    context.user_data["feedback_index"] = index + 1
    await ask_next_feedback(update, context)


async def finish_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    part_id = context.user_data.get("feedback_part_id")
    answers = context.user_data.get("feedback_answers", {})
    user_id = get_user_id_by_telegram(update.effective_user.id)
    for q_key, answer in answers.items():
        save_part_feedback(part_id, user_id, q_key, answer)
    for k in ["feedback_part_id", "feedback_questions", "feedback_index",
              "feedback_answers", "feedback_activity_type"]:
        context.user_data.pop(k, None)
    await update.message.reply_text(
        "✅ **بازخورد ثبت شد!**\n\nممنون از وقتی که گذاشتی. 🌟",
        parse_mode=ParseMode.HTML
    )
    plan = context.user_data.get("current_plan", {})
    if plan.get("parts"):
        await show_parts_final(update, context, plan["parts"])


# ==================== Onboarding ====================
def get_subjects_by_field(field: str, grade: str) -> List[str]:
    if field == "ریاضی":
        return ["ریاضی", "فیزیک", "شیمی", "ادبیات", "عربی", "دینی", "زبان"]
    elif field == "تجربی":
        return ["ریاضی", "فیزیک", "شیمی", "زیست", "ادبیات", "عربی", "دینی", "زبان", "زمین"]
    elif field == "انسانی":
        return ["ریاضی", "ادبیات", "عربی", "دینی", "زبان", "تاریخ", "جغرافیا", "اقتصاد", "منطق", "روانشناسی"]
    return ["ریاضی", "فیزیک", "شیمی", "زیست", "ادبیات", "عربی", "دینی", "زبان", "زمین"]


async def analyze_resources_with_ai(text: str) -> Dict:
    prompt = f"""متن زیر را تحلیل کن و منابع مطالعاتی کاربر را استخراج کن.

=== متن کاربر ===
"{text}"

=== خروجی JSON ===
{{
  "providers": ["ماز", "قلمچی"],
  "subjects_resources": {{
    "ریاضی": ["کلاس ماز", "خیلی سبز"]
  }},
  "raw_text": "متن اصلی"
}}

⚠️ فقط JSON خالص."""
    try:
        response = await call_ai(prompt, max_tokens=1500, temperature=0.2)
        if not response:
            return {"providers": [], "subjects_resources": {}, "raw_text": text}
        parsed = safe_json_loads(response, dict)
        if parsed:
            parsed["raw_text"] = text
            return parsed
    except Exception as e:
        logger.error(f"خطا در تحلیل منابع: {e}")
    return {"providers": [], "subjects_resources": {}, "raw_text": text}


def parse_study_hours(text: str) -> int:
    if not text:
        return 0
    try:
        nums = re.findall(r'\d+', text)
        if nums:
            return int(nums[0]) * 60 * 7
    except Exception:
        pass
    return 0


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    telegram_id = str(user.id)
    reset_user_state(context)
    nav_reset(context)

    user_data = get_user_data(telegram_id)
    if user_data and user_data.get("is_onboarded"):
        balance = get_user_kok(user_data["id"])
        sub = get_user_subscription(user_data["id"])
        plan_name = sub.get("plan_name") if sub else "بدون اشتراک"
        await update.message.reply_text(
            f"🎯 سلام {user.full_name}! خوش برگشتی.\n\n"
            f"📚 امروز {get_today_shamsi()} - ساعت {get_iran_time_str()}\n"
            f"💎 KOK: {balance}\n"
            f"📦 پلن: {plan_name}\n\nمنتظرت هستم 👇",
            reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
        )
        return

    context.user_data["onboarding_step"] = 0
    context.user_data["onboarding_data"] = {
        "telegram_id": telegram_id, "username": user.username,
        "full_name": user.full_name, "providers": [],
        "weak_subjects": [], "strong_subjects": []
    }
    await update.message.reply_text(
        "👋 سلام! به ربات هوشمند مطالعه خوش اومدی!\n\n"
        "📋 لطفاً به سوالات زیر جواب بده:\n\n"
        "❓ <b>۱. پایه تحصیلی‌ات چیه؟</b>",
        reply_markup=ReplyKeyboardMarkup(
            [["🎓 دهم", "🎓 یازدهم"], ["🎓 دوازدهم", "🎓 فارغ‌التحصیل"], ["✏️ سایر"]],
            resize_keyboard=True, one_time_keyboard=True
        ),
        parse_mode=ParseMode.HTML
    )


async def onboarding_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    step = context.user_data.get("onboarding_step", 0)
    data = context.user_data.get("onboarding_data", {})

    if step == 0:
        grade_map = {"🎓 دهم": "دهم", "🎓 یازدهم": "یازدهم",
                     "🎓 دوازدهم": "دوازدهم", "🎓 فارغ‌التحصیل": "فارغ‌التحصیل"}
        if text in grade_map:
            data["grade"] = grade_map[text]
        elif text == "✏️ سایر":
            await update.message.reply_text("✏️ پایه خودت رو بنویس:")
            context.user_data["awaiting_custom"] = "grade"
            return
        else:
            data["grade"] = text
        context.user_data["onboarding_data"] = data
        context.user_data["onboarding_step"] = 1
        await update.message.reply_text(
            "❓ <b>۲. رشته تحصیلی‌ات چیه؟</b>",
            reply_markup=ReplyKeyboardMarkup(
                [["🧪 تجربی", "➗ ریاضی"], ["📖 انسانی", "🔧 فنی‌وحرفه‌ای"], ["✏️ سایر"]],
                resize_keyboard=True, one_time_keyboard=True
            ),
            parse_mode=ParseMode.HTML
        )

    elif step == 1:
        field_map = {"🧪 تجربی": "تجربی", "➗ ریاضی": "ریاضی",
                     "📖 انسانی": "انسانی", "🔧 فنی‌وحرفه‌ای": "فنی‌وحرفه‌ای"}
        if text in field_map:
            data["field"] = field_map[text]
        elif text == "✏️ سایر":
            await update.message.reply_text("✏️ رشته خودت رو بنویس:")
            context.user_data["awaiting_custom"] = "field"
            return
        else:
            data["field"] = text
        context.user_data["onboarding_data"] = data
        context.user_data["onboarding_step"] = 2
        subjects = get_subjects_by_field(data.get("field", ""), data.get("grade", ""))
        subjects_text = "\n".join([f"📚 {s}" for s in subjects])
        await update.message.reply_text(
            f"❓ <b>۳. منابع مطالعاتی‌ات چیه؟</b>\n\n{subjects_text}\n\n"
            f"✍️ مثال:\n«ریاضی: کلاس ماز + خیلی سبز\nفیزیک: کتاب درسی + مبتکران»",
            reply_markup=ReplyKeyboardMarkup([["⏭ رد کردن"]], resize_keyboard=True, one_time_keyboard=True),
            parse_mode=ParseMode.HTML
        )

    elif step == 2:
        if text != "⏭ رد کردن":
            await update.message.reply_text("🧠 در حال تحلیل منابع...")
            analysis = await analyze_resources_with_ai(text)
            data["providers"] = analysis.get("providers", [])
            data["subjects_resources"] = analysis.get("subjects_resources", {})
            data["resources_text"] = text
        else:
            data["providers"] = []
            data["subjects_resources"] = {}
        context.user_data["onboarding_data"] = data
        context.user_data["onboarding_step"] = 3
        subjects = get_subjects_by_field(data.get("field", ""), data.get("grade", ""))
        subject_buttons = [[f"💪 {s}" for s in subjects[i:i+2]] for i in range(0, len(subjects), 2)]
        subject_buttons.append(["✅ تمام"])
        await update.message.reply_text(
            "❓ <b>۴. قوی‌ترین درس‌هات چیه؟</b>\n\n"
            "میتونی چند تا انتخاب کنی، بعد دکمه «تمام» رو بزن.",
            reply_markup=ReplyKeyboardMarkup(subject_buttons, resize_keyboard=True),
            parse_mode=ParseMode.HTML
        )

    elif step == 3:
        if text == "✅ تمام":
            context.user_data["onboarding_step"] = 4
            subjects = get_subjects_by_field(data.get("field", ""), data.get("grade", ""))
            subject_buttons = [[f"📉 {s}" for s in subjects[i:i+2]] for i in range(0, len(subjects), 2)]
            subject_buttons.append(["✅ تمام"])
            await update.message.reply_text(
                "❓ <b>۵. ضعیف‌ترین درس‌هات چیه؟</b>",
                reply_markup=ReplyKeyboardMarkup(subject_buttons, resize_keyboard=True),
                parse_mode=ParseMode.HTML
            )
            return
        if text.startswith("💪 "):
            subject = text.replace("💪 ", "").strip()
            if subject not in data.get("strong_subjects", []):
                data.setdefault("strong_subjects", []).append(subject)
        context.user_data["onboarding_data"] = data

    elif step == 4:
        if text == "✅ تمام":
            context.user_data["onboarding_step"] = 5
            await update.message.reply_text(
                "❓ <b>۶. روزی چند ساعت مطالعه می‌کنی؟</b>",
                reply_markup=ReplyKeyboardMarkup(
                    [["⏰ زیر ۲ ساعت", "⏰ ۲ تا ۴ ساعت"],
                     ["⏰ ۴ تا ۶ ساعت", "⏰ ۶ تا ۸ ساعت"],
                     ["⏰ بالای ۸ ساعت", "✏️ عدد دقیق"]],
                    resize_keyboard=True, one_time_keyboard=True
                ),
                parse_mode=ParseMode.HTML
            )
            return
        if text.startswith("📉 "):
            subject = text.replace("📉 ", "").strip()
            if subject not in data.get("weak_subjects", []):
                data.setdefault("weak_subjects", []).append(subject)
        context.user_data["onboarding_data"] = data

    elif step == 5:
        if text == "✏️ عدد دقیق":
            await update.message.reply_text("✏️ تعداد ساعت رو بنویس (مثلاً 5):")
            context.user_data["awaiting_custom"] = "study_hours"
            return
        data["study_hours"] = text
        context.user_data["onboarding_data"] = data
        context.user_data["onboarding_step"] = 6
        await update.message.reply_text(
            "❓ <b>۷. هدف اصلی‌ات چیه؟</b>",
            reply_markup=ReplyKeyboardMarkup(
                [["🎯 کنکور - رتبه برتر", "🎯 کنکور - رتبه متوسط"],
                 ["🎯 کنکور - قبولی رشته خاص", "🎯 معدل بالا"],
                 ["🎯 تقویت پایه", "✏️ سایر"]],
                resize_keyboard=True, one_time_keyboard=True
            ),
            parse_mode=ParseMode.HTML
        )

    elif step == 6:
        if text == "✏️ سایر":
            await update.message.reply_text("✏️ هدفت رو بنویس:")
            context.user_data["awaiting_custom"] = "goal"
            return
        await _finalize_onboarding(update, context, data, goal=text)


async def _finalize_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                data: Dict, goal: str = None) -> None:
    data["goal"] = goal or data.get("goal", "")
    data["is_onboarded"] = True
    context.user_data["onboarding_data"] = data
    final_data = {
        "telegram_id": data["telegram_id"],
        "username": data.get("username"),
        "full_name": data.get("full_name"),
        "goal": data.get("goal", ""),
        "grade": data.get("grade", ""),
        "field": data.get("field", ""),
        "weak_subjects": data.get("weak_subjects", []),
        "strong_subjects": data.get("strong_subjects", []),
        "providers": data.get("providers", []),
        "study_hours_per_week": parse_study_hours(data.get("study_hours", "")),
        "peak_time": None, "focus_duration": 45, "break_duration": 10,
        "is_onboarded": True,
        "onboarding_profile": {
            "subjects_resources": data.get("subjects_resources", {}),
            "study_hours": data.get("study_hours"),
            "resources_text": data.get("resources_text", "")
        }
    }
    user_id = save_user(final_data)
    if user_id:
        add_kok(user_id, 30, "signup", description="هدیه ثبت‌نام")
        for k in ["onboarding_step", "onboarding_data"]:
            context.user_data.pop(k, None)
        await update.message.reply_text(
            "🎉 **ثبت‌نام با موفقیت انجام شد!**\n\n"
            f"🎓 پایه: {final_data.get('grade')}\n"
            f"🧪 رشته: {final_data.get('field')}\n"
            f"🎯 هدف: {final_data.get('goal')}\n\n"
            f"🎁 **هدیه ثبت‌نام: ۳۰ KOK**\n\n"
            "برای شروع، از منو استفاده کن 👇",
            reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            "❌ خطا در ثبت اطلاعات. لطفاً دوباره /start رو بزن.",
            reply_markup=get_main_keyboard()
        )


async def handle_onboarding_custom(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    custom_type = context.user_data.get("awaiting_custom")
    if not custom_type:
        return False
    data = context.user_data.get("onboarding_data", {})
    context.user_data.pop("awaiting_custom", None)

    if custom_type == "grade":
        data["grade"] = text
        context.user_data["onboarding_data"] = data
        context.user_data["onboarding_step"] = 1
        await update.message.reply_text(
            "❓ <b>۲. رشته تحصیلی‌ات چیه؟</b>",
            reply_markup=ReplyKeyboardMarkup(
                [["🧪 تجربی", "➗ ریاضی"], ["📖 انسانی", "🔧 فنی‌وحرفه‌ای"], ["✏️ سایر"]],
                resize_keyboard=True, one_time_keyboard=True
            ),
            parse_mode=ParseMode.HTML
        )
        return True
    elif custom_type == "field":
        data["field"] = text
        context.user_data["onboarding_data"] = data
        context.user_data["onboarding_step"] = 2
        subjects = get_subjects_by_field(data.get("field", ""), data.get("grade", ""))
        subjects_text = "\n".join([f"📚 {s}" for s in subjects])
        await update.message.reply_text(
            f"❓ <b>۳. منابع مطالعاتی‌ات چیه؟</b>\n\n{subjects_text}",
            reply_markup=ReplyKeyboardMarkup([["⏭ رد کردن"]], resize_keyboard=True, one_time_keyboard=True),
            parse_mode=ParseMode.HTML
        )
        return True
    elif custom_type == "study_hours":
        data["study_hours"] = text
        context.user_data["onboarding_data"] = data
        context.user_data["onboarding_step"] = 6
        await update.message.reply_text(
            "❓ <b>۷. هدف اصلی‌ات چیه؟</b>",
            reply_markup=ReplyKeyboardMarkup(
                [["🎯 کنکور - رتبه برتر", "🎯 کنکور - رتبه متوسط"],
                 ["🎯 کنکور - قبولی رشته خاص", "🎯 معدل بالا"],
                 ["🎯 تقویت پایه", "✏️ سایر"]],
                resize_keyboard=True, one_time_keyboard=True
            ),
            parse_mode=ParseMode.HTML
        )
        return True
    elif custom_type == "goal":
        await _finalize_onboarding(update, context, data, goal=text)
        return True
    return False


# ==================== نمایش ====================
def get_activity_emoji(activity: str) -> str:
    emojis = {"study": "📖", "test": "📝", "review": "🔁",
              "class": "🏫", "rest": "😴", "exercise": "🏃",
              "fun": "🎮", "other": "📌", "video": "🎥",
              "reminder": "🔔", "routine": "⏰"}
    return emojis.get(activity, "📖")


async def show_parts_initial(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: List[Dict]) -> None:
    if not parts:
        await update.message.reply_text("❌ هیچ پارتی وجود ندارد.", reply_markup=get_main_keyboard())
        return
    text = f"📋 **برنامه پیشنهادی**\n\n"
    text += f"📊 تعداد پارت‌ها: {len(parts)}\n"
    text += f"⏱ زمان کل: {format_time_hours_minutes(sum(p['planned_minutes'] for p in parts))}\n\n"
    for part in sorted(parts, key=lambda x: x.get("part_number", 0)):
        planned_start = part.get("planned_start_time") or part.get("planned_start") or ""
        planned_end = part.get("planned_end_time") or part.get("planned_end") or ""
        time_info = f" {planned_start}-{planned_end}" if planned_start and planned_end else (
            f" {part['time_slot']}" if part.get("time_slot") else "")
        fixed_tag = " 🔒" if part.get("is_fixed_time", False) else ""
        floating_tag = " 🕊" if is_floating_part(part) else ""
        reminder_tag = " 🔔" if is_reminder_part(part) else ""
        emoji = get_activity_emoji(part.get('activity', 'study'))
        text += (f"{part['part_number']}. ⬜ {emoji} {part['title']} "
                 f"({part['planned_minutes']}د){time_info}{fixed_tag}{floating_tag}{reminder_tag} ↕️\n")
    text += "\n🔧 **تنظیم ترتیب:**\n"
    text += "• با زدن دکمه ↕️ هر پارت، یک ردیف بالا می‌رود\n"
    text += "• بعد دکمه «تایید برنامه»"
    await update.message.reply_text(text, reply_markup=get_part_buttons_initial(parts), parse_mode=ParseMode.HTML)


async def show_parts_final(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            parts: List[Dict], show_date: bool = False) -> None:
    if not parts:
        await update.message.reply_text("📭 هیچ پارتی وجود ندارد.", reply_markup=get_main_keyboard())
        return
    selected_date = context.user_data.get("selected_date")
    today = get_today_date()
    back_to_calendar = bool(selected_date and selected_date != today)
    is_past = False
    is_future = False
    if selected_date:
        try:
            sd = datetime.strptime(selected_date, "%Y-%m-%d").date()
            td = datetime.strptime(today, "%Y-%m-%d").date()
            is_past = sd < td
            is_future = sd > td
        except Exception:
            pass

    def sort_key(p):
        st = p.get("planned_start_time") or p.get("planned_start") or "00:00"
        try:
            return (0, time_to_minutes(st))
        except Exception:
            return (1, p.get("part_number", 0))
    sorted_parts = sorted(parts, key=sort_key)

    if show_date and selected_date:
        text = f"📋 برنامه {get_shamsi_date(selected_date)}\n\n"
    else:
        text = "📋 برنامه نهایی\n\n"

    total_parts = len(sorted_parts)
    completed_parts = sum(1 for p in sorted_parts if p.get("completed", False))
    total_minutes = sum(p.get("planned_minutes", 0) for p in sorted_parts)
    text += (f"📊 تعداد: {total_parts} | ✅ انجام: {completed_parts} | "
             f"⬜ باقی: {total_parts - completed_parts}\n")
    text += f"⏱ زمان کل: {format_time_hours_minutes(total_minutes)}\n\n"
    for part in sorted_parts:
        status = "✅" if part.get("completed", False) else "⬜"
        planned_start = part.get("planned_start_time") or part.get("planned_start") or ""
        planned_end = part.get("planned_end_time") or part.get("planned_end") or ""
        time_info = f" {planned_start}-{planned_end}" if planned_start and planned_end else (
            f" {part['time_slot']}" if part.get("time_slot") else "")
        fixed_tag = " 🔒" if part.get("is_fixed_time", False) else ""
        floating_tag = " 🕊" if is_floating_part(part) else ""
        reminder_tag = " 🔔" if is_reminder_part(part) else ""
        overflow_tag = " ⚠️" if (part.get("metadata") or {}).get("is_overflow") else ""
        actual_info = (f" (واقعی: {part['actual_minutes']}د)"
                       if part.get("completed") and part.get("actual_minutes", 0) > 0 else "")
        emoji = get_activity_emoji(part.get('activity', 'study'))
        text += (f"{part.get('part_number', 0)}. {status} {emoji} {part['title']} "
                 f"({part.get('planned_minutes', 0)}د){time_info}{fixed_tag}{floating_tag}"
                 f"{reminder_tag}{overflow_tag}{actual_info}\n")
    text += "\n⏰ روی هر پارت کلیک کن تا عملیات نمایش داده شود"
    await update.message.reply_text(
        text,
        reply_markup=get_part_buttons_final(sorted_parts, show_date,
                                             back_to_calendar=back_to_calendar,
                                             is_past=is_past, is_future=is_future),
        parse_mode=ParseMode.HTML
    )


async def show_part_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    part = next((p for p in parts if p.get("part_id") == part_id), None)
    if not part:
        db_result = execute_query(
            """SELECT part_id, part_number, title, planned_minutes, actual_minutes,
                      time_slot, completed, pages,
                      to_char(planned_start_time, 'HH24:MI'), to_char(planned_end_time, 'HH24:MI'),
                      is_fixed_time, reason, explicit_start, explicit_end, depends_on,
                      activity_type, content_type, metadata
               FROM study_parts WHERE part_id = %s""",
            (part_id,), fetch=True
        )
        if not db_result:
            await update.message.reply_text("❌ پارت یافت نشد.")
            return
        part = {
            "part_id": db_result[0], "part_number": db_result[1], "title": db_result[2],
            "planned_minutes": db_result[3], "actual_minutes": db_result[4] or 0,
            "time_slot": db_result[5] or "", "completed": db_result[6], "pages": db_result[7] or 0,
            "planned_start_time": db_result[8] or "", "planned_end_time": db_result[9] or "",
            "planned_start": db_result[8] or "", "planned_end": db_result[9] or "",
            "is_fixed_time": db_result[10] or False, "reason": db_result[11] or "",
            "explicit_start": db_result[12], "explicit_end": db_result[13],
            "depends_on": db_result[14], "activity_type": db_result[15] or "study",
            "content_type": db_result[16] or "book", "metadata": db_result[17] or {}
        }
        if not any(p.get("part_id") == part_id for p in parts):
            parts.append(part)
            plan["parts"] = parts
            context.user_data["current_plan"] = plan

    if part.get("completed"):
        text = f"✅ <b>{part['title']}</b> (انجام شده)\n\n"
        text += (f"⏱ برنامه: {part['planned_minutes']}د | "
                 f"واقعی: {part.get('actual_minutes', part['planned_minutes'])}د\n")
        if part.get("planned_start") and part.get("planned_end"):
            text += f"🕒 {part['planned_start']} - {part['planned_end']}\n"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    text = f"📖 <b>{part['title']}</b>\n\n⏱ زمان: {part['planned_minutes']} دقیقه\n"
    if part.get("planned_start") and part.get("planned_end"):
        text += f"🕒 {part['planned_start']} - {part['planned_end']}\n"
    if part.get("is_fixed_time"):
        text += "🔒 زمان ثابت\n"
    if is_floating_part(part):
        orig = (part.get("metadata") or {}).get("original_time") or {}
        if orig.get("start"):
            text += f"🕊 شناور (قبلاً {orig.get('start')} بود)\n"
        else:
            text += "🕊 شناور\n"
    if is_reminder_part(part):
        text += "🔔 یادآور\n"
    if part.get("reason"):
        text += f"📝 {part['reason']}\n"
    text += "✅ وضعیت: در انتظار ⬜\n"
    context.user_data["active_part"] = part_id
    context.user_data["back_to_date"] = context.user_data.get("selected_date") or get_today_date()
    is_running = part_id in active_timers
    elapsed = get_current_elapsed(part_id)
    await update.message.reply_text(
        text,
        reply_markup=get_part_detail_buttons(part_id, is_running, elapsed,
                                              is_reminder=is_reminder_part(part)),
        parse_mode=ParseMode.HTML
    )


# ==================== Nav Back مرکزی — بخش ۲، ۴ ====================
async def handle_back_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    بازگشت مرکزی:
    1. اگر pending_confirmation است → پرسش «لغو کنم؟»
    2. pop از nav_stack
    3. اگر خالی بود → منو اصلی
    """
    # pending تأییدها
    if context.user_data.get("pending_change"):
        await update.message.reply_text(
            "⚠️ یک تغییر در انتظار تایید است. لغو کنم؟",
            reply_markup=ReplyKeyboardMarkup(
                [["✅ تایید تغییر"], ["❌ رد تغییر"]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
        return
    if context.user_data.get("pending_card_update"):
        await update.message.reply_text(
            "⚠️ یک ویرایش کارت در انتظار است. لغو کنم؟",
            reply_markup=ReplyKeyboardMarkup(
                [["✅ ذخیره تغییرات"], ["🔙 بازگشت"]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
        return

    # پاک‌سازی stateهای صفحه قبل
    for k in ("active_part", "edit_mode", "edit_selected_part",
              "cards_page", "admin_cards_mode", "admin_cards_page",
              "project_mode", "pending_project_source", "pending_project_card",
              "pending_routine_cards", "pending_routine_clarify",
              "pending_reminder_data", "pending_reminder_plan",
              "pending_card_suggestion", "pending_split", "pending_clarify",
              "pending_class_card", "pending_card_delete", "pending_delete",
              "pending_early_complete", "challenge_mode",
              "rag_mode", "role_mode", "awaiting_card_text",
              "pending_overflow_choice", "pending_calendar_update",
              "pending_compaction"):
        context.user_data.pop(k, None)

    entry = nav_pop(context)
    if entry is None:
        context.user_data.pop("current_plan", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("back_to_date", None)
        await update.message.reply_text("🔙 بازگشت به صفحه اصلی", reply_markup=get_main_keyboard())
        return

    page = entry.get("page")
    if page == "today_plan":
        await handle_today_plan(update, context)
    elif page == "cards":
        await handle_cards_list(update, context)
    elif page == "calendar":
        user_id = get_user_id_by_telegram(update.effective_user.id)
        if user_id:
            page_num = context.user_data.get("calendar_page", 0)
            await show_calendar_page(update, context, user_id, page=page_num)
    elif page == "report":
        await handle_report(update, context)
    elif page == "subscription":
        await handle_subscription(update, context)
    elif page == "projects":
        await project_command(update, context)
    elif page == "admin":
        user_id = update.effective_user.id
        if user_id in ADMIN_IDS:
            await admin_command(update, context)
        else:
            await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
    elif page == "plan_final":
        plan = context.user_data.get("current_plan") or {}
        if plan.get("parts"):
            await show_parts_final(update, context, plan["parts"])
        else:
            await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("🔙 بازگشت به صفحه اصلی", reply_markup=get_main_keyboard())


# ==================== تقویم ====================
async def handle_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    context.user_data["calendar_page"] = 0
    nav_push(context, "calendar")
    await show_calendar_page(update, context, user_id, page=0)


async def show_calendar_page(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              user_id: int, page: int = 0) -> None:
    context.user_data["calendar_page"] = page
    items = get_calendar_dates(user_id, page)
    if not items:
        await update.message.reply_text("📭 داده‌ای موجود نیست.", reply_markup=get_main_keyboard())
        return
    if page == 0:
        title = "📅 **تقویم — امروز و ۷ روز آینده**"
    elif page < 0:
        title = f"📅 **تقویم — صفحه {abs(page)} قبل**"
    else:
        title = f"📅 **تقویم — صفحه {page} بعد**"
    text = f"{title}\n\n"
    keyboard = []
    for item in items:
        label = format_calendar_date(item)
        keyboard.append([f"📅 {item['shamsi']} | {label}"])
    nav_row = []
    if page > -10:
        nav_row.append("⬅️ صفحه قبل")
    if page < 10:
        nav_row.append("➡️ صفحه بعد")
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append(["🔙 بازگشت"])
    await update.message.reply_text(
        text, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_calendar_nav(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    current_page = context.user_data.get("calendar_page", 0)
    if text == "⬅️ صفحه قبل":
        new_page = current_page - 1
    elif text == "➡️ صفحه بعد":
        new_page = current_page + 1
    else:
        return
    await show_calendar_page(update, context, user_id, page=new_page)


async def handle_calendar_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if text in ["⬅️ صفحه قبل", "➡️ صفحه بعد"]:
        await handle_calendar_nav(update, context, text)
        return
    if not text.startswith("📅 "):
        return
    try:
        date_part = text.replace("📅 ", "").split("|")[0].strip()
        shamsi_date = date_part.strip()
    except Exception:
        return
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    try:
        parts = shamsi_date.split("/")
        if len(parts) == 3:
            year, month, day = map(int, parts)
            jdate = jdatetime.date(year, month, day)
            date_str = jdate.togregorian().strftime("%Y-%m-%d")
        else:
            return
    except Exception:
        await update.message.reply_text("❌ تاریخ نامعتبر.")
        return
    plan = get_plan_by_date(user_id, date_str)
    context.user_data["selected_date"] = date_str
    context.user_data["selected_shamsi"] = shamsi_date
    nav_push(context, "plan_final")
    if not plan or not plan.get("parts"):
        await update.message.reply_text(
            f"📭 در {shamsi_date} برنامه‌ای نداری.",
            reply_markup=ReplyKeyboardMarkup(
                [["🧠 ساخت برنامه برای این روز"], ["🔙 بازگشت"]],
                resize_keyboard=True
            )
        )
        return
    context.user_data["current_plan"] = plan
    if plan.get("confirmed", False):
        await show_parts_final(update, context, plan["parts"], show_date=True)
    else:
        await show_parts_initial(update, context, plan["parts"])


# ==================== کارت‌های من ====================
CARD_TYPE_LABELS_FA = {
    "routine": "روتین", "project": "پروژه", "class": "کلاس",
    "exam": "امتحان", "syllabus": "سرفصل", "resource": "منبع",
    "microtopic": "ریزمبحث", "rule": "قانون",
    "reminder": "یادآور", "news": "خبر",
}


def _is_card_expired(card: Dict) -> bool:
    ctype = card.get("card_type")
    data = card.get("data") or {}
    today = get_iran_now().date()
    now_time_min = get_iran_now().hour * 60 + get_iran_now().minute
    if ctype == "class":
        if data.get("recurring") or data.get("day_of_week"):
            return False
        d = data.get("date")
        if not d:
            return False
        try:
            d_date = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            return False
        if d_date < today:
            return True
        if d_date == today and data.get("end_time"):
            try:
                return now_time_min > time_to_minutes(data["end_time"])
            except Exception:
                return False
        return False
    if ctype == "exam":
        d = data.get("date") or data.get("exam_date_gregorian")
        if not d:
            return False
        try:
            d_date = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            return False
        return d_date < today
    if ctype == "reminder":
        d = data.get("date")
        if not d:
            return False
        try:
            d_date = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            return False
        if d_date < today:
            return True
        if d_date == today and data.get("time"):
            try:
                return now_time_min > (time_to_minutes(data["time"]) + 24 * 60)
            except Exception:
                return False
        return False
    return False


def _purge_expired_cards(user_id: int) -> int:
    cards = fetch_cards(user_id=user_id, mode="own", include_inactive=False)
    removed = 0
    for c in cards:
        if _is_card_expired(c):
            try:
                deactivate_card(c["id"])
                delete_rag_by_source(f"card_{c['id']}")
                removed += 1
            except Exception as e:
                logger.error(f"خطا در حذف کارت منقضی {c['id']}: {e}")
    return removed


async def handle_cards_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    _purge_expired_cards(user_id)
    cards = fetch_cards(user_id=user_id, mode="own", include_inactive=False)
    if not cards:
        await update.message.reply_text(
            "📭 هنوز کارتی نداری.\n\nاز /routin یا /project می‌تونی کارت بسازی.",
            reply_markup=get_main_keyboard()
        )
        return
    page = context.user_data.get("cards_page", 0)
    per_page = 8
    total_pages = max(1, (len(cards) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    context.user_data["cards_page"] = page
    page_cards = cards[page * per_page:(page + 1) * per_page]
    text = f"📋 **کارت‌های من** ({len(cards)} کارت — صفحه {page + 1} از {total_pages})\n\n"
    keyboard = []
    for c in page_cards:
        data = c["data"]
        title = data.get("title", "بدون عنوان")[:30]
        ctype = c["card_type"]
        label = CARD_TYPE_LABELS_FA.get(ctype, ctype)
        emoji = {"routine": "⏰", "project": "🎯", "class": "🏫",
                 "exam": "📝", "syllabus": "📚", "resource": "📖",
                 "microtopic": "🔬", "rule": "📜", "reminder": "🔔"}.get(ctype, "📌")
        keyboard.append([f"{emoji} {label} | {title} [{c['id']}]"])
    nav_row = []
    if page > 0:
        nav_row.append("⬅️ صفحه قبل")
    if page < total_pages - 1:
        nav_row.append("➡️ صفحه بعد")
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append(["🔙 بازگشت"])
    # ثبت nav
    nav_push(context, "cards")
    await update.message.reply_text(
        text, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def show_card_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, card_id: int) -> None:
    """بخش ۳.۴ — اگر کارت نیست، پیام + nav pop"""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    card_row = execute_query(
        """SELECT id, card_type, content_type, data, priority, is_active, created_by
           FROM cards WHERE id = %s""",
        (card_id,), fetch=True
    )
    if not card_row:
        logger.info(f"show_card_detail: not_found card_id={card_id} user={user_id}")
        await update.message.reply_text(
            "❌ این کارت دیگر وجود ندارد. به لیست برمی‌گردم...",
            reply_markup=get_main_keyboard()
        )
        await handle_cards_list(update, context)
        return
    cid, ctype, content_type, data, priority, is_active, created_by = card_row
    is_owner = (created_by == user_id)
    is_admin = update.effective_user.id in ADMIN_IDS
    can_edit = is_owner or is_admin
    data = data or {}
    title = data.get("title", "بدون عنوان")
    type_label = CARD_TYPE_LABELS_FA.get(ctype, ctype)
    text = f"📌 **{title}**\n🏷 نوع: {type_label}\n"
    if not is_active:
        text += "🔴 غیرفعال\n"
    text += "\n"
    if ctype == "routine":
        rec = data.get("recurrence", "")
        days = data.get("days_of_week", [])
        text += f"🔁 تکرار: {rec}"
        if days:
            text += f" ({', '.join(days)})"
        text += "\n"
        if data.get("start_time"):
            text += f"⏰ {data.get('start_time')}-{data.get('end_time', '')}\n"
    elif ctype == "project":
        if data.get("provider"):
            text += f"🎓 {data['provider']}\n"
        if data.get("subjects"):
            text += f"📚 {', '.join(data['subjects'])}\n"
        if data.get("exam_date_shamsi"):
            text += f"📅 {data['exam_date_shamsi']}\n"
    elif ctype == "class":
        if data.get("teacher"):
            text += f"🎙 {data['teacher']}\n"
        if data.get("start_time"):
            text += f"⏰ {data.get('start_time')}-{data.get('end_time', '')}\n"
        if data.get("date"):
            text += f"📅 {get_shamsi_date(data['date'])}\n"
    elif ctype in ("exam", "syllabus"):
        if data.get("subject"):
            text += f"📖 {data['subject']}\n"
        if data.get("exam_date_shamsi"):
            text += f"📅 {data['exam_date_shamsi']}\n"
    elif ctype == "reminder":
        if data.get("date"):
            text += f"📅 {get_shamsi_date(data['date'])}\n"
        if data.get("time"):
            text += f"⏰ {data['time']}\n"
        if data.get("message"):
            text += f"💬 {data['message']}\n"
    elif ctype == "microtopic":
        if data.get("subject"):
            text += f"📖 {data['subject']}\n"
        if data.get("topic"):
            text += f"📚 {data['topic']}\n"
    keyboard = []
    if can_edit:
        keyboard.append(["✏️ ویرایش"])
        if is_active:
            keyboard.append(["🗑 حذف کارت"])
    keyboard.append(["🔙 بازگشت به لیست"])
    context.user_data["card_edit_target"] = card_id
    await update.message.reply_text(
        text, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def start_card_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    card_id = context.user_data.get("card_edit_target")
    if not card_id:
        await update.message.reply_text("❌ کارتی انتخاب نشده.")
        return
    card_row = execute_query("SELECT data FROM cards WHERE id = %s", (card_id,), fetch=True)
    if not card_row:
        await update.message.reply_text("❌ کارت یافت نشد.")
        return
    data = card_row[0] or {}
    title = data.get("title", "بدون عنوان")
    context.user_data["card_edit_mode"] = True
    await update.message.reply_text(
        f"✏️ **ویرایش کارت: {title}**\n\n"
        f"متن آزاد بنویس چی رو تغییر بدم.\n\n"
        f"📝 مثال: «عنوان رو بکن X» / «ساعت رو بکن ۱۸»",
        reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_card_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    card_id = context.user_data.get("card_edit_target")
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not card_id or not user_id:
        return
    if text == "🔙 بازگشت":
        context.user_data.pop("card_edit_mode", None)
        await show_card_detail(update, context, card_id)
        return
    if not await check_and_consume_kok(update, user_id, "card_edit"):
        return
    card_row = execute_query(
        "SELECT card_type, content_type, data FROM cards WHERE id = %s",
        (card_id,), fetch=True
    )
    if not card_row:
        await update.message.reply_text("❌ کارت یافت نشد.")
        return
    ctype, content_type, data = card_row
    data = data or {}
    wait = await update.message.reply_text("🧠 در حال تحلیل...")
    prompt = f"""تو یه ویرایش‌گر کارت هستی.

=== کارت فعلی ===
نوع: {ctype}
داده: {json.dumps(data, ensure_ascii=False, indent=2)[:3000]}

=== درخواست ===
"{text}"

=== وظیفه ===
فقط فیلدهای تغییر یافته رو برگردون.

=== خروجی JSON ===
{{
  "changed_fields": {{"field_name": "new_value"}},
  "reasoning": "توضیح کوتاه"
}}
⚠️ فقط JSON خالص."""
    model_type = get_user_model_type(user_id)
    response = await call_ai(prompt, max_tokens=1500, temperature=0.2, model_type=model_type)
    await wait.delete()
    if not response:
        await update.message.reply_text("❌ AI پاسخ نداد.")
        return
    result = safe_json_loads(response, dict)
    if not result:
        await update.message.reply_text("❌ خطا در تحلیل.")
        return
    changed = result.get("changed_fields", {})
    if not isinstance(changed, dict) or not changed:
        await update.message.reply_text("❌ هیچ تغییری تشخیص داده نشد.")
        return
    new_data = dict(data)
    for k, v in changed.items():
        new_data[k] = v
    context.user_data["pending_card_update"] = {
        "card_id": card_id, "old_data": data,
        "new_data": new_data, "changed": changed,
    }
    text_msg = "📋 **پیش‌نمایش تغییرات:**\n\n"
    for k, v in changed.items():
        old = data.get(k, "—")
        text_msg += f"• {k}: {str(old)[:40]} → {str(v)[:40]}\n"
    text_msg += "\nذخیره کنم؟"
    await update.message.reply_text(
        text_msg,
        reply_markup=ReplyKeyboardMarkup(
            [["✅ ذخیره تغییرات"], ["🔙 بازگشت"]], resize_keyboard=True
        ),
        parse_mode=ParseMode.HTML
    )


async def save_card_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_card_update")
    if not pending:
        return
    card_id = pending["card_id"]
    new_data = pending["new_data"]
    if update_card_data(card_id, new_data):
        try:
            await index_card_to_rag(card_id)
        except Exception:
            pass
        context.user_data.pop("pending_card_update", None)
        context.user_data.pop("card_edit_mode", None)
        await update.message.reply_text(
            "✅ کارت ذخیره شد و RAG دوباره ایندکس شد.",
            reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت به لیست"]], resize_keyboard=True)
        )
    else:
        await update.message.reply_text("❌ خطا در ذخیره.")


async def delete_card_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    card_id = context.user_data.get("card_edit_target")
    if not card_id:
        return
    card_row = execute_query("SELECT data FROM cards WHERE id = %s", (card_id,), fetch=True)
    title = (card_row[0] or {}).get("title", "بدون عنوان") if card_row else "کارت"
    await update.message.reply_text(
        f"⚠️ **مطمئنی کارت برای همیشه حذف شود؟**\n\n"
        f"📌 {title}\n\n"
        f"این کارت کاملاً از لیست تو حذف می‌شه (نه فقط غیرفعال).\n"
        f"پارت‌های وابسته تو برنامه دست‌نخورده می‌مونن.",
        reply_markup=ReplyKeyboardMarkup(
            [["✅ بله، حذف کن"], ["🔙 لغو"]], resize_keyboard=True
        ),
        parse_mode=ParseMode.HTML
    )
    context.user_data["pending_card_delete"] = card_id


async def do_delete_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    card_id = context.user_data.get("pending_card_delete")
    if not card_id:
        return
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if hard_delete_card(card_id):
        logger.info(f"card_hard_deleted: card_id={card_id} user={user_id}")
        context.user_data.pop("pending_card_delete", None)
        context.user_data.pop("card_edit_target", None)
        await update.message.reply_text(
            "✅ کارت برای همیشه حذف شد.",
            reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت به لیست"]], resize_keyboard=True)
        )
        if context.user_data.get("admin_cards_mode"):
            await show_admin_cards_list(update, context)
        else:
            await handle_cards_list(update, context)
    else:
        await update.message.reply_text("❌ خطا در حذف.")


# ==================== پروژه (بازطراحی — بخش ۱۳ و حذف readiness) ====================
async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    if ADMIN_IDS:
        placeholders = ",".join(["%s"] * len(ADMIN_IDS))
        project_cards = execute_query(
            f"""SELECT id, card_type, content_type, data, priority, created_by
                FROM cards
                WHERE is_active = TRUE
                  AND card_type IN ('project', 'exam')
                  AND created_by IN ({placeholders})
                ORDER BY priority DESC, created_at DESC LIMIT 10""",
            tuple(ADMIN_IDS), fetchall=True
        ) or []
    else:
        project_cards = []
    cards = [{"id": r[0], "card_type": r[1], "content_type": r[2],
              "data": r[3] or {}, "priority": r[4] or 5,
              "created_by": r[5]} for r in project_cards]
    active = execute_query(
        """SELECT id, project_type, title, data FROM user_projects
           WHERE user_id = %s AND is_active = TRUE
           ORDER BY created_at DESC LIMIT 1""",
        (user_id,), fetch=True
    )
    active_card_id = None
    if active and active[3]:
        try:
            active_card_id = int((active[3] or {}).get("source_card_id"))
        except Exception:
            active_card_id = None
    text = "🎯 **پروژه‌ها**\n\n"
    if active:
        text += f"**فعال:** 📌 {active[2]}\n\n"
    if cards:
        text += "پروژه‌های موجود:"
    else:
        text += "هنوز پروژه‌ای توسط ادمین اضافه نشده.\nمی‌تونی پروژه دلخواه بسازی."
    nav_push(context, "projects")
    await update.message.reply_text(
        text, reply_markup=get_project_keyboard(cards, active_card_id),
        parse_mode=ParseMode.HTML
    )


def render_project_summary(card_data: Dict, is_active: bool) -> str:
    title = card_data.get("title", "پروژه")
    provider = card_data.get("provider", "")
    subjects = card_data.get("subjects", [])
    if not isinstance(subjects, list):
        subjects = []
    exam_date = card_data.get("exam_date_shamsi", "")
    text = f"🎯 **{title}**\n\n"
    if provider:
        text += f"🎓 ارائه‌دهنده: {provider}\n"
    if subjects:
        text += f"📚 درس‌ها: {', '.join(subjects)}\n"
    if exam_date:
        text += f"📅 تاریخ آزمون: {exam_date}\n"
    text += f"\nوضعیت: {'✅ فعال' if is_active else '⬜ غیرفعال'}\n"
    return text


async def handle_project_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """بخش ۱۳.۱ و ۱۳.۲ — کلیک روی پروژه → خلاصه، سپس تایید یا مدیریت."""
    if not text.startswith("🎯 "):
        return
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    id_match = re.search(r'\[(\d+)\]', text)
    if not id_match:
        await update.message.reply_text("❌ کارت پیدا نشد.")
        return
    card_id = int(id_match.group(1))
    result = execute_query(
        """SELECT id, data FROM cards
           WHERE id = %s AND is_active = TRUE
             AND card_type IN ('project', 'exam')""",
        (card_id,), fetch=True
    )
    if not result:
        await update.message.reply_text("❌ کارت پیدا نشد.")
        return
    _, data = result
    data = data or {}

    # بررسی آیا این پروژه فعال است
    active = execute_query(
        """SELECT id, data FROM user_projects
           WHERE user_id = %s AND is_active = TRUE
           ORDER BY created_at DESC LIMIT 1""",
        (user_id,), fetch=True
    )
    is_active = False
    if active and (active[1] or {}).get("source_card_id") == card_id:
        is_active = True

    context.user_data["pending_project_source"] = {"card_id": card_id, "card_data": data}
    nav_push(context, "project_summary", card_id=card_id)

    summary = render_project_summary(data, is_active)
    if is_active:
        summary += "\nچه کاری انجام بدم؟"
    else:
        summary += "\nفعال کنم و برنامه هفته رو بسازم؟"

    await update.message.reply_text(
        summary,
        reply_markup=get_project_summary_keyboard(is_active),
        parse_mode=ParseMode.HTML
    )


async def handle_project_readiness(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """این تابع دیگر استفاده نمی‌شود — طبق دستور حذف readiness. برای سازگاری خالی مانده."""
    return


async def confirm_project_activation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بخش ۱۳.۲ — فعال‌سازی بدون readiness (همیشه medium)."""
    pending = context.user_data.get("pending_project_source")
    if not pending:
        return
    user_id = get_user_id_by_telegram(update.effective_user.id)
    user_data = get_user_data(str(update.effective_user.id))
    if not user_id:
        return

    if not await check_and_consume_kok(update, user_id, "project_setup"):
        return

    card_data = pending["card_data"]
    card_id = pending["card_id"]

    wait = await update.message.reply_text("🧠 در حال فعال‌سازی و ساخت برنامه هفته...")
    try:
        project_record = {
            "source_card_id": card_id,
            "readiness_level": "medium",  # همیشه medium
            "activated_at": get_today_date(),
            "card_snapshot": card_data,
        }
        result = execute_query(
            """INSERT INTO user_projects (user_id, project_type, title, data, is_active)
               VALUES (%s, %s, %s, %s, TRUE) RETURNING id""",
            (user_id, card_data.get("card_type", "project"),
             card_data.get("title", "پروژه"), json.dumps(project_record)),
            fetch=True
        )
        if result:
            project_id = result[0]
            execute_query(
                "UPDATE user_projects SET is_active = FALSE WHERE user_id = %s AND id != %s",
                (user_id, project_id)
            )
            await rebuild_week_for_project_internal(user_id, user_data, card_data, "medium")
            context.user_data.pop("pending_project_source", None)
            try:
                await wait.delete()
            except Exception:
                pass
            await update.message.reply_text(
                f"✅ **پروژه فعال شد و برنامه هفته ساخته شد!**\n\n"
                f"📌 {card_data.get('title')}\n\n"
                f"از این به بعد، «ساخت با AI» حول این پروژه خواهد بود.",
                reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
            )
        else:
            try:
                await wait.delete()
            except Exception:
                pass
            await update.message.reply_text("❌ خطا در ذخیره پروژه.")
    except Exception as e:
        logger.error(f"خطا در confirm_project_activation: {e}")
        try:
            await wait.delete()
        except Exception:
            pass
        await update.message.reply_text(f"❌ خطا: {str(e)[:100]}")


async def handle_project_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """پروژه دلخواه آزاد — بدون readiness."""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    user_data = get_user_data(str(update.effective_user.id))
    if not user_id:
        return
    if text == "🔙 بازگشت":
        context.user_data.pop("project_mode", None)
        await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
        return
    if not await check_and_consume_kok(update, user_id, "project_setup"):
        return
    wait = await update.message.reply_text("🧠 در حال تحلیل...")
    prompt = f"""متن زیر یک پروژه/چالش مطالعه کاربر است. به کارت پروژه ساختاریافته تبدیل کن.

=== متن کاربر ===
"{text}"

=== پروفایل ===
پایه: {user_data.get('grade', 'نامشخص') if user_data else 'نامشخص'}
رشته: {user_data.get('field', 'نامشخص') if user_data else 'نامشخص'}
هدف: {user_data.get('goal', 'نامشخص') if user_data else 'نامشخص'}

=== خروجی JSON ===
{{
  "project_type": "provider_based|from_scratch|challenge|routine|exam_path",
  "title": "عنوان",
  "provider": "",
  "subjects": ["ریاضی"],
  "commitment": {{"duration_weeks": 4, "sessions_per_week": 4, "session_duration": 90}},
  "schedule_pattern": {{"days": ["شنبه"], "time": "18:00", "is_fixed": true}},
  "goals": ["هدف"],
  "priority": 8
}}
⚠️ فقط JSON خالص."""
    model_type = get_user_model_type(user_id)
    response = await call_ai(prompt, max_tokens=1500, temperature=0.3, model_type=model_type)
    await wait.delete()
    if not response:
        await update.message.reply_text("❌ AI پاسخ نداد.")
        return
    project_data = safe_json_loads(response, dict)
    if not project_data:
        await update.message.reply_text("❌ خطا در تحلیل.")
        return
    context.user_data["pending_project_card"] = {
        "text": text, "project_data": project_data, "user_id": user_id,
    }
    title = project_data.get("title", "پروژه")
    text_msg = f"🎯 **کارت پروژه پیشنهادی**\n\n📌 {title}\n"
    text_msg += f"🏷 نوع: {project_data.get('project_type', '')}\n"
    if project_data.get("provider"):
        text_msg += f"🎓 {project_data['provider']}\n"
    if project_data.get("subjects"):
        subs = project_data['subjects']
        if isinstance(subs, list):
            text_msg += f"📚 {', '.join(subs)}\n"
    text_msg += "\nآیا این کارت رو ذخیره کنم؟"
    await update.message.reply_text(
        text_msg,
        reply_markup=ReplyKeyboardMarkup(
            [["✅ ذخیره پروژه"], ["❌ لغو"]], resize_keyboard=True
        ),
        parse_mode=ParseMode.HTML
    )


async def save_pending_project_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_project_card")
    if not pending:
        return
    user_id = pending["user_id"]
    project_data = pending["project_data"]
    result = execute_query(
        """INSERT INTO user_projects (user_id, project_type, title, data, is_active)
           VALUES (%s, %s, %s, %s, TRUE) RETURNING id""",
        (user_id, project_data.get("project_type", "challenge"),
         project_data.get("title", "پروژه"), json.dumps(project_data)),
        fetch=True
    )
    if not result:
        await update.message.reply_text("❌ خطا در ذخیره.")
        return
    project_id = result[0]
    project_data["project_id"] = project_id
    card_id = save_card(
        card_type="project",
        content_type=project_data.get("project_type", "challenge"),
        data=project_data, priority=project_data.get("priority", 8),
        raw_text=pending["text"], created_by=user_id
    )
    if not card_id:
        await update.message.reply_text("❌ خطا در ذخیره کارت.")
        return
    project_data["source_card_id"] = card_id
    execute_query(
        "UPDATE user_projects SET data = %s WHERE id = %s",
        (json.dumps(project_data), project_id)
    )
    try:
        await index_card_to_rag(card_id)
    except Exception:
        pass
    execute_query(
        "UPDATE user_projects SET is_active = FALSE WHERE user_id = %s AND id != %s",
        (user_id, project_id)
    )
    execute_query(
        """UPDATE cards SET is_active = FALSE
           WHERE created_by = %s AND card_type = 'project' AND id != %s""",
        (user_id, card_id)
    )
    context.user_data.pop("pending_project_card", None)
    context.user_data.pop("project_mode", None)
    wait = await update.message.reply_text("🧠 در حال ساخت برنامه هفته...")
    user_data = get_user_data(str(update.effective_user.id))
    try:
        await rebuild_week_for_project_internal(user_id, user_data, project_data, "medium")
    except Exception as e:
        logger.error(f"خطا در ساخت برنامه پروژه: {e}")
    try:
        await wait.delete()
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ **پروژه فعال شد و برنامه هفته ساخته شد!**\n\n📌 {project_data.get('title')}",
        reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
    )


async def rebuild_week_for_project_internal(user_id: int, user_data: Dict,
                                              card_data: Dict, readiness: str = "medium"):
    """بازسازی هفته — با حفاظت پارهای محافظت‌شده (بخش ۱۰.۷)."""
    today = get_iran_now().date()
    readiness_context = {
        "high": "مرور کوتاه، تمرکز تست",
        "medium": "برنامه متعادل",
        "low": "مطالعه عمیق، پایه‌ای",
    }.get(readiness, "برنامه متعادل")

    request = (
        f"برنامه هفتگی بر اساس کارت «{card_data.get('title')}» بساز.\n"
        f"ارائه‌دهنده: {card_data.get('provider', '')}\n"
        f"درس‌ها: {', '.join(card_data.get('subjects', []) if isinstance(card_data.get('subjects', []), list) else [])}\n"
        f"سطح: {readiness_context}\n"
        f"⚠️ ریزمباحث از کارت ادمین انتخاب کن."
    )
    for i in range(1, 8):
        target_date = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        existing = execute_query(
            """SELECT session_id FROM study_sessions
               WHERE user_id = %s AND date = %s AND archived = FALSE
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, target_date), fetch=True
        )
        if existing:
            execute_query(
                """DELETE FROM study_parts
                   WHERE session_id = %s
                     AND origin IN ('ai', 'project')
                     AND NOT COALESCE(is_fixed_time, FALSE)
                     AND COALESCE(content_type, '') NOT IN ('reminder', 'class')""",
                (existing[0],)
            )
        ai_response = await generate_plan_for_date(user_id, user_data, target_date, request)
        if ai_response and ai_response.get("parts"):
            for p in ai_response["parts"]:
                if isinstance(p, dict):
                    p["origin"] = "project"
            create_plan_for_date(user_id, user_data, ai_response, target_date)
        await asyncio.sleep(0.1)


async def rebuild_week_for_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    user_data = get_user_data(str(update.effective_user.id))
    if not user_id or not user_data:
        return
    wait = await update.message.reply_text("🔄 در حال بازسازی هفته...")
    try:
        project_card = get_active_project_card(user_id)
        if not project_card:
            await wait.delete()
            await update.message.reply_text("❌ پروژه فعالی پیدا نشد.")
            return
        card_data = project_card.get("data", {})
        await rebuild_week_for_project_internal(user_id, user_data, card_data, "medium")
        await wait.delete()
        await update.message.reply_text(
            f"✅ هفته بازسازی شد!\n\n🎯 {card_data.get('title', 'پروژه')}\n\nپارت‌های شخصی و یادآورها حفظ شدند.",
            reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"خطا: {e}")
        try:
            await wait.delete()
        except Exception:
            pass
        await update.message.reply_text(f"❌ خطا: {str(e)[:100]}")


async def delete_active_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بخش ۱۳.۳ — حذف پروژه فعال."""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    active = execute_query(
        """SELECT id, data FROM user_projects
           WHERE user_id = %s AND is_active = TRUE
           ORDER BY created_at DESC LIMIT 1""",
        (user_id,), fetch=True
    )
    if not active:
        await update.message.reply_text("پروژه فعالی نیست.")
        return
    project_id, data = active
    card_id = (data or {}).get("source_card_id")
    try:
        execute_query("UPDATE user_projects SET is_active = FALSE WHERE id = %s", (project_id,))
        if card_id:
            deactivate_card(card_id)
            delete_rag_by_source(f"card_{card_id}")
        await update.message.reply_text(
            "✅ پروژه غیرفعال شد.\n\n"
            "پارت‌های موجود در برنامه دست‌نخورده مانده‌اند. "
            "برای حذف پارتها از دکمه‌های ویرایش برنامه استفاده کن.",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.error(f"خطا در حذف پروژه: {e}")
        await update.message.reply_text(f"❌ خطا: {str(e)[:100]}")


# ==================== چالش ====================
async def handle_challenge_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["challenge_mode"] = "waiting_content"
    await update.message.reply_text(
        "🔥 **چالش جدید**\n\nمحتوای چالش رو بنویس.\n\n"
        "📝 مثال:\n• «جمع‌بندی ریاضی پایه در ۱ ماه»\n"
        "• «روتین هر شب شکل‌های زیست»\n• «۵۰ تست فیزیک در ۲ هفته»",
        reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


# ==================== روتین ====================
async def routin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    context.user_data["routin_mode"] = "free_text"
    await update.message.reply_text(
        "⏰ **تنظیم روتین**\n\nمتن روتینت رو بنویس.\n\n"
        "📝 مثال‌ها:\n• «خوابم ۲۳ تا ۷ صبحه»\n• «هر روز ناهار ۱۳، شام ۲۰»\n"
        "• «کلاس‌هام: شنبه و دوشنبه ۱۶-۱۸»\n• «باشگاه روزهای زوج ۱۸-۲۰»",
        reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_routin_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    if text in ("✅ ذخیره همه", "🔙 لغو", "✅ بله، کارت بساز"):
        return
    if text == "🔙 بازگشت":
        context.user_data.pop("routin_mode", None)
        await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
        return
    if not await check_and_consume_kok(update, user_id, "routine_setup"):
        return
    wait = await update.message.reply_text("🧠 در حال تحلیل...")
    prompt = f"""تو یه تحلیل‌گر متن روتین روزانه هستی.

=== متن ===
"{text}"

انواع: sleep, meal, class, study, exercise, other.
⚠️ «N بخش» یا «N جلسه» در عنوان نگذار.
⚠️ خروجی آرایه JSON خالص."""
    model_type = get_user_model_type(user_id)
    response = await call_ai(prompt, max_tokens=2000, temperature=0.2, model_type=model_type)
    await wait.delete()
    if not response:
        await update.message.reply_text("❌ AI پاسخ نداد.")
        return
    routines = safe_json_loads(response, list)
    if not routines:
        await update.message.reply_text("❌ خطا در تحلیل.")
        return
    routines = [r for r in routines if isinstance(r, dict)]
    if not routines:
        await update.message.reply_text("❌ روتینی تشخیص داده نشد.")
        return
    needs_clarify = False
    clarify_idx = 0
    for i, r in enumerate(routines):
        if not r.get("start_time") or not r.get("end_time"):
            needs_clarify = True
            clarify_idx = i
            break
    if needs_clarify:
        context.user_data["pending_routine_clarify"] = {
            "routines": routines, "text": text, "user_id": user_id,
            "question_key": "time", "index": clarify_idx,
        }
        r0 = routines[clarify_idx]
        await update.message.reply_text(
            f"⏰ **ساعت {r0.get('title', 'این روتین')} رو مشخص کن.**\n\n"
            f"مثال: `23:00` یا `23:00 تا 07:00`",
            reply_markup=ReplyKeyboardMarkup([["🔙 لغو"]], resize_keyboard=True),
            parse_mode=ParseMode.HTML
        )
        return
    context.user_data["pending_routine_cards"] = {
        "routines": routines, "text": text, "user_id": user_id,
    }
    text_msg = f"📋 **{len(routines)} روتین تشخیص داده شد**\n\n"
    for i, r in enumerate(routines, 1):
        title = r.get("title", "روتین")
        rtype = r.get("routine_type", "")
        rec = r.get("recurrence", "daily")
        days = r.get("days_of_week", [])
        st = r.get("start_time", "")
        en = r.get("end_time", "")
        emoji = {"sleep": "🛏", "meal": "🍽", "class": "🏫",
                 "study": "📚", "exercise": "🏋️", "other": "📌"}.get(rtype, "📌")
        rec_text = "هر روز" if rec == "daily" else (", ".join(days) if days else rec)
        time_info = f" {st}-{en}" if st and en else ""
        text_msg += f"{i}. {emoji} {title} — {rec_text}{time_info}\n"
    text_msg += "\nذخیره کنم؟"
    await update.message.reply_text(
        text_msg,
        reply_markup=ReplyKeyboardMarkup([["✅ ذخیره همه"], ["🔙 لغو"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_routine_clarify_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    pending = context.user_data.get("pending_routine_clarify")
    if not pending:
        return
    if text == "🔙 لغو":
        context.user_data.pop("pending_routine_clarify", None)
        context.user_data.pop("pending_routine_cards", None)
        context.user_data.pop("routin_mode", None)
        await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
        return
    routines = pending.get("routines", [])
    question_key = pending.get("question_key")
    idx = pending.get("index", 0)
    if question_key == "time" and idx < len(routines):
        normalized = normalize_time_str(text) or text
        if "تا" in normalized or "-" in normalized:
            try:
                parts_split = re.split(r'\s*(?:تا|[-–—])\s*', normalized)
                if len(parts_split) == 2:
                    h1, m1 = map(int, parts_split[0].strip().split(":"))
                    h2, m2 = map(int, parts_split[1].strip().split(":"))
                    routines[idx]["start_time"] = f"{h1:02d}:{m1:02d}"
                    routines[idx]["end_time"] = f"{h2:02d}:{m2:02d}"
                else:
                    raise ValueError
            except Exception:
                await update.message.reply_text("❌ ساعت نامعتبر. مثال: 23:00 یا 23:00 تا 07:00")
                return
        else:
            try:
                h, m = map(int, normalized.split(":"))
                routines[idx]["start_time"] = f"{h:02d}:{m:02d}"
                if not routines[idx].get("end_time"):
                    try:
                        end_min = (h * 60 + m + int(routines[idx].get("duration_minutes", 60))) % (24 * 60)
                        routines[idx]["end_time"] = minutes_to_time(end_min)
                    except Exception:
                        pass
            except Exception:
                await update.message.reply_text("❌ ساعت نامعتبر. مثال: 23:00 یا 23:00 تا 07:00")
                return
    context.user_data["pending_routine_cards"] = {
        "routines": routines, "text": pending.get("text", ""),
        "user_id": pending.get("user_id"),
    }
    context.user_data.pop("pending_routine_clarify", None)
    text_msg = f"📋 **{len(routines)} روتین**\n\n"
    for i, r in enumerate(routines, 1):
        title = r.get("title", "روتین")
        st = r.get("start_time", "")
        en = r.get("end_time", "")
        time_info = f" {st}-{en}" if st and en else ""
        text_msg += f"{i}. {title}{time_info}\n"
    text_msg += "\nذخیره کنم؟"
    await update.message.reply_text(
        text_msg,
        reply_markup=ReplyKeyboardMarkup([["✅ ذخیره همه"], ["🔙 لغو"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def save_pending_routine_cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_routine_cards")
    if not pending:
        return
    user_id = pending["user_id"]
    routines = pending["routines"]
    saved = 0
    parts_created = 0
    today = get_iran_now().date()
    for r in routines:
        if not isinstance(r, dict):
            continue
        card_data = {
            "title": r.get("title", "روتین"),
            "routine_type": r.get("routine_type", "other"),
            "recurrence": r.get("recurrence", "daily"),
            "days_of_week": r.get("days_of_week", []),
            "start_time": r.get("start_time"),
            "end_time": r.get("end_time"),
            "duration_minutes": r.get("duration_minutes", 60),
            "is_fixed": r.get("is_fixed", True),
            "priority": r.get("priority", "normal"),
        }
        card_type = "class" if r.get("routine_type") == "class" else "routine"
        card_id = save_card(
            card_type=card_type,
            content_type=r.get("routine_type", "other"),
            data=card_data, priority=7, raw_text=pending["text"], created_by=user_id
        )
        if card_id:
            try:
                await index_card_to_rag(card_id)
            except Exception:
                pass
            execute_query(
                """INSERT INTO user_routines (user_id, routine_type, time_value, data, is_active)
                   VALUES (%s, %s, %s, %s, TRUE)""",
                (user_id, r.get("routine_type", "other"),
                 r.get("start_time"), json.dumps(card_data))
            )
            saved += 1
            st = r.get("start_time")
            en = r.get("end_time")
            if st and en:
                try:
                    s_min = time_to_minutes(st)
                    e_min = time_to_minutes(en)
                    dur = e_min - s_min
                    if dur <= 0:
                        dur = (24 * 60 - s_min) + e_min
                except Exception:
                    dur = int(r.get("duration_minutes", 60))
                rec = r.get("recurrence", "daily")
                days = r.get("days_of_week", [])
                day_map = {"شنبه": 5, "یکشنبه": 6, "دوشنبه": 0,
                           "سه‌شنبه": 1, "سه شنبه": 1, "چهارشنبه": 2,
                           "پنجشنبه": 3, "پنج شنبه": 3, "جمعه": 4}
                for offset in range(0, 7):
                    d = today + timedelta(days=offset)
                    if rec == "daily":
                        match_day = True
                    elif rec == "weekly" and days:
                        match_day = d.weekday() in [day_map.get(x) for x in days if day_map.get(x) is not None]
                    else:
                        match_day = True
                    if not match_day:
                        continue
                    date_str = d.strftime("%Y-%m-%d")
                    sess_id = get_or_create_session(user_id, date_str)
                    if not sess_id:
                        continue
                    part_title = card_data.get('title')
                    pd = {
                        "title": part_title,
                        "planned_minutes": max(15, dur),
                        "time_slot": f"{st}-{en}",
                        "planned_start_time": st, "planned_end_time": en,
                        "is_fixed_time": True,
                        "explicit_start": st, "explicit_end": en,
                        "reason": f"روتین {card_data.get('routine_type', '')}",
                        "pages": 0, "user_id": user_id,
                        "activity": "routine" if r.get("routine_type") not in ("class", "exercise", "rest") else r.get("routine_type"),
                        "content_type": "routine", "origin": "routine",
                        "matched_card_id": card_id,
                        "metadata": {"routine_card_id": card_id, "recurring": True}
                    }
                    if add_part_to_session(sess_id, pd):
                        parts_created += 1
    context.user_data.pop("pending_routine_cards", None)
    context.user_data.pop("routin_mode", None)
    context.user_data.pop("pending_routine_clarify", None)
    await update.message.reply_text(
        f"✅ **{saved} کارت روتین ذخیره و در {parts_created} پارت تقویم اعمال شد!**",
        reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
    )


# ==================== یادآور ====================
async def reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    context.user_data["reminder_mode"] = "free_text"
    await update.message.reply_text(
        "🔔 **یادآور جدید**\n\nمتن یادآور رو بنویس (تاریخ + ساعت).\n\n"
        "📝 مثال:\n• «۲۳ آبان ساعت ۹ صبح امتحان عربی دارم»\n"
        "• «فردا ساعت ۱۴ جلسه دکتر»",
        reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_reminder_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    if text == "🔙 بازگشت":
        context.user_data.pop("reminder_mode", None)
        await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
        return
    if not await check_and_consume_kok(update, user_id, "reminder_setup"):
        return
    wait = await update.message.reply_text("🧠 در حال تحلیل...")
    prompt = f"""تو یه تحلیل‌گر یادآور هستی.

=== متن ===
"{text}"

=== امروز شمسی ===
{get_today_shamsi()}
میلادی: {get_today_date()} | ساعت: {get_iran_time_str()}

=== خروجی JSON ===
{{
  "title": "عنوان کوتاه",
  "date": "1405/08/23",
  "date_gregorian": "2026-11-14",
  "time": "09:00",
  "message": "پیام دوستانه"
}}
⚠️ فقط JSON خالص."""
    model_type = get_user_model_type(user_id)
    response = await call_ai(prompt, max_tokens=800, temperature=0.2, model_type=model_type)
    await wait.delete()
    if not response:
        await update.message.reply_text("❌ AI پاسخ نداد.")
        return
    reminder_data = safe_json_loads(response, dict)
    if not reminder_data:
        await update.message.reply_text("❌ خطا در تحلیل.")
        return
    context.user_data["pending_reminder_data"] = {
        "data": reminder_data, "user_id": user_id, "text": text,
    }
    title = reminder_data.get("title", "یادآور")
    date_shamsi = reminder_data.get("date", "")
    time_str = reminder_data.get("time", "")
    message = reminder_data.get("message", title)
    text_msg = f"🔔 **یادآور پیشنهادی**\n\n📌 {title}\n"
    if date_shamsi:
        text_msg += f"📅 {date_shamsi}\n"
    if time_str:
        text_msg += f"⏰ {time_str}\n"
    if message:
        text_msg += f"\n💬 {message}\n"
    text_msg += "\nذخیره کنم؟"
    await update.message.reply_text(
        text_msg,
        reply_markup=ReplyKeyboardMarkup(
            [["✅ ذخیره یادآور"], ["🔙 لغو"]], resize_keyboard=True
        ),
        parse_mode=ParseMode.HTML
    )


async def save_pending_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بخش ۵ — یک بار پیام، پاک کردن همه stateها، return."""
    pending = context.user_data.get("pending_reminder_data")
    if not pending:
        return
    user_id = pending["user_id"]
    reminder_data = pending["data"]
    result = save_user_reminder_from_ai(user_id, reminder_data)
    # پاک‌سازی همه stateهای مربوطه
    context.user_data.pop("pending_reminder_data", None)
    context.user_data.pop("reminder_mode", None)
    context.user_data.pop("pending_reminder_plan", None)
    context.user_data.pop("processing", None)
    if result:
        date_shamsi = result.get("date_shamsi") or ""
        await update.message.reply_text(
            f"✅ **یادآور ثبت شد!**\n\n📅 {date_shamsi}\n🔔 از قبل و سر وقت یادت می‌ندازم.",
            reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("❌ خطا در ثبت.", reply_markup=get_main_keyboard())


# ==================== Plan Actions ====================
async def handle_part_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    plan = context.user_data.get("current_plan", {}) or {}
    parts = plan.get("parts", [])
    edit_mode = context.user_data.get("edit_mode")
    id_match = re.search(r'\[(\d+)\]', text)
    if not id_match:
        return
    part_id = int(id_match.group(1))
    found_part = next((p for p in parts if p.get("part_id") == part_id), None)
    if not found_part:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    if edit_mode == "order":
        await handle_edit_order_click(update, context, part_id)
        return
    if edit_mode == "time":
        await handle_edit_time_click(update, context, part_id)
        return
    await show_part_detail(update, context, part_id)


async def move_part_up(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    index = next((i for i, p in enumerate(parts) if p["part_id"] == part_id), None)
    if index is None:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    if index > 0:
        parts[index], parts[index-1] = parts[index-1], parts[index]
        for i, p in enumerate(parts):
            p["part_number"] = i + 1
        for p in parts:
            try:
                execute_query(
                    "UPDATE study_parts SET part_number = %s WHERE part_id = %s",
                    (p["part_number"], p["part_id"])
                )
            except Exception as e:
                logger.error(f"خطا در ذخیره ترتیب: {e}")
        plan["parts"] = parts
        context.user_data["current_plan"] = plan
        await update.message.reply_text(f"⬆️ {parts[index]['title']} یک ردیف بالا رفت!")
        if plan.get("confirmed", False):
            await show_parts_final(update, context, parts)
        else:
            await show_parts_initial(update, context, parts)
    else:
        await update.message.reply_text("❌ این پارت در بالاترین ردیف است.")


# ==================== ویرایش دو مرحله‌ای ====================
async def start_edit_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan = context.user_data.get("current_plan", {})
    if not plan.get("parts"):
        await update.message.reply_text("❌ برنامه‌ای برای ویرایش وجود ندارد.")
        return
    context.user_data["edit_mode"] = "order"
    await show_edit_order(update, context)


async def show_edit_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = sorted(plan.get("parts", []), key=lambda x: x.get("part_number", 0))
    if not parts:
        await update.message.reply_text("❌ برنامه‌ای وجود ندارد.", reply_markup=get_main_keyboard())
        context.user_data.pop("edit_mode", None)
        return
    text = "🔀 **ویرایش ترتیب پارت‌ها**\n\nبا کلیک روی هر پارت، اون یک ردیف بالا می‌ره.\n\n"
    for part in parts:
        status = "✅" if part.get("completed") else "⬜"
        title = part.get("title", "بدون عنوان")
        dur = part.get("planned_minutes", 0)
        planned_start = part.get("planned_start_time") or ""
        planned_end = part.get("planned_end_time") or ""
        time_info = f" {planned_start}-{planned_end}" if planned_start and planned_end else ""
        fixed = " 🔒" if part.get("is_fixed_time") else ""
        floating_tag = " 🕊" if is_floating_part(part) else ""
        reminder_tag = " 🔔" if is_reminder_part(part) else ""
        emoji = get_activity_emoji(part.get("activity", "study"))
        text += (f"{part['part_number']}. {status} {emoji} {title} ({dur}د)"
                 f"{time_info}{fixed}{floating_tag}{reminder_tag} [{part['part_id']}]\n")
    text += "\n💡 روی هر پارت بزن تا جابه‌جا بشه."
    keyboard = []
    for part in parts:
        status = "✅" if part.get("completed") else "⬜"
        title = part.get("title", "")
        dur = part.get("planned_minutes", 0)
        emoji = get_activity_emoji(part.get("activity", "study"))
        fixed = " 🔒" if part.get("is_fixed_time") else ""
        floating_tag = " 🕊" if is_floating_part(part) else ""
        reminder_tag = " 🔔" if is_reminder_part(part) else ""
        keyboard.append([f"{status} {emoji} {title} ({dur}د){fixed}{floating_tag}{reminder_tag} ↕️ [{part['part_id']}]"])
    keyboard.append(["✅ تایید ترتیب و رفتن به ویرایش زمان"])
    keyboard.append(["❌ لغو ویرایش"])
    await update.message.reply_text(
        text, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_edit_order_click(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = sorted(plan.get("parts", []), key=lambda x: x.get("part_number", 0))
    index = next((i for i, p in enumerate(parts) if p.get("part_id") == part_id), None)
    if index is None or index == 0:
        await update.message.reply_text("❌ این پارت در بالاترین ردیف است.")
        return
    part = parts[index]
    if part.get("completed"):
        await update.message.reply_text("❌ پارت انجام‌شده قابل جابه‌جایی نیست.")
        return
    if is_reminder_part(part):
        await update.message.reply_text("🔔 یادآورها قابل جابه‌جایی نیستن.")
        return
    if is_protected_part(part):
        await update.message.reply_text("🔒 پارت محافظت‌شده قابل جابه‌جایی نیست.")
        return
    parts[index], parts[index-1] = parts[index-1], parts[index]
    for i, p in enumerate(parts):
        p["part_number"] = i + 1
    plan["parts"] = parts
    context.user_data["current_plan"] = plan
    await show_edit_order(update, context)


async def confirm_edit_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    session_id = plan.get("session_id")
    selected_date = context.user_data.get("selected_date") or get_today_date()
    parts_sorted = sorted(parts, key=lambda x: x.get("part_number", 0))
    for p in parts_sorted:
        try:
            execute_query(
                "UPDATE study_parts SET part_number = %s WHERE part_id = %s",
                (p["part_number"], p["part_id"])
            )
        except Exception as e:
            logger.error(f"خطا در ذخیره ترتیب: {e}")
    parts = reschedule_parts(
        parts, base_start_time=get_iran_now(), session_id=session_id,
        user_id=plan.get("user_id"), source_date=selected_date
    )
    plan["parts"] = parts
    context.user_data["current_plan"] = plan
    context.user_data["edit_mode"] = "time"
    context.user_data["edit_selected_part"] = None
    await show_edit_time_list(update, context)


async def show_edit_time_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = sorted(plan.get("parts", []), key=lambda x: x.get("part_number", 0))
    if not parts:
        await update.message.reply_text("❌ برنامه‌ای وجود ندارد.", reply_markup=get_main_keyboard())
        context.user_data.pop("edit_mode", None)
        return
    total_min = sum(p.get("planned_minutes", 0) for p in parts)
    text = "⏰ **ویرایش زمان پارت‌ها**\n\n"
    text += f"⏱ زمان کل: {format_time_hours_minutes(total_min)}\n\n"
    for part in parts:
        status = "✅" if part.get("completed") else "⬜"
        title = part.get("title", "")
        dur = part.get("planned_minutes", 0)
        planned_start = part.get("planned_start_time") or ""
        planned_end = part.get("planned_end_time") or ""
        time_info = f" {planned_start}-{planned_end}" if planned_start and planned_end else ""
        fixed = " 🔒" if part.get("is_fixed_time") else ""
        floating_tag = " 🕊" if is_floating_part(part) else ""
        reminder_tag = " 🔔" if is_reminder_part(part) else ""
        emoji = get_activity_emoji(part.get("activity", "study"))
        text += (f"{part['part_number']}. {status} {emoji} {title} ({dur}د)"
                 f"{time_info}{fixed}{floating_tag}{reminder_tag} [{part['part_id']}]\n")
    text += "\n⏰ روی هر پارت بزن تا زمانش رو تغییر بدی."
    keyboard = []
    for part in parts:
        status = "✅" if part.get("completed") else "⬜"
        title = part.get("title", "")
        dur = part.get("planned_minutes", 0)
        emoji = get_activity_emoji(part.get("activity", "study"))
        fixed = " 🔒" if part.get("is_fixed_time") else ""
        floating_tag = " 🕊" if is_floating_part(part) else ""
        reminder_tag = " 🔔" if is_reminder_part(part) else ""
        keyboard.append([f"{status} {emoji} {title} ({dur}د){fixed}{floating_tag}{reminder_tag} [{part['part_id']}]"])
    keyboard.append(["✅ اتمام ویرایش زمان"])
    keyboard.append(["🔙 برگشت به ویرایش ترتیب"])
    keyboard.append(["❌ لغو کل ویرایش"])
    await update.message.reply_text(
        text, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_edit_time_click(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    part = next((p for p in parts if p.get("part_id") == part_id), None)
    if not part:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    if part.get("completed"):
        await update.message.reply_text("❌ پارت انجام‌شده قابل تغییر نیست.")
        return
    if part.get("is_fixed_time") and not is_floating_part(part):
        await update.message.reply_text("🔒 این پارت زمان ثابت داره.")
        return
    if is_reminder_part(part):
        await update.message.reply_text("🔔 یادآورها قابل تغییر زمان نیستن.")
        return
    context.user_data["edit_selected_part"] = part_id
    await show_edit_part_time(update, context, part_id)


async def show_edit_part_time(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    part = next((p for p in parts if p.get("part_id") == part_id), None)
    if not part:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    title = part.get("title", "")
    dur = part.get("planned_minutes", 45)
    planned_start = part.get("planned_start_time") or ""
    planned_end = part.get("planned_end_time") or ""
    time_info = f"🕒 {planned_start} - {planned_end}" if planned_start and planned_end else ""
    text = f"⏰ **ویرایش: {title}**\n\nزمان فعلی: {format_time_hours_minutes(dur)}\n"
    if time_info:
        text += f"{time_info}\n"
    text += "\nاز دکمه‌های زیر استفاده کن.\nحداقل: ۱۵ دقیقه"
    await update.message.reply_text(
        text, reply_markup=get_edit_part_time_keyboard(), parse_mode=ParseMode.HTML
    )


async def adjust_part_time(update: Update, context: ContextTypes.DEFAULT_TYPE, delta: int) -> None:
    part_id = context.user_data.get("edit_selected_part")
    if not part_id:
        await update.message.reply_text("❌ پارتی انتخاب نشده.")
        return
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    part = next((p for p in parts if p.get("part_id") == part_id), None)
    if not part:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    current = part.get("planned_minutes", 45)
    new_dur = current + delta
    if new_dur < 15:
        await update.message.reply_text("⚠️ حداقل زمان ۱۵ دقیقه است.")
        return
    part["planned_minutes"] = new_dur
    plan["parts"] = parts
    context.user_data["current_plan"] = plan
    selected_date = context.user_data.get("selected_date") or get_today_date()
    parts_rescheduled = reschedule_parts(
        parts, base_start_time=get_iran_now(),
        user_id=plan.get("user_id"), source_date=selected_date
    )
    plan["parts"] = parts_rescheduled
    context.user_data["current_plan"] = plan
    await show_edit_part_time(update, context, part_id)


async def confirm_part_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["edit_selected_part"] = None
    await show_edit_time_list(update, context)


async def back_to_order_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["edit_mode"] = "order"
    context.user_data["edit_selected_part"] = None
    await show_edit_order(update, context)


async def finish_edit_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan = context.user_data.get("current_plan", {})
    session_id = plan.get("session_id")
    selected_date = context.user_data.get("selected_date") or get_today_date()
    user_id = plan.get("user_id") or get_user_id_by_telegram(update.effective_user.id)
    parts = plan.get("parts", [])
    parts = reschedule_parts(
        parts, base_start_time=get_iran_now(), session_id=session_id,
        user_id=user_id, source_date=selected_date
    )
    for p in parts:
        if is_protected_part(p) and not is_floating_part(p):
            continue
        try:
            execute_query(
                """UPDATE study_parts
                   SET planned_minutes = %s, planned_start_time = %s,
                       planned_end_time = %s, time_slot = %s
                   WHERE part_id = %s""",
                (p.get("planned_minutes", 45), p.get("planned_start_time"),
                 p.get("planned_end_time"), p.get("time_slot", ""), p.get("part_id"))
            )
        except Exception as e:
            logger.error(f"خطا در ذخیره: {e}")
    context.user_data.pop("edit_mode", None)
    context.user_data.pop("edit_selected_part", None)
    fresh = get_plan_by_date(user_id, selected_date) if user_id else None
    if fresh:
        context.user_data["current_plan"] = fresh
        parts = fresh["parts"]
    await update.message.reply_text("✅ ویرایش برنامه ذخیره شد!")
    await show_parts_final(update, context, parts, show_date=(selected_date != get_today_date()))


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    date_str = context.user_data.get("selected_date") or get_today_date()
    context.user_data.pop("edit_mode", None)
    context.user_data.pop("edit_selected_part", None)
    if user_id:
        plan = get_plan_by_date(user_id, date_str)
        if plan:
            context.user_data["current_plan"] = plan
            await update.message.reply_text("❌ ویرایش لغو شد.")
            await show_parts_final(update, context, plan["parts"])
            return
    await update.message.reply_text("❌ ویرایش لغو شد.", reply_markup=get_main_keyboard())


# ==================== تایید و اتمام ====================
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
    await update.message.reply_text("✅ <b>برنامه تایید شد!</b>", parse_mode=ParseMode.HTML)
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
    if session_id:
        finish_session(session_id)
    text = f"📅 برنامه امروز به پایان رسید!\n\n📊 پیشرفت: {len(completed_parts)}/{len(parts)}\n\n"
    if incomplete_parts:
        text += "📋 انجام نشده:\n"
        for part in incomplete_parts[:5]:
            text += f"⬜ {part['title']} ({part.get('planned_minutes', 0)}د)\n"
    context.user_data.pop("current_plan", None)
    context.user_data.pop("active_part", None)
    await update.message.reply_text(text, reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML)


# ==================== تکمیل پارت + run_reschedule مرکزی ====================
async def handle_done_part(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if part_id in active_timers:
        active_timers[part_id].schedule_removal()
        del active_timers[part_id]
    timer_data.pop(part_id, None)
    check_result = execute_query(
        """SELECT completed, title, planned_minutes, session_id,
                  to_char(planned_start_time, 'HH24:MI'),
                  to_char(planned_end_time, 'HH24:MI'),
                  is_fixed_time, part_number,
                  activity_type, user_id, origin, subject
           FROM study_parts WHERE part_id = %s""",
        (part_id,), fetch=True
    )
    if not check_result:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    (is_completed, title, planned_minutes, session_id, _, _, _, part_number,
     activity_type, part_user_id, origin, subject) = check_result
    if is_completed:
        await update.message.reply_text(f"⚠️ <b>{title}</b> قبلاً انجام شده.", parse_mode=ParseMode.HTML)
        return
    selected_date = context.user_data.get("selected_date")
    today = get_today_date()
    is_early = False
    if selected_date and selected_date != today:
        try:
            sd = datetime.strptime(selected_date, "%Y-%m-%d").date()
            td = datetime.strptime(today, "%Y-%m-%d").date()
            if sd > td:
                is_early = True
        except Exception:
            pass
    if is_early:
        context.user_data["pending_early_complete"] = part_id
        shamsi = get_shamsi_date(selected_date)
        await update.message.reply_text(
            f"⚠️ **تکمیل زودهنگام**\n\nاین پارت مربوط به {shamsi} هست.\nمطمئنی الان انجامش دادی؟",
            reply_markup=ReplyKeyboardMarkup(
                [["✅ بله، انجام دادم"], ["❌ نه، لغو"]],
                resize_keyboard=True, one_time_keyboard=True
            ),
            parse_mode=ParseMode.HTML
        )
        return
    await do_complete_part(update, context, part_id, session_id, title,
                            planned_minutes, activity_type, origin, subject, user_id)


async def do_complete_part(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            part_id: int, session_id: int, title: str,
                            planned_minutes: int, activity_type: str,
                            origin: str, subject: str, user_id: int) -> None:
    now = datetime.now(IRAN_TZ)
    execute_query(
        """UPDATE study_parts SET completed = TRUE,
           actual_minutes = %s, actual_end_time = %s WHERE part_id = %s""",
        (planned_minutes, now, part_id)
    )
    execute_query(
        """UPDATE study_sessions SET completed_parts = (
            SELECT COUNT(*) FROM study_parts WHERE session_id = %s AND completed = TRUE
        ) WHERE session_id = %s""",
        (session_id, session_id)
    )
    execute_query(
        """UPDATE reminder_notifications SET status = 'cancelled'
           WHERE part_id = %s AND status = 'pending'""",
        (part_id,)
    )
    if not subject:
        user_data = get_user_data(str(update.effective_user.id))
        detection = await detect_subject_for_part(title, activity_type, user_data)
        if detection.get("subject"):
            execute_query(
                """UPDATE study_parts SET subject = %s, topic_resolved = %s,
                   detection_confidence = %s, matched_card_id = %s
                   WHERE part_id = %s""",
                (detection["subject"], detection.get("topic"),
                 detection.get("confidence"), detection.get("matched_card_id"), part_id)
            )
            subject = detection["subject"]
    activity_data = {
        "user_id": user_id, "date": get_today_date(),
        "subject": subject or title, "topic": "",
        "activity_type": activity_type or "study",
        "planned_duration": planned_minutes, "actual_duration": planned_minutes,
        "status": "done", "part_order": 0
    }
    save_activity(activity_data)
    update_subject_status(user_id, subject or title, activity_data)
    context.user_data.pop("active_part", None)
    await update.message.reply_text(
        f"✅ **{title} انجام شد.**\n⏱ در حال به‌روزرسانی ساعت‌ها...",
        parse_mode=ParseMode.HTML
    )
    # بازچینی خودکار (بخش ۷)
    selected_date = context.user_data.get("selected_date") or get_today_date()
    # مسیر مرکزی — trigger=complete، بدون پرسش تقویم اضافه
    await run_reschedule(update, context, user_id, selected_date,
                          trigger="complete", ask_calendar=False, show_final=False)
    # بررسی شناوری
    await cleanup_expired_parts_job(user_id=user_id)
    # پیام بازخورد
    await ask_activity_type(update, context, part_id)


async def confirm_early_complete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    part_id = context.user_data.get("pending_early_complete")
    if not part_id:
        return
    user_id = get_user_id_by_telegram(update.effective_user.id)
    check_result = execute_query(
        """SELECT title, planned_minutes, session_id, activity_type,
                  origin, subject FROM study_parts WHERE part_id = %s""",
        (part_id,), fetch=True
    )
    if not check_result:
        context.user_data.pop("pending_early_complete", None)
        return
    title, planned_minutes, session_id, activity_type, origin, subject = check_result
    context.user_data.pop("pending_early_complete", None)
    await do_complete_part(update, context, part_id, session_id, title,
                            planned_minutes, activity_type, origin, subject, user_id)


async def handle_delete_part(update: Update, context: ContextTypes.DEFAULT_TYPE, part_id: int = None) -> None:
    if part_id is None:
        part_id = context.user_data.get("active_part")
    if not part_id:
        await update.message.reply_text("❌ پارت فعالی نیست.")
        return
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    part = next((p for p in parts if p["part_id"] == part_id), None)
    if not part:
        await update.message.reply_text("❌ پارت یافت نشد.")
        return
    if part.get("completed"):
        await update.message.reply_text("❌ پارت انجام شده قابل حذف نیست.")
        return
    await update.message.reply_text(
        f"⚠️ **آیا مطمئنی {part['title']} حذف بشه؟**",
        reply_markup=get_confirm_delete_keyboard(), parse_mode=ParseMode.HTML
    )
    context.user_data["pending_delete"] = {
        "part_id": part_id, "part_title": part['title'],
        "session_id": plan.get("session_id")
    }


async def handle_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if text == "✅ بله، حذف کن":
        pending = context.user_data.get("pending_delete")
        if not pending:
            await update.message.reply_text("❌ عملیات لغو شد.")
            return
        part_id = pending["part_id"]
        part_title = pending["part_title"]
        session_id = pending.get("session_id")
        plan = context.user_data.get("current_plan", {})
        user_id = get_user_id_by_telegram(update.effective_user.id)
        selected_date = context.user_data.get("selected_date") or get_today_date()
        execute_query("UPDATE reminder_notifications SET status = 'cancelled' WHERE part_id = %s", (part_id,))
        execute_query("DELETE FROM study_parts WHERE part_id = %s", (part_id,))
        plan["parts"] = [p for p in plan.get("parts", []) if p["part_id"] != part_id]
        for i, p in enumerate(plan["parts"]):
            p["part_number"] = i + 1
        execute_query("UPDATE study_sessions SET total_parts = %s WHERE session_id = %s",
                     (len(plan["parts"]), session_id))
        # استفاده از run_reschedule مرکزی
        context.user_data["current_plan"] = plan
        await run_reschedule(update, context, user_id, selected_date,
                              trigger="apply_change", ask_calendar=False, show_final=False)
        fresh = get_plan_by_date(user_id, selected_date)
        context.user_data["current_plan"] = fresh
        context.user_data.pop("pending_delete", None)
        context.user_data.pop("active_part", None)
        await update.message.reply_text(f"🗑 <b>{part_title}</b> حذف شد!", parse_mode=ParseMode.HTML)
        if fresh and fresh.get("parts"):
            await show_parts_final(update, context, fresh["parts"])
        else:
            await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
    elif text == "❌ نه، لغو":
        context.user_data.pop("pending_delete", None)
        await update.message.reply_text("❌ حذف لغو شد.")
        plan = context.user_data.get("current_plan", {})
        if plan.get("parts"):
            await show_parts_final(update, context, plan["parts"])
        else:
            await update.message.reply_text("🔙 بازگشت", reply_markup=get_main_keyboard())


# ==================== split question ====================
async def check_and_ask_split(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                duration_minutes: int, title: str,
                                user_id: int, activity: str = None,
                                is_fixed: bool = False,
                                explicit_start: str = None,
                                parts_count_hint: int = 0) -> bool:
    if parts_count_hint and parts_count_hint > 1:
        return False  # قبلاً کاربر گفته
    if duration_minutes <= 60:
        return False
    if is_fixed or explicit_start:
        return False
    if activity and activity in ("exercise", "rest", "fun", "class", "reminder", "other"):
        return False
    t = (title or "").lower()
    for kw in ("باشگاه", "ورزش", "کلاس", "جلسه", "امتحان", "آزمون", "یادآور"):
        if kw in t:
            return False
    context.user_data["pending_split"] = {
        "duration": duration_minutes, "title": title, "user_id": user_id,
    }
    text = (f"📊 **تقسیم پارت**\n\n📚 {title}\n"
            f"⏱ زمان تخمینی: {format_time_hours_minutes(duration_minutes)}\n\n"
            f"چند پارت کنم؟")
    await update.message.reply_text(
        text, reply_markup=get_split_question_keyboard(), parse_mode=ParseMode.HTML
    )
    return True


async def handle_split_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    pending = context.user_data.get("pending_split")
    if not pending:
        return
    if text == "🔙 لغو":
        context.user_data.pop("pending_split", None)
        await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
        return
    if text == "✏️ خودم تعیین می‌کنم":
        context.user_data["pending_split"]["mode"] = "custom"
        await update.message.reply_text(
            "✏️ تعداد پارت‌ها رو بنویس (مثلاً «۳ پارت»):",
            reply_markup=ReplyKeyboardMarkup([["🔙 لغو"]], resize_keyboard=True)
        )
        return
    if "۱ پارت" in text or "1 پارت" in text:
        pending["count"] = 1
        context.user_data["pending_split"] = pending
        await do_split_parts(update, context)
        return
    nums = re.findall(r'\d+', text)
    if not nums:
        await update.message.reply_text("❌ تعداد نامعتبر.")
        return
    count = int(nums[0])
    if count < 1 or count > 6:
        await update.message.reply_text("⚠️ بین ۱ تا ۶ پارت.")
        return
    pending["count"] = count
    context.user_data["pending_split"] = pending
    await do_split_parts(update, context)


async def do_split_parts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_split")
    if not pending:
        return
    count = pending.get("count", 1)
    duration = pending.get("duration")
    title = pending.get("title")
    if count == 1:
        change = context.user_data.get("pending_change")
        if change:
            change["split_count"] = 1
            target = change.get("target", {})
            target["duration"] = duration
            change["target"] = target
            context.user_data["pending_change"] = change
        context.user_data.pop("pending_split", None)
        text_msg = f"📋 **یک پارت**\n\n📖 {title} — {duration} دقیقه\n\nتایید؟"
        await update.message.reply_text(
            text_msg, reply_markup=get_confirm_change_keyboard(), parse_mode=ParseMode.HTML
        )
        return
    per_part = duration // count
    remainder = duration % count
    change = context.user_data.get("pending_change")
    if change:
        target = change.get("target", {})
        target["duration"] = per_part
        change["split_count"] = count
        context.user_data["pending_change"] = change
    context.user_data.pop("pending_split", None)
    text_msg = f"📋 **{count} پارت**\n\n"
    for i in range(count):
        d = per_part + (1 if i < remainder else 0)
        text_msg += f"{i+1}. 📖 {title} (بخش {i+1}/{count}) — {d} دقیقه\n"
    text_msg += f"\n⏱ مجموع: {format_time_hours_minutes(duration)}\n\nتایید؟"
    await update.message.reply_text(
        text_msg, reply_markup=get_confirm_change_keyboard(), parse_mode=ParseMode.HTML
    )


# ==================== چت AI ====================
async def handle_ai_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        context.user_data["mode"] = None
        return
    text = update.message.text.strip()
    if text == "🔙 بازگشت به منو":
        context.user_data.pop("mode", None)
        context.user_data.pop("ai_context_summary", None)
        await update.message.reply_text("🔙 برگشتی به منو 👇", reply_markup=get_main_keyboard())
        return
    if text == "🔄 مکالمه جدید":
        clear_chat_history(user_id)
        await update.message.reply_text("🔄 مکالمه جدید. سوال بپرس.", reply_markup=get_ai_chat_keyboard())
        return
    if len(text) > 1000:
        await update.message.reply_text("⚠️ پیام کوتاه‌تر بفرست.")
        return
    if not await check_and_consume_kok(update, user_id, "ai_chat"):
        context.user_data["mode"] = None
        return
    today = get_today_date()
    plan = get_plan_by_date(user_id, today)
    plan_summary = ""
    if plan and plan.get("parts"):
        plan_summary = "📋 برنامه امروز:\n"
        for p in plan["parts"]:
            status = "✅" if p.get("completed") else "⬜"
            plan_summary += f"  {status} {p.get('title')} ({p.get('planned_minutes')}د) {p.get('time_slot', '')}\n"
    else:
        plan_summary = "📋 برنامه‌ای برای امروز وجود ندارد."
    today_acts = get_today_activities(user_id)
    if today_acts:
        today_summary = "📊 فعالیت‌های امروز:\n"
        for a in today_acts:
            status = "✅" if a.get("status") == "done" else "⬜"
            today_summary += f"  {status} {a['subject']} - {a.get('actual_duration', 0)}د\n"
    else:
        today_summary = "📊 امروز فعالیتی ثبت نشده."
    yesterday_acts = get_yesterday_activities(user_id)
    if yesterday_acts:
        yesterday_summary = "📅 دیروز:\n"
        total_y = 0
        for a in yesterday_acts:
            status = "✅" if a.get("status") == "done" else "⬜"
            dur = a.get('actual_duration', 0) or 0
            total_y += dur
            yesterday_summary += f"  {status} {a['subject']} - {dur}د\n"
        yesterday_summary += f"  📊 جمع: {format_time_hours_minutes(total_y)}\n"
    else:
        yesterday_summary = "📅 دیروز فعالیتی ثبت نشده."
    week_data = get_user_study_summary(user_id, days=7)
    week_summary = "📈 خلاصه ۷ روز اخیر:\n"
    week_summary += f"  • روزهای فعال: {week_data['active_days']} روز\n"
    week_summary += f"  • کل زمان: {format_time_hours_minutes(week_data['total_minutes'])}\n"
    role_prefix = build_role_prefix(user_id)
    system_prompt = f"""{role_prefix}
تو یک **دستیار هوشمند مدیریت برنامه روزانه** هستی.

=== اطلاعات کاربر ===
{context.user_data.get('ai_context_summary', '')}

=== برنامه امروز ===
{plan_summary}

=== فعالیت‌های امروز ===
{today_summary}

=== فعالیت‌های دیروز ===
{yesterday_summary}

=== خلاصه آماری ===
{week_summary}

⚠️ **قوانین:**
1. دامنه کاری گسترده است: برنامه‌ریزی، ورزش، استراحت، تغذیه، انگیزه.
2. اگر درباره گذشته پرسید، از داده‌های بالا جواب بده.
3. پاسخ‌ها **کوتاه، دقیق و فارسی روان**.
"""
    history = get_chat_history(user_id, limit=10)
    messages = [{"role": "system", "content": system_prompt}]
    messages += history
    messages.append({"role": "user", "content": text})
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    model_type = get_user_model_type(user_id)
    model_name = AI_MODEL_ADVANCED if model_type == "advanced" else AI_MODEL_LIGHT
    try:
        completion = await client.chat.completions.create(
            model=model_name, messages=messages,
            max_tokens=800, temperature=0.6
        )
        reply = completion.choices[0].message.content
        save_chat_message(user_id, "user", text)
        save_chat_message(user_id, "assistant", reply)
        plan_changed = await process_ai_plan_change(update, context, text, reply)
        balance = get_user_kok(user_id)
        if not plan_changed:
            await update.message.reply_text(f"{reply}\n\n💎 {balance} KOK", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"خطا در چت AI: {e}")
        await update.message.reply_text(f"⚠️ خطا: {str(e)[:100]}")


# ==================== process_ai_plan_change با clarify gate و type validation ====================
async def process_ai_plan_change(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  user_text: str, ai_reply: str) -> bool:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return False
    today = get_today_date()
    user_data = get_user_data(str(update.effective_user.id))
    plan = get_plan_by_date(user_id, today) or {}
    parts_summary = ""
    if plan.get("parts"):
        for i, p in enumerate(plan["parts"], 1):
            status = "✅" if p.get("completed") else "⬜"
            fixed = " 🔒" if p.get("is_fixed_time") else ""
            parts_summary += f"{i}. {status} {p.get('title')} ({p.get('planned_minutes')}د) {p.get('time_slot', '')}{fixed}\n"
    else:
        parts_summary = "هیچ برنامه‌ای وجود ندارد"

    active_project = get_active_project_card(user_id)
    prompt = build_plan_change_prompt(user_id, user_data, user_text, parts_summary,
                                        active_project=active_project)
    model_type = get_user_model_type(user_id)
    response = await call_ai(prompt, max_tokens=2000, temperature=0.3, model_type=model_type)
    if not response:
        return False

    interpretation = safe_json_loads(response, dict)
    if not interpretation:
        logger.error(
            f"parse_error: user={user_id} text='{user_text[:80]}' "
            f"raw='{(response or '')[:200]}'"
        )
        return await _humanize_parse_error(update, context, user_text)

    # type validation سخت
    if not isinstance(interpretation, dict):
        return await _humanize_parse_error(update, context, user_text)
    subjects = interpretation.get("subjects")
    if subjects is not None and not isinstance(subjects, list):
        subjects = []
        interpretation["subjects"] = subjects
    target = interpretation.get("target")
    if target is not None and not isinstance(target, dict):
        interpretation["target"] = {}
    reminder = interpretation.get("reminder")
    if reminder is not None and not isinstance(reminder, dict):
        interpretation["reminder"] = {}
    classes = interpretation.get("classes")
    if classes is not None and not isinstance(classes, list):
        interpretation["classes"] = []

    action = interpretation.get("action", "chat")
    if action == "chat":
        return False

    # بخش ۱۶ — clarify gate
    clarify_key = _clarify_fingerprint(user_text)
    already_asked_for = context.user_data.get("clarify_asked_for_text")
    # اگر در حال پاسخ به clarify قبلی هستیم، اجازه بده
    in_followup = bool(context.user_data.get("clarify_followup"))

    if action in CLARIFY_ACTIONS and not in_followup and already_asked_for != clarify_key:
        q = _decide_clarify_question(action, interpretation, user_text)
        if q:
            question_text, options = q
            keyboard = [[opt] for opt in options]
            context.user_data["pending_clarify"] = {
                "question": question_text,
                "options": [{"label": opt, "value": opt} for opt in options],
                "original_text": user_text,
                "created_at": time.time(),
                "interpretation": interpretation,
            }
            context.user_data["clarify_asked_for_text"] = clarify_key
            try:
                await update.message.reply_text(
                    f"🤔 {question_text}",
                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            logger.info(f"clarify_gate: user={user_id} action={action} asked=True")
            return True

    # از اینجا به بعد → پیش‌نمایش
    context.user_data.pop("clarify_asked_for_text", None)
    context.user_data.pop("clarify_followup", None)

    # ادامه منطق با اعتبارسنجی
    return await _dispatch_interpretation(update, context, user_id, user_data,
                                            plan, interpretation, user_text, today)


async def _humanize_parse_error(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 user_text: str) -> bool:
    """بخش «باگ ۳۰ تست» — تبدیل خطا به clarify."""
    context.user_data["pending_clarify"] = {
        "question": "متوجه نشدم. یه بار دیگه با جزئیات بیشتر می‌گی چه چیزی رو اضافه کنم؟",
        "options": [
            {"label": "📚 درس را می‌گویم", "value": "subject"},
            {"label": "⏰ زمان را می‌گویم", "value": "time"},
            {"label": "🔙 بازگشت", "value": "cancel"},
        ],
        "original_text": user_text,
        "created_at": time.time(),
    }
    await update.message.reply_text(
        "🤔 متوجه نشدم.\nدوباره بگو چه چیزی و چه زمانی؟",
        reply_markup=ReplyKeyboardMarkup(
            [["📚 درس را می‌گویم"], ["⏰ زمان را می‌گویم"], ["🔙 بازگشت"]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )
    return True


async def _dispatch_interpretation(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    user_id: int, user_data: Dict, plan: Dict,
                                    interpretation: Dict, user_text: str, today: str) -> bool:
    action = interpretation.get("action", "chat")
    target = interpretation.get("target") or {}
    reason = interpretation.get("reason", "")

    if action == "clarify":
        question = interpretation.get("question", "")
        options = interpretation.get("options", [])
        clarify_context = interpretation.get("context", {})
        if not question or not options:
            return False
        if not isinstance(options, list):
            options = []
        if len(options) > 4:
            options = options[:4]
        keyboard = []
        for opt in options:
            if isinstance(opt, dict):
                label = str(opt.get("label", ""))[:30]
            else:
                label = str(opt)[:30]
            if label:
                keyboard.append([label])
        if not keyboard:
            return False
        context.user_data["pending_clarify"] = {
            "question": question, "options": options,
            "context": clarify_context, "created_at": time.time(),
            "original_text": user_text,
        }
        await update.message.reply_text(
            f"🤔 **{question}**",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
            parse_mode=ParseMode.HTML
        )
        return True

    if action == "add_class":
        classes = interpretation.get("classes") or []
        if not isinstance(classes, list):
            classes = []
        cleaned = _prepare_classes(classes)
        if not cleaned:
            return False
        context.user_data["pending_class_card"] = {
            "classes": cleaned, "user_id": user_id, "raw_text": user_text,
        }
        text_msg = "🎓 **کلاس‌ها/رویدادها**\n\n"
        for i, c in enumerate(cleaned, 1):
            shamsi = get_shamsi_date(c["date"])
            try:
                weekday = get_weekday_fa(datetime.strptime(c["date"], "%Y-%m-%d"))
            except Exception:
                weekday = ""
            text_msg += f"**{i}. {c['title']}**\n"
            if c.get("teacher"):
                text_msg += f"   🎙 {c['teacher']}\n"
            text_msg += f"   📅 {weekday} {shamsi} ⏰ {c['start_time']}-{c['end_time']}\n"
        text_msg += "\nذخیره کنم؟"
        await update.message.reply_text(
            text_msg,
            reply_markup=ReplyKeyboardMarkup(
                [["✅ ذخیره کلاس‌ها"], ["❌ لغو"]],
                resize_keyboard=True, one_time_keyboard=True
            ),
            parse_mode=ParseMode.HTML
        )
        return True

    if action == "build_new":
        subjects = interpretation.get("subjects", [])
        if not isinstance(subjects, list):
            subjects = []
        cleaned_subjects = _clean_subjects(subjects)
        if not cleaned_subjects:
            return await _humanize_parse_error(update, context, user_text)
        context.user_data["pending_change"] = {
            "action": "build_new", "subjects": cleaned_subjects,
            "reason": reason,
            "session_id": plan.get("session_id") if plan else None,
            "user_id": user_id,
            "has_existing": bool(plan and plan.get("parts")),
        }
        text_msg = "🆕 **برنامه پیشنهادی جدید**\n\n"
        total_min = 0
        for i, subj in enumerate(cleaned_subjects, 1):
            duration = subj.get("duration", 45)
            sessions = subj.get("sessions", 1) or 1
            total_min += duration * sessions
            if sessions > 1:
                text_msg += f"{i}. **{subj.get('title')}** — {sessions} × {duration} دقیقه\n"
            else:
                text_msg += f"{i}. **{subj.get('title')}** ({duration} دقیقه)\n"
        text_msg += f"\n⏱ زمان کل: {format_time_hours_minutes(total_min)}\n"
        if plan and plan.get("parts"):
            text_msg += "\n⚠️ **برنامه فعلی جایگزین می‌شود.**"
        text_msg += "\n\nآیا تایید می‌کنید؟"
        await update.message.reply_text(text_msg, reply_markup=get_confirm_change_keyboard(), parse_mode=ParseMode.HTML)
        return True

    if action == "multi_add":
        subjects = interpretation.get("subjects", [])
        if not isinstance(subjects, list):
            subjects = []
        if len(subjects) > 5:
            subjects = subjects[:5]
            await update.message.reply_text("⚠️ حداکثر ۵ فعالیت در یه پیام. مابقی حذف شد.")
        cleaned = _clean_subjects(subjects)
        if not cleaned:
            return await _humanize_parse_error(update, context, user_text)
        for subj in cleaned:
            det = await detect_subject_for_part(subj.get("title", ""), subj.get("activity_type"), user_data)
            if det.get("subject"):
                subj["subject"] = det["subject"]
                subj["topic_resolved"] = det.get("topic")
                subj["detection_confidence"] = det.get("confidence")
                subj["matched_card_id"] = det.get("matched_card_id")
        time_slots = analyze_time_slots(user_text)
        context.user_data["pending_change"] = {
            "action": "multi_add", "subjects": cleaned, "reason": reason,
            "session_id": plan.get("session_id") if plan else None,
            "user_id": user_id,
            "has_existing": bool(plan and plan.get("parts")),
            "time_slots": time_slots,
        }
        text_msg = f"📋 **پیش‌نمایش {len(cleaned)} فعالیت**\n\n"
        total_min = 0
        for i, subj in enumerate(cleaned, 1):
            d = subj.get("duration", 45)
            sessions = subj.get("sessions", 1) or 1
            tdate = subj.get("target_date") or today
            tshamsi = get_shamsi_date(tdate)
            day_tag = "امروز" if tdate == today else tshamsi
            total_min += d * sessions
            line = f"{i}. 📖 **{subj.get('title')}** — "
            if sessions > 1:
                line += f"{sessions} × {d} دقیقه"
            else:
                line += f"{d} دقیقه"
            line += f"  📅 {day_tag}\n"
            if subj.get("explicit_start") and subj.get("explicit_end"):
                line += f"   🔒 {subj['explicit_start']}-{subj['explicit_end']}\n"
            text_msg += line
        text_msg += f"\n⏱ مجموع: {format_time_hours_minutes(total_min)}\n\nتایید؟"
        await update.message.reply_text(text_msg, reply_markup=get_confirm_change_keyboard(), parse_mode=ParseMode.HTML)
        return True

    if action == "reminder":
        reminder_data = interpretation.get("reminder") or {}
        if not isinstance(reminder_data, dict) or not reminder_data:
            await update.message.reply_text("❓ اطلاعات یادآور ناقصه.")
            return True
        result = save_user_reminder_from_ai(user_id, reminder_data)
        if not result:
            await update.message.reply_text("❌ خطا در ثبت یادآور.")
            return True
        title = reminder_data.get("title", "یادآور")
        date_shamsi = result.get("date_shamsi") or ""
        time_str = reminder_data.get("time") or ""
        message = reminder_data.get("message") or title
        header = f"🔔 **یادآور ثبت شد:** {title}"
        if date_shamsi:
            header += f" — {date_shamsi}"
        if time_str:
            header += f" {time_str}"
        await update.message.reply_text(
            f"{header}\n\n💬 {message}\n\nمی‌خوای براش برنامه مطالعاتی هم بچینم؟",
            reply_markup=get_reminder_suggest_keyboard(), parse_mode=ParseMode.HTML
        )
        context.user_data["pending_reminder_plan"] = {
            "part_id": result["part_id"], "session_id": result["session_id"],
            "date": result["date"], "reminder": reminder_data, "user_id": user_id,
        }
        return True

    if action == "suggest_card":
        card_suggestion = interpretation.get("card_suggestion", {})
        if not isinstance(card_suggestion, dict):
            card_suggestion = {}
        card_type = card_suggestion.get("type", "routine")
        text_msg = (f"🤔 **متوجه شدم**\n\n"
                    f"به نظر می‌رسه از یه الگوی تکرارشونده حرف می‌زنی.\n\n"
                    f"می‌خوای به عنوان کارت {'روتین' if card_type == 'routine' else 'پروژه'} ذخیره کنم؟\n\n"
                    f"متن: «{user_text}»")
        await update.message.reply_text(
            text_msg,
            reply_markup=ReplyKeyboardMarkup(
                [["✅ بله، کارت بساز"], ["❌ نه، فقط امروز"]],
                resize_keyboard=True, one_time_keyboard=True
            ),
            parse_mode=ParseMode.HTML
        )
        context.user_data["pending_card_suggestion"] = {"type": card_type, "text": user_text}
        return True

    parts = plan.get("parts", [])
    session_id = plan.get("session_id") if plan else None
    if action in ["add", "delete", "update"] and not target:
        return await _humanize_parse_error(update, context, user_text)

    pending_change = {
        "action": action, "target": target, "reason": reason,
        "session_id": session_id, "user_id": user_id, "parts": parts.copy(),
        "time_slots": analyze_time_slots(user_text),
    }

    if action == "add":
        title = (target.get("title") or "").strip()
        if not title or title == "فعالیت":
            followup = context.user_data.get("clarify_followup") or {}
            ot = followup.get("original_text") or user_text
            m = re.search(r'[\u0600-\u06FFA-Za-z][\u0600-\u06FFA-Za-z0-9\s\.\-]{2,}', ot)
            title = (m.group().strip() if m else "فعالیت")
        title = re.sub(r'\((فردا صبح|امروز|پس‌فردا|فردا)\)', '', title).strip() or title
        duration = target.get("duration")
        if duration is None:
            try:
                duration = int(re.search(r'(\d+)', user_text).group(1))
            except Exception:
                duration = 45
        try:
            duration = int(duration)
        except Exception:
            duration = 45
        duration = max(15, duration)
        target["duration"] = duration
        target["title"] = title
        explicit_start = target.get("explicit_start")
        explicit_end = target.get("explicit_end")
        is_fixed = bool(target.get("is_fixed_time")) or bool(explicit_start and explicit_end)
        target_date = target.get("target_date") or today
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', str(target_date)):
            target_date = today
        target["target_date"] = target_date

        # بخش ۱۷ — اگر زمان مشخص نیست، سوال ساعت
        if not explicit_start and not is_fixed:
            schedule_choice = context.user_data.get("schedule_choice_pending")
            if not schedule_choice:
                context.user_data["pending_change"] = pending_change
                context.user_data["schedule_choice_pending"] = {"title": title, "duration": duration, "target_date": target_date}
                await update.message.reply_text(
                    f"📚 {title} — {duration} دقیقه\n\nچه زمانی؟",
                    reply_markup=get_schedule_choice_keyboard(),
                    parse_mode=ParseMode.HTML
                )
                return True
            # اگر schedule_choice داریم → اعمال
            ch = schedule_choice.get("choice")
            if ch == "now":
                now = get_iran_now()
                start = f"{now.hour:02d}:{now.minute:02d}"
                end_min = now.hour * 60 + now.minute + duration
                end_min = min(end_min, DAY_END_MIN)
                explicit_start = start
                explicit_end = minutes_to_time(end_min)
                target["explicit_start"] = explicit_start
                target["explicit_end"] = explicit_end
                target["is_fixed_time"] = True
                is_fixed = True
            elif ch == "explicit":
                explicit_start = schedule_choice.get("start")
                explicit_end = schedule_choice.get("end")
                target["explicit_start"] = explicit_start
                target["explicit_end"] = explicit_end
                target["is_fixed_time"] = True
                is_fixed = True
            elif ch == "let_ai":
                target["is_fixed_time"] = False
            context.user_data.pop("schedule_choice_pending", None)

        if duration > 60 and not is_fixed and not explicit_start:
            # بررسی parts_count
            if "بخش" not in user_text and "جلسه" not in user_text:
                if await check_and_ask_split(update, context, duration, title, user_id,
                                              activity=target.get("activity_type"),
                                              is_fixed=is_fixed, explicit_start=explicit_start):
                    context.user_data["pending_change"] = pending_change
                    return True

        pending_change["target"] = target
        context.user_data["pending_change"] = pending_change
        context.user_data.pop("clarify_followup", None)

        text_msg = f"📋 **اضافه کردن پارت**\n\n📚 {title}\n⏱ {duration} دقیقه\n"
        if explicit_start and explicit_end:
            text_msg += f"🔒 {explicit_start} - {explicit_end}\n"
        elif not is_fixed:
            text_msg += "🕊 شناور — AI در جای مناسب می‌گذارد\n"
        if target_date != today:
            text_msg += f"📅 {get_shamsi_date(target_date)}\n"
        if reason:
            text_msg += f"💡 {reason}\n"
        text_msg += "\nآیا تایید می‌کنید؟"
        await update.message.reply_text(text_msg, reply_markup=get_confirm_change_keyboard(), parse_mode=ParseMode.HTML)
        return True

    elif action == "delete":
        current_title = target.get("title") or target.get("current_title") or ""
        if not current_title:
            return await _humanize_parse_error(update, context, user_text)
        target_part = next((p for p in parts if p.get("title") == current_title), None)
        if not target_part:
            await update.message.reply_text(f"❌ پارت «{current_title}» پیدا نشد.")
            return True
        if target_part.get("completed"):
            await update.message.reply_text(f"❌ پارت «{current_title}» تکمیل شده.")
            return True
        if is_protected_part(target_part):
            await update.message.reply_text(f"🔒 پارت «{current_title}» محافظت‌شده است.")
            return True
        pending_change["target_part"] = target_part
        context.user_data["pending_change"] = pending_change
        text_msg = f"⚠️ **حذف پارت**\n\n📚 {current_title}\n"
        if reason:
            text_msg += f"💡 {reason}\n"
        text_msg += "\nمطمئنی؟"
        await update.message.reply_text(text_msg, reply_markup=get_confirm_delete_keyboard(), parse_mode=ParseMode.HTML)
        return True

    elif action == "update":
        current_title = target.get("current_title") or target.get("title") or ""
        if not current_title:
            return await _humanize_parse_error(update, context, user_text)
        target_part = next((p for p in parts if p.get("title") == current_title), None)
        if not target_part:
            await update.message.reply_text(f"❌ پارت «{current_title}» پیدا نشد.")
            return True
        if is_protected_part(target_part):
            await update.message.reply_text(f"🔒 پارت «{current_title}» محافظت‌شده است.")
            return True
        updates = {}
        if target.get("title") and target.get("title") != current_title:
            updates["عنوان"] = f"{current_title} → {target.get('title')}"
        if target.get("duration"):
            updates["مدت"] = f"{target_part.get('planned_minutes', 0)} → {target.get('duration')} دقیقه"
        if target.get("start_time") and target.get("end_time"):
            updates["زمان"] = f"{target_part.get('time_slot', '')} → {target.get('start_time')}-{target.get('end_time')}"
        if not updates:
            return await _humanize_parse_error(update, context, user_text)
        pending_change["target_part"] = target_part
        pending_change["updates"] = updates
        context.user_data["pending_change"] = pending_change
        text_msg = f"📝 **به‌روزرسانی پارت**\n\n📚 {current_title}\n\n📋 تغییرات:\n"
        for k, v in updates.items():
            text_msg += f"• {k}: {v}\n"
        if reason:
            text_msg += f"\n💡 {reason}\n"
        text_msg += "\nتایید می‌کنی؟"
        await update.message.reply_text(text_msg, reply_markup=get_confirm_change_keyboard(), parse_mode=ParseMode.HTML)
        return True

    elif action == "clear":
        pending_change["parts"] = parts.copy()
        context.user_data["pending_change"] = pending_change
        text_msg = (f"⚠️ **پاک کردن همه پارت‌ها**\n\n📊 تعداد: {len(parts)}\n\n"
                    f"پارت‌های محافظت‌شده (کلاس/یادآور/روتین/ثابت) حذف نمی‌شوند.\n\nمطمئنی؟")
        await update.message.reply_text(text_msg, reply_markup=get_confirm_clear_keyboard(), parse_mode=ParseMode.HTML)
        return True

    context.user_data.pop("pending_change", None)
    return False


def _prepare_classes(classes: List[Dict]) -> List[Dict]:
    today_date = get_iran_now().date()
    day_map = {"شنبه": 5, "یکشنبه": 6, "دوشنبه": 0,
               "سه‌شنبه": 1, "سه شنبه": 1, "چهارشنبه": 2,
               "پنجشنبه": 3, "پنج شنبه": 3, "جمعه": 4}
    cleaned = []
    for c in classes:
        if not isinstance(c, dict):
            continue
        title = str(c.get("title") or c.get("subject") or "کلاس").strip()
        if not title:
            continue
        start = normalize_time_str(c.get("start_time")) or ""
        end = normalize_time_str(c.get("end_time")) or ""
        if not start:
            continue
        target_date = None
        if c.get("date_gregorian"):
            try:
                target_date = datetime.strptime(c["date_gregorian"], "%Y-%m-%d").date()
            except Exception:
                pass
        if not target_date and c.get("date"):
            try:
                jd = jdatetime.datetime.strptime(c["date"], "%Y/%m/%d")
                target_date = jd.togregorian().date()
            except Exception:
                pass
        if not target_date and c.get("day_of_week"):
            day_num = day_map.get(str(c["day_of_week"]).strip())
            if day_num is not None:
                days_ahead = (day_num - today_date.weekday()) % 7
                if days_ahead == 0:
                    try:
                        sh, sm = map(int, start.split(":"))
                        now = get_iran_now()
                        if (sh * 60 + sm) > (now.hour * 60 + now.minute):
                            days_ahead = 0
                        else:
                            days_ahead = 7
                    except Exception:
                        days_ahead = 0
                target_date = today_date + timedelta(days=days_ahead)
        if not target_date:
            continue
        rb = c.get("reminder_before_minutes") or 30
        try:
            rb = int(rb)
        except (ValueError, TypeError):
            rb = 30
        rb = max(5, min(120, rb))
        cleaned.append({
            "title": title,
            "teacher": str(c.get("teacher") or "").strip(),
            "subject": str(c.get("subject") or title).strip(),
            "day_of_week": c.get("day_of_week") or "",
            "date": target_date.strftime("%Y-%m-%d"),
            "start_time": start, "end_time": end,
            "location": str(c.get("location") or "").strip(),
            "recurring": bool(c.get("recurring", False)),
            "reminder_before_minutes": rb,
        })
    return cleaned


def _clean_subjects(subjects: Union[List, str]) -> List[Dict]:
    """بخش «باگ ۳۰ تست» — هر آیتم غیر dict → skip"""
    if not isinstance(subjects, list):
        return []
    today = get_today_date()
    cleaned = []
    for subj in subjects:
        if not isinstance(subj, dict):
            continue
        d = subj.get("duration")
        if d is None:
            d = subj.get("session_duration") or 45
        try:
            d = int(d)
        except (ValueError, TypeError):
            d = 45
        d = max(15, d)
        subj["duration"] = d
        s = subj.get("sessions")
        try:
            s = int(s) if s is not None else 1
        except (ValueError, TypeError):
            s = 1
        s = max(1, min(10, s))
        subj["sessions"] = s
        t = subj.get("target_date")
        if not t or not re.match(r'^\d{4}-\d{2}-\d{2}$', str(t)):
            if isinstance(t, str) and "فردا" in t:
                t = (get_iran_now().date() + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                t = today
        subj["target_date"] = t
        title = (subj.get("title") or subj.get("subject") or "فعالیت").strip()
        title = re.sub(r'\((فردا صبح|امروز|پس‌فردا|فردا)\)', '', title).strip()
        title = re.sub(r'\s+', ' ', title).strip()
        if not title:
            title = "فعالیت"
        subj["title"] = title
        if not subj.get("subject"):
            subj["subject"] = title
        if not subj.get("activity_type"):
            subj["activity_type"] = "study"
        cleaned.append(subj)
    return cleaned


# ==================== apply_pending_change ====================
async def apply_pending_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_change")
    if not pending:
        await update.message.reply_text("❌ هیچ تغییر در انتظار تاییدی وجود ندارد.")
        return
    action = pending.get("action")
    user_id = pending.get("user_id")
    today = get_today_date()
    selected_date = context.user_data.get("selected_date") or today
    try:
        if action == "build_new":
            subjects = pending.get("subjects", [])
            if not subjects:
                await update.message.reply_text("❌ اطلاعات برنامه وجود ندارد.")
                context.user_data.pop("pending_change", None)
                return
            if pending.get("has_existing") and pending.get("session_id"):
                execute_query(
                    "UPDATE study_sessions SET archived = TRUE WHERE session_id = %s",
                    (pending["session_id"],)
                )
            user_data = get_user_data(str(update.effective_user.id))
            ai_response = {"parts": subjects}
            session_id = create_plan_from_ai_response(user_id, user_data, ai_response)
            if session_id:
                new_plan = get_plan_by_date(user_id, today)
                if new_plan:
                    context.user_data["current_plan"] = new_plan
                    context.user_data.pop("pending_change", None)
                    await update.message.reply_text(
                        f"✅ **برنامه جدید ساخته شد!**\n\n📊 تعداد: {len(new_plan['parts'])} پارت",
                        reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
                    )
                    await show_parts_initial(update, context, new_plan["parts"])
                    return
        elif action == "multi_add":
            subjects = pending.get("subjects", [])
            if not subjects:
                context.user_data.pop("pending_change", None)
                return
            time_slots = pending.get("time_slots") or []
            affected_dates = set()
            added = 0
            for subj in subjects:
                if not isinstance(subj, dict):
                    continue
                tdate = subj.get("target_date") or selected_date
                if not re.match(r'^\d{4}-\d{2}-\d{2}$', str(tdate)):
                    tdate = selected_date
                sessions = subj.get("sessions", 1) or 1
                per_dur = subj.get("duration", 45)
                base_title = subj.get("title") or "فعالیت"
                target_session_id = get_or_create_session(user_id, tdate)
                if not target_session_id:
                    continue
                explicit_start = subj.get("explicit_start")
                explicit_end = subj.get("explicit_end")
                is_fixed = bool(subj.get("is_fixed_time")) or bool(explicit_start and explicit_end)
                for i in range(sessions):
                    part_title = f"{base_title} — جلسه {i+1} از {sessions}" if sessions > 1 else base_title
                    pd = {
                        "title": part_title, "planned_minutes": per_dur,
                        "time_slot": f"{explicit_start}-{explicit_end}" if explicit_start and explicit_end else "",
                        "planned_start_time": explicit_start, "planned_end_time": explicit_end,
                        "is_fixed_time": is_fixed,
                        "explicit_start": explicit_start, "explicit_end": explicit_end,
                        "reason": pending.get("reason", ""), "pages": subj.get("pages", 0),
                        "user_id": user_id, "activity": subj.get("activity_type", "study"),
                        "content_type": "book", "origin": "user",
                        "subject": subj.get("subject"), "topic_resolved": subj.get("topic_resolved"),
                        "detection_confidence": subj.get("detection_confidence"),
                        "matched_card_id": subj.get("matched_card_id"),
                    }
                    if add_part_to_session(target_session_id, pd):
                        added += 1
                affected_dates.add(tdate)
            context.user_data.pop("pending_change", None)
            for tdate in affected_dates:
                plan_x = get_plan_by_date(user_id, tdate)
                if not plan_x:
                    continue
                slots_for_date = time_slots if (tdate == selected_date and time_slots) else None
                plan_x["parts"] = reschedule_parts(
                    plan_x["parts"], base_start_time=get_iran_now(),
                    session_id=plan_x.get("session_id"), user_id=user_id,
                    source_date=tdate, time_slots=slots_for_date
                )
                fresh = get_plan_by_date(user_id, tdate)
                if tdate == selected_date:
                    context.user_data["current_plan"] = fresh
            await update.message.reply_text(
                f"✅ **{added} پارت اضافه شد!**",
                reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
            )
            plan = get_plan_by_date(user_id, selected_date)
            if plan:
                context.user_data["current_plan"] = plan
                await show_parts_final(update, context, plan["parts"])
            return
        target = pending.get("target") or {}
        session_id = pending.get("session_id")
        if action == "add":
            title = (target.get("title") or "فعالیت").strip() or "فعالیت"
            duration = target.get("duration")
            try:
                duration = int(duration) if duration is not None else 45
            except Exception:
                duration = 45
            duration = max(15, duration)
            split_count = pending.get("split_count", 1)
            explicit_start = target.get("explicit_start")
            explicit_end = target.get("explicit_end")
            is_fixed = bool(target.get("is_fixed_time")) or bool(explicit_start and explicit_end)
            tdate = target.get("target_date") or selected_date
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', str(tdate)):
                tdate = selected_date
            target_session_id = get_or_create_session(user_id, tdate)
            if not target_session_id:
                await update.message.reply_text("❌ خطا در ساخت سشن.")
                context.user_data.pop("pending_change", None)
                return
            added = 0
            if split_count > 1:
                for i in range(split_count):
                    pd = {
                        "title": f"{title} (بخش {i+1}/{split_count})",
                        "planned_minutes": duration, "time_slot": "",
                        "planned_start_time": None, "planned_end_time": None,
                        "is_fixed_time": False, "reason": pending.get("reason", ""),
                        "user_id": user_id, "origin": "user",
                        "subject": target.get("subject"),
                        "topic_resolved": target.get("topic_resolved"),
                    }
                    if add_part_to_session(target_session_id, pd):
                        added += 1
            else:
                pd = {
                    "title": title, "planned_minutes": duration,
                    "time_slot": f"{explicit_start}-{explicit_end}" if explicit_start and explicit_end else "",
                    "planned_start_time": explicit_start, "planned_end_time": explicit_end,
                    "is_fixed_time": is_fixed,
                    "explicit_start": explicit_start, "explicit_end": explicit_end,
                    "reason": pending.get("reason", ""), "pages": 0,
                    "user_id": user_id, "origin": "user",
                    "subject": target.get("subject"),
                    "topic_resolved": target.get("topic_resolved"),
                }
                if add_part_to_session(target_session_id, pd):
                    added += 1
            context.user_data.pop("pending_change", None)
            plan = get_plan_by_date(user_id, tdate)
            if plan:
                if not is_fixed:
                    plan["parts"] = reschedule_parts(
                        plan["parts"], base_start_time=get_iran_now(),
                        session_id=target_session_id, user_id=user_id,
                        source_date=tdate, time_slots=pending.get("time_slots") or None
                    )
                    plan = get_plan_by_date(user_id, tdate)
                if tdate == selected_date:
                    context.user_data["current_plan"] = plan
                await update.message.reply_text(
                    f"✅ **پارت جدید اضافه شد!**\n\n📚 {title} | ⏱ {duration}د",
                    reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
                )
                await show_parts_final(update, context, plan["parts"])
            return
        elif action == "delete":
            target_part = pending.get("target_part")
            if not target_part:
                context.user_data.pop("pending_change", None)
                return
            part_id = target_part.get("part_id")
            title = target_part.get("title")
            execute_query("UPDATE reminder_notifications SET status = 'cancelled' WHERE part_id = %s", (part_id,))
            execute_query("DELETE FROM study_parts WHERE part_id = %s", (part_id,))
            execute_query("UPDATE study_sessions SET total_parts = total_parts - 1 WHERE session_id = %s", (session_id,))
            context.user_data.pop("pending_change", None)
            plan = get_plan_by_date(user_id, selected_date)
            if plan:
                plan["parts"] = reschedule_parts(
                    plan["parts"], base_start_time=get_iran_now(),
                    session_id=session_id, user_id=user_id, source_date=selected_date
                )
                plan = get_plan_by_date(user_id, selected_date)
                context.user_data["current_plan"] = plan
                await update.message.reply_text(
                    f"✅ **پارت '{title}' حذف شد!**",
                    reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
                )
                await show_parts_final(update, context, plan["parts"])
            return
        elif action == "update":
            target_part = pending.get("target_part")
            updates = pending.get("updates", {})
            if not target_part:
                context.user_data.pop("pending_change", None)
                return
            part_id = target_part.get("part_id")
            current_title = target_part.get("title")
            update_fields = {}
            if target.get("title") and target.get("title") != current_title:
                update_fields["title"] = target.get("title")
            if target.get("duration"):
                update_fields["planned_minutes"] = target.get("duration")
            if target.get("start_time") and target.get("end_time"):
                update_fields["planned_start_time"] = target.get("start_time")
                update_fields["planned_end_time"] = target.get("end_time")
                update_fields["time_slot"] = f"{target.get('start_time')}-{target.get('end_time')}"
            if update_fields:
                set_clause = ", ".join([f"{k} = %s" for k in update_fields.keys()])
                values = list(update_fields.values()) + [part_id]
                execute_query(f"UPDATE study_parts SET {set_clause} WHERE part_id = %s", tuple(values))
            context.user_data.pop("pending_change", None)
            plan = get_plan_by_date(user_id, selected_date)
            if plan:
                plan["parts"] = reschedule_parts(
                    plan["parts"], base_start_time=get_iran_now(),
                    session_id=session_id, user_id=user_id, source_date=selected_date
                )
                plan = get_plan_by_date(user_id, selected_date)
                context.user_data["current_plan"] = plan
                change_text = "\n".join([f"• {k}: {v}" for k, v in updates.items()])
                await update.message.reply_text(
                    f"✅ **پارت '{current_title}' تغییر کرد!**\n\n{change_text}",
                    reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
                )
                await show_parts_final(update, context, plan["parts"])
            return
        elif action == "clear":
            # حفاظت پارهای محافظت‌شده (بخش ۱۰.۵)
            execute_query(
                """DELETE FROM study_parts WHERE session_id = %s AND NOT (
                    is_fixed_time = TRUE
                    OR content_type IN ('reminder', 'class')
                    OR origin IN ('routine', 'reminder', 'card')
                    OR COALESCE(metadata->>'is_reminder', 'false') = 'true'
                    OR completed = TRUE
                )""",
                (session_id,)
            )
            execute_query(
                """UPDATE study_sessions SET total_parts = (
                    SELECT COUNT(*) FROM study_parts WHERE session_id = %s
                ) WHERE session_id = %s""",
                (session_id, session_id)
            )
            context.user_data.pop("pending_change", None)
            plan = get_plan_by_date(user_id, selected_date)
            if plan:
                context.user_data["current_plan"] = plan
            await update.message.reply_text(
                "🗑 **پارت‌های غیرمحافظت‌شده پاک شدند** (کلاس/یادآور/روتین حفظ شدند)",
                reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
            )
            return
        await update.message.reply_text("❌ خطا در اعمال تغییرات.")
        context.user_data.pop("pending_change", None)
    except Exception as e:
        logger.error(f"خطا در apply_pending_change: {e}")
        await update.message.reply_text("⚠️ خطای موقت. دوباره تلاش کن.")
        context.user_data.pop("pending_change", None)


async def reject_pending_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_change")
    if not pending:
        await update.message.reply_text("❌ هیچ تغییر در انتظار تاییدی وجود ندارد.")
        return
    action = pending.get("action")
    action_names = {"add": "اضافه کردن", "delete": "حذف",
                    "update": "به‌روزرسانی", "clear": "پاک کردن",
                    "build_new": "ساخت برنامه", "multi_add": "افزودن چند فعالیت"}
    action_name = action_names.get(action, "تغییر")
    context.user_data.pop("pending_change", None)
    context.user_data.pop("pending_split", None)
    context.user_data.pop("schedule_choice_pending", None)
    await update.message.reply_text(
        f"❌ **{action_name} لغو شد.**",
        reply_markup=get_main_keyboard()
    )


# ==================== clarify answer ====================
async def handle_clarify_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    pending = context.user_data.get("pending_clarify")
    if not pending:
        return
    options = pending.get("options", [])
    matched_opt = None
    if isinstance(options, list):
        for opt in options:
            if isinstance(opt, dict):
                if text == opt.get("label"):
                    matched_opt = opt
                    break
            elif isinstance(opt, str) and text == opt:
                matched_opt = {"label": opt, "value": opt}
                break
    if matched_opt and matched_opt.get("value") == "cancel":
        context.user_data.pop("pending_clarify", None)
        await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
        return
    if text == "🔙 بازگشت":
        context.user_data.pop("pending_clarify", None)
        await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
        return
    original_text = pending.get("original_text", "")
    question = pending.get("question", "")
    user_answer = matched_opt.get("label") if matched_opt else text
    context.user_data.pop("pending_clarify", None)

    # اگر جواب یک انتخاب خاص بود → اعمال
    if user_answer == "▶️ همین الان شروع کنم":
        context.user_data["schedule_choice_pending"] = {"choice": "now"}
        await process_ai_plan_change(update, context, original_text, "")
        return
    if user_answer == "🕐 ساعت مشخص":
        await update.message.reply_text(
            "🕐 ساعت را بنویس (مثال: ۱۶:۳۰ یا ۱۶:۳۰ تا ۱۷:۳۰):",
            reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True)
        )
        context.user_data["awaiting_schedule_time"] = {"original_text": original_text}
        return
    if user_answer == "🧠 AI در جای مناسب بگذارد":
        context.user_data["schedule_choice_pending"] = {"choice": "let_ai"}
        await process_ai_plan_change(update, context, original_text, "")
        return
    if user_answer in ("🔒 ثابت", "✅ بله", "✅ همین خوبه", "✅ بله، انجام دادم"):
        context.user_data["schedule_choice_pending"] = {"choice": "explicit"}
        context.user_data["clarify_followup"] = {"original_text": original_text,
                                                   "question": question, "user_answer": user_answer}
        await process_ai_plan_change(update, context, original_text, "")
        return
    if user_answer in ("🕊 شناور", "❌ نه"):
        context.user_data["schedule_choice_pending"] = {"choice": "let_ai"}
        context.user_data["clarify_followup"] = {"original_text": original_text,
                                                   "question": question, "user_answer": user_answer}
        await process_ai_plan_change(update, context, original_text, "")
        return
    if user_answer == "🕐 تغییر بدم":
        await update.message.reply_text(
            "🕐 ساعت جدید را بنویس:",
            reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True)
        )
        context.user_data["awaiting_reminder_time"] = {"original_text": original_text}
        return
    if user_answer in ("⏰ صبح", "🌙 شب"):
        hint = "09:00" if user_answer == "⏰ صبح" else "21:00"
        context.user_data["clarify_followup"] = {"original_text": original_text + f" (ساعت: {hint})",
                                                   "question": question, "user_answer": user_answer}
        await process_ai_plan_change(update, context, original_text + f" ساعت {hint}", "")
        return

    combined = f"{original_text}\n[پاسخ به سوال «{question}»: {user_answer}]"
    context.user_data["clarify_followup"] = {
        "original_text": original_text, "question": question,
        "user_answer": user_answer,
    }
    await process_ai_plan_change(update, context, combined, "")


# ==================== plan today ====================
async def handle_today_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    today = get_today_date()
    context.user_data["selected_date"] = today
    plan = get_plan_by_date(user_id, today)
    if plan and plan["parts"]:
        context.user_data["current_plan"] = plan
        nav_push(context, "today_plan")
        if plan.get("confirmed", False):
            await show_parts_final(update, context, plan["parts"])
        else:
            await show_parts_initial(update, context, plan["parts"])
        return
    await update.message.reply_text(
        "📝 **برنامه‌ای وجود ندارد.**\n\nچگونه بسازیم؟",
        reply_markup=get_build_plan_keyboard(),
        parse_mode=ParseMode.HTML
    )


async def handle_build_with_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بخش ۱۵ و ۲۱ — با پروژه فعال، ساخت حول آن."""
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    if not await check_and_consume_kok(update, user_id, "ai_plan_build"):
        return
    user_data = get_user_data(str(update.effective_user.id))
    if not user_data:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    target_date = get_today_date()
    wait_msg = await update.message.reply_text("🧠 در حال ساخت برنامه با AI...")
    try:
        ai_response = await generate_plan_for_date(user_id, user_data, target_date)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        if ai_response and ai_response.get('parts'):
            session_id = create_plan_for_date(user_id, user_data, ai_response, target_date)
            if session_id:
                # خواندن تاریخ session (نه get_today_date ثابت)
                session_row = execute_query(
                    "SELECT date FROM study_sessions WHERE session_id = %s",
                    (session_id,), fetch=True
                )
                actual_date = session_row[0] if session_row else target_date
                plan = get_plan_by_date(user_id, actual_date)
                if plan and plan.get("parts"):
                    context.user_data["current_plan"] = plan
                    context.user_data["selected_date"] = actual_date
                    nav_push(context, "today_plan")
                    await show_parts_initial(update, context, plan["parts"])
                    return
            logger.error(f"build_ai_empty: user={user_id} target={target_date} session_id={session_id}")
        add_kok(user_id, get_kok_cost("ai_plan_build"), "refund",
                description="بازگشت KOK - شکست ساخت برنامه")
        await update.message.reply_text(
            "❌ برنامه ساخته شد ولی خالی است. KOK برگشت داده شد.",
            reply_markup=get_build_plan_keyboard()
        )
    except Exception as e:
        logger.error(f"خطا در handle_build_with_ai: {e}")
        try:
            await wait_msg.delete()
        except Exception:
            pass
        add_kok(user_id, get_kok_cost("ai_plan_build"), "refund")
        await update.message.reply_text("❌ خطا. KOK برگشت داده شد.")


# ==================== Manual build ====================
async def start_manual_build(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["build_mode"] = "smart_manual"
    context.user_data["build_step"] = "time_input"
    context.user_data["build_data"] = {}
    await update.message.reply_text(
        "⏰ **زمان‌های مطالعه امروز:**\n\n"
        "هر خط یک بازه (۲۴ ساعته). بازه‌ها می‌تونن ناپیوسته باشن.\n\n"
        "مثال‌ها:\n• `۱۴-۱۸`\n• `۱۶:۲۰_۲۰:۳۰`\n• `۱۴:۳۰ تا ۱۸:۴۵`\n\n"
        "📝 یا دکمه **⏭ رد کردن**",
        reply_markup=ReplyKeyboardMarkup([["⏭ رد کردن"], ["🔙 بازگشت"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_smart_manual_time(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if text == "⏭ رد کردن":
        context.user_data["build_data"]["time_slots"] = []
        context.user_data["build_step"] = "topic_input"
        await update.message.reply_text(
            "📚 **مباحث:**\n\nمثال: `زیست ۱۱: مطالعه ۲۰ صفحه`",
            reply_markup=ReplyKeyboardMarkup([["⏭ رد کردن"], ["🔙 بازگشت"]], resize_keyboard=True),
            parse_mode=ParseMode.HTML
        )
        return
    time_slots = analyze_time_slots(text)
    if not time_slots:
        await update.message.reply_text(
            "❌ زمان مشخصی تشخیص داده نشد. دوباره یا **⏭ رد کردن**.",
            parse_mode=ParseMode.HTML
        )
        return
    context.user_data["build_data"]["time_slots"] = time_slots
    context.user_data["build_step"] = "topic_input"
    total_min = total_slot_minutes(time_slots)
    time_text = "، ".join(time_slots)
    await update.message.reply_text(
        f"✅ زمان ثبت شد: `{time_text}`\n⏱ مجموع: {format_time_hours_minutes(total_min)}\n\n"
        "📚 **حالا مباحث:**",
        reply_markup=ReplyKeyboardMarkup([["⏭ رد کردن"], ["🔙 بازگشت"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_smart_manual_topics(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if text in ["⏭ رد کردن", "🔙 بازگشت"]:
        if text == "🔙 بازگشت":
            for k in ["build_mode", "build_step", "build_data"]:
                context.user_data.pop(k, None)
            await update.message.reply_text("🔙 بازگشت", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("❌ برای ساخت برنامه، مباحث لازمه.")
        return
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    if not await check_and_consume_kok(update, user_id, "manual_plan_build"):
        return
    user_data = get_user_data(str(update.effective_user.id))
    time_slots = context.user_data["build_data"].get("time_slots", [])
    total_min = total_slot_minutes(time_slots)
    loading = await update.message.reply_text("🧠 **در حال تحلیل...**", parse_mode=ParseMode.HTML)
    ai_topics = await detect_topics_with_ai(text, user_data, time_slots)
    topics = None
    source = "AI"
    if ai_topics:
        topics = ai_topics
    else:
        topics = detect_topics_fallback_regex(text, user_data)
        source = "regex"
        if not topics:
            await loading.delete()
            await update.message.reply_text("❌ هیچ مبحثی تشخیص داده نشد.")
            return
    for t in topics:
        if not isinstance(t, dict):
            continue
        title = t.get('title') or ''
        subject = t.get('subject') or ''
        title = str(title).strip() if title else ''
        subject = str(subject).strip() if subject else ''
        if not title and subject:
            t['title'] = subject
        if not subject and title:
            t['subject'] = title
        if not t.get('title'):
            t['title'] = t.get('topic') or 'مطالعه'
        if not t.get('subject'):
            t['subject'] = t['title']
    await loading.delete()
    finalized = finalize_detected_topics(
        topics, user_data, 45,
        time_slots=time_slots if time_slots else None,
        total_slot_min=total_min if time_slots else 0
    )
    if not finalized:
        await update.message.reply_text("❌ هیچ مبحثی تشخیص داده نشد.")
        return
    summary = f"📋 **مباحث** ({source}):\n\n"
    total_planned = 0
    for i, t in enumerate(finalized, 1):
        summary += f"{i}. 📌 **{t['title']}**\n"
        if t.get('subject') and t['subject'] != t['title']:
            summary += f"   📚 {t['subject']}\n"
        if t.get('pages', 0) > 0:
            summary += f"   📄 {t['pages']} صفحه\n"
        if t.get('test_count', 0) > 0:
            summary += f"   📝 {t['test_count']} تست\n"
        if t.get('is_fixed_time'):
            summary += f"   🔒 {t.get('explicit_start')}-{t.get('explicit_end')}\n"
        summary += f"   ⏱ **{t['total_minutes']} دقیقه**\n\n"
        total_planned += t['total_minutes']
    context.user_data["build_data"]["topics"] = finalized
    if time_slots:
        summary += f"\n⏱ مجموع بازه‌ها: {format_time_hours_minutes(total_min)}\n"
        summary += f"📊 مجموع پارت‌ها: {format_time_hours_minutes(total_planned)}\n"
        summary += "\n🚀 **در حال ساخت...**"
        await update.message.reply_text(summary, parse_mode=ParseMode.HTML)
        context.user_data["build_data"]["part_duration"] = 0
        await build_manual_plan(update, context)
    else:
        await update.message.reply_text(
            f"{summary}\n⏱ **زمان میانگین هر پارت:**",
            reply_markup=get_duration_keyboard(), parse_mode=ParseMode.HTML
        )
        context.user_data["build_step"] = "duration_input"


async def handle_smart_manual_duration(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if text.startswith("⏱ "):
        try:
            duration = int(text.split()[1])
        except Exception:
            return
    elif text == "✏️ دلخواه":
        await update.message.reply_text("✏️ زمان دلخواه (۲۰-۹۰ دقیقه):")
        context.user_data["build_step"] = "custom_duration"
        return
    else:
        await update.message.reply_text("❌ یکی از گزینه‌ها رو انتخاب کن.")
        return
    context.user_data["build_data"]["part_duration"] = duration
    await build_manual_plan(update, context)


async def handle_smart_manual_custom_duration(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        duration = int(text)
        if 20 <= duration <= 90:
            context.user_data["build_data"]["part_duration"] = duration
            await build_manual_plan(update, context)
        else:
            await update.message.reply_text("❌ زمان باید بین ۲۰ تا ۹۰ باشه.")
    except Exception:
        await update.message.reply_text("❌ یک عدد معتبر وارد کن.")


async def build_manual_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    user_data = get_user_data(str(update.effective_user.id))
    build_data = context.user_data.get("build_data", {})
    topics = build_data.get("topics", [])
    time_slots = build_data.get("time_slots", [])
    part_duration = build_data.get("part_duration", 45)
    if not topics:
        await update.message.reply_text("❌ مبحثی وجود ندارد.")
        return
    parts = []
    part_number = 1
    for topic in topics:
        duration = topic["total_minutes"]
        if not time_slots and part_duration > 0:
            duration = part_duration
            if topic.get("priority") == "high":
                duration = int(duration * 1.2)
            elif topic.get("priority") == "low":
                duration = int(duration * 0.8)
            duration = max(20, min(90, duration))
        detection = await detect_subject_for_part(
            topic["title"], topic.get("activity", "study"), user_data
        )
        parts.append({
            "part_number": part_number, "title": topic["title"],
            "topic": topic.get("topic") or "",
            "activity": topic.get("activity", "study"),
            "planned_minutes": duration, "pages": topic.get("pages", 0),
            "time_slot": "", "planned_start_time": None, "planned_end_time": None,
            "completed": False, "is_fixed_time": topic.get("is_fixed_time", False),
            "explicit_start": topic.get("explicit_start"),
            "explicit_end": topic.get("explicit_end"),
            "depends_on": topic.get("depends_on"),
            "reason": topic.get("reason", "ساخت دستی"),
            "origin": "user", "subject": detection.get("subject"),
            "topic_resolved": detection.get("topic"),
            "detection_confidence": detection.get("confidence"),
            "matched_card_id": detection.get("matched_card_id"),
        })
        part_number += 1
    if time_slots:
        parts = schedule_parts_with_time_slots(parts, time_slots, get_today_date())
    else:
        parts = reschedule_parts(parts, base_start_time=get_iran_now())
    session_id = save_session_with_parts(user_id, parts, time_slots, topics)
    if not session_id:
        await update.message.reply_text("❌ خطا در ذخیره برنامه.")
        return
    for k in ["build_mode", "build_step", "build_data"]:
        context.user_data.pop(k, None)
    await update.message.reply_text(
        f"✅ **برنامه ساخته شد!**\n\n📊 {len(parts)} پارت\n"
        f"⏱ زمان کل: {format_time_hours_minutes(sum(p['planned_minutes'] for p in parts))}",
        parse_mode=ParseMode.HTML
    )
    plan = get_plan_by_date(user_id, get_today_date())
    if plan:
        context.user_data["current_plan"] = plan
        context.user_data["edit_mode"] = "order"
        await show_edit_order(update, context)


# ==================== گزارش ====================
async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    today = get_today_date()
    week_start = (get_iran_now() - timedelta(days=6)).strftime("%Y-%m-%d")
    delay_today = calc_delay_percent(user_id, today, today)
    delay_week = calc_delay_percent(user_id, week_start, today)
    time_totals = calc_time_totals(user_id)
    subjects = calc_subject_breakdown(user_id, week_start, today)
    text = f"📊 **گزارش عملکرد**\n📅 امروز {get_today_shamsi()}\n\n"
    delay_color = "🟢" if delay_today["percent"] < 10 else ("🟡" if delay_today["percent"] < 25 else "🔴")
    text += f"⚡ **عملکرد کلی**\n{delay_color} تأخیر امروز: {delay_today['percent']}٪\n"
    text += f"📉 تأخیر هفته: {delay_week['percent']}٪\n\n"
    text += f"⏱ **زمان‌ها**\n📅 امروز: {format_time_hours_minutes(time_totals['day']['minutes'])}\n"
    text += f"📆 هفته: {format_time_hours_minutes(time_totals['week']['minutes'])}\n"
    text += f"🗓 ماه: {format_time_hours_minutes(time_totals['month']['minutes'])}\n\n"
    if subjects:
        text += f"📚 **دروس این هفته:**\n"
        for s in subjects[:5]:
            text += f"  📖 {s['subject']}: {format_time_hours_minutes(s['done_minutes'])} ({s['time_percent']}٪)\n"
    balance = get_user_kok(user_id)
    text += f"\n💎 KOK: {balance}"
    keyboard = [
        ["📖 جزئیات دروس", "📈 نمودار هفتگی"],
        ["🧠 تحلیل AI", "📅 گزارش ماهانه"],
        ["🔙 بازگشت"]
    ]
    nav_push(context, "report")
    await update.message.reply_text(
        text, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def handle_report_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    today = get_today_date()
    week_start = (get_iran_now() - timedelta(days=6)).strftime("%Y-%m-%d")
    subjects = calc_subject_breakdown(user_id, week_start, today)
    if not subjects:
        await update.message.reply_text("📭 فعالیتی ثبت نشده.", reply_markup=get_main_keyboard())
        return
    text = "📚 **عملکرد دروس — هفته اخیر**\n\n"
    for s in subjects:
        text += f"📖 **{s['subject']}**\n"
        text += f"   ⏱ {format_time_hours_minutes(s['done_minutes'])} ({s['time_percent']}٪)\n"
        text += f"   ✅ تعهد: {s['commitment_percent']}٪\n"
        text += f"   📊 {s['completed_parts']}/{s['total_parts']} پارت\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_report_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    today = get_iran_now().date()
    text = "📈 **نمودار هفتگی**\n\n"
    max_min = 1
    days_data = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        stats = execute_query(
            """SELECT COALESCE(SUM(COALESCE(actual_minutes, 0)), 0)
               FROM study_parts p
               JOIN study_sessions s ON s.session_id = p.session_id
               WHERE s.user_id = %s AND s.date = %s""",
            (user_id, d_str), fetch=True
        )
        minutes = int(stats[0]) if stats and stats[0] else 0
        days_data.append({"weekday": get_weekday_fa(d), "minutes": minutes})
        if minutes > max_min:
            max_min = minutes
    for d in days_data:
        bar_len = int((d["minutes"] / max_min) * 20) if max_min > 0 else 0
        bar = "█" * bar_len
        text += f"{d['weekday']:<8} {bar} {format_time_hours_minutes(d['minutes'])}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_report_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    user_data = get_user_data(str(update.effective_user.id))
    if not user_id:
        return
    if not await check_and_consume_kok(update, user_id, "report_analysis"):
        return
    wait = await update.message.reply_text("🧠 در حال تحلیل...")
    today = get_today_date()
    week_start = (get_iran_now() - timedelta(days=6)).strftime("%Y-%m-%d")
    delay_week = calc_delay_percent(user_id, week_start, today)
    subjects = calc_subject_breakdown(user_id, week_start, today)
    time_totals = calc_time_totals(user_id)
    hourly = calc_hourly_breakdown(user_id, week_start, today)
    subjects_text = ""
    for s in subjects:
        subjects_text += f"  • {s['subject']}: {s['done_minutes']}د، تعهد {s['commitment_percent']}٪\n"
    prompt = f"""تو یک تحلیل‌گر عملکرد مطالعه هستی.

=== آمار هفته ===
تأخیر: {delay_week['percent']}٪ | زمان هفته: {time_totals['week']['minutes']}د
تفکیک دروس:\n{subjects_text}
تفکیک ساعتی: صبح {hourly['morning']}٪ ظهر {hourly['noon']}٪ عصر {hourly['evening']}٪ شب {hourly['night']}٪
کاربر: {user_data.get('goal', 'نامشخص') if user_data else 'نامشخص'}

=== خروجی JSON ===
{{"weaknesses": ["..."], "suggestions": ["..."], "focus_next_week": ["..."]}}
⚠️ فقط JSON خالص."""
    model_type = get_user_model_type(user_id)
    response = await call_ai(prompt, max_tokens=1000, temperature=0.4, model_type=model_type)
    await wait.delete()
    if not response:
        await update.message.reply_text("❌ AI پاسخ نداد.")
        return
    result = safe_json_loads(response, dict) or {}
    text = "🧠 **تحلیل هوشمند AI**\n\n"
    if result.get("weaknesses"):
        text += "⚠️ **نقاط ضعف:**\n"
        for w in result["weaknesses"]:
            text += f"  • {w}\n"
        text += "\n"
    if result.get("suggestions"):
        text += "💡 **پیشنهادها:**\n"
        for s in result["suggestions"]:
            text += f"  • {s}\n"
        text += "\n"
    if result.get("focus_next_week"):
        text += "🎯 **تمرکز این هفته:**\n"
        for f in result["focus_next_week"]:
            text += f"  • {f}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_report_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        return
    today = get_today_date()
    month_start = (get_iran_now() - timedelta(days=29)).strftime("%Y-%m-%d")
    delay = calc_delay_percent(user_id, month_start, today)
    time_totals = calc_time_totals(user_id)
    subjects = calc_subject_breakdown(user_id, month_start, today)
    text = "📅 **گزارش ماهانه (۳۰ روز)**\n\n"
    text += f"⏱ زمان کل: {format_time_hours_minutes(time_totals['month']['minutes'])}\n"
    text += f"📊 روزهای فعال: {time_totals['month']['active_days']}/30\n"
    text += f"⚡ تأخیر کل: {delay['percent']}٪\n\n"
    if subjects:
        text += "📚 **دروس:**\n"
        for s in subjects[:8]:
            text += f"  📖 {s['subject']}: {format_time_hours_minutes(s['done_minutes'])}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ==================== اشتراک ====================
async def kok_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    balance = get_user_kok(user_id)
    txs = execute_query(
        """SELECT amount, balance_after, reason, created_at
           FROM kok_transactions WHERE user_id = %s
           ORDER BY created_at DESC LIMIT 10""",
        (user_id,), fetchall=True
    )
    text = f"💎 **موجودی: {balance} KOK**\n\n"
    if txs:
        text += "📜 **آخرین تراکنش‌ها:**\n\n"
        for tx in txs:
            amount, after, reason, created = tx
            label = KOK_ACTION_LABELS.get(reason, reason)
            sign = "+" if amount > 0 else ""
            emoji = "🟢" if amount > 0 else "🔴"
            dt = created.strftime("%m/%d %H:%M") if created else ""
            text += f"{emoji} {sign}{amount} | {label}\n   → {after} | {dt}\n\n"
    else:
        text += "تراکنشی نداری.\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    balance = get_user_kok(user_id)
    total_used = execute_query("SELECT total_kok_used FROM users WHERE id = %s", (user_id,), fetch=True)
    total_used = total_used[0] if total_used else 0
    plans = get_available_plans()
    text = f"💰 **KOK و اشتراک**\n\n💎 موجودی: **{balance}** KOK\n📊 مصرف کل: {total_used} KOK\n\n"
    text += "━━━━━━━━━━━━━━━━\n\n🌟 **پلن‌ها:**\n\n"
    for p in plans:
        name = p["name"]
        price = p["price"]
        kok = p["kok_amount"]
        access = "🚀 پیشرفته" if p["model_access"] == "advanced" else "📘 عادی"
        if price == 0:
            text += f"🌱 **{name}** — رایگان\n   💎 {kok} KOK هدیه\n\n"
        else:
            price_fmt = f"{price:,}".replace(",", "،")
            text += f"📦 **{name}**\n   💎 {kok} KOK\n   💰 {price_fmt} تومان\n   🎯 {access}\n\n"
    text += "━━━━━━━━━━━━━━━━\n💳 شماره کارت:\n`6219861837569689`\n👤 مصطفی فرخندئی\n\n"
    text += "📸 بعد از واریز، رسید رو بفرست."
    nav_push(context, "subscription")
    await update.message.reply_text(text, reply_markup=get_subscription_keyboard(), parse_mode=ParseMode.HTML)


async def handle_payment_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    if not context.user_data.get("awaiting_receipt"):
        await update.message.reply_text(
            "ℹ️ برای ارسال رسید، اول از منوی 💰 KOK و اشتراک یک پلن انتخاب کن.",
            reply_markup=get_main_keyboard()
        )
        return
    photo = update.message.photo[-1]
    file_id = photo.file_id
    user = update.effective_user
    caption_text = update.message.caption or ""
    plan_id = context.user_data.get("pending_plan")
    if not plan_id:
        for p in get_available_plans():
            if p["name"] in caption_text:
                plan_id = p["id"]
                break
    if not plan_id:
        await update.message.reply_text(
            "❌ پلن مشخص نیست. دوباره از منو انتخاب کن.",
            reply_markup=get_main_keyboard()
        )
        context.user_data.pop("awaiting_receipt", None)
        return
    execute_query(
        "INSERT INTO pending_payments (user_id, photo_file_id, caption, plan_id) VALUES (%s, %s, %s, %s)",
        (user_id, file_id, caption_text, plan_id)
    )
    plan_info = execute_query(
        "SELECT name, kok_amount, price FROM subscription_plans WHERE id = %s",
        (plan_id,), fetch=True
    )
    plan_name = plan_info[0] if plan_info else "نامشخص"
    plan_kok = plan_info[1] if plan_info else 0
    plan_price = plan_info[2] if plan_info else 0
    caption = f"""📸 **رسید جدید**

👤 {user.full_name}
🆔 {user.id}
📱 @{user.username if user.username else 'ندارد'}
📅 {get_today_shamsi()}
💎 پلن: {plan_name} ({plan_kok} KOK)
💰 مبلغ: {plan_price:,} تومان

/approve {user.id} - تایید
/reject {user.id} - رد
"""
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin_id, photo=file_id,
                                          caption=caption, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"خطا در ارسال به ادمین: {e}")
    context.user_data.pop("pending_plan", None)
    context.user_data.pop("awaiting_receipt", None)
    await update.message.reply_text(
        f"✅ رسید پلن **{plan_name}** ارسال شد.\n"
        f"پس از تایید ادمین، {plan_kok} KOK اضافه می‌شه.",
        reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
    )


# ==================== یادآور action ====================
async def handle_reminder_action(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user_telegram_id = update.effective_user.id
    active = context.bot_data.get("active_reminders", {}).get(str(user_telegram_id))
    if not active:
        await update.message.reply_text("❌ یادآور فعالی نیست.", reply_markup=get_main_keyboard())
        return
    part_id = active["part_id"]
    if text == "✅ انجامش دادم":
        user_id = get_user_id_by_telegram(user_telegram_id)
        check = execute_query(
            "SELECT title, planned_minutes, session_id FROM study_parts WHERE part_id = %s",
            (part_id,), fetch=True
        )
        if check:
            title, planned_min, session_id = check
            execute_query(
                """UPDATE study_parts SET completed = TRUE,
                   actual_minutes = %s, actual_end_time = %s WHERE part_id = %s""",
                (planned_min, datetime.now(IRAN_TZ), part_id)
            )
            execute_query(
                """UPDATE study_sessions SET completed_parts = (
                    SELECT COUNT(*) FROM study_parts
                    WHERE session_id = %s AND completed = TRUE
                ) WHERE session_id = %s""",
                (session_id, session_id)
            )
            execute_query(
                "UPDATE reminder_notifications SET status = 'cancelled' WHERE part_id = %s AND status = 'pending'",
                (part_id,)
            )
            await update.message.reply_text(
                f"✅ **{title}** انجام شد! آفرین 🎉",
                reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
            )
    elif text == "⏰ ۳۰ دقیقه دیگه":
        new_time = get_iran_now() + timedelta(minutes=30)
        execute_query(
            """INSERT INTO reminder_notifications
               (part_id, user_id, notify_type, scheduled_at, status)
               VALUES (%s, (SELECT user_id FROM study_parts WHERE part_id = %s), 'snooze', %s, 'pending')""",
            (part_id, part_id, new_time.replace(tzinfo=None))
        )
        await update.message.reply_text(
            "⏰ باشه، ۳۰ دقیقه دیگه دوباره یادت می‌ندازم.",
            reply_markup=get_main_keyboard()
        )
    context.bot_data["active_reminders"].pop(str(user_telegram_id), None)


# ==================== pending card suggestion ====================
async def handle_pending_card_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    pending = context.user_data.get("pending_card_suggestion")
    if not pending:
        return
    if text == "✅ بله، کارت بساز":
        card_type = pending["type"]
        source_text = pending["text"]
        context.user_data.pop("pending_card_suggestion", None)
        if card_type == "routine":
            context.user_data["routin_mode"] = "free_text"
            await handle_routin_free_text(update, context, source_text)
        else:
            context.user_data["project_mode"] = "free_text"
            await handle_project_free_text(update, context, source_text)
    else:
        context.user_data.pop("pending_card_suggestion", None)
        await update.message.reply_text("✅ باشه، فقط همین امروز.", reply_markup=get_main_keyboard())


async def handle_reminder_plan_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    pending_reminder = context.user_data.get("pending_reminder_plan")
    if not pending_reminder:
        return
    if text == "❌ نه، فقط یادآور":
        context.user_data.pop("pending_reminder_plan", None)
        await update.message.reply_text(
            "✅ باشه، فقط یادآور رو نگه می‌دارم. 🔔",
            reply_markup=get_main_keyboard()
        )
        return
    if text == "📚 بله، برنامه بچین":
        reminder = pending_reminder.get("reminder", {})
        target_date = pending_reminder.get("date")
        title = reminder.get("title", "")
        date_shamsi = reminder.get("date", "")
        if not date_shamsi and target_date:
            date_shamsi = get_shamsi_date(target_date)
        request = (f"برای «{title}» که تاریخش {date_shamsi} هست، "
                   f"یه برنامه مطالعاتی مناسب بچین. یادآور «{title}» در این روز حفظ بشه.")
        user_id = pending_reminder.get("user_id")
        context.user_data.pop("pending_reminder_plan", None)
        user_data = get_user_data(str(update.effective_user.id))
        await update.message.reply_text("🧠 در حال ساخت برنامه...")
        ai_response = await generate_plan_for_date(user_id, user_data, target_date, request)
        if ai_response and ai_response.get('parts'):
            session_id = create_plan_for_date(user_id, user_data, ai_response, target_date)
            if session_id:
                plan = get_plan_by_date(user_id, target_date)
                if plan:
                    context.user_data["current_plan"] = plan
                    context.user_data["selected_date"] = target_date
                    await update.message.reply_text(
                        f"✅ **برنامه برای {date_shamsi} ساخته شد!**",
                        parse_mode=ParseMode.HTML
                    )
                    await show_parts_initial(update, context, plan["parts"])
                    return
        await update.message.reply_text("❌ نتونستم برنامه بسازم.", reply_markup=get_main_keyboard())


# ==================== save_pending_class_card ====================
async def save_pending_class_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_class_card")
    if not pending:
        return
    user_id = pending["user_id"]
    classes = pending["classes"]
    raw_text = pending.get("raw_text", "")
    wait = await update.message.reply_text("🔄 در حال ذخیره...")
    try:
        parts_created = 0
        reminders_created = 0
        for cls in classes:
            date_str = cls["date"]
            target_session = execute_query(
                """SELECT session_id FROM study_sessions
                   WHERE user_id = %s AND date = %s AND archived = FALSE
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, date_str), fetch=True
            )
            if target_session:
                new_session_id = target_session[0]
            else:
                new_session_id = get_or_create_session(user_id, date_str)
                if not new_session_id:
                    continue
            start_min = time_to_minutes(cls["start_time"])
            end_min = time_to_minutes(cls["end_time"])
            duration = max(15, end_min - start_min)
            card_data = {
                "title": cls["title"], "subject": cls.get("subject") or cls["title"],
                "teacher": cls.get("teacher", ""), "day_of_week": cls.get("day_of_week", ""),
                "start_time": cls["start_time"], "end_time": cls["end_time"],
                "location": cls.get("location", ""), "date": date_str,
            }
            card_id = save_card(
                card_type="class", content_type="class",
                data=card_data, priority=8, raw_text=raw_text, created_by=user_id
            )
            if card_id:
                try:
                    await index_card_to_rag(card_id)
                except Exception:
                    pass
            part_data = {
                "title": f"🏫 {cls['title']}", "planned_minutes": duration,
                "time_slot": f"{cls['start_time']}-{cls['end_time']}",
                "planned_start_time": cls["start_time"],
                "planned_end_time": cls["end_time"],
                "is_fixed_time": True, "explicit_start": cls["start_time"],
                "explicit_end": cls["end_time"],
                "reason": f"کلاس {cls.get('teacher', '')}".strip(),
                "pages": 0, "user_id": user_id, "activity": "class",
                "content_type": "class", "origin": "card",
                "subject": cls.get("subject") or cls["title"],
                "matched_card_id": card_id,
                "metadata": {"teacher": cls.get("teacher", ""), "location": cls.get("location", "")}
            }
            new_part_id = add_part_to_session(new_session_id, part_data)
            if not new_part_id:
                continue
            parts_created += 1
            try:
                sh, sm = map(int, cls["start_time"].split(":"))
                remind_min = max(0, sh * 60 + sm - cls["reminder_before_minutes"])
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                remind_dt = datetime.combine(target_date, datetime.min.time())
                remind_dt = remind_dt.replace(hour=remind_min // 60, minute=remind_min % 60)
                remind_dt = IRAN_TZ.localize(remind_dt)
                if remind_dt > get_iran_now():
                    execute_query(
                        """INSERT INTO reminder_notifications
                           (part_id, user_id, notify_type, scheduled_at, status)
                           VALUES (%s, %s, 'class_reminder', %s, 'pending')
                           ON CONFLICT (part_id, notify_type) DO NOTHING""",
                        (new_part_id, user_id, remind_dt.replace(tzinfo=None))
                    )
                    reminders_created += 1
            except Exception as e:
                logger.error(f"خطا در یادآور کلاس: {e}")
        await wait.delete()
        await update.message.reply_text(
            f"✅ **ذخیره شد!**\n\n🏫 {parts_created} کلاس ثبت شد\n🔔 {reminders_created} یادآور زمان‌بندی شد",
            reply_markup=get_main_keyboard(), parse_mode=ParseMode.HTML
        )
        context.user_data.pop("pending_class_card", None)
    except Exception as e:
        logger.error(f"خطا در save_pending_class_card: {e}")
        try:
            await wait.delete()
        except Exception:
            pass
        await update.message.reply_text("❌ خطای موقت. دوباره تلاش کن.")
        context.user_data.pop("pending_class_card", None)
# ==================== bot.py - بخش 4/4 ====================
# RAG handlers + Role handlers + Cards admin + یادآورها + ادمین +
# handle_text_other (router با اولویت‌بندی دقیق) + cleanup jobs + main

# ==================== RAG Handlers ====================
async def rag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    context.user_data["rag_mode"] = True
    nav_push(context, "admin")
    await update.message.reply_text(
        "📚 **حالت RAG فعال شد**\n\n"
        "متن یا فایل مورد نظر رو ارسال کن.\n\n"
        "دستورات:\n/rag_list — لیست اسناد\n/rag_delete [id] — حذف\n/rag_end — خروج",
        reply_markup=get_rag_keyboard(), parse_mode=ParseMode.HTML
    )


async def handle_rag_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user_id = update.effective_user.id
    if text == "🔙 بازگشت" or text == "/rag_end":
        context.user_data.pop("rag_mode", None)
        await update.message.reply_text("🔙 از حالت RAG خارج شدی.", reply_markup=get_admin_panel_keyboard())
        return
    if text == "📋 لیست اسناد":
        await rag_list_command(update, context)
        return
    if text == "🗑 حذف سند":
        await update.message.reply_text("برای حذف از /rag_delete [id] استفاده کن.")
        return
    if text == "📤 ارسال متن":
        await update.message.reply_text("✍️ متن مورد نظر رو بفرست:")
        return
    if text == "📁 ارسال فایل":
        await update.message.reply_text("📁 فایل PDF یا TXT رو بفرست:")
        return
    if text.startswith("/"):
        return
    wait_msg = await update.message.reply_text("🧠 در حال Embed...")
    chunks = []
    max_len = 1500
    if len(text) > max_len:
        for i in range(0, len(text), max_len):
            chunks.append(text[i:i+max_len])
    else:
        chunks = [text]
    saved = 0
    for chunk in chunks:
        if len(chunk.strip()) < 20:
            continue
        emb = await embed_text(chunk)
        if emb:
            doc_id = save_rag_document(
                content=chunk, embedding=emb,
                source=f"admin_{user_id}", category="general", created_by=user_id
            )
            if doc_id:
                saved += 1
    await wait_msg.delete()
    if saved > 0:
        await update.message.reply_text(
            f"✅ **{saved} سند ذخیره شد!**\n\n📚 مجموع: {len(chunks)} بخش",
            reply_markup=get_rag_keyboard(), parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("❌ خطا در ذخیره.", reply_markup=get_rag_keyboard())


async def handle_rag_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.user_data.get("rag_mode"):
        return
    doc = update.message.document
    user_id = update.effective_user.id
    wait_msg = await update.message.reply_text("📥 در حال دریافت فایل...")
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        text = ""
        if doc.file_name and doc.file_name.endswith(".txt"):
            text = file_bytes.decode('utf-8', errors='ignore')
        elif doc.file_name and doc.file_name.endswith(".pdf"):
            try:
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                for page in reader.pages:
                    text += page.extract_text() or ""
                    text += "\n"
            except ImportError:
                text = file_bytes.decode('utf-8', errors='ignore')
        else:
            text = file_bytes.decode('utf-8', errors='ignore')
        await wait_msg.edit_text(f"🧠 در حال Embed {len(text)} کاراکتر...")
        chunks = []
        max_len = 1500
        for i in range(0, len(text), max_len):
            chunks.append(text[i:i+max_len])
        saved = 0
        for chunk in chunks:
            if len(chunk.strip()) < 20:
                continue
            emb = await embed_text(chunk)
            if emb:
                doc_id = save_rag_document(
                    content=chunk, embedding=emb,
                    source=doc.file_name or f"file_{user_id}",
                    category="file", created_by=user_id
                )
                if doc_id:
                    saved += 1
        await wait_msg.delete()
        await update.message.reply_text(
            f"✅ **{saved} بخش ذخیره شد!**\n\n📄 {doc.file_name}\n📊 {len(text)} کاراکتر",
            reply_markup=get_rag_keyboard(), parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"خطا در پردازش فایل RAG: {e}")
        try:
            await wait_msg.edit_text("❌ خطا در پردازش فایل.")
        except Exception:
            pass


async def rag_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    docs = list_rag_documents(30)
    if not docs:
        await update.message.reply_text("📭 سندی موجود نیست.")
        return
    result = "📚 **اسناد RAG:**\n\n"
    for d in docs:
        result += f"🆔 {d['id']} | {d.get('source', '')}\n   {d['content'][:80]}...\n\n"
    await update.message.reply_text(result, parse_mode=ParseMode.HTML)


async def rag_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("❌ /rag_delete [id]")
        return
    try:
        doc_id = int(context.args[0])
        if delete_rag_document(doc_id):
            await update.message.reply_text(f"✅ سند #{doc_id} حذف شد.")
        else:
            await update.message.reply_text("❌ خطا در حذف.")
    except ValueError:
        await update.message.reply_text("❌ ID نامعتبر.")


async def rag_end_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("rag_mode", None)
    await update.message.reply_text("🔙 از حالت RAG خارج شدی.", reply_markup=get_admin_panel_keyboard())


# ==================== Role Handlers ====================
async def role_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    if not context.args:
        context.user_data["role_mode"] = True
        nav_push(context, "admin")
        await update.message.reply_text(
            "🎭 **ثبت شخصیت AI**\n\nمتن شخصیت رو بنویس.\n\n"
            "مثال: «تو یک مشاور سختگیر و جدی هستی که مستقیم حرف می‌زنی»\n\n"
            "دستورات:\n/role_list — لیست\n/role_active [id] — فعال‌سازی\n/role_delete [id] — حذف",
            reply_markup=get_role_keyboard(), parse_mode=ParseMode.HTML
        )
        return
    role_text = " ".join(context.args)
    role_id = save_ai_role(role_text, created_by=update.effective_user.id)
    if role_id:
        await update.message.reply_text(
            f"✅ شخصیت #{role_id} ذخیره شد.\n\nبرای فعال‌سازی: /role_active {role_id}",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("❌ خطا در ذخیره.")


async def handle_role_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.user_data.get("role_mode"):
        return
    if text == "🔙 بازگشت":
        context.user_data.pop("role_mode", None)
        await update.message.reply_text("🔙", reply_markup=get_admin_panel_keyboard())
        return
    if text == "📋 لیست شخصیت‌ها":
        await role_list_command(update, context)
        return
    if text == "✏️ شخصیت جدید":
        await update.message.reply_text("✏️ متن شخصیت رو بنویس:")
        return
    if text == "✅ فعال‌سازی":
        await update.message.reply_text("برای فعال‌سازی: /role_active [id]")
        return
    if text == "🗑 حذف":
        await update.message.reply_text("برای حذف: /role_delete [id]")
        return
    if text.startswith("/"):
        return
    role_id = save_ai_role(text, created_by=update.effective_user.id)
    if role_id:
        await update.message.reply_text(
            f"✅ شخصیت #{role_id} ذخیره شد.\n\nبرای فعال‌سازی: /role_active {role_id}",
            reply_markup=get_role_keyboard(), parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("❌ خطا در ذخیره.")


async def role_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    roles = list_ai_roles()
    if not roles:
        await update.message.reply_text("📭 شخصیتی موجود نیست.")
        return
    result = "🎭 **شخصیت‌های AI:**\n\n"
    for r in roles:
        status = "✅" if r['is_active'] else "⬜"
        result += f"{status} #{r['id']}\n   {r['role_text'][:100]}...\n\n"
    await update.message.reply_text(result, parse_mode=ParseMode.HTML)


async def role_active_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("❌ /role_active [id]")
        return
    try:
        role_id = int(context.args[0])
        if activate_role(role_id):
            await update.message.reply_text(f"✅ شخصیت #{role_id} فعال شد.")
        else:
            await update.message.reply_text("❌ خطا در فعال‌سازی.")
    except ValueError:
        await update.message.reply_text("❌ ID نامعتبر.")


async def role_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("❌ /role_delete [id]")
        return
    try:
        role_id = int(context.args[0])
        if delete_ai_role(role_id):
            await update.message.reply_text(f"✅ شخصیت #{role_id} حذف شد.")
        else:
            await update.message.reply_text("❌ خطا.")
    except ValueError:
        await update.message.reply_text("❌ ID نامعتبر.")


# ==================== Cards Admin ====================
async def process_cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    pending_text = context.user_data.get("pending_card_text")
    if not pending_text:
        await update.message.reply_text(
            "❌ ابتدا متن آزاد رو ارسال کن، سپس /cards رو بزن."
        )
        return
    await update.message.reply_text("🧠 در حال تحلیل...")
    prompt = f"""متن زیر را تحلیل کن و کارت‌های ساختاریافته بساز.

=== متن ===
"{pending_text}"

=== خروجی JSON (آرایه) ===
[
  {{
    "card_type": "exam|class|rule|resource|syllabus|microtopic|news",
    "content_type": "نوع",
    "data": {{"title": "عنوان"}},
    "priority": 5
  }}
]
⚠️ فقط JSON خالص."""
    response = await call_ai(prompt, max_tokens=3000, temperature=0.2, model_type="advanced")
    if not response:
        await update.message.reply_text("❌ AI پاسخ نداد.")
        return
    cards = safe_json_loads(response, list)
    if not cards:
        await update.message.reply_text("❌ خطا در تحلیل.")
        return
    cards = [c for c in cards if isinstance(c, dict)]
    if not cards:
        await update.message.reply_text("❌ فرمت نامعتبر.")
        return
    context.user_data["pending_cards"] = cards
    context.user_data["pending_cards_raw"] = pending_text
    context.user_data.pop("pending_card_text", None)
    text = "📋 **کارت‌های تشخیص داده شده:**\n\n"
    for i, card in enumerate(cards, 1):
        ct = card.get("card_type", "")
        data = card.get("data", {})
        if not isinstance(data, dict):
            data = {}
        text += f"{i}. **{ct}**: {data.get('title', 'بدون عنوان')}\n"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ذخیره همه", callback_data="save_cards"),
         InlineKeyboardButton("❌ لغو", callback_data="cancel_cards")]
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def handle_card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "save_cards":
        cards = context.user_data.get("pending_cards", [])
        raw_text = context.user_data.get("pending_cards_raw", "")
        user_id = update.effective_user.id
        saved_ids = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            card_type = card.get("card_type")
            content_type = card.get("content_type")
            data = card.get("data", {})
            if not isinstance(data, dict):
                data = {}
            applies_to = card.get("applies_to", {})
            priority = card.get("priority", 5)
            card_id = save_card(card_type, data, applies_to, priority, raw_text,
                                created_by=user_id, content_type=content_type)
            if card_id:
                saved_ids.append(card_id)
                try:
                    await index_card_to_rag(card_id)
                except Exception as e:
                    logger.error(f"خطا در ایندکس: {e}")
        context.user_data.pop("pending_cards", None)
        context.user_data.pop("pending_cards_raw", None)
        await query.edit_message_text(f"✅ {len(saved_ids)} کارت ذخیره و ایندکس شد!")
    elif query.data == "cancel_cards":
        context.user_data.pop("pending_cards", None)
        context.user_data.pop("pending_cards_raw", None)
        await query.edit_message_text("❌ لغو شد.")


# ==================== Admin Cards List ====================
async def list_cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    context.user_data["admin_cards_mode"] = True
    context.user_data["admin_cards_page"] = 0
    await show_admin_cards_list(update, context)


async def show_admin_cards_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بخش ۱۴.۲ — فقط is_active، شمارنده بازمحاسبه، clamp صفحه."""
    if not ADMIN_IDS:
        await update.message.reply_text("❌ ادمینی تعریف نشده.")
        return
    placeholders = ",".join(["%s"] * len(ADMIN_IDS))
    results = execute_query(
        f"""SELECT id, card_type, content_type, data, priority, is_active, created_by
            FROM cards
            WHERE created_by IN ({placeholders}) AND is_active = TRUE
            ORDER BY priority DESC, created_at DESC""",
        tuple(ADMIN_IDS), fetchall=True
    )
    if not results:
        context.user_data.pop("admin_cards_mode", None)
        await update.message.reply_text("📭 کارتی موجود نیست.", reply_markup=get_admin_panel_keyboard())
        return
    cards = [{"id": r[0], "card_type": r[1], "content_type": r[2],
              "data": r[3] or {}, "priority": r[4] or 5,
              "is_active": r[5], "created_by": r[6]} for r in results]
    page = context.user_data.get("admin_cards_page", 0)
    per_page = 8
    total_pages = max(1, (len(cards) + per_page - 1) // per_page)
    # clamp
    page = max(0, min(page, total_pages - 1))
    context.user_data["admin_cards_page"] = page
    page_cards = cards[page * per_page:(page + 1) * per_page]
    text = f"📋 **کارت‌های ادمین** ({len(cards)} — صفحه {page + 1}/{total_pages})\n\n"
    keyboard = []
    for c in page_cards:
        data = c["data"]
        if not isinstance(data, dict):
            data = {}
        title = str(data.get("title", "بدون عنوان"))[:35]
        ctype = c["card_type"]
        emoji = {"routine": "⏰", "project": "🎯", "class": "🏫",
                 "exam": "📝", "syllabus": "📚", "resource": "📖",
                 "microtopic": "🔬", "rule": "📜", "reminder": "🔔"}.get(ctype, "📌")
        keyboard.append([f"🟢 {emoji} {title} [{c['id']}]"])
    nav = []
    if page > 0:
        nav.append("⬅️ صفحه قبل")
    if page < total_pages - 1:
        nav.append("➡️ صفحه بعد")
    if nav:
        keyboard.append(nav)
    keyboard.append(["🔙 بازگشت"])
    await update.message.reply_text(
        text, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )


async def del_card_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("❌ /delcard [id]")
        return
    try:
        card_id = int(context.args[0])
        user_id = get_user_id_by_telegram(update.effective_user.id)
        if update.effective_user.id not in ADMIN_IDS:
            check = execute_query("SELECT created_by FROM cards WHERE id = %s",
                                   (card_id,), fetch=True)
            if not check or check[0] != user_id:
                await update.message.reply_text("❌ این کارت مال تو نیست.")
                return
        if hard_delete_card(card_id):
            await update.message.reply_text(f"✅ کارت #{card_id} برای همیشه حذف شد.")
        else:
            await update.message.reply_text("❌ خطا.")
    except ValueError:
        await update.message.reply_text("❌ ID نامعتبر.")


# ==================== Admin Panel ====================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    nav_push(context, "admin")
    await update.message.reply_text(
        "👨‍💼 **پنل ادمین**\n\n"
        "دستورات:\n/rag — RAG\n/role — شخصیت AI\n/cards — کارت‌ها\n"
        "/listcards — لیست کارت‌ها\n/stats — آمار\n/payments — پرداخت‌ها\n"
        "/approve [id] — تایید\n/reject [id] — رد\n"
        "/advice [text] — توصیه\n/testai — تست AI",
        reply_markup=get_admin_panel_keyboard(), parse_mode=ParseMode.HTML
    )


async def advice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("📝 /advice متن توصیه")
        return
    admin_text = " ".join(context.args)
    await update.message.reply_text("🧠 در حال پردازش...")
    prompt = f"""توصیه ادمین را به داده ساختاریافته تبدیل کن:
"{admin_text}"
خروجی JSON:
[{{"topic": "ریاضی", "label": "همه", "condition": "همیشه", "advice": "...", "priority": 9, "time": "morning", "frequency": "daily", "subjects": ["ریاضی"]}}]
⚠️ فقط JSON خالص."""
    try:
        response = await call_ai(prompt, max_tokens=1000, temperature=0.2)
    except Exception:
        response = None
    if not response:
        await update.message.reply_text("❌ خطا در پردازش.")
        return
    processed = safe_json_loads(response, list) or []
    saved = 0
    for advice in processed:
        if not isinstance(advice, dict):
            continue
        advice["created_by"] = update.effective_user.id
        if save_advice(advice):
            saved += 1
    await update.message.reply_text(f"✅ {saved} توصیه ثبت شد!")


def save_advice(advice: Dict) -> Optional[int]:
    try:
        result = execute_query(
            """INSERT INTO advisory_rules
               (topic, label, condition, advice, priority, time, frequency, subjects, created_by, is_active)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE) RETURNING id""",
            (advice.get("topic"), advice.get("label"), advice.get("condition"),
             advice.get("advice"), advice.get("priority", 5),
             advice.get("time"), advice.get("frequency"),
             json.dumps(advice.get("subjects", [])), advice.get("created_by")),
            fetch=True
        )
        return result[0] if result else None
    except Exception as e:
        logger.error(f"خطا در save_advice: {e}")
        return None


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    users = execute_query("SELECT COUNT(*) FROM users", fetch=True)
    onboarded = execute_query("SELECT COUNT(*) FROM users WHERE is_onboarded = TRUE", fetch=True)
    activities = execute_query("SELECT COUNT(*) FROM activity_log", fetch=True)
    cards = execute_query("SELECT COUNT(*) FROM cards WHERE is_active = TRUE", fetch=True)
    chat_msgs = execute_query("SELECT COUNT(*) FROM chat_history", fetch=True)
    payments = execute_query("SELECT COUNT(*) FROM pending_payments WHERE status = 'pending'", fetch=True)
    rag_docs = execute_query("SELECT COUNT(*) FROM rag_documents", fetch=True)
    roles = execute_query("SELECT COUNT(*) FROM ai_roles WHERE is_active = TRUE", fetch=True)
    total_kok = execute_query("SELECT COALESCE(SUM(kok_balance), 0) FROM users", fetch=True)
    used_kok = execute_query("SELECT COALESCE(SUM(total_kok_used), 0) FROM users", fetch=True)
    purchased_kok = execute_query("SELECT COALESCE(SUM(total_kok_purchased), 0) FROM users", fetch=True)
    text = "📊 **آمار**\n\n"
    text += f"👥 کاربران: {users[0] if users else 0}\n"
    text += f"✅ ثبت‌نام: {onboarded[0] if onboarded else 0}\n"
    text += f"📋 فعالیت: {activities[0] if activities else 0}\n"
    text += f"🃏 کارت: {cards[0] if cards else 0}\n"
    text += f"💬 چت: {chat_msgs[0] if chat_msgs else 0}\n"
    text += f"💰 پرداخت: {payments[0] if payments else 0}\n"
    text += f"📚 RAG: {rag_docs[0] if rag_docs else 0}\n"
    text += f"🎭 نقش فعال: {roles[0] if roles else 0}\n\n"
    text += f"💎 **KOK**\n  • موجودی: {total_kok[0] if total_kok else 0}\n"
    text += f"  • مصرف: {used_kok[0] if used_kok else 0}\n"
    text += f"  • خرید: {purchased_kok[0] if purchased_kok else 0}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def test_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بخش ۱۵.۷ — تست با build_plan_prompt واقعی."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("🧠 تست AI...")
    try:
        # تست پایه
        start = time.time()
        response = await call_ai("سلام، فقط بگو 'AI وصل است'", max_tokens=20, temperature=0.1)
        elapsed = time.time() - start
        text = ""
        if response:
            text += f"✅ **AI وصل است**\n⏱ {elapsed:.2f}s\n📝 {response}\n"
            text += f"📌 Light: {AI_MODEL_LIGHT}\n📌 Advanced: {AI_MODEL_ADVANCED}\n\n"
        else:
            text += "❌ AI پاسخ نداد.\n\n"

        # تست build_plan_prompt
        user_id = get_user_id_by_telegram(update.effective_user.id)
        if user_id:
            user_data = get_user_data(str(update.effective_user.id))
            if user_data:
                cards = get_user_cards_direct(user_id)
                history = get_last_n_days_data(user_id, 7)
                prompt = build_plan_prompt(user_data, user_id, cards, history,
                                            "برنامه تست", "", None, get_today_date())
                raw = await call_ai(prompt, max_tokens=2000, temperature=0.2)
                parsed = safe_json_loads(raw, dict) if raw else None
                if raw:
                    text += f"📋 build_plan_prompt raw_len: {len(raw)}\n"
                    text += f"🔍 parsed: {'✅ dict' if parsed else '❌ null'}\n"
                    if parsed and parsed.get("parts"):
                        text += f"📊 parts: {len(parsed['parts'])}\n"
                    text += f"\nپیش‌نمایش raw (200):\n`{raw[:200]}`\n"
                else:
                    text += "❌ build_plan_prompt پاسخ نداد.\n"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {str(e)[:200]}")


async def approve_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("❌ /approve 123456789")
        return
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID نامعتبر.")
        return
    user = execute_query(
        "SELECT id, telegram_id, full_name FROM users WHERE telegram_id = %s",
        (str(target_user_id),), fetch=True
    )
    if not user:
        await update.message.reply_text(f"❌ کاربر {target_user_id} یافت نشد.")
        return
    db_user_id, telegram_id, full_name = user[0], user[1], user[2] or "کاربر"
    payment = execute_query(
        """SELECT id, plan_id FROM pending_payments
           WHERE user_id = %s AND status = 'pending'
           ORDER BY created_at DESC LIMIT 1""",
        (db_user_id,), fetch=True
    )
    if not payment:
        await update.message.reply_text("❌ پرداخت pending یافت نشد.")
        return
    payment_id, plan_id = payment
    if not plan_id:
        plan_id_result = execute_query(
            "SELECT id FROM subscription_plans WHERE price > 0 ORDER BY price ASC LIMIT 1",
            fetch=True
        )
        if plan_id_result:
            plan_id = plan_id_result[0]
    if not plan_id:
        await update.message.reply_text("❌ پلنی تعریف نشده.")
        return
    try:
        if activate_subscription(db_user_id, plan_id):
            execute_query("UPDATE pending_payments SET status = 'approved' WHERE id = %s", (payment_id,))
            plan_info = execute_query(
                "SELECT name, kok_amount FROM subscription_plans WHERE id = %s",
                (plan_id,), fetch=True
            )
            plan_name = plan_info[0] if plan_info else ""
            kok_amount = plan_info[1] if plan_info else 0
            await update.message.reply_text(
                f"✅ اشتراک {full_name} تایید شد.\n📦 {plan_name}\n💎 {kok_amount} KOK اضافه شد."
            )
            try:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=f"🎉 **اشتراک شما فعال شد!**\n\n📦 {plan_name}\n💎 {kok_amount} KOK به حساب شما اضافه شد.\n\nموفق باشی! 🚀",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"خطا در ارسال به کاربر: {e}")
        else:
            await update.message.reply_text("❌ خطا در فعال‌سازی.")
    except Exception as e:
        logger.error(f"خطا در تایید اشتراک: {e}")
        await update.message.reply_text("❌ خطا در تایید.")


async def reject_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("❌ /reject 123456789")
        return
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID نامعتبر.")
        return
    user = execute_query(
        "SELECT id, telegram_id, full_name FROM users WHERE telegram_id = %s",
        (str(target_user_id),), fetch=True
    )
    if not user:
        await update.message.reply_text(f"❌ کاربر {target_user_id} یافت نشد.")
        return
    db_user_id, telegram_id, full_name = user[0], user[1], user[2] or "کاربر"
    try:
        execute_query(
            "UPDATE pending_payments SET status = 'rejected' WHERE user_id = %s AND status = 'pending'",
            (db_user_id,)
        )
        await update.message.reply_text(f"❌ اشتراک {full_name} رد شد.")
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text="❌ متأسفیم، اشتراک شما تایید نشد.",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"خطا در رد اشتراک: {e}")


async def list_pending_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    results = execute_query(
        """SELECT p.id, u.telegram_id, u.full_name, p.created_at, p.photo_file_id
           FROM pending_payments p JOIN users u ON u.id = p.user_id
           WHERE p.status = 'pending' ORDER BY p.created_at DESC""",
        fetchall=True
    )
    if not results:
        await update.message.reply_text("📭 پرداخت در انتظاری نیست.")
        return
    text = "📸 **پرداخت‌های در انتظار:**\n\n"
    for r in results:
        text += f"👤 {r[2] or 'کاربر'} | 🆔 {r[1]}\n"
        text += f"/approve {r[1]} | /reject {r[1]}\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ==================== Job Tasks ====================
async def send_reminder_notifications(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        now = get_iran_now()
        now_naive = now.replace(tzinfo=None)
        pending = execute_query(
            """SELECT rn.id, rn.part_id, rn.user_id, rn.notify_type,
                      rn.scheduled_at,
                      sp.title, to_char(sp.planned_start_time, 'HH24:MI'),
                      to_char(sp.planned_end_time, 'HH24:MI'),
                      sp.completed, sp.reason, sp.metadata, sp.session_id,
                      ss.date as session_date, u.telegram_id, u.full_name
               FROM reminder_notifications rn
               JOIN study_parts sp ON sp.part_id = rn.part_id
               JOIN study_sessions ss ON ss.session_id = sp.session_id
               JOIN users u ON u.id = rn.user_id
               WHERE rn.status = 'pending'
                 AND rn.scheduled_at <= %s
                 AND rn.scheduled_at >= %s
               ORDER BY rn.scheduled_at ASC LIMIT 50""",
            (now_naive, now_naive - timedelta(hours=6)), fetchall=True
        )
        if not pending:
            return
        for row in pending:
            (notif_id, part_id, user_id, notify_type, scheduled_at,
             title, planned_start, planned_end, completed, reason, metadata,
             session_id, session_date, telegram_id, full_name) = row
            if completed and notify_type != "overdue":
                execute_query("UPDATE reminder_notifications SET status = 'cancelled' WHERE id = %s", (notif_id,))
                continue
            if notify_type == "overdue" and completed:
                execute_query("UPDATE reminder_notifications SET status = 'cancelled' WHERE id = %s", (notif_id,))
                continue
            message = build_reminder_message(notify_type, title, planned_start, planned_end,
                                              reason, metadata, session_date)
            if not message:
                execute_query("UPDATE reminder_notifications SET status = 'cancelled' WHERE id = %s", (notif_id,))
                continue
            try:
                keyboard = None
                if notify_type in ("at_time", "overdue"):
                    keyboard = get_reminder_action_keyboard()
                await context.bot.send_message(
                    chat_id=telegram_id, text=message, reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                execute_query(
                    "UPDATE reminder_notifications SET status = 'sent', sent_at = %s WHERE id = %s",
                    (now_naive, notif_id)
                )
                context.bot_data.setdefault("active_reminders", {})[str(telegram_id)] = {
                    "part_id": part_id, "session_id": session_id, "notif_id": notif_id,
                }
            except Exception as e:
                logger.error(f"خطا در ارسال یادآور {notif_id}: {e}")
                execute_query("UPDATE reminder_notifications SET status = 'failed' WHERE id = %s", (notif_id,))
            await asyncio.sleep(0.05)
    except Exception as e:
        logger.error(f"❌ خطا در send_reminder_notifications: {e}")


async def cleanup_expired_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """job ساعتی cleanup (بخش ۱۱ و ۱۲)."""
    try:
        res = await cleanup_expired_parts_job()
        logger.info(f"cleanup_job: {res}")
    except Exception as e:
        logger.error(f"cleanup_job خطا: {e}")


async def check_daily_stats(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        yesterday = (get_iran_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        users = execute_query(
            "SELECT id FROM users WHERE is_active = TRUE AND is_onboarded = TRUE",
            fetchall=True
        )
        for u in (users or []):
            update_daily_stats(u[0], yesterday)
            try:
                _purge_expired_cards(u[0])
                await cleanup_expired_parts_job(user_id=u[0])
                # اطلاع‌رسانی شناور (بخش ۱۲.۸)
                floating_count = execute_query(
                    """SELECT COUNT(*) FROM study_parts
                       WHERE user_id = %s AND COALESCE(metadata->>'converted_from_fixed', 'false') = 'true'
                         AND COALESCE(metadata->>'floating_notified', 'false') != 'true'
                         AND completed = FALSE""",
                    (u[0],), fetch=True
                )
                if floating_count and floating_count[0] and floating_count[0] > 0:
                    logger.info(f"floating_notice: user={u[0]} count={floating_count[0]}")
            except Exception as e:
                logger.error(f"خطا در پاک‌سازی کارت منقضی {u[0]}: {e}")
        logger.info(f"✅ آمار روزانه {yesterday} و cleanup انجام شد")
    except Exception as e:
        logger.error(f"خطا در check_daily_stats: {e}")


async def nightly_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = execute_query(
        "SELECT id, telegram_id, full_name FROM users WHERE is_active = TRUE AND is_onboarded = TRUE",
        fetchall=True
    )
    if not users:
        return
    today_shamsi = get_today_shamsi()
    for user in users:
        user_id, telegram_id, full_name = user[0], user[1], user[2] or "کاربر"
        try:
            activities = get_today_activities(user_id)
            if not activities:
                continue
            total_time = sum(a.get("actual_duration", a.get("planned_duration", 0)) for a in activities)
            done = len([a for a in activities if a.get("status") == "done"])
            text = f"🌙 **گزارش شبانه - {today_shamsi}**\n\n👤 {full_name}\n\n"
            text += f"⏱ زمان: {format_time_hours_minutes(total_time)}\n"
            text += f"✅ تکمیل: {done}/{len(activities)}\n\n🔜 فردا منتظرت هستم! 🌟"
            await context.bot.send_message(telegram_id, text, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"خطا در گزارش شبانه به {telegram_id}: {e}")


# ==================== handle_plan_actions ====================
async def handle_plan_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = get_user_id_by_telegram(update.effective_user.id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    plan = context.user_data.get("current_plan", {})
    parts = plan.get("parts", [])
    if text == "✅ تایید ترتیب و رفتن به ویرایش زمان":
        await confirm_edit_order(update, context)
        return
    if text == "❌ لغو ویرایش":
        await cancel_edit(update, context)
        return
    if text == "✅ اتمام ویرایش زمان":
        await finish_edit_time(update, context)
        return
    if text == "🔙 برگشت به ویرایش ترتیب":
        await back_to_order_edit(update, context)
        return
    if text == "❌ لغو کل ویرایش":
        await cancel_edit(update, context)
        return
    if text in ("➕ ۱۵ دقیقه", "➖ ۱۵ دقیقه", "➕ ۳۰ دقیقه", "➖ ۳۰ دقیقه", "➕ ۱ ساعت", "➖ ۱ ساعت"):
        delta = 0
        if "۱۵" in text:
            delta = 15 if text.startswith("➕") else -15
        elif "۳۰" in text:
            delta = 30 if text.startswith("➕") else -30
        else:
            delta = 60 if text.startswith("➕") else -60
        await adjust_part_time(update, context, delta)
        return
    if text == "✅ تایید این پارت":
        await confirm_part_time(update, context)
        return
    if text == "🔙 برگشت به لیست":
        context.user_data["edit_selected_part"] = None
        await show_edit_time_list(update, context)
        return
    if text == "✅ تایید برنامه":
        await confirm_plan(update, context)
        return
    if text == "✅ اتمام برنامه":
        await handle_finish_plan(update, context)
        return
    if text == "✏️ ویرایش برنامه":
        await start_edit_order(update, context)
        return
    if text == "🔄 بازچینی برنامه":
        # بخش ۸.۱ — استفاده از run_reschedule مرکزی با ask_calendar=True
        selected_date = context.user_data.get("selected_date") or get_today_date()
        await run_reschedule(update, context, user_id, selected_date,
                              trigger="manual", ask_calendar=True, show_final=False)
        return
    if text in ["✏️ ساخت دستی", "✏️ خودم می‌نویسم"]:
        await start_manual_build(update, context)
        return
    if text in ["⏱ تایمر", "▶️ ادامه تایمر", "⏹ توقف", "✅ تکمیل", "🗑 حذف پارت"]:
        active_part = context.user_data.get("active_part")
        if not active_part:
            await update.message.reply_text("❌ ابتدا روی یک پارت کلیک کن.")
            return
        if text in ["⏱ تایمر", "▶️ ادامه تایمر"]:
            await start_timer_command(update, context, active_part)
        elif text == "⏹ توقف":
            await stop_timer_command(update, context, active_part)
        elif text == "✅ تکمیل":
            await handle_done_part(update, context, active_part)
        elif text == "🗑 حذف پارت":
            await handle_delete_part(update, context, active_part)
        return
    if text == "🧠 ساخت با AI":
        await handle_build_with_ai(update, context)
        return


# ==================== handle_text_other — router اصلی ====================
async def handle_text_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Router اصلی با اولویت‌بندی دقیق (بخش ۳ و ۴ و ۵):
    - چک‌های pending FIRST-CLASS (بخش ۵.۲)
    - onboarding
    - حالت‌های stateful
    - بازگشت مرکزی
    - entity detection (بخش ۳.۱)
    - منوها
    - fallback AI
    """
    text = update.message.text.strip()
    user_telegram_id = update.effective_user.id

    # ============ PRIORITY 0: pending confirmations first-class (بخش ۵.۲) ============
    if context.user_data.get("pending_reminder_data"):
        if text == "✅ ذخیره یادآور":
            if context.user_data.get("processing"):
                return
            context.user_data["processing"] = True
            await save_pending_reminder(update, context)
            context.user_data.pop("processing", None)
            return
        elif text == "🔙 لغو":
            context.user_data.pop("pending_reminder_data", None)
            context.user_data.pop("reminder_mode", None)
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
            return

    if context.user_data.get("pending_class_card"):
        if text == "✅ ذخیره کلاس‌ها":
            await save_pending_class_card(update, context)
            return
        elif text == "❌ لغو":
            context.user_data.pop("pending_class_card", None)
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
            return

    if context.user_data.get("pending_project_card"):
        if text == "✅ ذخیره پروژه":
            await save_pending_project_card(update, context)
            return
        elif text == "❌ لغو":
            context.user_data.pop("pending_project_card", None)
            context.user_data.pop("project_mode", None)
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
            return

    if context.user_data.get("pending_routine_cards"):
        if text == "✅ ذخیره همه":
            await save_pending_routine_cards(update, context)
            return
        elif text == "🔙 لغو":
            context.user_data.pop("pending_routine_cards", None)
            context.user_data.pop("routin_mode", None)
            context.user_data.pop("pending_routine_clarify", None)
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
            return
        elif context.user_data.get("pending_routine_clarify"):
            await handle_routine_clarify_answer(update, context, text)
            return

    if context.user_data.get("pending_change"):
        if text == "✅ تایید تغییر":
            await apply_pending_change(update, context)
            return
        if text == "❌ رد تغییر":
            await reject_pending_change(update, context)
            return
        if text == "🗑 بله، همه را پاک کن":
            change = context.user_data.get("pending_change", {})
            if change.get("action") == "clear":
                await apply_pending_change(update, context)
                return

    if context.user_data.get("pending_card_update"):
        if text == "✅ ذخیره تغییرات":
            await save_card_update(update, context)
            return
        elif text == "🔙 بازگشت":
            context.user_data.pop("pending_card_update", None)
            card_id = context.user_data.get("card_edit_target")
            if card_id:
                await show_card_detail(update, context, card_id)
            return

    # ============ Onboarding ============
    if context.user_data.get("awaiting_custom"):
        handled = await handle_onboarding_custom(update, context, text)
        if handled:
            return
    if "onboarding_step" in context.user_data:
        await onboarding_handler(update, context)
        return

    # ============ Stateful modes ============
    if context.user_data.get("rag_mode") and user_telegram_id in ADMIN_IDS:
        await handle_rag_text(update, context, text)
        return
    if context.user_data.get("role_mode") and user_telegram_id in ADMIN_IDS:
        await handle_role_text(update, context, text)
        return
    if context.user_data.get("card_edit_mode") and context.user_data.get("card_edit_target"):
        if text == "🔙 بازگشت":
            context.user_data.pop("card_edit_mode", None)
            await show_card_detail(update, context, context.user_data.get("card_edit_target"))
            return
        await handle_card_edit_text(update, context, text)
        return
    if context.user_data.get("pending_card_delete"):
        if text == "✅ بله، حذف کن":
            await do_delete_card(update, context)
            return
        elif text == "🔙 لغو":
            context.user_data.pop("pending_card_delete", None)
            card_id = context.user_data.get("card_edit_target")
            if card_id:
                await show_card_detail(update, context, card_id)
            return
    if context.user_data.get("pending_card_suggestion") and text in ["✅ بله، کارت بساز", "❌ نه، فقط امروز"]:
        await handle_pending_card_suggestion(update, context, text)
        return
    if context.user_data.get("pending_routine_clarify"):
        await handle_routine_clarify_answer(update, context, text)
        return
    if context.user_data.get("routin_mode") == "free_text":
        if text == "🔙 بازگشت":
            context.user_data.pop("routin_mode", None)
            await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
            return
        await handle_routin_free_text(update, context, text)
        return
    if context.user_data.get("reminder_mode") == "free_text":
        if text == "🔙 بازگشت":
            context.user_data.pop("reminder_mode", None)
            await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
            return
        await handle_reminder_free_text(update, context, text)
        return
    if context.user_data.get("pending_reminder_plan") and text in ["📚 بله، برنامه بچین", "❌ نه، فقط یادآور"]:
        await handle_reminder_plan_choice(update, context, text)
        return

    # پروژه — بدون readiness
    if context.user_data.get("pending_project_source"):
        if text == "✅ تایید":
            await confirm_project_activation(update, context)
            return
        if text == "🔄 بازسازی هفته":
            await rebuild_week_for_project(update, context)
            return
        if text == "🗑 حذف پروژه":
            await delete_active_project(update, context)
            return
        if text == "🔙 بازگشت":
            await handle_back_button(update, context)
            return

    if context.user_data.get("project_mode") == "free_text":
        if text == "🔙 بازگشت":
            context.user_data.pop("project_mode", None)
            await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
            return
        await handle_project_free_text(update, context, text)
        return
    if context.user_data.get("challenge_mode") == "waiting_content":
        if text == "🔙 بازگشت":
            context.user_data.pop("challenge_mode", None)
            await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
            return
        context.user_data["project_mode"] = "free_text"
        context.user_data.pop("challenge_mode", None)
        await handle_project_free_text(update, context, text)
        return

    # schedule choice از clarify
    if context.user_data.get("schedule_choice_pending") and text in (
        "▶️ همین الان شروع کنم", "🕐 ساعت مشخص", "🧠 AI در جای مناسب بگذارد",
        "🔒 ثابت", "🕊 شناور"
    ):
        pending = context.user_data.get("schedule_choice_pending")
        # اینجا فقط انتخاب را ثبت می‌کنیم
        if text == "▶️ همین الان شروع کنم":
            pending["choice"] = "now"
        elif text == "🧠 AI در جای مناسب بگذارد" or text == "🕊 شناور":
            pending["choice"] = "let_ai"
        elif text == "🕐 ساعت مشخص":
            await update.message.reply_text(
                "🕐 ساعت را بنویس (مثال: ۱۶:۳۰ یا ۱۶:۳۰ تا ۱۷:۳۰):",
                reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True)
            )
            context.user_data["awaiting_schedule_time"] = {"from_choice": True}
            return
        elif text == "🔒 ثابت":
            pending["choice"] = "explicit"
        context.user_data["schedule_choice_pending"] = pending
        # تلاش مجدد
        schedule_info = pending
        # دوباره dispatch با schedule_choice
        pending_change = context.user_data.get("pending_change")
        if pending_change:
            # ساخت مجدد
            target = pending_change.get("target", {})
            if text == "▶️ همین الان شروع کنم":
                now = get_iran_now()
                start = f"{now.hour:02d}:{now.minute:02d}"
                end_min = min(now.hour * 60 + now.minute + target.get("duration", 45), DAY_END_MIN)
                target["explicit_start"] = start
                target["explicit_end"] = minutes_to_time(end_min)
                target["is_fixed_time"] = True
            pending_change["target"] = target
            context.user_data["pending_change"] = pending_change
            # پیش‌نمایش
            title = target.get("title", "فعالیت")
            duration = target.get("duration", 45)
            text_msg = f"📋 **اضافه کردن پارت**\n\n📚 {title}\n⏱ {duration} دقیقه\n"
            if target.get("explicit_start") and target.get("explicit_end"):
                text_msg += f"🔒 {target['explicit_start']} - {target['explicit_end']}\n"
            elif pending.get("choice") == "let_ai":
                text_msg += "🕊 شناور — AI در جای مناسب می‌گذارد\n"
            text_msg += "\nتایید می‌کنی؟"
            await update.message.reply_text(text_msg, reply_markup=get_confirm_change_keyboard(),
                                              parse_mode=ParseMode.HTML)
            return

    # awaiting_schedule_time
    if context.user_data.get("awaiting_schedule_time"):
        time_slots = analyze_time_slots(text)
        if not time_slots:
            await update.message.reply_text("❌ ساعت نامعتبر. مثال: 16:30 یا 16:30 تا 17:30")
            return
        slot = time_slots[0]
        start, end = slot.split("-")
        pending_change = context.user_data.get("pending_change")
        if pending_change:
            target = pending_change.get("target", {})
            target["explicit_start"] = start
            target["explicit_end"] = end
            target["is_fixed_time"] = True
            try:
                target["duration"] = max(15, time_to_minutes(end) - time_to_minutes(start))
            except Exception:
                pass
            pending_change["target"] = target
            context.user_data["pending_change"] = pending_change
            title = target.get("title", "فعالیت")
            duration = target.get("duration", 45)
            text_msg = f"📋 **اضافه کردن پارت**\n\n📚 {title}\n⏱ {duration} دقیقه\n"
            text_msg += f"🔒 {start} - {end}\n\nتایید می‌کنی؟"
            await update.message.reply_text(text_msg, reply_markup=get_confirm_change_keyboard(),
                                              parse_mode=ParseMode.HTML)
        context.user_data.pop("awaiting_schedule_time", None)
        context.user_data.pop("schedule_choice_pending", None)
        return

    if context.user_data.get("pending_split"):
        await handle_split_choice(update, context, text)
        return
    if context.user_data.get("pending_clarify"):
        pending = context.user_data["pending_clarify"]
        if time.time() - pending.get("created_at", 0) > 300:
            context.user_data.pop("pending_clarify", None)
            await update.message.reply_text("⏰ سوال قبلی منقضی شد.", reply_markup=get_main_keyboard())
            return
        await handle_clarify_answer(update, context, text)
        return
    if context.user_data.get("pending_early_complete"):
        if text == "✅ بله، انجام دادم":
            await confirm_early_complete(update, context)
            return
        elif text == "❌ نه، لغو":
            context.user_data.pop("pending_early_complete", None)
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
            return
    if context.user_data.get("pending_delete"):
        if text in ["✅ بله، حذف کن", "❌ نه، لغو"]:
            await handle_confirm_delete(update, context)
            return

    # pending_calendar_update (بخش ۸.۲)
    if context.user_data.get("pending_calendar_update"):
        pending_cal = context.user_data["pending_calendar_update"]
        if text == "✅ بله، روزهای بعد را هم به‌روزرسانی کن":
            user_id = pending_cal["user_id"]
            target_date = pending_cal["target_date"]
            plan = get_plan_by_date(user_id, target_date)
            if plan:
                overflow = [p for p in plan["parts"] if (p.get("metadata") or {}).get("is_overflow")]
                if overflow:
                    res = add_parts_to_next_day(user_id, overflow, target_date)
                    shamsi_list = [get_shamsi_date(d) for d in res["dates"]]
                    if shamsi_list:
                        await update.message.reply_text(
                            f"✅ برنامه روزهای {', '.join(shamsi_list)} به‌روزرسانی شد.",
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await update.message.reply_text("⚠️ پارتی جا نشد.", reply_markup=get_main_keyboard())
            context.user_data.pop("pending_calendar_update", None)
            return
        elif text == "❌ نه، فقط امروز":
            context.user_data.pop("pending_calendar_update", None)
            await update.message.reply_text("✅ فقط امروز به‌روز شد.", reply_markup=get_main_keyboard())
            return

    # reminder active actions
    if text in ["✅ انجامش دادم", "⏰ ۳۰ دقیقه دیگه"]:
        if context.bot_data.get("active_reminders", {}).get(str(user_telegram_id)):
            await handle_reminder_action(update, context, text)
            return

    # feedback
    if context.user_data.get("feedback_questions"):
        await handle_feedback_answer(update, context, text)
        return
    if context.user_data.get("awaiting_activity_type"):
        await handle_activity_type(update, context, text)
        return

    # build manual
    if context.user_data.get("build_mode") == "smart_manual":
        step = context.user_data.get("build_step")
        if text == "🔙 بازگشت":
            for k in ["build_mode", "build_step", "build_data"]:
                context.user_data.pop(k, None)
            await update.message.reply_text("🔙 بازگشت", reply_markup=get_main_keyboard())
            return
        if step == "time_input":
            await handle_smart_manual_time(update, context, text)
            return
        elif step == "topic_input":
            await handle_smart_manual_topics(update, context, text)
            return
        elif step == "duration_input":
            await handle_smart_manual_duration(update, context, text)
            return
        elif step == "custom_duration":
            await handle_smart_manual_custom_duration(update, context, text)
            return

    # ============ CENTRAL BACK ============
    if text == "🔙 بازگشت":
        await handle_back_button(update, context)
        return

    # ============ 2.5: بازگشت‌های اختصاصی ============
    if text == "🔙 بازگشت به تقویم":
        user_id = get_user_id_by_telegram(user_telegram_id)
        if user_id:
            page = context.user_data.get("calendar_page", 0)
            await show_calendar_page(update, context, user_id, page=page)
        return
    if text == "🔙 بازگشت به لیست":
        if context.user_data.get("admin_cards_mode"):
            await show_admin_cards_list(update, context)
        else:
            context.user_data["cards_page"] = 0
            await handle_cards_list(update, context)
        return
    if text == "🔙 بازگشت به منو":
        context.user_data.pop("mode", None)
        await update.message.reply_text("🔙", reply_markup=get_main_keyboard())
        return

    # ============ ENTITY DETECTION (بخش ۳.۱) ============
    entity_type, entity_id = detect_entity(text, context, get_user_id_by_telegram(user_telegram_id))
    if entity_type == "part":
        await handle_part_click(update, context)
        return
    if entity_type == "card":
        await show_card_click(update, context, entity_id)
        return
    # اگر id داری ولی هیچ‌کدام → پیام مناسب (بخش ۳.۵)
    if entity_id and not entity_type and "[" in text and "]" in text:
        # شاید از previous state مانده
        if context.user_data.get("current_plan", {}).get("parts"):
            # احتمالاً پارت است
            await handle_part_click(update, context)
            return

    # ============ Calendar ============
    if text == "📅 تقویم":
        await handle_calendar(update, context)
        return
    if text in ["⬅️ صفحه قبل", "➡️ صفحه بعد"]:
        if context.user_data.get("admin_cards_mode"):
            if text == "⬅️ صفحه قبل":
                context.user_data["admin_cards_page"] = max(0, context.user_data.get("admin_cards_page", 0) - 1)
            else:
                context.user_data["admin_cards_page"] = context.user_data.get("admin_cards_page", 0) + 1
            await show_admin_cards_list(update, context)
            return
        elif context.user_data.get("cards_page") is not None:
            if text == "⬅️ صفحه قبل":
                context.user_data["cards_page"] = max(0, context.user_data.get("cards_page", 0) - 1)
            else:
                context.user_data["cards_page"] = context.user_data.get("cards_page", 0) + 1
            await handle_cards_list(update, context)
            return
        else:
            await handle_calendar_date(update, context)
            return
    if text.startswith("📅 ") and "|" in text:
        await handle_calendar_date(update, context)
        return
    if text == "🧠 ساخت برنامه برای این روز":
        selected_date = context.user_data.get("selected_date")
        if selected_date:
            user_data = get_user_data(str(user_telegram_id))
            user_id = get_user_id_by_telegram(user_telegram_id)
            if user_id and user_data:
                if not await check_and_consume_kok(update, user_id, "ai_plan_build"):
                    return
                wait = await update.message.reply_text("🧠 در حال ساخت برنامه...")
                ai_response = await generate_plan_for_date(user_id, user_data, selected_date)
                try:
                    await wait.delete()
                except Exception:
                    pass
                if ai_response and ai_response.get("parts"):
                    session_id = create_plan_for_date(user_id, user_data, ai_response, selected_date)
                    if session_id:
                        plan = get_plan_by_date(user_id, selected_date)
                        if plan and plan.get("parts"):
                            context.user_data["current_plan"] = plan
                            await show_parts_initial(update, context, plan["parts"])
                            return
                add_kok(user_id, get_kok_cost("ai_plan_build"), "refund")
                await update.message.reply_text("❌ خطا. KOK برگشت داده شد.")
        return

    # ============ Report ============
    if text == "📖 جزئیات دروس":
        await handle_report_subjects(update, context)
        return
    if text == "📈 نمودار هفتگی":
        await handle_report_chart(update, context)
        return
    if text == "🧠 تحلیل AI":
        await handle_report_ai(update, context)
        return
    if text == "📅 گزارش ماهانه":
        await handle_report_monthly(update, context)
        return

    # ============ Subscription ============
    if text == "📊 تاریخچه KOK":
        await kok_command(update, context)
        return
    if text.startswith("💳 "):
        kok_match = re.search(r'(\d+)\s*KOK', text)
        if kok_match:
            kok_amount = int(kok_match.group(1))
            plan_choice = next((p for p in get_available_plans() if p["kok_amount"] == kok_amount), None)
            if plan_choice:
                context.user_data["pending_plan"] = plan_choice["id"]
                context.user_data["awaiting_receipt"] = True
                price_fmt = f"{plan_choice['price']:,}".replace(",", "،")
                await update.message.reply_text(
                    f"🛒 **پلن {plan_choice['name']}**\n\n"
                    f"💎 {plan_choice['kok_amount']} KOK\n💰 {price_fmt} تومان\n\n"
                    f"💳 شماره کارت:\n`6219861837569689`\n👤 مصطفی فرخندئی\n\n"
                    f"📸 حالا عکس رسید رو بفرست.",
                    parse_mode=ParseMode.HTML
                )
                return
        await update.message.reply_text(
            "❌ پلن تشخیص داده نشد. دوباره از منو انتخاب کن.",
            reply_markup=get_subscription_keyboard()
        )
        return

    # ============ Projects ============
    if text == "✏️ پروژه دلخواه":
        context.user_data["project_mode"] = "free_text"
        await update.message.reply_text(
            "✏️ متن پروژه رو بنویس:",
            reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True)
        )
        return
    if text.startswith("🎯 "):
        await handle_project_selection(update, context, text)
        return
    if text == "🔄 بازسازی هفته" and context.user_data.get("pending_project_source"):
        await rebuild_week_for_project(update, context)
        return
    if text == "🗑 حذف پروژه" and context.user_data.get("pending_project_source"):
        await delete_active_project(update, context)
        return

    # ============ Main Menu ============
    if text == "📝 برنامه امروز":
        await handle_today_plan(update, context)
        return
    if text == "📊 گزارش":
        await handle_report(update, context)
        return
    if text == "💰 KOK و اشتراک":
        await handle_subscription(update, context)
        return
    if text == "📋 کارت‌های من":
        context.user_data["cards_page"] = 0
        await handle_cards_list(update, context)
        return

    # ============ Card detail actions ============
    if text == "✏️ ویرایش":
        await start_card_edit(update, context)
        return
    if text == "🗑 حذف کارت":
        await delete_card_confirm(update, context)
        return
    if text == "🔄 بروزرسانی وضعیت":
        await handle_cards_list(update, context)
        return

    # ============ Plan buttons ============
    plan_buttons = [
        "✅ تایید برنامه", "✅ اتمام برنامه", "✏️ ویرایش برنامه", "🔄 بازچینی برنامه",
        "⏱ تایمر", "▶️ ادامه تایمر", "⏹ توقف",
        "✅ تکمیل", "🗑 حذف پارت", "🧠 ساخت با AI",
        "✏️ ساخت دستی", "✏️ خودم می‌نویسم",
        "✅ تایید ترتیب و رفتن به ویرایش زمان", "❌ لغو ویرایش",
        "✅ اتمام ویرایش زمان", "🔙 برگشت به ویرایش ترتیب", "❌ لغو کل ویرایش",
        "➕ ۱۵ دقیقه", "➖ ۱۵ دقیقه",
        "➕ ۳۰ دقیقه", "➖ ۳۰ دقیقه",
        "➕ ۱ ساعت", "➖ ۱ ساعت",
        "✅ تایید این پارت", "🔙 برگشت به لیست",
    ]
    if text in plan_buttons:
        await handle_plan_actions(update, context)
        return

    # ============ Admin Panel ============
    if user_telegram_id in ADMIN_IDS:
        if text == "📚 RAG":
            await rag_command(update, context)
            return
        if text == "🎭 Role":
            await role_command(update, context)
            return
        if text == "🃏 Cards":
            context.user_data["awaiting_card_text"] = True
            await update.message.reply_text("🃏 متن آزاد رو ارسال کن، سپس /cards رو بزن.")
            return
        if text == "📊 آمار":
            await stats_command(update, context)
            return  
        if text == "💳 پرداخت‌ها":
            await list_pending_payments(update, context)
            return
        if context.user_data.get("awaiting_card_text"):
            context.user_data["pending_card_text"] = text
            context.user_data.pop("awaiting_card_text", None)
            await update.message.reply_text("✅ متن دریافت شد. حالا /cards رو بزن.")
            return

    # ============ Fallback: AI Chat ============
    user_id = get_user_id_by_telegram(user_telegram_id)
    if not user_id:
        await update.message.reply_text("❌ لطفاً اول /start رو بزن.")
        return
    if not await check_and_consume_kok(update, user_id, "ai_chat", silent_admin=False):
        return
    context.user_data["mode"] = "ai_chat"
    user_data = get_user_data(str(user_telegram_id))
    context_summary = ""
    if user_data:
        weak = ", ".join(user_data.get("weak_subjects", [])) or "ندارد"
        context_summary = f"هدف: {user_data.get('goal', 'نامشخص')} | ضعیف: {weak}"
    context.user_data["ai_context_summary"] = context_summary
    await handle_ai_chat_message(update, context)


async def show_card_click(update: Update, context: ContextTypes.DEFAULT_TYPE, card_id: int) -> None:
    await show_card_detail(update, context, card_id)


# ==================== main ====================
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

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(f"❌ خطای مدیریت‌نشده: {context.error}", exc_info=context.error)
        if isinstance(update, Update):
            try:
                if update.effective_message:
                    await update.effective_message.reply_text(
                        "⚠️ یه خطای غیرمنتظره رخ داد. دوباره تلاش کن.",
                        reply_markup=get_main_keyboard()
                    )
            except Exception as e:
                logger.error(f"خطا در ارسال پیام خطا: {e}")

    application.add_error_handler(error_handler)

    # Admin commands
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("rag", rag_command))
    application.add_handler(CommandHandler("rag_list", rag_list_command))
    application.add_handler(CommandHandler("rag_delete", rag_delete_command))
    application.add_handler(CommandHandler("rag_end", rag_end_command))
    application.add_handler(CommandHandler("role", role_command))
    application.add_handler(CommandHandler("role_list", role_list_command))
    application.add_handler(CommandHandler("role_active", role_active_command))
    application.add_handler(CommandHandler("role_delete", role_delete_command))
    application.add_handler(CommandHandler("cards", process_cards_command))
    application.add_handler(CommandHandler("card", process_cards_command))
    application.add_handler(CommandHandler("listcards", list_cards_command))
    application.add_handler(CommandHandler("delcard", del_card_command))
    application.add_handler(CommandHandler("advice", advice_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("testai", test_ai_command))
    application.add_handler(CommandHandler("approve", approve_subscription))
    application.add_handler(CommandHandler("reject", reject_subscription))
    application.add_handler(CommandHandler("payments", list_pending_payments))

    # User commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("kok", kok_command))
    application.add_handler(CommandHandler("project", project_command))
    application.add_handler(CommandHandler("reminder", reminder_command))
    application.add_handler(CommandHandler("routin", routin_command))
    application.add_handler(CommandHandler("challenge", handle_challenge_start))

    # Callback
    application.add_handler(CallbackQueryHandler(handle_card_callback, pattern="^(save_cards|cancel_cards)$"))

    # پارت و فایل
    application.add_handler(MessageHandler(filters.Document.ALL, handle_rag_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_payment_photo))

    # هندلر اصلی
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_other))

    # Job queue
    job_queue = application.job_queue
    if job_queue:
        now = get_iran_now()
        target = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        seconds_until = (target - now).total_seconds()
        job_queue.run_repeating(nightly_report, interval=86400, first=seconds_until)

        stats_target = now.replace(hour=0, minute=30, second=0, microsecond=0)
        if now >= stats_target:
            stats_target += timedelta(days=1)
        stats_seconds = (stats_target - now).total_seconds()
        job_queue.run_repeating(check_daily_stats, interval=86400, first=stats_seconds)

        job_queue.run_repeating(
            send_reminder_notifications,
            interval=60, first=10, name="reminder_sender"
        )
        job_queue.run_repeating(
            cleanup_expired_job,
            interval=3600, first=60, name="cleanup_expired"
        )

        logger.info("✅ تسک‌های زمان‌بندی‌شده تنظیم شدند")

    logger.info("🤖 ربات مطالعه هوشمند شروع به کار کرد!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
