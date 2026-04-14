import telebot
import requests
import base64
import json
import re
import time
import sqlite3
import threading
import logging
import os
import hashlib
from datetime import datetime, timedelta
from telebot import types
from flask import Flask
from threading import Thread

# ==================== الإعدادات (متغيرات البيئة) ====================
TOKEN = os.environ.get("BOT_TOKEN", "8665720382:AAEzrjTSqC5Gt5QXXu-gWfYu-vkUodOfwGw")
OXAPAY_KEY = os.environ.get("OXAPAY_KEY", "LYMACY-HJVRXA-D02BTO-AHUK8R")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")  # مفتاح Groq API في متغير البيئة
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8188643525"))
MY_ACCOUNT = os.environ.get("BANK_ACCOUNT", "4636998")
USD_TO_SDG_RATE = int(os.environ.get("USD_TO_SDG_RATE", "3600"))
DEVELOPER_WHATSAPP = os.environ.get("DEV_WHATSAPP", "249901758765")

# أرقام المحافظ الإلكترونية (بدون فودافون كاش)
FAWRY_NUMBER = os.environ.get("FAWRY_NUMBER", "0123456789")
BRAVO_NUMBER = os.environ.get("BRAVO_NUMBER", "0123456789")
MYCASH_NUMBER = os.environ.get("MYCASH_NUMBER", "0123456789")

OXAPAY_CREATE_URL = 'https://api.oxapay.com/merchants/request'
OXAPAY_INQUIRY_URL = 'https://api.oxapay.com/merchants/inquiry'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY غير مضبوط. ميزة تحليل الصور معطلة.")
if not OPENWEATHER_API_KEY:
    logger.warning("OPENWEATHER_API_KEY غير مضبوط. ميزة الطقس المباشر معطلة.")

# ==================== حل مشكلة تعدد النسخ ====================
# إزالة webhook قبل البدء
try:
    bot = telebot.TeleBot(TOKEN, threaded=False)
    bot.remove_webhook()
    time.sleep(0.5)
    logger.info("✅ تم إزالة webhook بنجاح")
except Exception as e:
    logger.error(f"خطأ في إزالة webhook: {e}")

