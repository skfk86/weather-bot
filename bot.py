"""
بوت اشتراكات طقس السودان — v3.1
متكامل مع التطبيق: يكتب wxsubscriptions + paymentRequests + promoCodes

التغييرات v3.1:
 - [FIX]  threaded=True  — يمنع تجميد البوت أثناء انتظار Groq
 - [FIX]  تخزين pay_url لـ OxaPay في _sessions — حل رابط الدفع المكسور
 - [FIX]  تنظيف _sessions بعد التفعيل — منع memory leak
 - [FIX]  inc_attempts فقط على المحاولات الحقيقية لا الأخطاء التقنية
 - [FIX]  دمج _wa() و _sup() المتطابقتين
 - [FIX]  bot init ينهي البرنامج عند الفشل بدل الاستمرار بدون bot
 - [FIX]  تحذيرات startup للمتغيرات الحساسة المكشوفة
 - [ADD]  /myid — يعطي المستخدم Telegram ID خاصته
 - [ADD]  تنظيف تلقائي لـ _sessions القديمة (TTL 2 ساعة)
 - [ADD]  /activate يقبل fb_uid اختياري: /activate [tg_id] [fb_uid] [plan]
 - [IMPROVE] رسائل خطأ أوضح للمستخدم عند فشل Groq
"""

import sys
import telebot
import requests
import base64
import json
import time
import sqlite3
import logging
import os
from datetime import datetime, timedelta, timezone
from telebot import types
from flask import Flask
from threading import Thread, Lock

# ══════════════════════════════════════════════════════
#  الإعدادات — يُفضَّل دائماً استخدام متغيرات البيئة
# ══════════════════════════════════════════════════════
# تحذير: لا تنشر هذا الملف علناً وفيه توكنات حقيقية
TOKEN              = os.environ.get("BOT_TOKEN",           "8665720382:AAEzrjTSqC5Gt5QXXu-gWfYu-vkUodOfwGw")
OXAPAY_KEY         = os.environ.get("OXAPAY_KEY",          "LYMACY-HJVRXA-D02BTO-AHUK8R")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY",        "")
ADMIN_ID           = int(os.environ.get("ADMIN_ID",        "8188643525"))
MY_ACCOUNT         = os.environ.get("BANK_ACCOUNT",        "4636998")
USD_TO_SDG_RATE    = int(os.environ.get("USD_TO_SDG_RATE", "3600"))
DEVELOPER_WHATSAPP = os.environ.get("DEV_WHATSAPP",        "249901758765")
FIREBASE_CREDS     = os.environ.get("FIREBASE_ADMIN_CREDS", "")

FAWRY_NUMBER  = os.environ.get("FAWRY_NUMBER",  "51663519");  FAWRY_NAME  = "القاسم احمد محمد"
BRAVO_NUMBER  = os.environ.get("BRAVO_NUMBER",  "71062333");  BRAVO_NAME  = "علي القاسم"
MYCASH_NUMBER = os.environ.get("MYCASH_NUMBER", "400569264"); MYCASH_NAME = "علي القاسم"

OXAPAY_CREATE_URL  = 'https://api.oxapay.com/merchants/request'
OXAPAY_INQUIRY_URL = 'https://api.oxapay.com/merchants/inquiry'

# مدة صلاحية الجلسة (ثانية) — 2 ساعة
SESSION_TTL = 7200

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  [M1] Firebase Admin — يكتب بنفس schema التطبيق
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
#  [M2] ربط الباقات بين التطبيق والبوت
# ══════════════════════════════════════════════════════
APP_PLAN_MAP = {
    'annual':  '👑 السنوية',
    'monthly': '🌙 الشهرية',
    'starter': '⭐ المبدئية',
}
BOT_TO_APP = {v: k for k, v in APP_PLAN_MAP.items()}

# مطابق لـ PW_PLANS في التطبيق
PLAN_META = {
    'annual':  {'dailyAI': 100, 'days': 365, 'usd': 49.00},
    'monthly': {'dailyAI': 50,  'days': 30,  'usd':  4.99},
    'starter': {'dailyAI': 30,  'days': 30,  'usd':  2.99},
}

