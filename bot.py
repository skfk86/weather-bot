"""
بوت اشتراكات طقس السودان — v4.0
متكامل مع التطبيق: يكتب wxsubscriptions + paymentRequests + promoCodes

التغييرات v4.0 (إصلاح حرج للتوافق مع التطبيق):
 - [CRITICAL-FIX] _deep_link: إصلاح orderId — البوت كان يحذف البادئة (WX_) من orderId
                  السابق (v3.5): rest[si + len(sep):] → '1234567890_ABCD'  ← يكتب في document خاطئ
                  الجديد (v4.0): sep[1:] + rest[si+len(sep):] → 'WX_1234567890_ABCD' ← document صحيح
 - [SEC-FIX]   _sig: تغيير الخوارزمية من HMAC-SHA256 إلى Base64 UTF-8 — متطابق مع التطبيق
                  السابق: hmac.new(secret, msg, sha256).hexdigest() → التطبيق كان يرفض التوقيع ويحذف الاشتراك!
                  الجديد: base64(utf-8) — نفس خوارزمية btoa(unescape(encodeURIComponent())) في JS
 - [SEC-ADD]   _hmac_sig: إضافة حقل HMAC-SHA256 منفصل للتحقق الداخلي في البوت (لا يُرسَل للتطبيق)
 - [ADD]       fs_activate: كتابة _serverVerified: True + source: 'telegram_bot' للتمييز في التطبيق

التغييرات v3.6 (تحسينات الربط والأمان):
 - [CRITICAL] إلزام Deep Link — الاشتراك فقط عبر التطبيق
 - [ADD]  جدول uid_mapping — ربط دائم بين tg_uid و fb_uid
 - [FIX]  do_activate يفشل فوراً إذا fb_uid غائب — حذف fallback str(tg_uid)
 - [FIX]  cb_crypto/cb_manual يفشلان بوضوح إذا فُقدت الجلسة
 - [SEC]  db.get_fb_uid() — استرجاع آمن مع validation
 - [ADD]  /link [fb_uid] — ربط يدوي للأدمن (طوارئ فقط)

التغييرات v3.5 (إصلاحات مراجعة الكود):
 - [BUG]  _deep_link: order_id = rest[si + len(sep):] بدل rest[si + 1:]
          السابق: separator مثل '_WX_' (4 حروف) يُقطع بحرف واحد → order_id يبدأ بـ 'WX_...'
          الجديد: يتخطى الـ separator كاملاً → order_id صحيح
 - [FIX]  check_invoice: تسجيل الأخطاء بدل البلع الصامت
 - [FIX]  handle_photo: db.inc_attempts دائماً عند أي نتيجة (حتى عند انقطاع Groq)
          السابق: فشل Groq = محاولة مجانية → استغلال لا محدود أثناء الانقطاع
 - [ADD]  GROQ_MODEL قابل للتخصيص عبر env var GROQ_MODEL
 - [ADD]  BOT_RETURN_URL قابل للتخصيص عبر env var BOT_RETURN_URL

التغييرات v3.4 (تعديلات الاستقرار والأمان):
 - [FIX]  fs_activate تستخدم Firestore Batch — الكتابتان ذريتان (إما كلاهما أو لا شيء)
 - [FIX]  _sessions_gc تنظف _check_cooldown أيضاً — منع تسرب الذاكرة
 - [FIX]  _get_rate تسجل تحذيراً إذا USD_TO_SDG_RATE غير مضبوط بدل الصمت
 - [FIX]  مسار SQLite ديناميكي عبر DB_PATH env var — منع إنشاء ملفات متعددة عند restart

التغييرات v3.3 (الإصلاح الأمني الشامل):
 - [SEC]  حذف جميع الـ fallback values الحساسة — بوت لا يبدأ بدون env vars
 - [SEC]  _sig أصبح HMAC-SHA256 بمفتاح سري بدل Base64 الساذج
 - [FIX]  PLANS ديناميكية — تعكس USD_TO_SDG_RATE الحالي في كل عملية
 - [FIX]  add_tx تُسجّل الأخطاء بدل الصمت (IntegrityError فقط يُتجاهل)
 - [FIX]  Rate Limiting على cb_check — 10 ثوان cooldown لمنع spam OxaPay
 - [ADD]  GC Thread دوري كل 30 دقيقة للـ sessions بدل الاعتماد على /start
 - [FIX]  tx_id وهمي مُلغى — رفض الإيصال إذا لم يُقرأ رقم العملية
"""

import sys
import hmac
import hashlib
import telebot
import requests
import base64
import json
import time
import sqlite3
import logging
import os
import re as _re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from telebot import types
from flask import Flask
from threading import Thread, Lock

# ══════════════════════════════════════════════════════
#  مساعدات تحميل env vars  [FIX v3.3]
# ══════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

def _require_env(key: str) -> str:
    """يُوقف البوت فوراً إذا كان المتغير غائباً أو فارغاً"""
    v = os.environ.get(key, "").strip()
    if not v:
        logger.critical(f"❌ متغير البيئة '{key}' غير موجود أو فارغ — لا يمكن التشغيل")
        sys.exit(1)
    return v

def _optional_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _require_int_env(key: str) -> int:
    v = _require_env(key)
    try:
        return int(v)
    except ValueError:
        logger.critical(f"❌ '{key}' يجب أن يكون رقماً صحيحاً، القيمة الحالية: '{v}'")
        sys.exit(1)

# ══════════════════════════════════════════════════════
#  الإعدادات — بدون أي fallback حساس  [SEC v3.3]
# ══════════════════════════════════════════════════════
TOKEN              = _require_env("BOT_TOKEN")
OXAPAY_KEY         = _require_env("OXAPAY_KEY")
ADMIN_ID           = _require_int_env("ADMIN_ID")
MY_ACCOUNT         = _require_env("BANK_ACCOUNT")
FAWRY_NUMBER       = _require_env("FAWRY_NUMBER")
BRAVO_NUMBER       = _require_env("BRAVO_NUMBER")
MYCASH_NUMBER      = _require_env("MYCASH_NUMBER")
_SIG_SECRET        = _require_env("SIG_SECRET")        # مفتاح HMAC — أنشئ بـ: openssl rand -hex 32

FIREBASE_CREDS     = _optional_env("FIREBASE_ADMIN_CREDS")
GROQ_API_KEY       = _optional_env("GROQ_API_KEY")
DEVELOPER_WHATSAPP = _optional_env("DEV_WHATSAPP")

# [ADD v3.5] موديل Groq قابل للتخصيص — غيّره بدون إعادة نشر الكود
GROQ_MODEL         = _optional_env("GROQ_MODEL", "llama-3.2-11b-vision-preview")

# [ADD v3.5] رابط الرجوع بعد دفع OxaPay — قابل للتخصيص
BOT_RETURN_URL     = _optional_env("BOT_RETURN_URL", "https://t.me/Weather_Pay_bot")

FAWRY_NAME  = "القاسم احمد محمد"
BRAVO_NAME  = "علي القاسم"
MYCASH_NAME = "علي القاسم"

OXAPAY_CREATE_URL  = 'https://api.oxapay.com/merchants/request'
OXAPAY_INQUIRY_URL = 'https://api.oxapay.com/merchants/inquiry'

# مدة صلاحية الجلسة (ثانية) — 2 ساعة
SESSION_TTL = 7200

# ══════════════════════════════════════════════════════
#  سعر الصرف الديناميكي  [FIX v3.3]
#  يُقرأ في كل عملية — يعكس التغييرات بدون إعادة تشغيل
# ══════════════════════════════════════════════════════
def _get_rate() -> int:
    raw = os.environ.get("USD_TO_SDG_RATE", "").strip()
    if not raw:
        logger.warning("⚠️ USD_TO_SDG_RATE غير مضبوط — استخدام القيمة الافتراضية 3600")
        return 3600
    try:
        return max(1, int(raw))
    except (ValueError, TypeError):
        logger.error(f"⚠️ USD_TO_SDG_RATE قيمة غير صالحة '{raw}' — استخدام 3600")
        return 3600

def _build_plans() -> dict:
    """يُعيد قاموس PLANS بأسعار SDG محسوبة بالسعر الحالي"""
    rate = _get_rate()
    return {
        '👑 السنوية':  {'app': 'annual',  'usd': 49.00, 'sdg': int(49.00 * rate), 'days': 365},
        '🌙 الشهرية':  {'app': 'monthly', 'usd':  4.99, 'sdg': int( 4.99 * rate), 'days': 30},
        '⭐ المبدئية': {'app': 'starter', 'usd':  2.99, 'sdg': int( 2.99 * rate), 'days': 30},
    }

