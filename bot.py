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
USD_TO_SDG_RATE = int(os.environ.get("USD_TO_SDG_RATE", "3600"))
DEVELOPER_WHATSAPP = os.environ.get("DEV_WHATSAPP", "249901758765")

# ==================== معلومات الحسابات البنكية ====================
BANK_ACCOUNT = "46369925"
BANK_NAME = "بنك الخرطوم"

FAWRY_BANK = "بنك فيصل الإسلامي"
FAWRY_ACCOUNT_NAME = "القاسم احمد محمد"
FAWRY_ACCOUNT_NUMBER = "51663519"

BRAVO_NAME = "علي القاسم"
BRAVO_NUMBER = "71062333"

MYCASH_NAME = "علي القاسم"
MYCASH_NUMBER = "400569264"

OXAPAY_CREATE_URL = 'https://api.oxapay.com/merchants/request'
OXAPAY_INQUIRY_URL = 'https://api.oxapay.com/merchants/inquiry'

# إعدادات Open-Meteo API
OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY غير مضبوط. تحليل الصور معطل.")

# ==================== حل مشكلة تعدد النسخ ====================
try:
    bot = telebot.TeleBot(TOKEN, threaded=False)
    bot.remove_webhook()
    time.sleep(0.5)
    logger.info("✅ تم إزالة webhook بنجاح")
except Exception as e:
    logger.error(f"خطأ في إزالة webhook: {e}")