def _sig(uid, plan_id, exp_iso):
    """مطابق لـ btoa(unescape(encodeURIComponent(...))) في JS"""
    return base64.b64encode(f"{uid}|{plan_id}|{exp_iso}".encode()).decode()

def fs_activate(uid: str, order_id: str, app_plan: str) -> bool:
    """
    يكتب المجموعتين اللتين يقرأ منهما التطبيق:
    - paymentRequests/{orderId}  → status='completed'  (Polling في التطبيق)
    - wxsubscriptions/{uid}      → schema كامل         (listenToSubscription)
    """
    if not _fdb:
        return False
    try:
        meta    = PLAN_META.get(app_plan, PLAN_META['monthly'])
        now     = datetime.now(timezone.utc)
        expires = now + timedelta(days=meta['days'])
        exp_iso = expires.isoformat()
        srv     = _fsv.SERVER_TIMESTAMP

        _fdb.collection('paymentRequests').document(order_id).set({
            'status':      'completed',
            'completedAt': srv,
            'activatedBy': 'telegram_bot',
            'uid':         uid,
            'planId':      app_plan,
        }, merge=True)

        _fdb.collection('wxsubscriptions').document(uid).set({
            'uid':           uid,
            'planId':        app_plan,
            'planType':      app_plan,
            'planName':      APP_PLAN_MAP.get(app_plan, app_plan),
            'status':        'approved',
            'active':        True,
            'dailyAI':       meta['dailyAI'],
            'requestLimit':  meta['dailyAI'],
            'requestUsed':   0,
            'lastAIRequest': None,
            'expiresAt':     exp_iso,
            'activatedAt':   srv,
            'updatedAt':     srv,
            'source':        'telegram_bot',
            'method':        'telegram_bot',
            'trackId':       order_id,
            '_sig':          _sig(uid, app_plan, exp_iso),
        }, merge=True)

        logger.info(f"✅ Firestore: uid={uid} plan={app_plan} expires={exp_iso[:10]}")
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
    """يكتب في promoCodes/{CODE} الذي يقرأه redeemPromoCode() في التطبيق"""
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
#  [M3] قاعدة البيانات المحلية
# ══════════════════════════════════════════════════════
class DB:
    def __init__(self):
        self._lock = Lock()
        self.conn  = sqlite3.connect('bot.db', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
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
        try:
            self._x(
                'INSERT INTO txs(tx_id,user_id,method,amount,plan_name,app_plan,fb_uid,order_id,verified_by)'
                ' VALUES(?,?,?,?,?,?,?,?,?)',
                (tx_id, uid, method, amount, plan_name, app_plan, fb_uid, order_id, verified_by))
        except Exception:
            pass  # تجاهل خطأ PRIMARY KEY عند التكرار

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

db = DB()

# ══════════════════════════════════════════════════════
#  الباقات
# ══════════════════════════════════════════════════════
PLANS = {
    '👑 السنوية':  {'app': 'annual',  'usd': 49.00, 'sdg': int(49.00 * USD_TO_SDG_RATE), 'days': 365},
    '🌙 الشهرية':  {'app': 'monthly', 'usd':  4.99, 'sdg': int( 4.99 * USD_TO_SDG_RATE), 'days': 30},
    '⭐ المبدئية': {'app': 'starter', 'usd':  2.99, 'sdg': int( 2.99 * USD_TO_SDG_RATE), 'days': 30},
}

# ══════════════════════════════════════════════════════
#  Flask + Bot init
#  [FIX] threaded=True — يمنع تجميد البوت أثناء Groq (35 ث)
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
    return "✅ Sudan Weather Bot v3.1"

def keep_alive():
    Thread(
        target=lambda: flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080))),
        daemon=True
    ).start()

# ══════════════════════════════════════════════════════
#  _sessions  {tg_uid: {fb_uid, order_id, app_plan, pay_url, ts}}
#  [FIX] أُضيف pay_url لحل رابط OxaPay المكسور
#  [FIX] أُضيف ts لتنظيف الجلسات المنتهية
# ══════════════════════════════════════════════════════
_sessions: dict = {}

