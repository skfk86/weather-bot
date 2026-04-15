import telebot
import requests
import base64
import json
import time
import sqlite3
import threading
import logging
import os
from datetime import datetime, timedelta
from telebot import types
from flask import Flask
from threading import Thread

# ==================== الإعدادات ====================
TOKEN = os.environ.get("BOT_TOKEN", "8665720382:AAEzrjTSqC5Gt5QXXu-gWfYu-vkUodOfwGw")
OXAPAY_KEY = os.environ.get("OXAPAY_KEY", "LYMACY-HJVRXA-D02BTO-AHUK8R")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8188643525"))
MY_ACCOUNT = os.environ.get("BANK_ACCOUNT", "4636998")
USD_TO_SDG_RATE = int(os.environ.get("USD_TO_SDG_RATE", "3600"))
DEVELOPER_WHATSAPP = os.environ.get("DEV_WHATSAPP", "249901758765")

# أرقام المحافظ
FAWRY_NUMBER = os.environ.get("FAWRY_NUMBER", "51663519")
FAWRY_NAME = "القاسم احمد محمد"
BRAVO_NUMBER = os.environ.get("BRAVO_NUMBER", "71062333")
BRAVO_NAME = "علي القاسم"
MYCASH_NUMBER = os.environ.get("MYCASH_NUMBER", "400569264")
MYCASH_NAME = "علي القاسم"

OXAPAY_CREATE_URL = 'https://api.oxapay.com/merchants/request'
OXAPAY_INQUIRY_URL = 'https://api.oxapay.com/merchants/inquiry'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY غير مضبوط. ميزة تحليل الصور معطلة.")

# ==================== حل تعدد النسخ ====================
try:
    bot = telebot.TeleBot(TOKEN, threaded=False)
    bot.remove_webhook()
    time.sleep(0.5)
    logger.info("✅ تم إزالة webhook بنجاح")
except Exception as e:
    logger.error(f"خطأ في إزالة webhook: {e}")

# ==================== Flask (لإبقاء البوت حياً) ====================
app = Flask('')

@app.route('/')
def home():
    return "Bot is Running!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ==================== قاعدة البيانات ====================
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('bot.db', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self):
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            attempts INTEGER DEFAULT 0,
            last_attempt TIMESTAMP,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS subs (
            user_id INTEGER PRIMARY KEY,
            plan TEXT,
            expires TIMESTAMP,
            payment_method TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS transactions (
            tx_id TEXT PRIMARY KEY,
            user_id INTEGER,
            payment_method TEXT,
            amount REAL,
            plan TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verified_by TEXT
        )''')
        self.conn.commit()
        logger.info("✅ تم تهيئة قاعدة البيانات")

    def get_or_create_user(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
        if not c.fetchone():
            c.execute('INSERT INTO users (user_id, joined_at) VALUES (?, datetime("now"))', (user_id,))
            self.conn.commit()
        return True

    def get_sub(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT * FROM subs WHERE user_id = ? AND expires > datetime("now")', (user_id,))
        return c.fetchone()

    def add_sub(self, user_id, plan, days, payment_method):
        c = self.conn.cursor()
        existing = self.get_sub(user_id)
        if existing:
            current_expires = datetime.strptime(existing['expires'], '%Y-%m-%d %H:%M:%S.%f')
            new_expires = max(current_expires, datetime.now()) + timedelta(days=days)
            c.execute('UPDATE subs SET plan = ?, expires = ?, payment_method = ? WHERE user_id = ?',
                      (plan, new_expires, payment_method, user_id))
        else:
            expires = datetime.now() + timedelta(days=days)
            c.execute('INSERT INTO subs VALUES (?, ?, ?, ?)',
                      (user_id, plan, expires, payment_method))
        self.conn.commit()
        logger.info(f"✅ اشتراك جديد: {user_id} - {plan} - {days} يوم")

    def add_tx(self, tx_id, user_id, payment_method, amount, plan, verified_by=None):
        c = self.conn.cursor()
        c.execute('''INSERT INTO transactions (tx_id, user_id, payment_method, amount, plan, verified_by) 
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  (tx_id, user_id, payment_method, amount, plan, verified_by))
        self.conn.commit()

    def tx_exists(self, tx_id):
        c = self.conn.cursor()
        c.execute('SELECT 1 FROM transactions WHERE tx_id = ?', (tx_id,))
        return c.fetchone() is not None

    def get_attempts(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT attempts, last_attempt FROM users WHERE user_id = ?', (user_id,))
        return c.fetchone()

    def inc_attempts(self, user_id):
        c = self.conn.cursor()
        c.execute('''INSERT INTO users (user_id, attempts, last_attempt) 
                     VALUES (?, 1, datetime("now")) 
                     ON CONFLICT(user_id) DO UPDATE 
                     SET attempts = attempts + 1, last_attempt = datetime("now")''', (user_id,))
        self.conn.commit()

    def reset_attempts(self, user_id):
        c = self.conn.cursor()
        c.execute('UPDATE users SET attempts = 0 WHERE user_id = ?', (user_id,))
        self.conn.commit()

    def get_all_users(self):
        c = self.conn.cursor()
        c.execute('SELECT user_id FROM users')
        return [row['user_id'] for row in c.fetchall()]

    def get_stats(self):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) as total FROM users')
        total = c.fetchone()['total']
        c.execute('SELECT COUNT(*) as active FROM subs WHERE expires > datetime("now")')
        active = c.fetchone()['active']
        return total, active