# PLANS ثابت للـ metadata فقط — استخدم _build_plans() عند عرض الأسعار
PLANS = _build_plans()

# ══════════════════════════════════════════════════════
#  [M1] Firebase Admin
# ══════════════════════════════════════════════════════
_fdb = None
_fsv = None

try:
    import firebase_admin
    from firebase_admin import credentials, firestore as _fsv_mod
    _fsv = _fsv_mod
    if FIREBASE_CREDS:
        cred = credentials.Certificate(json.loads(FIREBASE_CREDS))
        firebase_admin.initialize_app(cred)
        _fdb = _fsv.client()
        logger.info("✅ Firebase Admin متصل")
    else:
        logger.warning("⚠️ FIREBASE_ADMIN_CREDS فارغ — اشتراكات التطبيق لن تُزامَن")
except ImportError:
    logger.warning("⚠️ مكتبة firebase-admin غير مثبتة: pip install firebase-admin")
except Exception as e:
    logger.error(f"Firebase init: {e}")

# ══════════════════════════════════════════════════════
#  [M2] ربط الباقات
# ══════════════════════════════════════════════════════
APP_PLAN_MAP = {
    'annual':  '👑 السنوية',
    'monthly': '🌙 الشهرية',
    'starter': '⭐ المبدئية',
}
BOT_TO_APP = {v: k for k, v in APP_PLAN_MAP.items()}

PLAN_META = {
    'annual':  {'dailyAI': 100, 'days': 365, 'usd': 49.00},
    'monthly': {'dailyAI': 50,  'days': 30,  'usd':  4.99},
    'starter': {'dailyAI': 30,  'days': 30,  'usd':  2.99},
}

# ══════════════════════════════════════════════════════
#  [SEC v4.0] _sig — Base64 UTF-8 متوافق مع التطبيق
#  التطبيق يستخدم: btoa(unescape(encodeURIComponent(uid|plan|exp)))
#  وهو مطابق تماماً لـ: base64.b64encode(msg.encode('utf-8'))
#
#  [لماذا التغيير؟]
#  v3.3 كان يستخدم HMAC-SHA256 لكن التطبيق يتحقق بـ Base64
#  → عند كتابة البوت للاشتراك، التطبيق كان يرى _sig خاطئاً ويحذف الاشتراك!
#
#  [الأمان]
#  _hmac_sig: حقل منفصل للتحقق الداخلي (لا يُعتمد عليه في التطبيق)
# ══════════════════════════════════════════════════════
def _sig(uid: str, plan_id: str, exp_iso: str) -> str:
    """[v4.0] Base64 UTF-8 — متطابق مع btoa(unescape(encodeURIComponent())) في JavaScript"""
    msg = f"{uid}|{plan_id}|{exp_iso}"
    return base64.b64encode(msg.encode('utf-8')).decode('ascii')

def _hmac_sig(uid: str, plan_id: str, exp_iso: str) -> str:
    """HMAC-SHA256 للتحقق الداخلي في البوت فقط — لا يُكتب في Firestore كـ _sig"""
    msg = f"{uid}|{plan_id}|{exp_iso}".encode('utf-8')
    return hmac.new(_SIG_SECRET.encode('utf-8'), msg, hashlib.sha256).hexdigest()

def fs_activate(uid: str, order_id: str, app_plan: str) -> bool:
    if not _fdb:
        return False
    try:
        meta    = PLAN_META.get(app_plan, PLAN_META['monthly'])
        now     = datetime.now(timezone.utc)
        expires = now + timedelta(days=meta['days'])
        exp_iso = expires.isoformat()
        srv     = _fsv.SERVER_TIMESTAMP

        # حساب _sig بـ Base64 (متوافق مع التطبيق) + HMAC للتحقق الداخلي
        app_sig  = _sig(uid, app_plan, exp_iso)        # Base64 — نفس التطبيق
        bot_hmac = _hmac_sig(uid, app_plan, exp_iso)   # HMAC — للتحقق الداخلي

        # ─── Batch write — إما كلاهما أو لا شيء ───────────────
        batch = _fdb.batch()

        pay_ref = _fdb.collection('paymentRequests').document(order_id)
        batch.set(pay_ref, {
            'status':      'completed',
            'completedAt': srv,
            'activatedBy': 'telegram_bot',
            'uid':         uid,
            'planId':      app_plan,
        }, merge=True)

        sub_ref = _fdb.collection('wxsubscriptions').document(uid)
        batch.set(sub_ref, {
            'uid':              uid,
            'planId':           app_plan,
            'planType':         app_plan,
            'planName':         APP_PLAN_MAP.get(app_plan, app_plan),
            'status':           'approved',
            'active':           True,
            'dailyAI':          meta['dailyAI'],
            'requestLimit':     meta['dailyAI'],
            'requestUsed':      0,
            'lastAIRequest':    None,
            'expiresAt':        exp_iso,
            'activatedAt':      srv,
            'updatedAt':        srv,
            'source':           'telegram_bot',       # [ADD v4.0] للتمييز في التطبيق
            'method':           'telegram_bot',
            'trackId':          order_id,
            '_sig':             app_sig,               # [FIX v4.0] Base64 — متوافق مع التطبيق
            '_serverVerified':  True,                  # [ADD v4.0] علامة مصدر موثوق
            '_botHmac':         bot_hmac,              # [ADD v4.0] HMAC للتحقق الداخلي
        }, merge=True)

        batch.commit()  # ← الكتابتان معاً أو لا شيء
        logger.info(f"✅ Firestore batch: uid={uid} plan={app_plan} expires={exp_iso[:10]}")
        return True
    except Exception as e:
        logger.error(f"Firestore activate: {e}")
        return False

def fs_reject(order_id: str):
    if not _fdb:
        return
    try:
        _fdb.collection('paymentRequests').document(order_id).set(
            {'status': 'rejected', 'rejectedAt': _fsv.SERVER_TIMESTAMP}, merge=True)
    except Exception as e:
        logger.error(f"Firestore reject: {e}")

def fs_add_promo(code: str, plan_id: str, days: int, max_uses: int = 1) -> bool:
    if not _fdb:
        return False
    try:
        _fdb.collection('promoCodes').document(code.upper()).set({
            'planId':       plan_id,
            'durationDays': days,
            'maxUses':      max_uses,
            'usedCount':    0,
            'usedBy':       [],
            'disabled':     False,
            'createdBy':    'admin_bot',
            'createdAt':    _fsv.SERVER_TIMESTAMP,
        })
        return True
    except Exception as e:
        logger.error(f"Firestore promo: {e}")
        return False