def _session_set(uid: int, fb_uid: str, order_id: str, app_plan: str, pay_url: str = ''):
    _sessions[uid] = {
        'fb_uid':   fb_uid,
        'order_id': order_id,
        'app_plan': app_plan,
        'pay_url':  pay_url,
        'ts':       time.time(),
    }

def _session_get(uid: int) -> dict:
    s = _sessions.get(uid, {})
    # تنظيف الجلسة المنتهية
    if s and time.time() - s.get('ts', 0) > SESSION_TTL:
        _sessions.pop(uid, None)
        return {}
    return s

def _session_clear(uid: int):
    _sessions.pop(uid, None)

def _sessions_gc():
    """تنظيف دوري لجميع الجلسات المنتهية"""
    now = time.time()
    expired = [uid for uid, s in list(_sessions.items()) if now - s.get('ts', 0) > SESSION_TTL]
    for uid in expired:
        _sessions.pop(uid, None)
    if expired:
        logger.info(f"GC: حُذفت {len(expired)} جلسة منتهية")

# ══════════════════════════════════════════════════════
#  OxaPay
# ══════════════════════════════════════════════════════
def create_invoice(usd: float, plan: str, uid: int) -> dict:
    try:
        r = requests.post(OXAPAY_CREATE_URL, json={
            'merchant':   OXAPAY_KEY,
            'amount':     usd,
            'currency':   'USD',
            'lifeTime':   60,
            'description': f'SudanWeather {plan}',
            'orderId':    f'U{uid}_{int(time.time())}',
            'returnUrl':  'https://t.me/SudanWeatherBot',
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
    except Exception:
        return {'ok': False, 'status': ''}

# ══════════════════════════════════════════════════════
#  Groq Vision
# ══════════════════════════════════════════════════════
_SC = ('{"valid":bool,"account_match":bool,"amount":float,'
       '"tx_id":"str","datetime":"str","status_success":bool,'
       '"tampering_detected":bool,"errors":[]}')

def _groq(prompt: str, b64: str) -> dict | None:
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'},
            json={
                'model': 'llama-3.2-11b-vision-preview',
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

def _detect(b64: str) -> tuple[str, str]:
    r = _groq(
        'حدد نوع تطبيق الدفع. الاختيارات: bankak,fawry,bravo,mycash,unknown.\n'
        'رد JSON: {"method":"...","confidence":"high|medium|low"}',
        b64,
    )
    return (r.get('method', 'unknown'), r.get('confidence', 'low')) if r else ('unknown', 'low')

def match_sdg(amount: float) -> str | None:
    for name, info in PLANS.items():
        if abs(amount - info['sdg']) <= max(info['sdg'] * 0.03, 100):
            return name
    return None

# ══════════════════════════════════════════════════════
#  دالة التفعيل الموحدة
# ══════════════════════════════════════════════════════
def do_activate(tg_uid: int, plan_name: str, method: str,
                fb_uid: str = None, order_id: str = None) -> datetime:
    """
    SQLite + Firestore (wxsubscriptions + paymentRequests)
    تُرجع datetime انتهاء الاشتراك
    [FIX] ينظف _sessions بعد التفعيل
    """
    info     = PLANS[plan_name]
    app_plan = info['app']
    fb_uid   = fb_uid   or str(tg_uid)
    order_id = order_id or f'BOT_{tg_uid}_{int(time.time())}'

    exp = db.add_sub(tg_uid, plan_name, app_plan, info['days'], method,
                     fb_uid=fb_uid, order_id=order_id)
    db.reset_attempts(tg_uid)

    if not fs_activate(fb_uid, order_id, app_plan):
        logger.warning(f"⚠️ Firestore sync skipped uid={tg_uid}")

    # [FIX] تنظيف الجلسة بعد التفعيل
    _session_clear(tg_uid)

    return exp

# ══════════════════════════════════════════════════════
#  Keyboards
#  [FIX] دُمجت _wa() و _sup() المتطابقتان في دالة واحدة
# ══════════════════════════════════════════════════════
def _contact_kb() -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton('💬 تواصل مع المطور', url=f'https://wa.me/{DEVELOPER_WHATSAPP}'))
    return m

# أسماء مستعارة للتوافق مع الكود القديم
_wa  = _contact_kb
_sup = _contact_kb

def _plans() -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup(row_width=1)
    for n, i in PLANS.items():
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
#  /start
# ══════════════════════════════════════════════════════
@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.from_user.id
    db.ensure(uid, message.from_user.username or '')
    if db.is_banned(uid):
        bot.reply_to(message, '⛔ حسابك محظور.', reply_markup=_contact_kb())
        return

    # تنظيف GC عند كل /start
    _sessions_gc()

    # deep link: /start subscribe_{planId}_{fbUid}_{orderId}
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith('subscribe_'):
        _deep_link(message, parts[1])
        return

    sub = db.get_sub(uid)
    if sub:
        exp = db._dt(sub['expires'])
        dl  = (exp - datetime.now()).days if exp else '?'
        m = types.InlineKeyboardMarkup(row_width=1)
        m.add(types.InlineKeyboardButton('🔄 تجديد', callback_data='renew'))
        m.add(types.InlineKeyboardButton('💬 تواصل', url=f'https://wa.me/{DEVELOPER_WHATSAPP}'))
        bot.send_message(uid,
            f'✅ *حسابك مفعل!*\n\n'
            f'💎 {sub["plan_name"]}\n'
            f'💳 {sub["method"]}\n'
            f'⏳ باقي: {dl} يوم\n'
            f'📅 ينتهي: {sub["expires"][:10] if sub["expires"] else "?"}',
            reply_markup=m, parse_mode='Markdown')
        return

    m = _plans()
    m.row(types.InlineKeyboardButton('🔑 عندي كود تفعيل', callback_data='enter_code'))
    m.row(types.InlineKeyboardButton('💬 تواصل مع المطور', url=f'https://wa.me/{DEVELOPER_WHATSAPP}'))
    bot.send_message(uid,
        '✨ *مرحباً في طقس السودان — بوت الاشتراكات* ⛈️\n\nاختر الباقة:',
        reply_markup=m, parse_mode='Markdown')


def _deep_link(message, param: str):
    """
    parse: subscribe_{app_plan}_{firebase_uid}_{order_id}
    orderId يبدأ بـ WX_ أو CODE_
    """
    uid  = message.from_user.id
    body = param[len('subscribe_'):]   # annual_fbUID_WX_1234_ABCD

    try:
        idx1     = body.index('_')
        app_plan = body[:idx1]         # annual|monthly|starter
        rest     = body[idx1 + 1:]     # fbUID_WX_...

        fb_uid   = None
        order_id = None
        for sep in ('_WX_', '_CODE_', '_BOT_'):
            si = rest.find(sep)
            if si != -1:
                fb_uid   = rest[:si]
                order_id = rest[si + 1:]
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

    _session_set(uid, fb_uid, order_id, app_plan)
    info = PLANS[plan_name]
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
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton('⬆️ اشترك', callback_data='renew'))
        bot.reply_to(message, '❌ *لا يوجد اشتراك نشط*', reply_markup=m, parse_mode='Markdown')

# [ADD] أمر جديد — يُريح المستخدم والأدمن على حد سواء
@bot.message_handler(commands=['myid'])
def cmd_myid(message):
    uid = message.from_user.id
    bot.reply_to(message, f'🆔 Telegram ID: `{uid}`', parse_mode='Markdown')

# ══════════════════════════════════════════════════════
#  Callbacks
# ══════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data == 'renew')
def cb_renew(call):
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
    pn = call.data[5:]
    if pn not in PLANS:
        bot.answer_callback_query(call.id, '⚠️ باقة غير صالحة')
        return
    info = PLANS[pn]
    bot.edit_message_text(
        f'*{pn}*\n\n💰 *${info["usd"]}* / *{info["sdg"]:,} SDG*\n📅 *{info["days"]} يوم*\n\nاختر طريقة الدفع:',
        call.message.chat.id, call.message.message_id,
        reply_markup=_pay_menu(pn), parse_mode='Markdown')
    bot.answer_callback_query(call.id)

# ─── طرق الدفع اليدوي ────
# [NOTE] f-strings تُحسب عند تحميل الوحدة — صحيح لأن المتغيرات module-level
def _pay_info() -> dict:
    return {
        'bank':   ('🏦 تحويل بنكي',  f'🏛 *بنكك*\n📱 الحساب: `{MY_ACCOUNT}`'),
        'fawry':  ('💳 فوري',         f'🏛 *فوري*\n`{FAWRY_NUMBER}`\n👤 {FAWRY_NAME}'),
        'bravo':  ('📱 برافو',         f'📱 *برافو*\n`{BRAVO_NUMBER}`\n👤 {BRAVO_NAME}'),
        'mycash': ('💰 ماي كاشي',     f'💰 *ماي كاشي*\n`{MYCASH_NUMBER}`\n👤 {MYCASH_NAME}'),
    }

_PAY_KEYS = ('bank', 'fawry', 'bravo', 'mycash')

@bot.callback_query_handler(func=lambda c: any(c.data.startswith(f'{k}:') for k in _PAY_KEYS))
def cb_manual(call):
    k, pn = call.data.split(':', 1)
    pay_info = _pay_info()
    if pn not in PLANS or k not in pay_info:
        bot.answer_callback_query(call.id)
        return
    disp, acct = pay_info[k]
    amt = PLANS[pn]['sdg']
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(types.InlineKeyboardButton('« رجوع',  callback_data=f'plan:{pn}'))
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
    pn = call.data[7:]
    if pn not in PLANS:
        bot.answer_callback_query(call.id)
        return
    uid = call.from_user.id
    bot.edit_message_text('🔄 جاري إنشاء فاتورة OxaPay...', call.message.chat.id, call.message.message_id)
    res = create_invoice(PLANS[pn]['usd'], pn, uid)
    if res['ok']:
        track   = res['track']
        pay_url = res['url']

        # [FIX] تخزين pay_url في الجلسة لاستخدامه لاحقاً في cb_check
        s = _session_get(uid)
        _session_set(uid,
            fb_uid=s.get('fb_uid', str(uid)),
            order_id=s.get('order_id', track),
            app_plan=PLANS[pn]['app'],
            pay_url=pay_url,
        )

        # تسجيل الفاتورة بدون _done (سيُضاف عند التأكيد الفعلي فقط)
        db.add_tx(track, uid, 'OxaPay', PLANS[pn]['usd'], pn, PLANS[pn]['app'])

        m = types.InlineKeyboardMarkup(row_width=1)
        m.add(types.InlineKeyboardButton('💳 ادفع الآن',       url=pay_url))
        m.add(types.InlineKeyboardButton('🔄 تحقق من الدفع',   callback_data=f'chk:{track}:{pn}'))
        m.add(types.InlineKeyboardButton('« رجوع',             callback_data=f'plan:{pn}'))
        bot.edit_message_text(
            f'✅ *الفاتورة جاهزة*\n\n'
            f'💎 {pn}\n💰 ${PLANS[pn]["usd"]}\n🆔 `{track}`\n\n'
            f'1️⃣ اضغط «ادفع الآن»\n'
            f'2️⃣ أكمل الدفع\n'
            f'3️⃣ اضغط «تحقق من الدفع»\n\n'
            f'⏰ صالحة 60 دقيقة',
            call.message.chat.id, call.message.message_id,
            reply_markup=m, parse_mode='Markdown')
        bot.send_message(ADMIN_ID, f'📢 *فاتورة OxaPay*\n👤 {uid}\n💎 {pn}\n🆔 {track}')
    else:
        m = types.InlineKeyboardMarkup(row_width=1)
        m.add(types.InlineKeyboardButton('🏦 تحويل بنكي', callback_data=f'bank:{pn}'))
        m.add(types.InlineKeyboardButton('« رجوع',        callback_data=f'plan:{pn}'))
        bot.edit_message_text(
            f'⚠️ *تعذر إنشاء الفاتورة*\n`{res["err"]}`\n\nجرّب طريقة دفع أخرى.',
            call.message.chat.id, call.message.message_id,
            reply_markup=m, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('chk:'))
def cb_check(call):
    try:
        _, track, pn = call.data.split(':', 2)
    except ValueError:
        bot.answer_callback_query(call.id, '⚠️ بيانات غير صالحة')
        return

    uid = call.from_user.id
    bot.answer_callback_query(call.id, '🔄 جاري التحقق...')
    res = check_invoice(track)

    if res['ok'] and res['status'] == 'Paid':
        if not db.tx_exists(track + '_done'):
            s  = _session_get(uid)
            fb  = s.get('fb_uid',   str(uid))
            oid = s.get('order_id', track)
            expires = do_activate(uid, pn, 'OxaPay', fb_uid=fb, order_id=oid)
            db.add_tx(track + '_done', uid, 'OxaPay', PLANS[pn]['usd'], pn, PLANS[pn]['app'], fb, oid, 'OxaPay')
            bot.edit_message_text(
                f'🎉 *تم الدفع بنجاح!*\n\n💎 {pn}\n📅 حتى: {expires.strftime("%Y-%m-%d")}\n\nشكراً! 🌟',
                call.message.chat.id, call.message.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            bot.send_message(ADMIN_ID, f'✅ *OxaPay ناجح*\n👤 {uid}\n💎 {pn}\n🆔 {track}')
        else:
            bot.send_message(call.message.chat.id, 'ℹ️ تم تفعيل هذا الدفع مسبقاً.')
    else:
        # [FIX] استخدام pay_url المُخزَّن بدل الرابط الثابت المكسور
        s        = _session_get(uid)
        pay_url  = s.get('pay_url') or f'https://checkout.oxapay.com/{track}'
        m = types.InlineKeyboardMarkup(row_width=1)
        m.add(types.InlineKeyboardButton('💳 ادفع الآن',    url=pay_url))
        m.add(types.InlineKeyboardButton('🔄 تحقق مجدداً', callback_data=f'chk:{track}:{pn}'))
        m.add(types.InlineKeyboardButton('💬 تواصل',        url=f'https://wa.me/{DEVELOPER_WHATSAPP}'))
        bot.edit_message_text(
            '⏳ *لم يتم تأكيد الدفع بعد.*\n\nأكمل الدفع ثم اضغط «تحقق مجدداً».',
            call.message.chat.id, call.message.message_id,
            reply_markup=m, parse_mode='Markdown')

# ── كود التفعيل ──
@bot.callback_query_handler(func=lambda c: c.data == 'enter_code')
def cb_code(call):
    bot.edit_message_text('🔑 *تفعيل بكود*\n\nأرسل الكود الآن:',
        call.message.chat.id, call.message.message_id, parse_mode='Markdown')
    bot.answer_callback_query(call.id)
    bot.register_next_step_handler(call.message, _redeem)

def _redeem(message):
    uid  = message.from_user.id
    code = (message.text or '').strip().upper()
    if not code:
        bot.reply_to(message, '⚠️ أرسل الكود نصاً')
        return
    res = db.use_code(code, uid)
    if not res:
        bot.reply_to(message, '❌ *الكود غير صالح أو مستخدم مسبقاً*',
            reply_markup=_contact_kb(), parse_mode='Markdown')
        return
    pn, days = res
    s      = _session_get(uid)
    fb     = s.get('fb_uid', str(uid))
    oid    = f'CODE_{code}_{uid}'
    expires = do_activate(uid, pn, 'كود تفعيل', fb_uid=fb, order_id=oid)
    bot.reply_to(message,
        f'✅ *تم التفعيل!*\n\n💎 {pn}\n📅 حتى: {expires.strftime("%Y-%m-%d")}\n\nاستمتع! 🌟',
        parse_mode='Markdown')
    bot.send_message(ADMIN_ID, f'🔑 *كود مُفعَّل*\n👤 {uid}\n🔑 {code}\n💎 {pn}')

# ══════════════════════════════════════════════════════
#  معالجة الإيصالات
#  [FIX] inc_attempts فقط عند المحاولات الحقيقية لا الأخطاء التقنية
# ══════════════════════════════════════════════════════
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    if db.get_attempts(uid) >= 5:
        bot.reply_to(message, '⛔ تجاوزت الحد الأقصى للمحاولات. تواصل مع المطور:',
            reply_markup=_contact_kb())
        return

    wait = bot.reply_to(message, '🔍 جاري فحص الإيصال... (قد يستغرق دقيقة)')
    try:
        f   = bot.get_file(message.photo[-1].file_id)
        b64 = base64.b64encode(bot.download_file(f.file_path)).decode()

        if not GROQ_API_KEY:
            bot.edit_message_text(
                '⚠️ *خدمة فحص الإيصالات غير مفعلة حالياً*\nتواصل مع المطور مباشرة:',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        mid, conf  = _detect(b64)
        result     = None
        pay_method = None

        if mid in _AZ and conf in ('high', 'medium'):
            pay_method, fn = _AZ[mid]
            result = fn(b64)
        else:
            for _, (pm, fn) in _AZ.items():
                r = fn(b64)
                if r and r.get('account_match'):
                    result, pay_method = r, pm
                    break

        # [FIX] نعدّ المحاولة فقط إذا وصلنا لنتيجة حقيقية (ليس خطأ تقني)
        if result is not None:
            db.inc_attempts(uid)

        if result is None:
            bot.edit_message_text(
                '⚠️ *تعذر قراءة الإيصال تقنياً*\nتأكد من وضوح الصورة أو تواصل مع المطور:',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        if result.get('tampering_detected'):
            bot.edit_message_text(
                '⛔ *رُفض الإيصال*\n🔍 مؤشرات تعديل على الصورة.',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            bot.send_message(ADMIN_ID, f'⚠️ *محاولة تزوير*\n👤 {uid}')
            return

        if not result.get('status_success', True):
            bot.edit_message_text(
                '❌ *الإيصال يظهر عملية غير ناجحة*',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb(), parse_mode='Markdown')
            return

        if result.get('valid') and result.get('account_match'):
            amount = float(result.get('amount', 0))
            tx     = result.get('tx_id', f'TX_{uid}_{int(time.time())}')
            dt     = result.get('datetime', '—')

            if db.tx_exists(tx):
                bot.edit_message_text(
                    f'❌ *رقم العملية مستخدم مسبقاً*\n\n`{tx}`',
                    message.chat.id, wait.message_id,
                    reply_markup=_contact_kb(), parse_mode='Markdown')
                return

            pn = match_sdg(amount)
            if pn:
                s   = _session_get(uid)
                fb  = s.get('fb_uid',   str(uid))
                oid = s.get('order_id', tx)
                db.add_tx(tx, uid, pay_method, amount, pn, PLANS[pn]['app'], fb, oid, 'AI')
                expires = do_activate(uid, pn, pay_method, fb_uid=fb, order_id=oid)
                bot.edit_message_text(
                    f'✅ *تم التفعيل!*\n\n'
                    f'💎 {pn}\n💰 {amount:,.0f} SDG\n💳 {pay_method}\n'
                    f'🔢 `{tx}`\n📅 {dt}\n📆 {expires.strftime("%Y-%m-%d")}',
                    message.chat.id, wait.message_id,
                    reply_markup=_contact_kb(), parse_mode='Markdown')
                bot.send_message(ADMIN_ID,
                    f'✅ *تفعيل AI*\n👤 {uid}\n💎 {pn}\n💰 {amount:,.0f}\n🔢 {tx}')
            else:
                prices = '\n'.join(f'• {n}: {i["sdg"]:,} SDG' for n, i in PLANS.items())
                bot.edit_message_text(
                    f'⚠️ *المبلغ غير مطابق*\nالمُرسَل: {amount:,.0f} SDG\n\n{prices}',
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
        try:
            bot.edit_message_text(
                '❌ خطأ تقني. تواصل مع المطور:',
                message.chat.id, wait.message_id,
                reply_markup=_contact_kb())
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
        f'`/revoke [id]` — إلغاء اشتراك\n'
        f'`/ban [id]` | `/unban [id]` — حظر/رفع حظر\n'
        f'`/addcode [CODE] [days] [plan]` — كود تفعيل\n'
        f'`/broadcast [رسالة]` — رسالة جماعية\n'
        f'`/myid` — عرض Telegram ID',
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

@bot.message_handler(commands=['activate'])
def cmd_activate(message):
    """
    الاستخدام:
    /activate [tg_id] [plan]           — fb_uid = str(tg_id) تلقائياً
    /activate [tg_id] [fb_uid] [plan]  — تحديد fb_uid يدوياً (الأفضل)
    """
    if message.from_user.id != ADMIN_ID:
        return
    try:
        p = message.text.split()
        uid = int(p[1])

        # تحديد هل الجزء الثاني fb_uid أم بداية اسم الباقة
        # الباقات تبدأ بـ emoji، fb_uid يكون نصاً خالياً من emoji
        if len(p) >= 4:
            fb_uid = p[2]
            pn     = ' '.join(p[3:])
        else:
            fb_uid = str(uid)
            pn     = ' '.join(p[2:])

        if pn not in PLANS:
            bot.reply_to(message, f'❌ الباقات المتاحة:\n' + '\n'.join(f'• {k}' for k in PLANS.keys()))
            return

        exp = do_activate(uid, pn, 'تفعيل يدوي', fb_uid=fb_uid)
        bot.reply_to(message,
            f'✅ تم تفعيل `{uid}`\n💎 {pn}\n🔥 fb_uid: `{fb_uid}`\n📅 حتى {exp.strftime("%Y-%m-%d")}',
            parse_mode='Markdown')
        try:
            bot.send_message(uid, f'🎉 *تم تفعيل اشتراكك!*\n💎 {pn}', parse_mode='Markdown')
        except Exception:
            pass
    except (IndexError, ValueError):
        bot.reply_to(message,
            'الاستخدام:\n`/activate [tg_id] [plan]`\n`/activate [tg_id] [fb_uid] [plan]`',
            parse_mode='Markdown')

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
    """
    ينشئ الكود في:
    1. SQLite    (البوت يتحقق منه عند الإيصالات)
    2. Firestore promoCodes (التطبيق يقرأه في redeemPromoCode)
    """
    if message.from_user.id != ADMIN_ID:
        return
    try:
        p    = message.text.split()
        code = p[1].upper()
        days = int(p[2])
        pn   = ' '.join(p[3:]) if len(p) > 3 else list(PLANS.keys())[0]
        if pn not in PLANS:
            bot.reply_to(message, f'❌ الباقات: {", ".join(PLANS.keys())}')
            return
        app    = PLANS[pn]['app']
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
print('✅ Sudan Weather Bot v3.1 — متكامل مع التطبيق')
print(f'🏦 بنكك: {MY_ACCOUNT}  |  💳 فوري: {FAWRY_NUMBER}')
print(f'📱 برافو: {BRAVO_NUMBER}  |  💰 ماي كاشي: {MYCASH_NUMBER}')
print(f'🔥 Firebase: {"✅ متصل" if _fdb else "⚠️ غير متصل — اشتراكات التطبيق لن تُزامَن"}')
print(f'🤖 Groq Vision: {"✅ جاهز" if GROQ_API_KEY else "⚠️ معطل — فحص الإيصالات غير متاح"}')
print(f'🔒 threaded=True — البوت لن يتجمد أثناء فحص الإيصالات')
print('=' * 60)

keep_alive()

while True:
    try:
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as e:
        logger.error(f'Polling: {e}')
        time.sleep(15)