db = Database()

# ==================== الباقات ====================
PLANS = {
    "⭐ المبدئية": {
        "usd": 2.99,
        "sdg": int(2.99 * USD_TO_SDG_RATE),
        "days": 30,
        "description": """
**المبدئية – ما تحصل عليه**

• **توقعات 14 يوماً** – بدلاً من 3 أيام فقط في المجاني
• **30 سؤالاً للمساعد الذكي كل 48 ساعة**
• **إنذار مطر مبكر** – ينبهك قبل وقوع المطر بساعات
• **رادار الأمطار الحي** – احتمالية ساعة بساعة لـ 48 ساعة
• **مؤشر الرياح الكامل** – السرعة + الاتجاه + الهبات
• **كاشف الغبار والأتربة**
• **جودة الهواء (AQI)**
• **مؤشر UV اليومي**
• **بدون إعلانات**
"""
    },
    "🌙 الشهرية": {
        "usd": 4.99,
        "sdg": int(4.99 * USD_TO_SDG_RATE),
        "days": 30,
        "description": """
**الشهرية – ما تحصل عليه**

• **50 سؤالاً يومياً للمساعد الذكي**
• **تنبيه السحب الركامية (Cb)**
• **محرك الغبار الذكي** – 4 مستويات دقيقة
• **مؤشر الحر الشديد (Heat Index) + نقطة الندى**
• **مقارنة الطقس بين مدن السودان**
• **تحليل جودة الهواء الكامل**
• **توقعات 14 يوماً** + جميع تنبيهات الخطة المبدئية
• **بدون إعلانات**
"""
    },
    "👑 السنوية": {
        "usd": 49.00,
        "sdg": int(49.00 * USD_TO_SDG_RATE),
        "days": 365,
        "description": """
**السنوية – ما تحصل عليه**

• **100 سؤال يومياً للمساعد الذكي**
• **5 محركات تحليل جوي متقدمة**
• **Nowcasting الفوري**
• **محرك السحب الركامية (Cb) لحظة بلحظة**
• **كاشف الرياح الهاطبة (Downburst)**
• **محرك الهباب الذكي**
• **تحليل ITCZ الكامل**
• **مقارنة المدن + مؤشر SWCI**
• **تحليل ATI البيومناخي**
• **وفر $10.88 سنوياً**
"""
    }
}

# ==================== دوال مساعدة ====================
def is_subscribed(user_id):
    return db.get_sub(user_id) is not None

def whatsapp_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💬 تواصل مع المطور", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
    return markup