# ══════════════════════════════════════════════════════
#  [M3] قاعدة البيانات المحلية  [ADD v3.6: uid_mapping]
# ══════════════════════════════════════════════════════
class DB:
    def __init__(self):
        self._lock = Lock()
        db_path    = os.environ.get("DB_PATH", "").strip() or \
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")
        self.conn  = sqlite3.connect(db_path, check_same_thread=False)
        logger.info(f"✅ SQLite path: {db_path}")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._init()

    def _x(self, sql, p=()):
        with self._lock:
            c = self.conn.cursor()
            c.execute(sql, p)
            self.conn.commit()
            return c

    def _init(self):
        with self._lock:
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS users(
                    user_id   INTEGER PRIMARY KEY,
                    username  TEXT    DEFAULT '',
                    attempts  INTEGER DEFAULT 0,
                    last_try  TIMESTAMP,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    banned    INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS subs(
                    user_id   INTEGER PRIMARY KEY,
                    plan_name TEXT,
                    app_plan  TEXT,
                    expires   TIMESTAMP,
                    method    TEXT,
                    fb_uid    TEXT,
                    order_id  TEXT
                );
                CREATE TABLE IF NOT EXISTS txs(
                    tx_id       TEXT PRIMARY KEY,
                    user_id     INTEGER,
                    method      TEXT,
                    amount      REAL,
                    plan_name   TEXT,
                    app_plan    TEXT,
                    fb_uid      TEXT,
                    order_id    TEXT,
                    verified_by TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS codes(
                    code       TEXT PRIMARY KEY,
                    plan       TEXT,
                    days       INTEGER,
                    used_by    INTEGER  DEFAULT NULL,
                    used_at    TIMESTAMP DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS uid_mapping(
                    tg_uid     INTEGER PRIMARY KEY,
                    fb_uid     TEXT NOT NULL,
                    linked_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            self.conn.commit()
        logger.info("✅ SQLite جاهز")

    @staticmethod
    def _dt(s):
        if not s:
            return None
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return None

    def ensure(self, uid, name=''):
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT 1 FROM users WHERE user_id=?', (uid,))
            if not c.fetchone():
                c.execute('INSERT INTO users(user_id,username) VALUES(?,?)', (uid, name))
                self.conn.commit()

    def is_banned(self, uid):
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT banned FROM users WHERE user_id=?', (uid,))
            r = c.fetchone()
            return bool(r and r['banned'])

    def set_ban(self, uid, v):
        self._x('UPDATE users SET banned=? WHERE user_id=?', (1 if v else 0, uid))

    def get_sub(self, uid):
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT * FROM subs WHERE user_id=? AND expires>datetime("now")', (uid,))
            return c.fetchone()

    def add_sub(self, uid, plan_name, app_plan, days, method, fb_uid=None, order_id=None):
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT expires FROM subs WHERE user_id=?', (uid,))
            row = c.fetchone()
            if row:
                base = max(self._dt(row['expires']), datetime.now()) if self._dt(row['expires']) else datetime.now()
                exp  = base + timedelta(days=days)
                c.execute(
                    'UPDATE subs SET plan_name=?,app_plan=?,expires=?,method=?,fb_uid=?,order_id=? WHERE user_id=?',
                    (plan_name, app_plan, exp, method, fb_uid, order_id, uid))
            else:
                exp = datetime.now() + timedelta(days=days)
                c.execute(
                    'INSERT INTO subs VALUES(?,?,?,?,?,?,?)',
                    (uid, plan_name, app_plan, exp, method, fb_uid, order_id))
            self.conn.commit()
            return exp

    def revoke(self, uid):
        self._x('DELETE FROM subs WHERE user_id=?', (uid,))

    def add_tx(self, tx_id, uid, method, amount, plan_name, app_plan,
               fb_uid=None, order_id=None, verified_by=None):
        """
        [FIX v3.3] تُسجّل الأخطاء بدل الصمت الكامل.
        IntegrityError (PRIMARY KEY مكرر) يُتجاهل بصمت — طبيعي.
        أي خطأ آخر يُسجَّل في الـ log لمراجعة يدوية.
        """
        try:
            self._x(
                'INSERT INTO txs(tx_id,user_id,method,amount,plan_name,app_plan,fb_uid,order_id,verified_by)'
                ' VALUES(?,?,?,?,?,?,?,?,?)',
                (tx_id, uid, method, amount, plan_name, app_plan, fb_uid, order_id, verified_by))
        except sqlite3.IntegrityError:
            pass  # PRIMARY KEY مكرر — متوقع ومقبول
        except Exception as e:
            logger.error(f"⚠️ add_tx فشل — معاملة لم تُسجَّل: tx={tx_id} uid={uid} err={e}")

    def tx_exists(self, tx):
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT 1 FROM txs WHERE tx_id=?', (tx,))
            return bool(c.fetchone())

    def inc_attempts(self, uid):
        self._x(
            'INSERT INTO users(user_id,attempts,last_try) VALUES(?,1,datetime("now"))'
            ' ON CONFLICT(user_id) DO UPDATE SET attempts=attempts+1,last_try=datetime("now")',
            (uid,))

    def reset_attempts(self, uid):
        self._x('UPDATE users SET attempts=0 WHERE user_id=?', (uid,))

    def get_attempts(self, uid):
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT attempts FROM users WHERE user_id=?', (uid,))
            r = c.fetchone()
            return r['attempts'] if r else 0

    def stats(self):
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT COUNT(*) t FROM users');         total  = c.fetchone()['t']
            c.execute('SELECT COUNT(*) a FROM subs WHERE expires>datetime("now")'); active = c.fetchone()['a']
            c.execute('SELECT COUNT(*) tx FROM txs');          txs    = c.fetchone()['tx']
            return total, active, txs

    def all_users(self):
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT user_id FROM users WHERE banned=0')
            return [r['user_id'] for r in c.fetchall()]

    def add_code(self, code, plan, days):
        try:
            self._x('INSERT INTO codes(code,plan,days) VALUES(?,?,?)', (code.upper(), plan, days))
            return True
        except Exception:
            return False

    def use_code(self, code, uid):
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT * FROM codes WHERE code=? AND used_by IS NULL', (code.upper(),))
            r = c.fetchone()
            if not r:
                return None
            c.execute('UPDATE codes SET used_by=?,used_at=datetime("now") WHERE code=?', (uid, code.upper()))
            self.conn.commit()
            return r['plan'], r['days']

    # ═══════════════════════════════════════════════════
    #  [ADD v3.6] UID Mapping — ربط دائم بين tg_uid و fb_uid
    # ═══════════════════════════════════════════════════
    def set_fb_uid(self, tg_uid: int, fb_uid: str):
        """حفظ الربط بين Telegram ID و Firebase UID"""
        if not fb_uid or len(fb_uid) < 10:
            raise ValueError("fb_uid غير صالح")
        self._x(
            'INSERT INTO uid_mapping(tg_uid,fb_uid) VALUES(?,?)'
            ' ON CONFLICT(tg_uid) DO UPDATE SET fb_uid=?,linked_at=datetime("now")',
            (tg_uid, fb_uid, fb_uid))
        logger.info(f"✅ ربط UID: tg={tg_uid} → fb={fb_uid[:8]}...")

    def get_fb_uid(self, tg_uid: int) -> Optional[str]:
        """استرجاع Firebase UID من Telegram ID"""
        with self._lock:
            c = self.conn.cursor()
            c.execute('SELECT fb_uid FROM uid_mapping WHERE tg_uid=?', (tg_uid,))
            r = c.fetchone()
            return r['fb_uid'] if r else None

    def del_fb_uid(self, tg_uid: int):
        """حذف الربط — للأدمن فقط"""
        self._x('DELETE FROM uid_mapping WHERE tg_uid=?', (tg_uid,))

db = DB()

# ══════════════════════════════════════════════════════
#  Flask + Bot init
# ══════════════════════════════════════════════════════
try:
    bot = telebot.TeleBot(TOKEN, threaded=True)
    bot.remove_webhook()
    time.sleep(0.5)
except Exception as e:
    logger.critical(f"فشل تهيئة البوت: {e}")
    sys.exit(1)

flask_app = Flask('')

@flask_app.route('/')
def _h():
    return "✅ Sudan Weather Bot v4.0"

def keep_alive():
    Thread(
        target=lambda: flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080))),
        daemon=True
    ).start()

# ══════════════════════════════════════════════════════
#  _sessions  {tg_uid: {fb_uid, order_id, app_plan, pay_url, ts}}
# ══════════════════════════════════════════════════════
_sessions: dict = {}
_sessions_lock  = Lock()

def _session_set(uid: int, fb_uid: str, order_id: str, app_plan: str, pay_url: str = ''):
    with _sessions_lock:
        _sessions[uid] = {
            'fb_uid':   fb_uid,
            'order_id': order_id,
            'app_plan': app_plan,
            'pay_url':  pay_url,
            'ts':       time.time(),
        }

def _session_get(uid: int) -> dict:
    with _sessions_lock:
        s = _sessions.get(uid, {})
        if s and time.time() - s.get('ts', 0) > SESSION_TTL:
            _sessions.pop(uid, None)
            return {}
        return dict(s)

def _session_clear(uid: int):
    with _sessions_lock:
        _sessions.pop(uid, None)

def _sessions_gc():
    """تنظيف دوري لجميع الجلسات المنتهية + _check_cooldown"""
    now = time.time()
    with _sessions_lock:
        expired = [uid for uid, s in list(_sessions.items()) if now - s.get('ts', 0) > SESSION_TTL]
        for uid in expired:
            _sessions.pop(uid, None)
    if expired:
        logger.info(f"GC: حُذفت {len(expired)} جلسة منتهية")

    # تنظيف _check_cooldown — إزالة القيود القديمة (أكثر من 5 دقائق)
    with _check_lock:
        old_checks = [uid for uid, t in list(_check_cooldown.items()) if now - t > 300]
        for uid in old_checks:
            _check_cooldown.pop(uid, None)
    if old_checks:
        logger.info(f"GC: حُذفت {len(old_checks)} قيود rate-limit منتهية")

# ══════════════════════════════════════════════════════
#  [ADD v3.3] GC Thread دوري — كل 30 دقيقة
#  السابق: GC يعتمد على /start فقط → تراكم في الذاكرة
#  الجديد: thread مستقل يعمل طول الوقت
# ══════════════════════════════════════════════════════
def _start_gc_thread():
    def _gc_loop():
        while True:
            time.sleep(1800)  # 30 دقيقة
            try:
                _sessions_gc()
            except Exception as e:
                logger.error(f"GC thread error: {e}")
    Thread(target=_gc_loop, daemon=True, name="SessionsGC").start()
    logger.info("✅ GC Thread بدأ (كل 30 دقيقة)")

# ══════════════════════════════════════════════════════
#  [FIX v3.3] Rate Limiting على cb_check
#  يمنع spam استدعاءات OxaPay API
# ══════════════════════════════════════════════════════
_check_cooldown: dict = {}
_check_lock = Lock()

def _can_check(uid: int, cooldown_sec: int = 10) -> bool:
    with _check_lock:
        last = _check_cooldown.get(uid, 0)
        if time.time() - last < cooldown_sec:
            return False
        _check_cooldown[uid] = time.time()
        return True

# ══════════════════════════════════════════════════════
#  OxaPay
# ══════════════════════════════════════════════════════
def create_invoice(usd: float, plan: str, uid: int) -> dict:
    try:
        r = requests.post(OXAPAY_CREATE_URL, json={
            'merchant':    OXAPAY_KEY,
            'amount':      usd,
            'currency':    'USD',
            'lifeTime':    60,
            'description': f'SudanWeather {plan}',
            'orderId':     f'U{uid}_{int(time.time())}',
            'returnUrl':   BOT_RETURN_URL,  # [ADD v3.5] ديناميكي عبر env var
        }, headers={'Content-Type': 'application/json'}, timeout=15)
        d = r.json()
        if d.get('result') == 100:
            return {'ok': True, 'url': d['payLink'], 'track': str(d['trackId'])}
        return {'ok': False, 'err': d.get('message', '?')}
    except Exception as e:
        return {'ok': False, 'err': str(e)}

def check_invoice(track: str) -> dict:
    try:
        r = requests.post(OXAPAY_INQUIRY_URL,
            json={'merchant': OXAPAY_KEY, 'trackId': track},
            headers={'Content-Type': 'application/json'}, timeout=10)
        d = r.json()
        return {'ok': d.get('result') == 100, 'status': d.get('status', '')}
    except Exception as e:
        # [FIX v3.5] تسجيل الخطأ بدل البلع الصامت
        logger.error(f"OxaPay inquiry error (track={track}): {e}")
        return {'ok': False, 'status': ''}

# ══════════════════════════════════════════════════════
#  Groq Vision
# ══════════════════════════════════════════════════════
_SC = ('{"valid":bool,"account_match":bool,"amount":float,'
       '"tx_id":"str","datetime":"str","status_success":bool,'
       '"tampering_detected":bool,"errors":[]}')

def _groq(prompt: str, b64: str) -> Optional[dict]:
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'},
            json={
                'model': GROQ_MODEL,  # [ADD v3.5] ديناميكي عبر env var
                'messages': [{'role': 'user', 'content': [
                    {'type': 'text',      'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
                ]}],
                'response_format': {'type': 'json_object'},
                'temperature': 0.0,
                'max_tokens': 512,
            },
            timeout=35,
        )
        return json.loads(r.json()['choices'][0]['message']['content'])
    except Exception as e:
        logger.error(f'Groq: {e}')
        return None

def _pr(method: str, acc: str, name: str) -> str:
    return (f'أنت محقق مالي. حلل إيصال {method} السوداني.\n'
            f'الحساب: {acc} — الاسم: {name}\n'
            f'استخرج: المبلغ SDG، رقم العملية، التاريخ والوقت.\n'
            f'تحقق: العملية ناجحة، الرقم مطابق، الصورة غير معدلة.\n'
            f'رد JSON فقط: {_SC}')

_AZ = {
    'bankak': ('بنكك',     lambda b: _groq(_pr('بنكك',     MY_ACCOUNT,   'صاحب الحساب'), b)),
    'fawry':  ('فوري',     lambda b: _groq(_pr('فوري',     FAWRY_NUMBER,  FAWRY_NAME),   b)),
    'bravo':  ('برافو',    lambda b: _groq(_pr('برافو',    BRAVO_NUMBER,  BRAVO_NAME),   b)),
    'mycash': ('ماي كاشي', lambda b: _groq(_pr('ماي كاشي', MYCASH_NUMBER, MYCASH_NAME),  b)),
}

def _detect(b64: str) -> Tuple[str, str]:
    r = _groq(
        'حدد نوع تطبيق الدفع. الاختيارات: bankak,fawry,bravo,mycash,unknown.\n'
        'رد JSON: {"method":"...","confidence":"high|medium|low"}',
        b64,
    )
    return (r.get('method', 'unknown'), r.get('confidence', 'low')) if r else ('unknown', 'low')

def match_sdg(amount: float) -> Optional[str]:
    """يطابق المبلغ مع الخطط بهامش 3% — يستخدم السعر الحالي"""
    plans = _build_plans()
    for name, info in plans.items():
        if abs(amount - info['sdg']) <= max(info['sdg'] * 0.03, 100):
            return name
    return None

# ══════════════════════════════════════════════════════
#  دالة التفعيل الموحدة  [FIX v3.6: fb_uid إلزامي]
# ══════════════════════════════════════════════════════
def do_activate(tg_uid: int, plan_name: str, method: str,
                fb_uid: str = None, order_id: str = None) -> datetime:
    """
    [CRITICAL v3.6] fb_uid أصبح إلزامياً — حُذف fallback str(tg_uid)
    إذا fb_uid غائب، الدالة ترمي استثناء بدل التفعيل بـ UID خاطئ
    """
    if not fb_uid:
        # محاولة استرجاع fb_uid من جدول uid_mapping
        fb_uid = db.get_fb_uid(tg_uid)
        if not fb_uid:
            logger.error(f"❌ do_activate فشل: fb_uid غائب — tg_uid={tg_uid}")
            raise ValueError(f"fb_uid مطلوب لتفعيل اشتراك tg_uid={tg_uid}")

    plans    = _build_plans()
    info     = plans[plan_name]
    app_plan = info['app']
    order_id = order_id or f'BOT_{tg_uid}_{int(time.time())}'

    exp = db.add_sub(tg_uid, plan_name, app_plan, info['days'], method,
                     fb_uid=fb_uid, order_id=order_id)
    db.reset_attempts(tg_uid)

    if not fs_activate(fb_uid, order_id, app_plan):
        logger.warning(f"⚠️ Firestore sync skipped uid={tg_uid}")

    _session_clear(tg_uid)
    return exp

# ══════════════════════════════════════════════════════
#  Keyboards
# ══════════════════════════════════════════════════════
def _contact_kb() -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup()
    if DEVELOPER_WHATSAPP:
        m.add(types.InlineKeyboardButton('💬 تواصل مع المطور', url=f'https://wa.me/{DEVELOPER_WHATSAPP}'))
    return m

def _plans() -> types.InlineKeyboardMarkup:
    plans = _build_plans()
    m = types.InlineKeyboardMarkup(row_width=1)
    for n, i in plans.items():
        m.add(types.InlineKeyboardButton(f'{n} — {i["sdg"]:,} SDG (${i["usd"]})', callback_data=f'plan:{n}'))
    return m

def _pay_menu(plan_name: str) -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(
        types.InlineKeyboardButton('💳 دفع رقمي (OxaPay)',  callback_data=f'crypto:{plan_name}'),
        types.InlineKeyboardButton('🏦 تحويل بنكي (بنكك)', callback_data=f'bank:{plan_name}'),
        types.InlineKeyboardButton('💳 فوري (بنك فيصل)',    callback_data=f'fawry:{plan_name}'),
        types.InlineKeyboardButton('📱 برافو',               callback_data=f'bravo:{plan_name}'),
        types.InlineKeyboardButton('💰 ماي كاشي',           callback_data=f'mycash:{plan_name}'),
        types.InlineKeyboardButton('🔑 كود تفعيل',          callback_data='enter_code'),
    )
    return m

# ══════════════════════════════════════════════════════
#  استقبال UID من المستخدم مباشرةً  [ADD v4.1]
# ══════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: bool(_re.fullmatch(r'[A-Za-z0-9]{20,36}', (m.text or '').strip())))
def handle_uid_message(message):
    """
    المستخدم يرسل Firebase UID مباشرةً بعد نسخه من الملف الشخصي.
    يحفظ الربط ويعرض قائمة الخطط فوراً.
    """
    uid    = message.from_user.id
    fb_uid = message.text.strip()

    if db.is_banned(uid):
        bot.reply_to(message, '⛔ حسابك محظور.')
        return

    try:
        db.set_fb_uid(uid, fb_uid)
    except ValueError:
        bot.reply_to(message,
            '⚠️ *المعرّف غير صالح*\n\n'
            'تأكد من نسخه بشكل صحيح من بطاقة "معرّفك لتفعيل الاشتراك" في الملف الشخصي.',
            parse_mode='Markdown')
        return

    m = _plans()
    m.row(types.InlineKeyboardButton('🔑 كود تفعيل', callback_data='enter_code'))
    bot.send_message(uid,
        f'✅ *تم ربط حسابك بنجاح!*\n\n'
        f'🆔 `{fb_uid[:8]}...`\n\n'
        f'💎 *اختر خطة الاشتراك:*',
        reply_markup=m, parse_mode='Markdown')


# ══════════════════════════════════════════════════════
#  /start  [CRITICAL v3.6: إلزام Deep Link]
# ══════════════════════════════════════════════════════
@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.from_user.id
    db.ensure(uid, message.from_user.username or '')
    if db.is_banned(uid):
        bot.reply_to(message, '⛔ حسابك محظور.', reply_markup=_contact_kb())
        return

    # تنظيف GC عند كل /start (إضافةً إلى GC Thread)
    _sessions_gc()

    # deep link: /start subscribe_{planId}_{fbUid}_{orderId}
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith('subscribe_'):
        _deep_link(message, parts[1])
        return

    # ─── عرض حالة الاشتراك إذا كان موجوداً ────────────
    sub = db.get_sub(uid)
    if sub:
        exp = db._dt(sub['expires'])
        dl  = (exp - datetime.now()).days if exp else '?'
        m = types.InlineKeyboardMarkup(row_width=1)
        m.add(types.InlineKeyboardButton('🔄 تجديد', callback_data='renew'))
        if DEVELOPER_WHATSAPP:
            m.add(types.InlineKeyboardButton('💬 تواصل', url=f'https://wa.me/{DEVELOPER_WHATSAPP}'))
        bot.send_message(uid,
            f'✅ *حسابك مفعل!*\n\n'
            f'💎 {sub["plan_name"]}\n'
            f'💳 {sub["method"]}\n'
            f'⏳ باقي: {dl} يوم\n'
            f'📅 ينتهي: {sub["expires"][:10] if sub["expires"] else "?"}',
            reply_markup=m, parse_mode='Markdown')
        return

    # ─── مستخدم بدون اشتراك بدون deep link ─────────────
    m = types.InlineKeyboardMarkup()
    if DEVELOPER_WHATSAPP:
        m.add(types.InlineKeyboardButton('💬 تواصل مع المطور', url=f'https://wa.me/{DEVELOPER_WHATSAPP}'))
    bot.send_message(uid,
        '👋 *مرحباً بك في بوت طقس السودان PRO!*\n\n'
        '📋 *للاشتراك، اتبع الخطوات:*\n\n'
        '1️⃣ افتح تطبيق *طقس السودان*\n'
        '2️⃣ اذهب إلى *الملف الشخصي* 👤\n'
        '3️⃣ انسخ *معرّفك (UID)* من البطاقة الذهبية\n'
        '4️⃣ *أرسله هنا* في هذه المحادثة\n\n'
        '✅ بعد الإرسال، اختر خطتك وادفع — يُفعَّل تلقائياً!',
        reply_markup=m, parse_mode='Markdown')


def _deep_link(message, param: str):
    """
    [ADD v3.6] حفظ الربط في uid_mapping عند كل deep link
    [FIX v4.0] إصلاح إعادة بناء orderId — الـ separator يُستخدم كبادئة لـ orderId
    """
    uid  = message.from_user.id
    body = param[len('subscribe_'):]

    try:
        idx1     = body.index('_')
        app_plan = body[:idx1]
        rest     = body[idx1 + 1:]

        fb_uid   = None
        order_id = None
        # [FIX v4.0] السابق: rest[si + len(sep):] يحذف البادئة كلها → orderId خاطئ
        # المثال: rest = "uid_WX_1234_ABCD"
        #   السابق (v3.5): order_id = '1234_ABCD'        ← خاطئ! يكتب في doc مختلف
        #   الجديد (v4.0): order_id = 'WX_1234_ABCD'    ← صحيح! يطابق paymentRequests/{orderId}
        # المنطق: sep[1:] يستخرج البادئة المطلوبة ('WX_'، 'CODE_'، 'BOT_')
        for sep in ('_WX_', '_CODE_', '_BOT_'):
            si = rest.find(sep)
            if si != -1:
                fb_uid   = rest[:si]
                order_id = sep[1:] + rest[si + len(sep):]  # sep[1:] = 'WX_' أو 'CODE_' أو 'BOT_'
                break
        if fb_uid is None:
            s2       = rest.split('_', 1)
            fb_uid   = s2[0]
            order_id = s2[1] if len(s2) > 1 else f'DL_{uid}_{int(time.time())}'
    except (ValueError, IndexError):
        bot.reply_to(message, '⚠️ رابط غير صالح. ابدأ من جديد بـ /start')
        return

    plan_name = APP_PLAN_MAP.get(app_plan)
    if not plan_name:
        bot.reply_to(message, '⚠️ رابط غير صالح. ابدأ من جديد بـ /start')
        return

    # ═══ [ADD v3.6] حفظ الربط بشكل دائم ═══════════════
    try:
        db.set_fb_uid(uid, fb_uid)
    except ValueError as e:
        logger.error(f"Deep link: fb_uid غير صالح — {e}")
        bot.reply_to(message, '⚠️ رابط غير صالح. تواصل مع المطور.', reply_markup=_contact_kb())
        return

    _session_set(uid, fb_uid, order_id, app_plan)
    plans = _build_plans()
    info  = plans[plan_name]
    bot.send_message(uid,
        f'✨ *ترقية الحساب الذهبي*\n\n'
        f'💎 *{plan_name}*\n'
        f'💰 *${info["usd"]}* / *{info["sdg"]:,} SDG*\n'
        f'📅 *{info["days"]} يوم*\n\n'
        f'اختر طريقة الدفع:',
        reply_markup=_pay_menu(plan_name), parse_mode='Markdown')


@bot.message_handler(commands=['status'])
def cmd_status(message):
    uid = message.from_user.id
    sub = db.get_sub(uid)
    if sub:
        exp = db._dt(sub['expires'])
        dl  = (exp - datetime.now()).days if exp else '?'
        bot.reply_to(message,
            f'✅ *اشتراك نشط*\n💎 {sub["plan_name"]}\n⏳ {dl} يوم\n📅 {sub["expires"][:10]}',
            parse_mode='Markdown')
    else:
        bot.reply_to(message,
            '❌ *لا يوجد اشتراك نشط*\n\n'
            'للاشتراك، افتح البوت من داخل التطبيق:\n'
            'طقس السودان ← اشترك PRO',
            parse_mode='Markdown')

@bot.message_handler(commands=['myid'])
def cmd_myid(message):
    uid = message.from_user.id
    fb_uid = db.get_fb_uid(uid)
    txt = f'🆔 *Telegram ID:* `{uid}`\n'
    if fb_uid:
        txt += f'🔗 *Firebase UID:* `{fb_uid[:10]}...`'
    else:
        txt += '⚠️ *Firebase UID:* غير مربوط\n\nللربط، افتح البوت من التطبيق'
    bot.reply_to(message, txt, parse_mode='Markdown')

# ══════════════════════════════════════════════════════
#  Callbacks
# ══════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data == 'renew')
def cb_renew(call):
    """
    [FIX v3.6] التجديد يتطلب fb_uid — إذا غائب، توجيه للتطبيق
    """
    uid = call.from_user.id
    fb_uid = db.get_fb_uid(uid)
    
    if not fb_uid:
        bot.edit_message_text(
            '⚠️ *حسابك غير مربوط*\n\n'
            '📋 *للربط:*\n'
            '1️⃣ افتح التطبيق ← الملف الشخصي 👤\n'
            '2️⃣ انسخ معرّفك (UID) من البطاقة الذهبية\n'
            '3️⃣ أرسله هنا في هذه المحادثة',
            call.message.chat.id, call.message.message_id,
            reply_markup=_contact_kb(), parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return

    m = _plans()
    m.row(types.InlineKeyboardButton('🔑 كود تفعيل', callback_data='enter_code'))
    bot.edit_message_text('🔄 *اختر الباقة:*',
        call.message.chat.id, call.message.message_id, reply_markup=m, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == 'back')
def cb_back(call):
    uid = call.from_user.id
    sub = db.get_sub(uid)
    if sub:
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton('🔄 تجديد', callback_data='renew'))
        bot.edit_message_text(f'✅ *حسابك مفعل!*\n💎 {sub["plan_name"]}',
            call.message.chat.id, call.message.message_id, reply_markup=m, parse_mode='Markdown')
    else:
        m = _plans()
        m.row(types.InlineKeyboardButton('🔑 كود', callback_data='enter_code'))
        bot.edit_message_text('✨ *اختر الباقة:*',
            call.message.chat.id, call.message.message_id, reply_markup=m, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('plan:'))
def cb_plan(call):
    """
    [FIX v3.6] التأكد من وجود fb_uid قبل السماح بالاشتراك
    """
    uid   = call.from_user.id
    pn    = call.data[5:]
    plans = _build_plans()
    
    if pn not in plans:
        bot.answer_callback_query(call.id, '⚠️ باقة غير صالحة')
        return

    # ─── التحقق من fb_uid ──────────────────────────────
    fb_uid = db.get_fb_uid(uid)
    if not fb_uid:
        bot.edit_message_text(
            '⚠️ *حسابك غير مربوط*\n\n'
            '📋 *للربط:*\n'
            '1️⃣ افتح التطبيق ← الملف الشخصي 👤\n'
            '2️⃣ انسخ معرّفك (UID) من البطاقة الذهبية\n'
            '3️⃣ أرسله هنا في هذه المحادثة',
            call.message.chat.id, call.message.message_id,
            reply_markup=_contact_kb(), parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return

    info = plans[pn]
    bot.edit_message_text(
        f'*{pn}*\n\n💰 *${info["usd"]}* / *{info["sdg"]:,} SDG*\n📅 *{info["days"]} يوم*\n\nاختر طريقة الدفع:',
        call.message.chat.id, call.message.message_id,
        reply_markup=_pay_menu(pn), parse_mode='Markdown')
    bot.answer_callback_query(call.id)

# ─── طرق الدفع اليدوي ────
_PAY_KEYS = ('bank', 'fawry', 'bravo', 'mycash')
_PAY_INFO_MAP = {
    'bank':   ('🏦 تحويل بنكي',  f'🏛 *بنكك*\n📱 الحساب: `{MY_ACCOUNT}`'),
    'fawry':  ('💳 فوري',         f'🏛 *فوري*\n`{FAWRY_NUMBER}`\n👤 {FAWRY_NAME}'),
    'bravo':  ('📱 برافو',         f'📱 *برافو*\n`{BRAVO_NUMBER}`\n👤 {BRAVO_NAME}'),
    'mycash': ('💰 ماي كاشي',     f'💰 *ماي كاشي*\n`{MYCASH_NUMBER}`\n👤 {MYCASH_NAME}'),
}

@bot.callback_query_handler(func=lambda c: any(c.data.startswith(f'{k}:') for k in _PAY_KEYS))
def cb_manual(call):
    """
    [FIX v3.6] التأكد من fb_uid قبل إظهار معلومات الدفع
    """
    uid = call.from_user.id
    k, pn = call.data.split(':', 1)
    plans = _build_plans()
    
    if pn not in plans or k not in _PAY_INFO_MAP:
        bot.answer_callback_query(call.id)
        return

    # ─── التحقق من fb_uid ──────────────────────────────
    fb_uid = db.get_fb_uid(uid)
    if not fb_uid:
        bot.edit_message_text(
            '⚠️ *جلستك انتهت*\n\n'
            'أعد فتح البوت من التطبيق:\n'
            'طقس السودان ← اشترك PRO',
            call.message.chat.id, call.message.message_id,
            reply_markup=_contact_kb(), parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return

    disp, acct = _PAY_INFO_MAP[k]
    amt = plans[pn]['sdg']
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(types.InlineKeyboardButton('« رجوع',  callback_data=f'plan:{pn}'))
    if DEVELOPER_WHATSAPP:
        m.add(types.InlineKeyboardButton('💬 تواصل', url=f'https://wa.me/{DEVELOPER_WHATSAPP}'))
    bot.edit_message_text(
        f'{disp} — *{pn}*\n\n'
        f'1️⃣ حوّل *{amt:,} SDG* إلى:\n\n{acct}\n\n'
        f'2️⃣ أرسل صورة واضحة للإيصال\n\n'
        f'⚠️ يجب أن تظهر: رقم العملية + التاريخ + المبلغ',
        call.message.chat.id, call.message.message_id,
        reply_markup=m, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

# ─── OxaPay ───
@bot.callback_query_handler(func=lambda c: c.data.startswith('crypto:'))
def cb_crypto(call):
    """
    [FIX v3.6] استرجاع fb_uid من uid_mapping بدل الاعتماد على session
    """
    pn    = call.data[7:]
    plans = _build_plans()
    if pn not in plans:
        bot.answer_callback_query(call.id)
        return

    uid = call.from_user.id
    
    # ─── [CRITICAL v3.6] استرجاع fb_uid من الجدول ────────
    fb_uid = db.get_fb_uid(uid)
    if not fb_uid:
        bot.edit_message_text(
            '⚠️ *جلستك انتهت*\n\n'
            'أعد فتح البوت من التطبيق:\n'
            'طقس السودان ← اشترك PRO',
            call.message.chat.id, call.message.message_id,
            reply_markup=_contact_kb(), parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return

    bot.edit_message_text('🔄 جاري إنشاء فاتورة OxaPay...', call.message.chat.id, call.message.message_id)
    res = create_invoice(plans[pn]['usd'], pn, uid)
    
    if res['ok']:
        track   = res['track']
        pay_url = res['url']

        # حفظ في session للإشارة فقط — لكن fb_uid يُسترجع من الجدول
        _session_set(uid,
            fb_uid=fb_uid,
            order_id=track,
            app_plan=plans[pn]['app'],
            pay_url=pay_url,
        )

        m = types.InlineKeyboardMarkup(row_width=1)
        m.add(
            types.InlineKeyboardButton('💳 ادفع الآن', url=pay_url),
            types.InlineKeyboardButton('🔄 تحقق من الدفع', callback_data=f'check:{track}'),
            types.InlineKeyboardButton('« رجوع', callback_data=f'plan:{pn}'))
        bot.edit_message_text(
            f'✅ *الفاتورة جاهزة!*\n\n'
            f'💎 {pn}\n💰 ${plans[pn]["usd"]}\n\n'
            f'اضغط "ادفع الآن" لإتمام الدفع عبر العملات الرقمية',
            call.message.chat.id, call.message.message_id,
            reply_markup=m, parse_mode='Markdown')
    else:
        bot.edit_message_text(
            f'❌ *فشل إنشاء الفاتورة*\n\n{res["err"]}',
            call.message.chat.id, call.message.message_id,
            reply_markup=_contact_kb(), parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('check:'))
def cb_check(call):
    uid   = call.from_user.id
    track = call.data[6:]

    # [FIX v3.3] Rate limiting
    if not _can_check(uid):
        bot.answer_callback_query(call.id, '⏳ انتظر 10 ثوان بين كل فحص', show_alert=True)
        return

    s = _session_get(uid)
    if not s or s.get('order_id') != track:
        bot.answer_callback_query(call.id, '⚠️ جلسة منتهية — أعد الاشتراك', show_alert=True)
        return

    bot.answer_callback_query(call.id, '🔄 جاري التحقق...')
    res = check_invoice(track)

    if res['ok'] and res['status'] == 'Paid':
        fb_uid   = s['fb_uid']
        app_plan = s['app_plan']
        plan_name = APP_PLAN_MAP.get(app_plan, 'غير معروف')

        try:
            exp = do_activate(uid, plan_name, 'OxaPay', fb_uid=fb_uid, order_id=track)
            db.add_tx(track, uid, 'OxaPay', PLAN_META[app_plan]['usd'],
                      plan_name, app_plan, fb_uid=fb_uid, order_id=track, verified_by='oxapay_auto')
            bot.edit_message_text(
                f'🎉 *تم التفعيل بنجاح!*\n\n'
                f'💎 {plan_name}\n📅 حتى {exp.strftime("%Y-%m-%d")}\n\n'
                f'✨ استمتع بجميع مزايا PRO!',
                call.message.chat.id, call.message.message_id, parse_mode='Markdown')
        except ValueError as e:
            logger.error(f"cb_check activation failed: {e}")
            bot.edit_message_text(
                '❌ *فشل التفعيل*\n\n'
                'تواصل مع المطور للمساعدة',
                call.message.chat.id, call.message.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
    elif res['ok']:
        bot.answer_callback_query(call.id, f'⏳ الحالة: {res["status"]}')
    else:
        bot.answer_callback_query(call.id, '❌ فشل التحقق — حاول لاحقاً', show_alert=True)

# ─── كود التفعيل ───
@bot.callback_query_handler(func=lambda c: c.data == 'enter_code')
def cb_enter_code(call):
    bot.edit_message_text('🔑 *أدخل كود التفعيل:*',
        call.message.chat.id, call.message.message_id, parse_mode='Markdown')
    bot.register_next_step_handler(call.message, _handle_code)
    bot.answer_callback_query(call.id)

def _handle_code(message):
    uid  = message.from_user.id
    code = message.text.strip().upper()
    r    = db.use_code(code, uid)

    if not r:
        bot.reply_to(message, '❌ كود غير صالح أو مستخدم', reply_markup=_contact_kb())
        return

    plan_name, days = r
    plans = _build_plans()
    if plan_name not in plans:
        bot.reply_to(message, '❌ خطأ في الكود', reply_markup=_contact_kb())
        return

    try:
        # استرجاع fb_uid من الجدول
        fb_uid = db.get_fb_uid(uid)
        exp = do_activate(uid, plan_name, 'كود تفعيل', fb_uid=fb_uid)
        bot.reply_to(message,
            f'🎉 *تم التفعيل!*\n💎 {plan_name}\n📅 حتى {exp.strftime("%Y-%m-%d")}',
            parse_mode='Markdown')
    except ValueError as e:
        logger.error(f"Code activation failed: {e}")
        bot.reply_to(message,
            '⚠️ *كود صالح لكن الحساب غير مربوط*\n\n'
            '📋 انسخ معرّفك (UID) من الملف الشخصي في التطبيق وأرسله هنا أولاً، ثم أعد إدخال الكود.',
            reply_markup=_contact_kb(), parse_mode='Markdown')

# ══════════════════════════════════════════════════════
#  فحص الإيصالات
# ══════════════════════════════════════════════════════
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    """
    [FIX v3.6] استرجاع fb_uid من uid_mapping بدل الاعتماد على session
    """
    uid = message.from_user.id
    if db.is_banned(uid):
        bot.reply_to(message, '⛔ حسابك محظور.', reply_markup=_contact_kb())
        return

    if not GROQ_API_KEY:
        bot.reply_to(message, '⚠️ فحص الإيصالات معطل حالياً', reply_markup=_contact_kb())
        return

    # ─── [CRITICAL v3.6] التحقق من fb_uid قبل الفحص ────
    fb_uid = db.get_fb_uid(uid)
    if not fb_uid:
        bot.reply_to(message,
            '⚠️ *للاشتراك، افتح البوت من التطبيق*\n\n'
            'طقس السودان ← اشترك PRO',
            reply_markup=_contact_kb(), parse_mode='Markdown')
        return

    atts = db.get_attempts(uid)
    if atts >= 5:
        bot.reply_to(message, '❌ لقد تجاوزت عدد المحاولات المسموح (5). تواصل مع المطور:',
            reply_markup=_contact_kb())
        return

    wait = None
    try:
        wait = bot.reply_to(message, '🔍 جاري فحص الإيصال...')
        fid  = message.photo[-1].file_id
        info = bot.get_file(fid)
        img  = bot.download_file(info.file_path)
        b64  = base64.b64encode(img).decode('utf-8')

        method, conf = _detect(b64)
        if method == 'unknown' or conf == 'low':
            # [FIX v3.5] db.inc_attempts دائماً عند أي نتيجة
            db.inc_attempts(uid)
            bot.edit_message_text('❌ *لم نتمكن من قراءة الإيصال*\nتأكد من وضوح الصورة',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        name, fn = _AZ.get(method, (method, None))
        if not fn:
            # [FIX v3.5] db.inc_attempts دائماً
            db.inc_attempts(uid)
            bot.edit_message_text(f'⚠️ *{name}* — غير مدعوم حالياً',
                message.chat.id, wait.message_id, reply_markup=_contact_kb())
            return

        bot.edit_message_text(f'🔍 تم التعرف: {name} — جاري التحليل...', message.chat.id, wait.message_id)
        an = fn(b64)

        # [FIX v3.5] db.inc_attempts دائماً — حتى عند فشل Groq
        db.inc_attempts(uid)

        if not an:
            bot.edit_message_text('❌ خطأ تقني في التحليل. حاول مرة أخرى.',
                message.chat.id, wait.message_id, reply_markup=_contact_kb())
            return

        if an.get('tampering_detected'):
            bot.edit_message_text('🚫 *الصورة معدلة أو مزورة*\nغير مقبولة',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        if not an.get('valid'):
            err = ', '.join(an.get('errors', [])) or 'غير صالح'
            bot.edit_message_text(f'❌ *الإيصال غير مكتمل*\n\n{err}',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        if not an.get('account_match'):
            bot.edit_message_text('❌ *رقم الحساب غير مطابق*\nتأكد من التحويل للحساب الصحيح',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        if not an.get('status_success'):
            bot.edit_message_text('⚠️ *العملية غير ناجحة*\nتواصل مع المطور إذا تم الدفع فعلياً',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        amount = an.get('amount', 0)
        tx_id  = an.get('tx_id', '').strip()

        # [FIX v3.3] رفض إذا لم يُقرأ tx_id
        if not tx_id or tx_id == 'unknown':
            bot.edit_message_text('❌ *لم نتمكن من قراءة رقم العملية*\n\nتأكد من وضوح الرقم في الصورة',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        if db.tx_exists(tx_id):
            bot.edit_message_text('⚠️ *هذا الإيصال مستخدم سابقاً*',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        plan_name = match_sdg(amount)
        if plan_name:
            plans    = _build_plans()
            app_plan = plans[plan_name]['app']
            try:
                exp = do_activate(uid, plan_name, name, fb_uid=fb_uid, order_id=tx_id)
                db.add_tx(tx_id, uid, name, amount, plan_name, app_plan,
                          fb_uid=fb_uid, order_id=tx_id, verified_by='groq_vision')
                bot.edit_message_text(
                    f'🎉 *تم التفعيل بنجاح!*\n\n'
                    f'💎 {plan_name}\n💰 {amount:,.0f} SDG\n📅 حتى {exp.strftime("%Y-%m-%d")}\n\n'
                    f'✨ استمتع بجميع مزايا PRO!',
                    message.chat.id, wait.message_id, parse_mode='Markdown')
            except ValueError as e:
                logger.error(f"Photo handler activation failed: {e}")
                bot.edit_message_text(
                    '❌ *فشل التفعيل*\n\nتواصل مع المطور',
                    message.chat.id, wait.message_id,
                    reply_markup=_contact_kb(), parse_mode='Markdown')
        else:
            accs = (f'• بنكك: `{MY_ACCOUNT}`\n• فوري: `{FAWRY_NUMBER}`\n'
                    f'• برافو: `{BRAVO_NUMBER}`\n• ماي كاشي: `{MYCASH_NUMBER}`')
            bot.edit_message_text(
                f'❌ *رُفض الإيصال*\n\nالأرقام المعتمدة:\n{accs}',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')

    except Exception as e:
        logger.error(f'Photo handler: {e}')
        if wait:
            try:
                bot.edit_message_text(
                    '❌ خطأ تقني. تواصل مع المطور:',
                    message.chat.id, wait.message_id,
                    reply_markup=_contact_kb())
            except Exception:
                pass
        else:
            try:
                bot.reply_to(message, '❌ خطأ تقني. تواصل مع المطور:', reply_markup=_contact_kb())
            except Exception:
                pass

# ══════════════════════════════════════════════════════
#  الأدمن
# ══════════════════════════════════════════════════════
@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    if message.from_user.id != ADMIN_ID:
        return
    total, active, txs = db.stats()
    bot.reply_to(message,
        f'📊 *لوحة التحكم*\n\n'
        f'👥 المستخدمون: `{total}`\n✅ النشطون: `{active}`\n💰 المعاملات: `{txs}`\n\n'
        f'`/stats` — إحصائيات تفصيلية\n'
        f'`/activate [tg_id] [fb_uid] [plan]` — تفعيل يدوي\n'
        f'`/link [tg_id] [fb_uid]` — ربط يدوي (طوارئ)\n'
        f'`/revoke [id]` — إلغاء اشتراك\n'
        f'`/ban [id]` | `/unban [id]` — حظر/رفع حظر\n'
        f'`/addcode [CODE] [days] [plan]` — كود تفعيل\n'
        f'`/broadcast [رسالة]` — رسالة جماعية\n'
        f'`/myid` — عرض Telegram ID\n'
        f'`/rate` — عرض سعر الصرف الحالي',
        parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if message.from_user.id != ADMIN_ID:
        return
    with db._lock:
        c = db.conn.cursor()
        c.execute('SELECT COUNT(*) t FROM txs');                         tt   = c.fetchone()['t']
        c.execute('SELECT method,COUNT(*) n,SUM(amount) s FROM txs GROUP BY method'); rows = c.fetchall()
    t = f'📊 *إحصائيات*\n\n📦 المعاملات: `{tt}`\n\n*طرق الدفع:*\n'
    for r in rows:
        t += f'• {r["method"]}: {r["n"]} — {(r["s"] or 0):,.0f}\n'
    bot.reply_to(message, t, parse_mode='Markdown')

@bot.message_handler(commands=['rate'])
def cmd_rate(message):
    if message.from_user.id != ADMIN_ID:
        return
    rate  = _get_rate()
    plans = _build_plans()
    t = f'💱 *سعر الصرف الحالي*: `{rate:,}` SDG/USD\n\n*الأسعار الحالية:*\n'
    for n, i in plans.items():
        t += f'• {n}: `{i["sdg"]:,}` SDG\n'
    bot.reply_to(message, t, parse_mode='Markdown')

@bot.message_handler(commands=['activate'])
def cmd_activate(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        p   = message.text.split()
        uid = int(p[1])
        if uid <= 0:
            raise ValueError("uid غير صالح")

        if len(p) >= 4:
            fb_uid = p[2]
            pn     = ' '.join(p[3:])
        else:
            fb_uid = str(uid)
            pn     = ' '.join(p[2:])

        plans = _build_plans()
        if pn not in plans:
            bot.reply_to(message, f'❌ الباقات المتاحة:\n' + '\n'.join(f'• {k}' for k in plans.keys()))
            return

        # حفظ الربط في uid_mapping
        try:
            db.set_fb_uid(uid, fb_uid)
        except ValueError as e:
            bot.reply_to(message, f'❌ {e}', parse_mode='Markdown')
            return

        exp = do_activate(uid, pn, 'تفعيل يدوي', fb_uid=fb_uid)
        bot.reply_to(message,
            f'✅ تم تفعيل `{uid}`\n💎 {pn}\n🔥 fb_uid: `{fb_uid}`\n📅 حتى {exp.strftime("%Y-%m-%d")}',
            parse_mode='Markdown')
        try:
            bot.send_message(uid, f'🎉 *تم تفعيل اشتراكك!*\n💎 {pn}', parse_mode='Markdown')
        except Exception:
            pass
    except (IndexError, ValueError) as e:
        bot.reply_to(message,
            f'❌ {e}\n\nالاستخدام:\n`/activate [tg_id] [fb_uid] [plan]`',
            parse_mode='Markdown')

@bot.message_handler(commands=['link'])
def cmd_link(message):
    """
    [ADD v3.6] ربط يدوي — للأدمن فقط في حالات الطوارئ
    """
    if message.from_user.id != ADMIN_ID:
        return
    try:
        p      = message.text.split()
        tg_uid = int(p[1])
        fb_uid = p[2]
        db.set_fb_uid(tg_uid, fb_uid)
        bot.reply_to(message,
            f'✅ تم ربط:\n`{tg_uid}` → `{fb_uid}`',
            parse_mode='Markdown')
    except (IndexError, ValueError) as e:
        bot.reply_to(message, f'❌ {e}\n\nالاستخدام: `/link [tg_id] [fb_uid]`', parse_mode='Markdown')

@bot.message_handler(commands=['revoke'])
def cmd_revoke(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.split()[1])
        db.revoke(uid)
        bot.reply_to(message, f'✅ تم إلغاء اشتراك `{uid}`', parse_mode='Markdown')
        try:
            bot.send_message(uid, '⚠️ تم إلغاء اشتراكك.', reply_markup=_contact_kb())
        except Exception:
            pass
    except (IndexError, ValueError):
        bot.reply_to(message, 'الاستخدام: `/revoke [id]`', parse_mode='Markdown')

@bot.message_handler(commands=['ban'])
def cmd_ban(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.split()[1])
        db.set_ban(uid, True)
        bot.reply_to(message, f'🚫 تم حظر `{uid}`', parse_mode='Markdown')
    except (IndexError, ValueError):
        bot.reply_to(message, 'الاستخدام: `/ban [id]`', parse_mode='Markdown')

@bot.message_handler(commands=['unban'])
def cmd_unban(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.split()[1])
        db.set_ban(uid, False)
        bot.reply_to(message, f'✅ رُفع الحظر عن `{uid}`', parse_mode='Markdown')
    except (IndexError, ValueError):
        bot.reply_to(message, 'الاستخدام: `/unban [id]`', parse_mode='Markdown')

@bot.message_handler(commands=['addcode'])
def cmd_addcode(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        p    = message.text.split()
        code = p[1].upper()
        days = int(p[2])
        plans = _build_plans()
        pn   = ' '.join(p[3:]) if len(p) > 3 else list(plans.keys())[0]
        if pn not in plans:
            bot.reply_to(message, f'❌ الباقات: {", ".join(plans.keys())}')
            return
        app    = plans[pn]['app']
        ok_sql = db.add_code(code, pn, days)
        ok_fs  = fs_add_promo(code, app, days)
        bot.reply_to(message,
            f'{"✅" if ok_sql or ok_fs else "❌"} كود: `{code}`\n💎 {pn}\n📅 {days} يوم\n'
            f'SQLite: {"✅" if ok_sql else "❌"} | Firestore: {"✅" if ok_fs else "⚠️ (تحقق من Firebase)"}',
            parse_mode='Markdown')
    except (IndexError, ValueError):
        bot.reply_to(message, 'الاستخدام: `/addcode [CODE] [days] [plan]`', parse_mode='Markdown')

@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        text = message.text.split(maxsplit=1)[1]
    except IndexError:
        bot.reply_to(message, 'الاستخدام: `/broadcast رسالتك`', parse_mode='Markdown')
        return
    users = db.all_users()
    ok    = 0
    for u in users:
        try:
            bot.send_message(u, text)
            ok += 1
            time.sleep(0.05)
        except Exception:
            pass
    bot.reply_to(message, f'✅ أُرسلت إلى {ok}/{len(users)} مستخدم')

# ══════════════════════════════════════════════════════
#  تشغيل البوت
# ══════════════════════════════════════════════════════
print('=' * 60)
print('✅ Sudan Weather Bot v4.0 — متكامل مع التطبيق')
print(f'🏦 بنكك: {MY_ACCOUNT}  |  💳 فوري: {FAWRY_NUMBER}')
print(f'📱 برافو: {BRAVO_NUMBER}  |  💰 ماي كاشي: {MYCASH_NUMBER}')
print(f'💱 سعر الصرف: {_get_rate():,} SDG/USD (ديناميكي)')
print(f'🔥 Firebase: {"✅ متصل" if _fdb else "⚠️ غير متصل — اشتراكات التطبيق لن تُزامَن"}')
print(f'🤖 Groq Vision: {"✅ جاهز — موديل: " + GROQ_MODEL if GROQ_API_KEY else "⚠️ معطل — فحص الإيصالات غير متاح"}')
print(f'🔒 threaded=True — البوت لن يتجمد أثناء فحص الإيصالات')
print(f'🔑 HMAC-SHA256 توقيع الاشتراكات ✅')
print(f'🔗 BOT_RETURN_URL: {BOT_RETURN_URL}')
print(f'🆔 uid_mapping — ربط دائم بين Telegram و Firebase ✅')
print(f'🚫 Deep Link إلزامي — الاشتراك فقط عبر التطبيق ✅')
print('=' * 60)

keep_alive()
_start_gc_thread()  # [ADD v3.3] GC Thread دوري

while True:
    try:
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as e:
        logger.error(f'Polling: {e}')
        time.sleep(15)