# ==================== Flask ====================
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
            referral_code TEXT UNIQUE,
            referred_by INTEGER,
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
        
        c.execute('''CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            rewarded BOOLEAN DEFAULT 0,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            daily_weather_notify BOOLEAN DEFAULT 0,
            notify_city TEXT DEFAULT 'Khartoum'
        )''')
        
        self.conn.commit()
        logger.info("✅ تم تهيئة قاعدة البيانات بنجاح")

    def get_or_create_user(self, user_id, referrer_id=None):
        c = self.conn.cursor()
        c.execute('SELECT referral_code FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        if not row:
            code = hashlib.md5(f"{user_id}{time.time()}".encode()).hexdigest()[:8].upper()
            c.execute('''INSERT INTO users (user_id, referral_code, referred_by, joined_at) 
                         VALUES (?, ?, ?, datetime('now'))''', 
                      (user_id, code, referrer_id))
            self.conn.commit()
            if referrer_id:
                c.execute('INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)',
                          (referrer_id, user_id))
                self.conn.commit()
                logger.info(f"👥 إحالة جديدة: {referrer_id} دعا {user_id}")
            return code
        return row['referral_code']

    def add_referral_reward(self, referrer_id):
        c = self.conn.cursor()
        c.execute('SELECT expires FROM subs WHERE user_id = ? AND expires > datetime("now")',
                  (referrer_id,))
        sub = c.fetchone()
        if sub:
            current_expires = datetime.strptime(sub['expires'], '%Y-%m-%d %H:%M:%S.%f')
            new_expires = current_expires + timedelta(days=7)
            c.execute('UPDATE subs SET expires = ? WHERE user_id = ?',
                      (new_expires, referrer_id))
            self.conn.commit()
            return True
        return False

    def get_referral_stats(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) as count FROM referrals WHERE referrer_id = ?', (user_id,))
        count = c.fetchone()['count']
        c.execute('SELECT referral_code FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        return count, row['referral_code'] if row else None

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

    def get_settings(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT daily_weather_notify, notify_city FROM user_settings WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        if not row:
            c.execute('INSERT INTO user_settings (user_id) VALUES (?)', (user_id,))
            self.conn.commit()
            return False, 'Khartoum'
        return bool(row['daily_weather_notify']), row['notify_city']

    def set_daily_notify(self, user_id, enabled, city='Khartoum'):
        c = self.conn.cursor()
        c.execute('''INSERT INTO user_settings (user_id, daily_weather_notify, notify_city) 
                     VALUES (?, ?, ?) 
                     ON CONFLICT(user_id) DO UPDATE 
                     SET daily_weather_notify = ?, notify_city = ?''',
                  (user_id, enabled, city, enabled, city))
        self.conn.commit()

    def get_stats(self):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) as total FROM users')
        total_users = c.fetchone()['total']
        c.execute('SELECT COUNT(*) as active FROM subs WHERE expires > datetime("now")')
        active_subs = c.fetchone()['active']
        return total_users, active_subs

    def get_all_users(self):
        c = self.conn.cursor()
        c.execute('SELECT user_id FROM users')
        return [row['user_id'] for row in c.fetchall()]

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
• **30 سؤالاً للمساعد الذكي كل 48 ساعة** – 6 أضعاف المجاني
• **إنذار مطر مبكر** – ينبهك قبل وقوع المطر بساعات
• **رادار الأمطار الحي** – احتمالية ساعة بساعة لـ 48 ساعة
• **مؤشر الرياح الكامل** – السرعة + الاتجاه + الهبات
• **كاشف الغبار والأتربة** – هل هو غبار عالق أم عاصفة؟
• **جودة الهواء (AQI)** – حماية صحتك يومياً
• **مؤشر UV اليومي** + توصية الحماية من الشمس
• **بدون إعلانات** – تجربة نظيفة تماماً
"""
    },
    "🌙 الشهرية": {
        "usd": 4.99,
        "sdg": int(4.99 * USD_TO_SDG_RATE),
        "days": 30,
        "description": """
**الشهرية – ما تحصل عليه**

• **50 سؤالاً يومياً للمساعد الذكي** – 10 أضعاف المجاني
• **تنبيه السحب الركامية (Cb)** – يحذرك من العواصف الرعدية قبل تشكلها
• **محرك الغبار الذكي** – 4 مستويات دقيقة: عالق – عجاج – عاصفة ترابية – هبوب
• **توقع موسم الخريف** – 5 مؤشرات مناخية + رسم بياني تفاعلي
• **محرك ITCZ** – موقع الفاصل المداري يومياً (مفتاح أمطار السودان)
• **مؤشر الحر الشديد (Heat Index) + نقطة الندى** – حماية من الإجهاد الحراري
• **مقارنة الطقس بين مدن السودان** – جداول + رسوم بيانية
• **تحليل جودة الهواء الكامل** – AQI + PM2.5 + تأثير صحي مفصل
• **توقعات 14 يوماً** + جميع تنبيهات الخطة المبدئية
• **بدون إعلانات** – تجربة نظيفة تماماً
"""
    },
    "👑 السنوية": {
        "usd": 49.00,
        "sdg": int(49.00 * USD_TO_SDG_RATE),
        "days": 365,
        "description": """
**السنوية – ما تحصل عليه**

• **100 سؤال يومياً للمساعد الذكي** – اسأل بلا حدود
• **5 محركات تحليل جوي متقدمة (Physio‑Intelligence)** – لا مثيل لها
• **Nowcasting الفوري** – يتنبأ بالعواصف بالدقائق لا بالساعات
• **محرك السحب الركامية (Cb)** – خريطة تطور العاصفة الرعدية لحظة بلحظة
• **كاشف الرياح الهاطبة (Downburst)** – تحذير من الخطر الأشد قبل وقوعه
• **محرك الهباب الذكي** – يميز بدقة: غبار عالق / عجاج / عاصفة / هبوب
• **تحليل ITCZ الكامل** – 5 مؤشرات موسمية + موقع الفاصل المداري يومياً
• **توقع موسم الخريف** + سجل مطري تاريخي لـ 16 مدينة سودانية
• **مقارنة المدن** + مؤشر SWCI الحصري للطقس السوداني
• **تحليل ATI البيومناخي** – أثر الطقس على صحتك بشكل علمي
• **وفر $10.88 سنوياً** – أقل من $4.1 شهرياً مقارنةً بالشهرية
"""
    }
}

# ==================== دوال مساعدة ====================
def is_subscribed(user_id):
    return db.get_sub(user_id) is not None

def get_weather_forecast(city):
    if not OPENWEATHER_API_KEY:
        return None, "⚠️ مفتاح OpenWeather غير مضبوط."
    url = f"http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ar&cnt=7"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get('cod') != "200":
            return None, data.get('message', 'خطأ غير معروف')
        
        forecasts = []
        for item in data['list'][:7]:
            forecasts.append({
                'date': item['dt_txt'],
                'temp': item['main']['temp'],
                'description': item['weather'][0]['description'],
                'humidity': item['main']['humidity']
            })
        
        return {
            'city': data['city']['name'],
            'forecasts': forecasts
        }, None
    except Exception as e:
        return None, str(e)

def whatsapp_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "💬 تواصل مع المطور عبر واتساب",
        url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
    ))
    return markup

def support_keyboard(include_back=False, back_callback="back_to_start"):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        "💬 تواصل مع المطور عبر واتساب",
        url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
    ))
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
        else:
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

# ==================== تحليل الصور (Prompts منفصلة لكل طريقة دفع) ====================
def analyze_bank_receipt(image_base64):
    """تحليل إيصال التحويل البنكي (بنكك)"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
    حلل صورة إشعار تحويل بنكي من تطبيق بنكك واستخرج بدقة:
    
    المعلومات المطلوبة:
    1. رقم الحساب المستلم (يجب أن يكون: {MY_ACCOUNT})
    2. المبلغ المحول بالجنيه السوداني (رقم فقط)
    3. رقم العملية/الإشعار (رقم المرجع)
    4. تاريخ ووقت التحويل (بصيغة YYYY-MM-DD HH:MM)
    
    قواعد التحقق الصارمة:
    - يجب أن يكون رقم الحساب المستلم مطابقاً تماماً لـ {MY_ACCOUNT}
    - يجب أن يكون المبلغ رقماً واضحاً
    - يجب أن يكون رقم العملية موجوداً وغير مستخدم مسبقاً
    - يجب أن يكون التاريخ والوقت حديثين (خلال 24 ساعة)
    
    رد بصيغة JSON فقط:
    {{"valid": true/false, "account_match": true/false, "amount": 0, "tx_id": "", "datetime": "", "errors": []}}
    """
    payload = {
        "model": "llama-3.2-11b-vision-preview",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        return json.loads(response.json()['choices'][0]['message']['content'])
    except:
        return None

def analyze_fawry_receipt(image_base64):
    """تحليل إيصال فوري"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
    حلل صورة إشعار تحويل فوري واستخرج بدقة:
    
    المعلومات المطلوبة:
    1. رقم الهاتف المستلم (يجب أن يكون: {FAWRY_NUMBER})
    2. المبلغ المحول بالجنيه السوداني (رقم فقط)
    3. رقم العملية/الإشعار (رقم المرجع)
    4. تاريخ ووقت التحويل (بصيغة YYYY-MM-DD HH:MM)
    
    قواعد التحقق الصارمة:
    - يجب أن يكون رقم الهاتف المستلم مطابقاً تماماً لـ {FAWRY_NUMBER}
    - يجب أن يكون المبلغ رقماً واضحاً
    - يجب أن يكون رقم العملية موجوداً وغير مستخدم مسبقاً
    - يجب أن يكون التاريخ والوقت حديثين (خلال 24 ساعة)
    - صورة فوري تكون من تطبيق فوري أو رسالة تأكيد
    
    رد بصيغة JSON فقط:
    {{"valid": true/false, "account_match": true/false, "amount": 0, "tx_id": "", "datetime": "", "errors": []}}
    """
    payload = {
        "model": "llama-3.2-11b-vision-preview",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        return json.loads(response.json()['choices'][0]['message']['content'])
    except:
        return None

def analyze_bravo_receipt(image_base64):
    """تحليل إيصال برافو"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
    حلل صورة إشعار تحويل برافو واستخرج بدقة:
    
    المعلومات المطلوبة:
    1. رقم الهاتف المستلم (يجب أن يكون: {BRAVO_NUMBER})
    2. المبلغ المحول بالجنيه السوداني (رقم فقط)
    3. رقم العملية/الإشعار (رقم المرجع)
    4. تاريخ ووقت التحويل (بصيغة YYYY-MM-DD HH:MM)
    
    قواعد التحقق الصارمة:
    - يجب أن يكون رقم الهاتف المستلم مطابقاً تماماً لـ {BRAVO_NUMBER}
    - يجب أن يكون المبلغ رقماً واضحاً
    - يجب أن يكون رقم العملية موجوداً وغير مستخدم مسبقاً
    - يجب أن يكون التاريخ والوقت حديثين (خلال 24 ساعة)
    - صورة برافو تكون من تطبيق برافو أو رسالة تأكيد
    
    رد بصيغة JSON فقط:
    {{"valid": true/false, "account_match": true/false, "amount": 0, "tx_id": "", "datetime": "", "errors": []}}
    """
    payload = {
        "model": "llama-3.2-11b-vision-preview",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        return json.loads(response.json()['choices'][0]['message']['content'])
    except:
        return None

def analyze_mycash_receipt(image_base64):
    """تحليل إيصال ماي كاشي"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
    حلل صورة إشعار تحويل ماي كاشي واستخرج بدقة:
    
    المعلومات المطلوبة:
    1. رقم الهاتف المستلم (يجب أن يكون: {MYCASH_NUMBER})
    2. المبلغ المحول بالجنيه السوداني (رقم فقط)
    3. رقم العملية/الإشعار (رقم المرجع)
    4. تاريخ ووقت التحويل (بصيغة YYYY-MM-DD HH:MM)
    
    قواعد التحقق الصارمة:
    - يجب أن يكون رقم الهاتف المستلم مطابقاً تماماً لـ {MYCASH_NUMBER}
    - يجب أن يكون المبلغ رقماً واضحاً
    - يجب أن يكون رقم العملية موجوداً وغير مستخدم مسبقاً
    - يجب أن يكون التاريخ والوقت حديثين (خلال 24 ساعة)
    - صورة ماي كاشي تكون من تطبيق ماي كاشي أو رسالة تأكيد
    
    رد بصيغة JSON فقط:
    {{"valid": true/false, "account_match": true/false, "amount": 0, "tx_id": "", "datetime": "", "errors": []}}
    """
    payload = {
        "model": "llama-3.2-11b-vision-preview",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        return json.loads(response.json()['choices'][0]['message']['content'])
    except:
        return None

def match_plan(amount):
    for name, info in PLANS.items():
        if abs(amount - info['sdg']) <= info['sdg'] * 0.05:
            return name
    return None

# ==================== أوامر البوت ====================
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    referrer_id = None
    if len(message.text.split()) > 1:
        code = message.text.split()[1]
        c = db.conn.cursor()
        c.execute('SELECT user_id FROM users WHERE referral_code = ?', (code,))
        row = c.fetchone()
        if row and row[0] != user_id:
            referrer_id = row[0]

    db.get_or_create_user(user_id, referrer_id)

    sub = db.get_sub(user_id)
    if sub:
        days_left = (datetime.strptime(sub['expires'], '%Y-%m-%d %H:%M:%S.%f') - datetime.now()).days
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔄 تجديد", callback_data="renew"),
            types.InlineKeyboardButton("🌦️ توقعات", callback_data="weather_forecast")
        )
        markup.add(
            types.InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings"),
            types.InlineKeyboardButton("👥 الإحالات", callback_data="referral_info")
        )
        markup.add(types.InlineKeyboardButton("💬 تواصل مع المطور", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
        bot.send_message(
            message.chat.id,
            f"✅ **حسابك مفعل!**\n\n💎 الباقة: {sub['plan']}\n💳 الدفع: {sub['payment_method']}\n⏳ المتبقي: {days_left} يوم",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for plan, info in PLANS.items():
        markup.add(types.InlineKeyboardButton(
            f"{plan} - {info['sdg']:,} SDG (${info['usd']})",
            callback_data=f"plan_{plan}"
        ))
    markup.add(types.InlineKeyboardButton("👥 نظام الإحالات", callback_data="referral_info"))
    markup.add(types.InlineKeyboardButton("💬 تواصل مع المطور", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))

    welcome = """
🌟 **طقس السودان – النسخة الذهبية** ⛈️

توقعات دقيقة جداً - تحليل جوي احترافي - مساعد ذكي بلا حدود

اختر باقتك لمشاهدة المزايا الكاملة:
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
    bot.edit_message_text(
        "🔄 **تجديد الاشتراك**\n\nاختر باقتك:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "weather_forecast")
def weather_forecast_callback(call):
    user_id = call.from_user.id
    if not is_subscribed(user_id):
        bot.answer_callback_query(call.id, "❌ هذه الميزة للمشتركين فقط", show_alert=True)
        return
    
    bot.answer_callback_query(call.id, "🔄 جاري جلب التوقعات...")
    _, city = db.get_settings(user_id)
    data, error = get_weather_forecast(city)
    
    if error:
        bot.send_message(call.message.chat.id, f"⚠️ خطأ: {error}")
        return
    
    text = f"🌍 **توقعات الطقس - {data['city']}**\n\n"
    for fc in data['forecasts']:
        date_obj = datetime.strptime(fc['date'], '%Y-%m-%d %H:%M:%S')
        text += f"📅 {date_obj.strftime('%Y-%m-%d %H:%M')}\n"
        text += f"🌡️ {fc['temp']:.1f}°C | 💧 {fc['humidity']}%\n"
        text += f"☁️ {fc['description']}\n\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("« رجوع", callback_data="back_to_start"))
    
    bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "settings")
def settings_menu(call):
    user_id = call.from_user.id
    notify, city = db.get_settings(user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    status = "✅ مفعل" if notify else "❌ معطل"
    markup.add(types.InlineKeyboardButton(f"إشعارات الطقس اليومية: {status}", callback_data="toggle_notify"))
    markup.add(types.InlineKeyboardButton("« رجوع", callback_data="back_to_start"))
    bot.edit_message_text(
        f"⚙️ **الإعدادات**\n\nالمدينة الحالية للإشعارات: **{city}**\nيمكنك تغيير المدينة باستخدام الأمر: `/setcity الخرطوم`",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "toggle_notify")
def toggle_notify(call):
    user_id = call.from_user.id
    current, city = db.get_settings(user_id)
    db.set_daily_notify(user_id, not current, city)
    bot.answer_callback_query(call.id, "تم تحديث الإعدادات ✅")
    settings_menu(call)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_start")
def back_to_start(call):
    call.message.text = "/start"
    start(call.message)
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['setcity'])
def set_notify_city(message):
    user_id = message.from_user.id
    try:
        city = message.text.split(maxsplit=1)[1]
    except:
        bot.reply_to(message, "استخدم: `/setcity ود مدني`", parse_mode="Markdown")
        return
    notify, _ = db.get_settings(user_id)
    db.set_daily_notify(user_id, notify, city)
    bot.reply_to(message, f"✅ تم تعيين **{city}** كمدينة افتراضية للإشعارات.", parse_mode="Markdown")

@bot.message_handler(commands=['weather'])
def weather_cmd(message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        bot.reply_to(message, "❌ هذه الميزة للمشتركين فقط. اشترك الآن للاستمتاع بتوقعات دقيقة!")
        return
    try:
        city = message.text.split(maxsplit=1)[1]
    except:
        city = 'Khartoum'
    data, error = get_weather_forecast(city)
    if error:
        bot.reply_to(message, f"⚠️ خطأ: {error}")
    else:
        text = f"🌍 **توقعات الطقس - {data['city']}**\n\n"
        for fc in data['forecasts'][:3]:
            date_obj = datetime.strptime(fc['date'], '%Y-%m-%d %H:%M:%S')
            text += f"📅 {date_obj.strftime('%Y-%m-%d %H:%M')}\n"
            text += f"🌡️ {fc['temp']:.1f}°C | 💧 {fc['humidity']}%\n"
            text += f"☁️ {fc['description']}\n\n"
        bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['referral'])
def referral_info_cmd(message):
    user_id = message.from_user.id
    count, code = db.get_referral_stats(user_id)
    if not code:
        code = db.get_or_create_user(user_id)
    
    bot_link = f"https://t.me/SudanWeatherBot?start={code}"
    text = f"""
👥 **نظام الإحالات**

🔗 **رابط الإحالة الخاص بك:**
`{bot_link}`

📊 **إحصائياتك:**
• عدد المدعوين: **{count}**
• المكافأة: **7 أيام مجانية** لكل مشترك جديد

📋 **كيف يعمل؟**
1. شارك رابطك مع أصدقائك
2. عندما يشترك صديقك عبر رابطك
3. تحصل تلقائياً على 7 أيام إضافية

🎯 انسخ رابطك وابدأ بالدعوة الآن!
"""
    bot.reply_to(message, text, parse_mode="Markdown", disable_web_page_preview=True)

@bot.callback_query_handler(func=lambda call: call.data == "referral_info")
def referral_callback(call):
    user_id = call.from_user.id
    count, code = db.get_referral_stats(user_id)
    if not code:
        code = db.get_or_create_user(user_id)
    
    bot_link = f"https://t.me/SudanWeatherBot?start={code}"
    text = f"""
👥 **نظام الإحالات**

🔗 **رابط الإحالة الخاص بك:**
`{bot_link}`

📊 **إحصائياتك:**
• عدد المدعوين: **{count}**
• المكافأة: **7 أيام مجانية** لكل مشترك جديد

📋 **كيف يعمل؟**
1. شارك رابطك مع أصدقائك
2. عندما يشترك صديقك عبر رابطك
3. تحصل تلقائياً على 7 أيام إضافية

🎯 انسخ رابطك وابدأ بالدعوة الآن!
"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📤 مشاركة الرابط", switch_inline_query=f"اشترك في بوت طقس السودان: {bot_link}"))
    markup.add(types.InlineKeyboardButton("« رجوع", callback_data="back_to_start"))
    
    bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("plan_"))
def select_plan(call):
    plan_name = call.data.replace("plan_", "")
    info = PLANS[plan_name]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💳 دفع بالعملات الرقمية (OxaPay)", callback_data=f"crypto_{plan_name}"),
        types.InlineKeyboardButton("🏦 تحويل بنكي (بنكك)", callback_data=f"bank_{plan_name}"),
        types.InlineKeyboardButton("💳 فوري", callback_data=f"fawry_{plan_name}"),
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
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

# دوال الدفع الموحدة
def create_payment_method_handler(method_name, method_display, account_number):
    def handler(call):
        plan_name = call.data.replace(f"{method_name}_", "")
        amount = PLANS[plan_name]['sdg']
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}"))
        markup.add(types.InlineKeyboardButton("💬 تواصل واتساب", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
        msg = f"""
{method_display} **- {plan_name}**

1️⃣ قم بتحويل **{amount:,} SDG** إلى:

📞 الرقم: `{account_number}`

2️⃣ بعد التحويل، أرسل لقطة الشاشة هنا

⚠️ **تنبيهات هامة:**
• الصورة يجب أن تكون واضحة
• يجب أن يظهر **رقم العملية** بوضوح
• يجب أن يظهر **تاريخ ووقت التحويل**
• الصورة غير معدلة أو مفوتوشوب
"""
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
        bot.answer_callback_query(call.id)
    return handler

# تسجيل معالجات طرق الدفع (بدون فودافون كاش)
payment_methods = [
    ('bank', '🏦 تحويل بنكي', MY_ACCOUNT),
    ('fawry', '💳 فوري', FAWRY_NUMBER),
    ('bravo', '📱 برافو', BRAVO_NUMBER),
    ('mycash', '💰 ماي كاشي', MYCASH_NUMBER)
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
2️⃣ أكمل الدفع بالعملة التي تفضلها
3️⃣ عد للبوت واضغط "تحقق من الدفع"

⏰ الفاتورة صالحة لمدة 60 دقيقة
"""
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
        bot.send_message(ADMIN_ID, f"📢 **فاتورة جديدة**\n👤 {user_id}\n💎 {plan_name}\n💰 ${amount_usd}\n🆔 {track_id}")
    else:
        error_msg = result.get('error', 'Unknown error')
        logger.error(f"OxaPay failed for user {user_id}: {error_msg}")
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("🏦 تحويل بنكي", callback_data=f"bank_{plan_name}"))
        markup.add(types.InlineKeyboardButton("💬 تواصل واتساب", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
        markup.add(types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}"))
        bot.edit_message_text(
            f"⚠️ **تعذر إنشاء فاتورة تلقائية**\n\nيمكنك:\n• استخدام طرق الدفع الأخرى\n• التواصل مع المطور للمساعدة",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_"))
def check_payment(call):
    parts = call.data.replace("check_", "").split("_")
    track_id = parts[0]
    plan_name = "_".join(parts[1:])
    user_id = call.from_user.id

    bot.answer_callback_query(call.id, "🔄 جاري التحقق من الدفع...")
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
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        bot.send_message(ADMIN_ID, f"✅ **دفع ناجح**\n👤 {user_id}\n💎 {plan_name}\n🆔 {track_id}")

        # مكافأة الإحالة
        c = db.conn.cursor()
        c.execute('SELECT referred_by FROM users WHERE user_id = ?', (user_id,))
        ref = c.fetchone()
        if ref and ref['referred_by']:
            if db.add_referral_reward(ref['referred_by']):
                bot.send_message(ref['referred_by'], "🎁 تمت إضافة 7 أيام مجانية لاشتراكك لأن أحد أصدقائك اشترك عبر رابط الإحالة الخاص بك!")
    else:
        pay_url = f"https://oxapay.com/payment/{track_id}"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("💳 ادفع الآن", url=pay_url))
        markup.add(types.InlineKeyboardButton("🔄 تحقق مجدداً", callback_data=f"check_{track_id}_{plan_name}"))
        markup.add(types.InlineKeyboardButton("💬 تواصل واتساب", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
        markup.add(types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}"))
        bot.edit_message_text(
            f"⏳ **لم يتم تأكيد الدفع بعد**\n\nإذا دفعت بالفعل، انتظر لحظة وحاول مجدداً.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )

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
        
        # محاولة تحليل الصورة بجميع الطرق
        result = None
        payment_method = None
        
        # بنكك
        result = analyze_bank_receipt(img_b64)
        if result and result.get('account_match'):
            payment_method = "تحويل بنكي"
        else:
            # فوري
            result = analyze_fawry_receipt(img_b64)
            if result and result.get('account_match'):
                payment_method = "فوري"
            else:
                # برافو
                result = analyze_bravo_receipt(img_b64)
                if result and result.get('account_match'):
                    payment_method = "برافو"
                else:
                    # ماي كاشي
                    result = analyze_mycash_receipt(img_b64)
                    if result and result.get('account_match'):
                        payment_method = "ماي كاشي"
        
        db.inc_attempts(user_id)

        if result and result.get('valid') and result.get('account_match'):
            amount = float(result.get('amount', 0))
            tx_id = result.get('tx_id', f"TX_{user_id}_{int(time.time())}")
            tx_datetime = result.get('datetime', 'غير معروف')
            
            if db.tx_exists(tx_id):
                markup = whatsapp_keyboard()
                bot.edit_message_text(
                    f"❌ **رقم العملية مستخدم مسبقاً**\n\nرقم العملية: `{tx_id}`\n\nللتواصل مع المطور:",
                    message.chat.id, 
                    wait.message_id,
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
                return

            plan_name = match_plan(amount)
            if plan_name:
                db.add_tx(tx_id, user_id, payment_method, amount, plan_name, verified_by="AI")
                db.add_sub(user_id, plan_name, PLANS[plan_name]['days'], payment_method)
                db.reset_attempts(user_id)
                expires = datetime.now() + timedelta(days=PLANS[plan_name]['days'])
                markup = whatsapp_keyboard()
                bot.edit_message_text(
                    f"✅ **تم التفعيل بنجاح!**\n\n"
                    f"💎 الباقة: {plan_name}\n"
                    f"💰 المبلغ: {amount:,.0f} SDG\n"
                    f"💳 طريقة الدفع: {payment_method}\n"
                    f"🔢 رقم العملية: `{tx_id}`\n"
                    f"📅 تاريخ التحويل: {tx_datetime}\n"
                    f"📆 صالح حتى: {expires.strftime('%Y-%m-%d')}\n\n"
                    f"🎉 مبروك! للتواصل مع المطور:",
                    message.chat.id,
                    wait.message_id,
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
                
                bot.send_message(
                    ADMIN_ID,
                    f"✅ **تفعيل جديد**\n👤 {user_id}\n💎 {plan_name}\n💰 {amount:,.0f} SDG\n💳 {payment_method}\n🔢 {tx_id}\n📅 {tx_datetime}",
                    parse_mode="Markdown"
                )
            else:
                expected = "\n".join([f"• {n}: {i['sdg']:,} SDG" for n, i in PLANS.items()])
                markup = support_keyboard()
                bot.edit_message_text(
                    f"⚠️ **المبلغ غير مطابق**\n\n"
                    f"المبلغ المستلم: {amount:,.0f} SDG\n\n"
                    f"المبالغ المطلوبة:\n{expected}\n\n"
                    f"للتواصل مع المطور:",
                    message.chat.id,
                    wait.message_id,
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
        else:
            errors = result.get('errors', []) if result else []
            error_text = "\n".join([f"• {e}" for e in errors]) if errors else "• لم يتم التعرف على الصورة كإيصال دفع صالح"
            markup = support_keyboard()
            bot.edit_message_text(
                f"❌ **رفض الإشعار**\n\n"
                f"{error_text}\n\n"
                f"**متطلبات القبول:**\n"
                f"• التحويل لأحد الأرقام المعتمدة\n"
                f"• المبلغ مطابق لإحدى الباقات\n"
                f"• الصورة واضحة وغير معدلة\n"
                f"• يظهر رقم العملية والتاريخ\n"
                f"• التحويل خلال 24 ساعة\n\n"
                f"للتواصل مع المطور:",
                message.chat.id,
                wait.message_id,
                reply_markup=markup,
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Photo error: {e}")
        markup = whatsapp_keyboard()
        bot.edit_message_text(
            "❌ حدث خطأ تقني. حاول مجدداً أو تواصل مع المطور:",
            message.chat.id,
            wait.message_id,
            reply_markup=markup
        )

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        return
    total, active = db.get_stats()
    bot.reply_to(message,
        f"📊 **لوحة التحكم**\n\n"
        f"👥 إجمالي المستخدمين: {total}\n"
        f"✅ المشتركون النشطون: {active}\n\n"
        f"**أوامر الأدمن:**\n"
        f"/broadcast [رسالة] - إرسال للجميع\n"
        f"/activate [user_id] [plan_name] - تفعيل يدوي\n"
        f"/stats - إحصائيات مفصلة",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['stats'])
def detailed_stats(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    c = db.conn.cursor()
    c.execute('SELECT COUNT(*) as total FROM transactions')
    total_tx = c.fetchone()['total']
    
    c.execute('''SELECT payment_method, COUNT(*) as count, SUM(amount) as total_amount 
                 FROM transactions GROUP BY payment_method''')
    methods = c.fetchall()
    
    text = "📊 **إحصائيات مفصلة**\n\n"
    text += f"📦 إجمالي المعاملات: {total_tx}\n\n"
    text += "**طرق الدفع:**\n"
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

# ==================== الإشعارات اليومية ====================
def daily_notification_worker():
    while True:
        now = datetime.now()
        if now.hour == 8 and now.minute == 0:
            logger.info("بدء إرسال الإشعارات اليومية...")
            c = db.conn.cursor()
            c.execute('SELECT user_id, notify_city FROM user_settings WHERE daily_weather_notify = 1')
            rows = c.fetchall()
            for row in rows:
                user_id, city = row['user_id'], row['notify_city']
                if is_subscribed(user_id):
                    data, error = get_weather_forecast(city)
                    if not error and data['forecasts']:
                        fc = data['forecasts'][0]
                        text = f"""
☀️ **نشرة الطقس اليومية - {city}**
📅 {datetime.now().strftime('%Y-%m-%d')}
🌡️ الحرارة: {fc['temp']:.1f}°C
💧 الرطوبة: {fc['humidity']}%
☁️ الحالة: {fc['description']}

للتوقعات الكاملة استخدم /weather
"""
                        try:
                            bot.send_message(user_id, text, parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"فشل إرسال إشعار لـ {user_id}: {e}")
            time.sleep(60)
        else:
            time.sleep(30)

Thread(target=daily_notification_worker, daemon=True).start()

# ==================== تشغيل ====================
print("=" * 50)
print("✅ بوت طقس السودان - الإصدار المتقدم")
print(f"💳 OxaPay: جاهز")
print(f"🏦 بنكك: {MY_ACCOUNT}")
print(f"💳 فوري: {FAWRY_NUMBER}")
print(f"📱 برافو: {BRAVO_NUMBER}")
print(f"💰 ماي كاشي: {MYCASH_NUMBER}")
print(f"📱 واتساب المطور: +{DEVELOPER_WHATSAPP}")
print("=" * 50)

keep_alive()

# استخدام polling مع skip_pending لحل مشكلة تعدد النسخ
while True:
    try:
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as e:
        logger.error(f"Polling error: {e}")
        time.sleep(15)