def support_keyboard(include_back=False, back_callback="back_to_start"):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("💬 تواصل مع المطور", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
    if include_back:
        markup.add(types.InlineKeyboardButton("« رجوع", callback_data=back_callback))
    return markup

# ==================== OxaPay ====================
def create_oxapay_invoice(amount_usd, plan_name, user_id):
    payload = {
        'merchant': OXAPAY_KEY,
        'amount': amount_usd,
        'currency': 'USD',
        'lifeTime': 60,
        'description': f"Subscription: {plan_name}",
        'orderId': f"USER_{user_id}_{int(time.time())}",
        'returnUrl': 'https://t.me/SudanWeatherBot'
    }
    headers = {'Content-Type': 'application/json'}
    try:
        response = requests.post(OXAPAY_CREATE_URL, json=payload, headers=headers, timeout=15)
        data = response.json()
        if data.get('result') == 100:
            return {'success': True, 'pay_url': data.get('payLink'), 'track_id': data.get('trackId')}
        return {'success': False, 'error': data.get('message')}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def check_oxapay_payment(track_id):
    try:
        response = requests.get(f"{OXAPAY_INQUIRY_URL}?trackId={track_id}", timeout=10)
        data = response.json()
        if data.get('result') == 100:
            return {'success': True, 'status': data.get('status')}
        return {'success': False}
    except:
        return {'success': False}

# ==================== تحليل الصور (Groq Vision) ====================
def _call_groq_vision(prompt: str, image_base64: str):
    if not GROQ_API_KEY:
        return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.2-11b-vision-preview",
        "messages": [{"role": "user", "content": [
            {"type": "text",      "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]}],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 512
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=35)
        raw = r.json()['choices'][0]['message']['content']
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Groq Vision error: {e}")
        return None

def analyze_bank_receipt(image_base64):
    prompt = f"""
أنت محقق مالي. حلل إيصال بنكك. رقم الحساب المستلم: {MY_ACCOUNT}.
استخرج: المبلغ (SDG)، رقم العملية، التاريخ والوقت.
تأكد أن العملية ناجحة وغير معدلة.
رد JSON: {{"valid": bool, "account_match": bool, "amount": float, "tx_id": str, "datetime": str, "status_success": bool, "tampering_detected": bool, "errors": []}}
"""
    return _call_groq_vision(prompt, image_base64)

def analyze_fawry_receipt(image_base64):
    prompt = f"فوري. رقم حساب فوري: {FAWRY_NUMBER} ({FAWRY_NAME}). JSON فقط."
    return _call_groq_vision(prompt, image_base64)

def analyze_bravo_receipt(image_base64):
    prompt = f"برافو. رقم المحفظة: {BRAVO_NUMBER} ({BRAVO_NAME}). JSON فقط."
    return _call_groq_vision(prompt, image_base64)

def analyze_mycash_receipt(image_base64):
    prompt = f"ماي كاشي. رقم المحفظة: {MYCASH_NUMBER} ({MYCASH_NAME}). JSON فقط."
    return _call_groq_vision(prompt, image_base64)

def detect_payment_method(image_base64):
    prompt = """حدد التطبيق: بنكك, فوري, برافو, ماي كاشي, unknown. JSON: {"method": "...", "confidence": "..."}"""
    r = _call_groq_vision(prompt, image_base64)
    if r:
        return r.get('method', 'unknown'), r.get('confidence', 'low')
    return 'unknown', 'low'

def match_plan(amount):
    for name, info in PLANS.items():
        if abs(amount - info['sdg']) <= info['sdg'] * 0.05:
            return name
    return None

# ==================== أوامر البوت ====================
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    db.get_or_create_user(user_id)

    sub = db.get_sub(user_id)
    if sub:
        days_left = (datetime.strptime(sub['expires'], '%Y-%m-%d %H:%M:%S.%f') - datetime.now()).days
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔄 تجديد الاشتراك", callback_data="renew"))
        markup.add(types.InlineKeyboardButton("💬 تواصل مع المطور", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
        bot.send_message(message.chat.id,
            f"✅ **حسابك مفعل!**\n\n💎 الباقة: {sub['plan']}\n💳 الدفع: {sub['payment_method']}\n⏳ المتبقي: {days_left} يوم",
            reply_markup=markup, parse_mode="Markdown")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for plan, info in PLANS.items():
        markup.add(types.InlineKeyboardButton(
            f"{plan} - {info['sdg']:,} SDG (${info['usd']})",
            callback_data=f"plan_{plan}"
        ))
    markup.add(types.InlineKeyboardButton("💬 تواصل مع المطور", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))

    welcome = """
🌟 **مرحباً بك في طقس السودان – بوت الاشتراكات** ⛈️

اختر باقتك للاستمتاع بالتوقعات الدقيقة والمزايا الحصرية:
"""
    bot.send_message(message.chat.id, welcome, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "renew")
def renew_subscription(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for plan, info in PLANS.items():
        markup.add(types.InlineKeyboardButton(
            f"{plan} - {info['sdg']:,} SDG (${info['usd']})",
            callback_data=f"plan_{plan}"
        ))
    markup.add(types.InlineKeyboardButton("« رجوع", callback_data="back_to_start"))
    bot.edit_message_text("🔄 **تجديد الاشتراك**\n\nاختر باقتك:", call.message.chat.id, call.message.message_id,
                          reply_markup=markup, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_start")
def back_to_start(call):
    call.message.text = "/start"
    start(call.message)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("plan_"))
def select_plan(call):
    plan_name = call.data.replace("plan_", "")
    info = PLANS[plan_name]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💳 دفع بالعملات الرقمية (OxaPay)", callback_data=f"crypto_{plan_name}"),
        types.InlineKeyboardButton("🏦 تحويل بنكي (بنكك)", callback_data=f"bank_{plan_name}"),
        types.InlineKeyboardButton("💳 فوري (بنك فيصل)", callback_data=f"fawry_{plan_name}"),
        types.InlineKeyboardButton("📱 برافو", callback_data=f"bravo_{plan_name}"),
        types.InlineKeyboardButton("💰 ماي كاشي", callback_data=f"mycash_{plan_name}"),
        types.InlineKeyboardButton("« رجوع", callback_data="renew")
    )
    text = f"""
**{plan_name}**

💰 السعر: **${info['usd']}** / **{info['sdg']:,} SDG**
📅 المدة: **{info['days']} يوم**

{info['description']}

اختر طريقة الدفع:
"""
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                          reply_markup=markup, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

# دوال الدفع الموحدة
def create_payment_method_handler(method_name, method_display, account_info):
    def handler(call):
        plan_name = call.data.replace(f"{method_name}_", "")
        amount = PLANS[plan_name]['sdg']
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}"))
        markup.add(types.InlineKeyboardButton("💬 تواصل واتساب", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
        msg = f"""
{method_display} **- {plan_name}**

1️⃣ قم بتحويل **{amount:,} SDG** إلى:

{account_info}

2️⃣ بعد التحويل، أرسل لقطة الشاشة هنا

⚠️ **تنبيهات هامة:**
• الصورة واضحة
• يظهر **رقم العملية** بوضوح
• يظهر **تاريخ ووقت التحويل**
• الصورة غير معدلة
"""
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id,
                              reply_markup=markup, parse_mode="Markdown")
        bot.answer_callback_query(call.id)
    return handler

bank_info = f"🏛 **بنك الخرطوم - تطبيق بنكك**\n📱 رقم الحساب: `{MY_ACCOUNT}`"
fawry_info = f"🏛 **بنك فيصل الإسلامي - تطبيق فوري**\n💳 رقم حساب فوري: `{FAWRY_NUMBER}`\n👤 الاسم: {FAWRY_NAME}"
bravo_info = f"📱 **محفظة برافو**\n📞 رقم المحفظة: `{BRAVO_NUMBER}`\n👤 الاسم: {BRAVO_NAME}"
mycash_info = f"💰 **محفظة ماي كاشي**\n📞 رقم المحفظة: `{MYCASH_NUMBER}`\n👤 الاسم: {MYCASH_NAME}"

payment_methods = [
    ('bank', '🏦 تحويل بنكي', bank_info),
    ('fawry', '💳 فوري', fawry_info),
    ('bravo', '📱 برافو', bravo_info),
    ('mycash', '💰 ماي كاشي', mycash_info)
]

for method, display, account in payment_methods:
    bot.callback_query_handler(func=lambda call, m=method: call.data.startswith(f"{m}_"))(create_payment_method_handler(method, display, account))

@bot.callback_query_handler(func=lambda call: call.data.startswith("crypto_"))
def pay_crypto(call):
    plan_name = call.data.replace("crypto_", "")
    user_id = call.from_user.id
    amount_usd = PLANS[plan_name]['usd']
    amount_sdg = PLANS[plan_name]['sdg']

    bot.edit_message_text("🔄 جاري إنشاء فاتورة OxaPay...", call.message.chat.id, call.message.message_id)
    result = create_oxapay_invoice(amount_usd, plan_name, user_id)

    if result['success']:
        pay_url = result['pay_url']
        track_id = result['track_id']
        db.add_tx(track_id, user_id, 'OxaPay', amount_usd, plan_name)

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("💳 ادفع الآن عبر OxaPay", url=pay_url))
        markup.add(types.InlineKeyboardButton("🔄 تحقق من الدفع", callback_data=f"check_{track_id}_{plan_name}"))
        markup.add(types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}"))

        msg = f"""
✅ **تم إنشاء الفاتورة بنجاح**

💎 الباقة: {plan_name}
💰 المبلغ: ${amount_usd} ({amount_sdg:,} SDG)
🆔 رقم التتبع: `{track_id}`

**الخطوات:**
1️⃣ اضغط "ادفع الآن"
2️⃣ أكمل الدفع
3️⃣ عد للبوت واضغط "تحقق من الدفع"

⏰ الفاتورة صالحة 60 دقيقة
"""
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id,
                              reply_markup=markup, parse_mode="Markdown")
        bot.send_message(ADMIN_ID, f"📢 **فاتورة جديدة**\n👤 {user_id}\n💎 {plan_name}\n💰 ${amount_usd}\n🆔 {track_id}")
    else:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("🏦 تحويل بنكي", callback_data=f"bank_{plan_name}"))
        markup.add(types.InlineKeyboardButton("💬 تواصل واتساب", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
        markup.add(types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}"))
        bot.edit_message_text(
            f"⚠️ **تعذر إنشاء فاتورة**\n\nيمكنك استخدام طرق الدفع الأخرى.",
            call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode="Markdown"
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_"))
def check_payment(call):
    parts = call.data.replace("check_", "").split("_")
    track_id = parts[0]
    plan_name = "_".join(parts[1:])
    user_id = call.from_user.id

    bot.answer_callback_query(call.id, "🔄 جاري التحقق...")
    result = check_oxapay_payment(track_id)

    if result['success'] and result['status'] == 'Paid':
        days = PLANS[plan_name]['days']
        db.add_sub(user_id, plan_name, days, 'OxaPay')
        db.reset_attempts(user_id)

        expires = datetime.now() + timedelta(days=days)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💬 تواصل مع المطور", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))

        bot.edit_message_text(
            f"🎉 **تم الدفع بنجاح!**\n\n💎 الباقة: {plan_name}\n📅 صالحة حتى: {expires.strftime('%Y-%m-%d')}\n\nشكراً لاشتراكك! 🌟",
            call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode="Markdown"
        )
        bot.send_message(ADMIN_ID, f"✅ **دفع ناجح**\n👤 {user_id}\n💎 {plan_name}\n🆔 {track_id}")
    else:
        pay_url = f"https://oxapay.com/payment/{track_id}"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("💳 ادفع الآن", url=pay_url))
        markup.add(types.InlineKeyboardButton("🔄 تحقق مجدداً", callback_data=f"check_{track_id}_{plan_name}"))
        markup.add(types.InlineKeyboardButton("💬 تواصل واتساب", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
        markup.add(types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}"))
        bot.edit_message_text("⏳ **لم يتم تأكيد الدفع بعد**", call.message.chat.id, call.message.message_id,
                              reply_markup=markup, parse_mode="Markdown")

# ==================== معالجة الصور (الإيصالات) ====================
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = message.from_user.id
    attempts = db.get_attempts(user_id)
    if attempts and attempts[0] >= 5:
        markup = whatsapp_keyboard()
        bot.reply_to(message, "⛔ تجاوزت الحد الأقصى للمحاولات.\n\nللحصول على مساعدة، تواصل مع المطور:", reply_markup=markup)
        return

    wait = bot.reply_to(message, "🔍 جاري فحص الإشعار بدقة... (قد تستغرق العملية دقيقة)")
    
    try:
        file = bot.get_file(message.photo[-1].file_id)
        img_data = bot.download_file(file.file_path)
        img_b64 = base64.b64encode(img_data).decode('utf-8')
        
        method_id, confidence = detect_payment_method(img_b64)
        
        ANALYZE_MAP = {
            'bankak': ('بنكك',       analyze_bank_receipt),
            'fawry':  ('فوري',       analyze_fawry_receipt),
            'bravo':  ('برافو',      analyze_bravo_receipt),
            'mycash': ('ماي كاشي',   analyze_mycash_receipt),
        }
        
        result = None
        payment_method = None

        if method_id in ANALYZE_MAP and confidence in ('high', 'medium'):
            payment_method, analyze_fn = ANALYZE_MAP[method_id]
            result = analyze_fn(img_b64)
        else:
            for mid, (pm, fn) in ANALYZE_MAP.items():
                result = fn(img_b64)
                if result and result.get('account_match'):
                    payment_method = pm
                    break

        db.inc_attempts(user_id)

        if result and result.get('tampering_detected'):
            markup = support_keyboard()
            bot.edit_message_text("⛔ **تم رفض الإيصال**\n\n🔍 مؤشرات تعديل على الصورة.\n\nللتواصل مع المطور:",
                                  message.chat.id, wait.message_id, reply_markup=markup, parse_mode="Markdown")
            bot.send_message(ADMIN_ID, f"⚠️ **محاولة تزوير**\n👤 {user_id}")
            return

        if result and not result.get('status_success', True):
            markup = support_keyboard()
            bot.edit_message_text("❌ **الإيصال يظهر عملية غير ناجحة**",
                                  message.chat.id, wait.message_id, reply_markup=markup, parse_mode="Markdown")
            return

        if result and result.get('valid') and result.get('account_match'):
            amount = float(result.get('amount', 0))
            tx_id = result.get('tx_id', f"TX_{user_id}_{int(time.time())}")
            tx_datetime = result.get('datetime', 'غير معروف')
            
            if db.tx_exists(tx_id):
                markup = whatsapp_keyboard()
                bot.edit_message_text(f"❌ **رقم العملية مستخدم مسبقاً**\n\n`{tx_id}`",
                                      message.chat.id, wait.message_id, reply_markup=markup, parse_mode="Markdown")
                return

            plan_name = match_plan(amount)
            if plan_name:
                db.add_tx(tx_id, user_id, payment_method, amount, plan_name, verified_by="AI")
                db.add_sub(user_id, plan_name, PLANS[plan_name]['days'], payment_method)
                db.reset_attempts(user_id)
                expires = datetime.now() + timedelta(days=PLANS[plan_name]['days'])
                markup = whatsapp_keyboard()
                bot.edit_message_text(
                    f"✅ **تم التفعيل بنجاح!**\n\n💎 {plan_name}\n💰 {amount:,.0f} SDG\n💳 {payment_method}\n🔢 `{tx_id}`\n📅 {tx_datetime}\n📆 {expires.strftime('%Y-%m-%d')}",
                    message.chat.id, wait.message_id, reply_markup=markup, parse_mode="Markdown"
                )
                bot.send_message(ADMIN_ID, f"✅ **تفعيل جديد**\n👤 {user_id}\n💎 {plan_name}\n💰 {amount:,.0f} SDG\n💳 {payment_method}\n🔢 {tx_id}")
            else:
                expected = "\n".join([f"• {n}: {i['sdg']:,} SDG" for n, i in PLANS.items()])
                markup = support_keyboard()
                bot.edit_message_text(f"⚠️ **المبلغ غير مطابق**\n\nالمبلغ: {amount:,.0f} SDG\n\nالمبالغ المطلوبة:\n{expected}",
                                      message.chat.id, wait.message_id, reply_markup=markup, parse_mode="Markdown")
        else:
            accounts_info = f"**الأرقام المعتمدة:**\n• بنكك: `{MY_ACCOUNT}`\n• فوري: `{FAWRY_NUMBER}`\n• برافو: `{BRAVO_NUMBER}`\n• ماي كاشي: `{MYCASH_NUMBER}`"
            markup = support_keyboard()
            bot.edit_message_text(f"❌ **رفض الإشعار**\n\n{accounts_info}\n\nللتواصل مع المطور:",
                                  message.chat.id, wait.message_id, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Photo error: {e}")
        markup = whatsapp_keyboard()
        bot.edit_message_text("❌ حدث خطأ تقني. تواصل مع المطور:", message.chat.id, wait.message_id, reply_markup=markup)

# ==================== لوحة تحكم الأدمن ====================
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        return
    total, active = db.get_stats()
    bot.reply_to(message,
        f"📊 **لوحة التحكم**\n\n👥 إجمالي المستخدمين: {total}\n✅ المشتركون النشطون: {active}\n\n"
        f"/broadcast [رسالة] - إرسال للجميع\n/activate [user_id] [plan_name] - تفعيل يدوي\n/stats - إحصائيات",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['stats'])
def detailed_stats(message):
    if message.from_user.id != ADMIN_ID:
        return
    c = db.conn.cursor()
    c.execute('SELECT COUNT(*) as total FROM transactions')
    total_tx = c.fetchone()['total']
    c.execute('SELECT payment_method, COUNT(*) as count, SUM(amount) as total_amount FROM transactions GROUP BY payment_method')
    methods = c.fetchall()
    text = f"📊 **إحصائيات**\n\n📦 إجمالي المعاملات: {total_tx}\n\n**طرق الدفع:**\n"
    for m in methods:
        text += f"• {m['payment_method']}: {m['count']} معاملة - {m['total_amount']:,.0f} SDG\n"
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        text = message.text.split(maxsplit=1)[1]
    except:
        bot.reply_to(message, "استخدم: /broadcast رسالتك هنا")
        return
    users = db.get_all_users()
    success = 0
    for uid in users:
        try:
            bot.send_message(uid, text)
            success += 1
        except:
            pass
    bot.reply_to(message, f"✅ تم الإرسال إلى {success} مستخدم.")

@bot.message_handler(commands=['activate'])
def admin_activate(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split()
        user_id = int(parts[1])
        plan_name = " ".join(parts[2:])
        if plan_name not in PLANS:
            bot.reply_to(message, "الباقة غير موجودة")
            return
        db.add_sub(user_id, plan_name, PLANS[plan_name]['days'], 'تفعيل يدوي')
        db.reset_attempts(user_id)
        bot.reply_to(message, f"✅ تم تفعيل {user_id} - {plan_name}")
        bot.send_message(user_id, f"🎉 **تم تفعيل اشتراكك!**\n💎 {plan_name}", parse_mode="Markdown")
    except:
        bot.reply_to(message, "استخدام: /activate [user_id] [plan_name]")

# ==================== تشغيل البوت ====================
print("=" * 50)
print("✅ بوت اشتراكات طقس السودان")
print(f"💳 OxaPay: جاهز")
print(f"🏦 بنكك: {MY_ACCOUNT}")
print(f"💳 فوري: {FAWRY_NUMBER} - {FAWRY_NAME}")
print(f"📱 برافو: {BRAVO_NUMBER} - {BRAVO_NAME}")
print(f"💰 ماي كاشي: {MYCASH_NUMBER} - {MYCASH_NAME}")
print("=" * 50)

keep_alive()

while True:
    try:
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as e:
        logger.error(f"Polling error: {e}")
        time.sleep(15)