# ==================== Flask لـ Render ====================
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
        c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, attempts INTEGER DEFAULT 0, last_attempt TIMESTAMP, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS subs (user_id INTEGER PRIMARY KEY, plan TEXT, expires TIMESTAMP, payment_method TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS transactions (tx_id TEXT PRIMARY KEY, user_id INTEGER, payment_method TEXT, amount REAL, plan TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, verified_by TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, daily_weather_notify BOOLEAN DEFAULT 0, notify_city TEXT DEFAULT 'Khartoum')''')
        self.conn.commit()
        logger.info("✅ تم تهيئة قاعدة البيانات بنجاح")

    def create_user(self, user_id):
        c = self.conn.cursor()
        c.execute('INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?, datetime("now"))', (user_id,))
        self.conn.commit()

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
            c.execute('UPDATE subs SET plan = ?, expires = ?, payment_method = ? WHERE user_id = ?', (plan, new_expires, payment_method, user_id))
        else:
            expires = datetime.now() + timedelta(days=days)
            c.execute('INSERT INTO subs VALUES (?, ?, ?, ?)', (user_id, plan, expires, payment_method))
        self.conn.commit()

    def add_tx(self, tx_id, user_id, payment_method, amount, plan, verified_by=None):
        c = self.conn.cursor()
        c.execute('INSERT INTO transactions (tx_id, user_id, payment_method, amount, plan, verified_by) VALUES (?, ?, ?, ?, ?, ?)', (tx_id, user_id, payment_method, amount, plan, verified_by))
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
        c.execute('''INSERT INTO users (user_id, attempts, last_attempt) VALUES (?, 1, datetime("now")) ON CONFLICT(user_id) DO UPDATE SET attempts = attempts + 1, last_attempt = datetime("now")''', (user_id,))
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
        c.execute('''INSERT INTO user_settings (user_id, daily_weather_notify, notify_city) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET daily_weather_notify = ?, notify_city = ?''', (user_id, enabled, city, enabled, city))
        self.conn.commit()

    def get_stats(self):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) as total FROM users')
        total_users = c.fetchone()['total']
        c.execute('SELECT COUNT(*) as active FROM subs WHERE expires > datetime("now")')
        active_subs = c.fetchone()['active']
        c.execute('SELECT SUM(amount) as total FROM transactions')
        total_revenue = c.fetchone()['total'] or 0
        c.execute('SELECT COUNT(*) as notify_count FROM user_settings WHERE daily_weather_notify = 1')
        notify_count = c.fetchone()['notify_count']
        return {'total_users': total_users, 'active_subs': active_subs, 'total_revenue': total_revenue, 'notify_count': notify_count}

    def get_all_users(self):
        c = self.conn.cursor()
        c.execute('SELECT user_id FROM users')
        return [row['user_id'] for row in c.fetchall()]

db = Database()

# ==================== الباقات ====================
PLANS = {
    "⭐ المبدئية": {"usd": 2.99, "sdg": int(2.99 * USD_TO_SDG_RATE), "days": 30, "description": """
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
"""},
    "🌙 الشهرية": {"usd": 4.99, "sdg": int(4.99 * USD_TO_SDG_RATE), "days": 30, "description": """
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
"""},
    "👑 السنوية": {"usd": 49.00, "sdg": int(49.00 * USD_TO_SDG_RATE), "days": 365, "description": """
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
"""}
}

# ==================== دوال الطقس عبر Open-Meteo ====================
# قاموس لتحويل أكواد WMO إلى وصف عربي
WEATHER_CODES = {
    0: "سماء صافية", 1: "صافي غالباً", 2: "غائم جزئياً", 3: "غائم",
    45: "ضباب", 48: "ضباب متجمد", 51: "رذاذ خفيف", 53: "رذاذ معتدل", 55: "رذاذ كثيف",
    56: "رذاذ متجمد خفيف", 57: "رذاذ متجمد كثيف", 61: "أمطار خفيفة", 63: "أمطار معتدلة", 65: "أمطار غزيرة",
    66: "أمطار متجمدة خفيفة", 67: "أمطار متجمدة غزيرة", 71: "ثلوج خفيفة", 73: "ثلوج معتدلة", 75: "ثلوج غزيرة",
    77: "حبات ثلج", 80: "زخات مطر خفيفة", 81: "زخات مطر معتدلة", 82: "زخات مطر عنيفة",
    85: "زخات ثلج خفيفة", 86: "زخات ثلج غزيرة", 95: "عاصفة رعدية", 96: "عاصفة رعدية مع برد خفيف", 99: "عاصفة رعدية مع برد كثيف"
}

def get_weather_description(code):
    """تحويل كود WMO إلى وصف نصي بالعربية"""
    return WEATHER_CODES.get(code, "غير معروف")

def get_city_coordinates(city_name):
    """تحويل اسم المدينة إلى إحداثيات باستخدام Open-Meteo Geocoding API (مجاني)"""
    try:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={city_name}&count=1&language=ar&format=json"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("results"):
            return data["results"][0]["latitude"], data["results"][0]["longitude"], data["results"][0]["name"]
    except:
        pass
    # إحداثيات افتراضية للخرطوم
    return 15.5007, 32.5599, "الخرطوم"

def get_weather_forecast(city):
    """جلب توقعات الطقس من Open-Meteo API"""
    lat, lon, city_name = get_city_coordinates(city)
    params = {
        "latitude": lat, "longitude": lon,
        "daily": ["weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum"],
        "hourly": ["temperature_2m", "relative_humidity_2m", "wind_speed_10m"],
        "timezone": "Africa/Khartoum",
        "forecast_days": 7
    }
    try:
        resp = requests.get(OPEN_METEO_BASE_URL, params=params, timeout=15)
        data = resp.json()
        forecasts = []
        daily = data.get("daily", {})
        for i in range(len(daily.get("time", []))):
            forecasts.append({
                "date": daily["time"][i],
                "temp_max": daily["temperature_2m_max"][i],
                "temp_min": daily["temperature_2m_min"][i],
                "precipitation": daily["precipitation_sum"][i],
                "description": get_weather_description(daily["weather_code"][i])
            })
        hourly = data.get("hourly", {})
        current = {
            "temp": hourly["temperature_2m"][0] if hourly.get("temperature_2m") else 0,
            "humidity": hourly["relative_humidity_2m"][0] if hourly.get("relative_humidity_2m") else 0,
            "wind_speed": hourly["wind_speed_10m"][0] if hourly.get("wind_speed_10m") else 0
        }
        return {"city": city_name, "forecasts": forecasts, "current": current}, None
    except Exception as e:
        return None, str(e)

def is_subscribed(user_id):
    return db.get_sub(user_id) is not None

def match_plan(amount):
    for name, info in PLANS.items():
        if abs(amount - info['sdg']) <= info['sdg'] * 0.05:
            return name
    return None

# ==================== تحليل الصور ====================
def analyze_receipt(image_base64, prompt):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
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

def analyze_bank_receipt(img_b64):
    prompt = f"""
    حلل صورة إشعار تحويل بنكي. استخرج:
    1. رقم الحساب المستلم (يجب: {BANK_ACCOUNT})
    2. المبلغ (رقم فقط)
    3. رقم العملية
    رد بصيغة JSON: {{"valid": true/false, "account_match": true/false, "amount": 0, "tx_id": ""}}
    """
    return analyze_receipt(img_b64, prompt)

def analyze_fawry_receipt(img_b64):
    prompt = f"""
    حلل صورة تحويل فوري. استخرج:
    1. رقم الحساب (يجب: {FAWRY_ACCOUNT_NUMBER})
    2. اسم المستلم (يجب: {FAWRY_ACCOUNT_NAME})
    3. المبلغ (رقم فقط)
    4. رقم العملية
    رد بصيغة JSON: {{"valid": true/false, "account_match": true/false, "amount": 0, "tx_id": ""}}
    """
    return analyze_receipt(img_b64, prompt)

def analyze_bravo_receipt(img_b64):
    prompt = f"""
    حلل صورة تحويل برافو. استخرج:
    1. رقم الهاتف (يجب: {BRAVO_NUMBER})
    2. اسم المستلم (يجب: {BRAVO_NAME})
    3. المبلغ (رقم فقط)
    4. رقم العملية
    رد بصيغة JSON: {{"valid": true/false, "account_match": true/false, "amount": 0, "tx_id": ""}}
    """
    return analyze_receipt(img_b64, prompt)

def analyze_mycash_receipt(img_b64):
    prompt = f"""
    حلل صورة تحويل ماي كاشي. استخرج:
    1. رقم الهاتف (يجب: {MYCASH_NUMBER})
    2. اسم المستلم (يجب: {MYCASH_NAME})
    3. المبلغ (رقم فقط)
    4. رقم العملية
    رد بصيغة JSON: {{"valid": true/false, "account_match": true/false, "amount": 0, "tx_id": ""}}
    """
    return analyze_receipt(img_b64, prompt)

# ==================== دوال الدفع ====================
def create_oxapay_invoice(amount_usd, plan_name, user_id):
    payload = {'merchant': OXAPAY_KEY, 'amount': amount_usd, 'currency': 'USD', 'lifeTime': 60, 'description': f"Subscription: {plan_name}", 'orderId': f"USER_{user_id}_{int(time.time())}", 'returnUrl': 'https://t.me/SudanWeatherBot'}
    try:
        response = requests.post(OXAPAY_CREATE_URL, json=payload, timeout=15)
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

# ==================== أوامر البوت ====================
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    db.create_user(user_id)
    sub = db.get_sub(user_id)
    if sub:
        days_left = (datetime.strptime(sub['expires'], '%Y-%m-%d %H:%M:%S.%f') - datetime.now()).days
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(types.InlineKeyboardButton("🔄 تجديد", callback_data="renew"), types.InlineKeyboardButton("🌦️ توقعات", callback_data="weather_forecast"))
        markup.add(types.InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings"))
        markup.add(types.InlineKeyboardButton("💬 تواصل مع المطور", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
        bot.send_message(message.chat.id, f"✅ **حسابك مفعل!**\n\n💎 الباقة: {sub['plan']}\n💳 الدفع: {sub['payment_method']}\n⏳ المتبقي: {days_left} يوم", reply_markup=markup, parse_mode="Markdown")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for plan, info in PLANS.items():
        markup.add(types.InlineKeyboardButton(f"{plan} - {info['sdg']:,} SDG (${info['usd']})", callback_data=f"plan_{plan}"))
    markup.add(types.InlineKeyboardButton("📊 لوحة المعلومات", callback_data="show_panel"))
    markup.add(types.InlineKeyboardButton("💬 تواصل مع المطور", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
    bot.send_message(message.chat.id, "🌟 **طقس السودان – النسخة الذهبية** ⛈️\n\nاختر باقتك لمشاهدة المزايا الكاملة:", reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(commands=['panel'])
def info_panel(message):
    stats = db.get_stats()
    user_id = message.from_user.id
    sub = db.get_sub(user_id)
    notify, city = db.get_settings(user_id)
    user_status = f"✅ مشترك نشط | {sub['plan']} | متبقي {(datetime.strptime(sub['expires'], '%Y-%m-%d %H:%M:%S.%f') - datetime.now()).days} يوم" if sub else "❌ غير مشترك"
    panel_text = f"""
╔══════════════════════════════════════╗
║       🌟 **لوحة معلومات طقس السودان** 🌟       ║
╚══════════════════════════════════════╝

**🕐 الوقت:** `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`
**👤 معرفك:** `{user_id}`
**💎 الاشتراك:** {user_status}
**🔔 الإشعارات:** {'✅ مفعل' if notify else '❌ معطل'} | **📍 المدينة:** {city}
**🤖 Groq:** {'✅ مفعل' if GROQ_API_KEY else '❌ معطل'}
**🌤️ Open-Meteo:** ✅ مفعل (مجاني)

**💰 طرق الدفع:**
🏦 بنكك: `{BANK_ACCOUNT}`
💳 فوري: `{FAWRY_ACCOUNT_NUMBER}`
📱 برافو: `{BRAVO_NUMBER}`
💰 ماي كاشي: `{MYCASH_NUMBER}`

**📦 الباقات:**
• ⭐ المبدئية: **{PLANS['⭐ المبدئية']['sdg']:,} SDG** - 30 يوم
• 🌙 الشهرية: **{PLANS['🌙 الشهرية']['sdg']:,} SDG** - 30 يوم
• 👑 السنوية: **{PLANS['👑 السنوية']['sdg']:,} SDG** - 365 يوم

**📈 الإحصائيات:**
👥 المستخدمين: {stats['total_users']}
✅ النشطون: {stats['active_subs']}
💰 الإيرادات: {stats['total_revenue']:,.0f} SDG
"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings"), types.InlineKeyboardButton("🌦️ التوقعات", callback_data="weather_forecast"))
    markup.add(types.InlineKeyboardButton("💳 الاشتراك", callback_data="renew"), types.InlineKeyboardButton("📞 تواصل", url=f"https://wa.me/{DEVELOPER_WHATSAPP}"))
    bot.send_message(message.chat.id, panel_text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "weather_forecast")
def weather_forecast_callback(call):
    user_id = call.from_user.id
    if not is_subscribed(user_id):
        bot.answer_callback_query(call.id, "❌ للمشتركين فقط", show_alert=True)
        return
    bot.answer_callback_query(call.id, "🔄 جاري جلب التوقعات...")
    _, city = db.get_settings(user_id)
    data, error = get_weather_forecast(city)
    if error:
        bot.send_message(call.message.chat.id, f"⚠️ خطأ: {error}")
        return
    text = f"🌍 **توقعات الطقس - {data['city']}**\n\n"
    text += f"🌡️ **الحالي:** {data['current']['temp']:.1f}°C | 💧 {data['current']['humidity']}% | 🌀 {data['current']['wind_speed']} كم/س\n\n"
    for fc in data['forecasts'][:5]:
        text += f"📅 **{fc['date']}**\n  ⬆️ {fc['temp_max']:.1f}°C ⬇️ {fc['temp_min']:.1f}°C | 💧 {fc['precipitation']:.1f} مم | ☁️ {fc['description']}\n\n"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("« رجوع", callback_data="back_to_start"))
    bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "settings")
def settings_menu(call):
    user_id = call.from_user.id
    notify, city = db.get_settings(user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(f"إشعارات الطقس: {'✅ مفعل' if notify else '❌ معطل'}", callback_data="toggle_notify"))
    markup.add(types.InlineKeyboardButton("« رجوع", callback_data="back_to_start"))
    bot.edit_message_text(f"⚙️ **الإعدادات**\n\n📍 المدينة: **{city}**\n⏰ موعد الإشعار: 08:00 صباحاً\n\nلتغيير المدينة: `/setcity [اسم المدينة]`", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "toggle_notify")
def toggle_notify(call):
    user_id = call.from_user.id
    current, city = db.get_settings(user_id)
    db.set_daily_notify(user_id, not current, city)
    bot.answer_callback_query(call.id, "✅ تم تحديث الإعدادات")
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
        bot.reply_to(message, "❌ هذه الميزة للمشتركين فقط.")
        return
    try:
        city = message.text.split(maxsplit=1)[1]
    except:
        city = 'Khartoum'
    data, error = get_weather_forecast(city)
    if error:
        bot.reply_to(message, f"⚠️ خطأ: {error}")
    else:
        text = f"🌍 **الطقس في {data['city']}**\n🌡️ الحرارة: {data['current']['temp']:.1f}°C\n💧 الرطوبة: {data['current']['humidity']}%\n🌀 الرياح: {data['current']['wind_speed']} كم/س\n\n**توقعات الأيام القادمة:**\n"
        for fc in data['forecasts'][:3]:
            text += f"📅 {fc['date']}: ⬆️{fc['temp_max']:.1f}° ⬇️{fc['temp_min']:.1f}° | {fc['description']}\n"
        bot.reply_to(message, text, parse_mode="Markdown")

# (باقي دوال الدفع ومعالجة الصور تبقى كما هي في النسخة السابقة مع تعديل اسم الدالة)

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
                    if not error:
                        text = f"☀️ **نشرة الطقس اليومية - {city}**\n📅 {datetime.now().strftime('%Y-%m-%d')}\n🌡️ {data['current']['temp']:.1f}°C | 💧 {data['current']['humidity']}%\n☁️ {data['forecasts'][0]['description']}\n\nللتوقعات الكاملة: /weather"
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
print("✅ بوت طقس السودان - Open-Meteo")
print(f"🌤️ مصدر الطقس: Open-Meteo (مجاني)")
print("=" * 50)
keep_alive()

while True:
    try:
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as e:
        logger.error(f"Polling error: {e}")
        time.sleep(15)
