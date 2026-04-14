import telebot
import requests
import base64
import json
import re
import time
import sqlite3
import threading
import logging
from datetime import datetime
from telebot import types

# ==================== الإعدادات ====================
TOKEN = '8665720382:AAEzrjTSqC5Gt5QXXu-gWfYu-vkUodOfwGw'
OXAPAY_KEY = 'LYMACY-HJVRXA-D02BTO-AHUK8R'
import os
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

ADMIN_ID = 8188643525
MY_ACCOUNT = "4636998"
USD_TO_SDG_RATE = 3600
DEVELOPER_WHATSAPP = "249901758765"

# روابط OxaPay
OXAPAY_CREATE_URL = 'https://api.oxapay.com/merchants/request'
OXAPAY_INQUIRY_URL = 'https://api.oxapay.com/merchants/inquiry'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN, threaded=True)

# ==================== قاعدة البيانات ====================
class SimpleDB:
    def __init__(self):
        self.conn = sqlite3.connect('bot.db', check_same_thread=False)
        self.init_db()
    
    def init_db(self):
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            attempts INTEGER DEFAULT 0,
            last_attempt TIMESTAMP
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
            track_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        self.conn.commit()
    
    def get_sub(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT * FROM subs WHERE user_id = ? AND expires > datetime("now")', (user_id,))
        return c.fetchone()
    
    def add_sub(self, user_id, plan, days, payment_method):
        c = self.conn.cursor()
        expires = datetime.fromtimestamp(time.time() + days * 86400)
        c.execute('INSERT OR REPLACE INTO subs VALUES (?, ?, ?, ?)', 
                  (user_id, plan, expires, payment_method))
        self.conn.commit()
    
    def add_tx(self, tx_id, user_id, track_id):
        c = self.conn.cursor()
        c.execute('INSERT INTO transactions (tx_id, user_id, track_id) VALUES (?, ?, ?)', 
                  (tx_id, user_id, track_id))
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

db = SimpleDB()

# ==================== الباقات ====================
PLANS = {
    "⭐ المبدئية": {"usd": 2.99, "sdg": int(2.99 * USD_TO_SDG_RATE), "days": 30},
    "🌙 الشهرية": {"usd": 4.99, "sdg": int(4.99 * USD_TO_SDG_RATE), "days": 30},
    "👑 السنوية": {"usd": 49.00, "sdg": int(49.00 * USD_TO_SDG_RATE), "days": 365}
}

# ==================== دوول مساعدة ====================
def create_inline_button(text, callback_data=None, url=None):
    """إنشاء زر inline صحيح"""
    if url:
        return types.InlineKeyboardButton(text, url=url)
    else:
        return types.InlineKeyboardButton(text, callback_data=callback_data)

def whatsapp_keyboard():
    """لوحة مفاتيح واتساب فقط"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "💬 تواصل مع المطور عبر واتساب",
        url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
    ))
    return markup

def support_keyboard(include_back=False, back_callback="back_to_start"):
    """لوحة مفاتيح الدعم"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        "💬 تواصل مع المطور عبر واتساب",
        url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
    ))
    if include_back:
        markup.add(types.InlineKeyboardButton(
            "« رجوع",
            callback_data=back_callback
        ))
    return markup

# ==================== OxaPay ====================
def create_oxapay_invoice(amount_usd, plan_name, user_id):
    """إنشاء فاتورة OxaPay"""
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
        logger.info(f"Creating OxaPay invoice for user {user_id}")
        response = requests.post(OXAPAY_CREATE_URL, json=payload, headers=headers, timeout=15)
        data = response.json()
        
        if data.get('result') == 100:
            return {
                'success': True,
                'pay_url': data.get('payLink'),  # نستخدم payLink بدلاً من payUrl
                'track_id': data.get('trackId')
            }
        else:
            logger.error(f"OxaPay Error: {data.get('message')}")
            return {'success': False, 'error': data.get('message')}
            
    except Exception as e:
        logger.error(f"OxaPay Exception: {e}")
        return {'success': False, 'error': str(e)}

def check_oxapay_payment(track_id):
    """التحقق من حالة الدفع"""
    try:
        response = requests.get(f"{OXAPAY_INQUIRY_URL}?trackId={track_id}", timeout=10)
        data = response.json()
        
        if data.get('result') == 100:
            return {
                'success': True,
                'status': data.get('status'),
                'amount': data.get('amount'),
                'currency': data.get('currency')
            }
        return {'success': False}
    except:
        return {'success': False}

# ==================== تحليل الصور ====================
def analyze_receipt(image_base64):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    prompt = f"""
    حلل صورة إشعار تحويل بنكي واستخرج:
    1. رقم الحساب المستلم (يجب: {MY_ACCOUNT})
    2. المبلغ المحول (رقم فقط)
    3. رقم العملية
    
    رد بصيغة JSON فقط:
    {{"valid": true/false, "account_match": true/false, "amount": 0, "tx_id": ""}}
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
    
    sub = db.get_sub(user_id)
    if sub:
        days = (datetime.strptime(sub[2], '%Y-%m-%d %H:%M:%S.%f') - datetime.now()).days
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("🔄 تجديد الاشتراك", callback_data="renew"))
        markup.add(types.InlineKeyboardButton(
            "💬 تواصل مع المطور",
            url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
        ))
        
        bot.send_message(
            message.chat.id,
            f"✅ **حسابك مفعل!**\n\n"
            f"💎 الباقة: {sub[1]}\n"
            f"💳 الدفع: {sub[3]}\n"
            f"⏳ المتبقي: {days} يوم",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return
    
    # عرض الباقات
    markup = types.InlineKeyboardMarkup(row_width=1)
    for plan, info in PLANS.items():
        markup.add(types.InlineKeyboardButton(
            f"{plan} - {info['sdg']:,} SDG (${info['usd']})",
            callback_data=f"plan_{plan}"
        ))
    markup.add(types.InlineKeyboardButton(
        "💬 تواصل مع المطور عبر واتساب",
        url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
    ))
    
    welcome = """
🌟 **طقس السودان - النسخة الذهبية** ⛈️

**المميزات:**
• توقعات دقيقة لمدة 15 يوم
• خرائط تفاعلية للأمطار
• تنبيهات فورية للعواصف
• بدون إعلانات

اختر باقتك:
    """
    
    bot.send_message(
        message.chat.id,
        welcome,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "renew")
def renew_subscription(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for plan, info in PLANS.items():
        markup.add(types.InlineKeyboardButton(
            f"{plan} - {info['sdg']:,} SDG (${info['usd']})",
            callback_data=f"plan_{plan}"
        ))
    
    bot.edit_message_text(
        "🔄 **تجديد الاشتراك**\n\nاختر باقتك:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("plan_"))
def select_plan(call):
    plan_name = call.data.replace("plan_", "")
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💳 دفع بالعملات الرقمية (OxaPay)", callback_data=f"crypto_{plan_name}"),
        types.InlineKeyboardButton("🏦 تحويل بنكي (بنكك)", callback_data=f"bank_{plan_name}"),
        types.InlineKeyboardButton("« رجوع", callback_data="renew")
    )
    
    info = PLANS[plan_name]
    bot.edit_message_text(
        f"**{plan_name}**\n\n"
        f"💰 السعر: ${info['usd']} / {info['sdg']:,} SDG\n"
        f"📅 المدة: {info['days']} يوم\n\n"
        "اختر طريقة الدفع:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("bank_"))
def pay_bank(call):
    plan_name = call.data.replace("bank_", "")
    amount = PLANS[plan_name]['sdg']
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}"))
    markup.add(types.InlineKeyboardButton(
        "💬 تواصل واتساب",
        url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
    ))
    
    msg = f"""
🏦 **تحويل بنكي - {plan_name}**

1️⃣ قم بتحويل **{amount:,} SDG** إلى:

📱 رقم الحساب: `{MY_ACCOUNT}`
🏛 البنك: بنك الخرطوم
📲 التطبيق: بنكك

2️⃣ بعد التحويل، أرسل لقطة الشاشة هنا

⚠️ **تنبيهات:**
• الصورة يجب أن تكون واضحة
• يظهر فيها رقم العملية
• غير معدلة أو مفوتوشوب
    """
    
    bot.edit_message_text(
        msg,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("crypto_"))
def pay_crypto(call):
    plan_name = call.data.replace("crypto_", "")
    user_id = call.from_user.id
    amount_usd = PLANS[plan_name]['usd']
    amount_sdg = PLANS[plan_name]['sdg']
    
    # رسالة مؤقتة
    bot.edit_message_text(
        "🔄 جاري إنشاء فاتورة OxaPay...",
        call.message.chat.id,
        call.message.message_id
    )
    
    # إنشاء الفاتورة
    result = create_oxapay_invoice(amount_usd, plan_name, user_id)
    
    if result['success']:
        pay_url = result['pay_url']
        track_id = result['track_id']
        
        # حفظ المعاملة
        db.add_tx(f"OXA_{track_id}", user_id, track_id)
        
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
        
        bot.edit_message_text(
            msg,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        
        # إشعار للأدمن
        bot.send_message(
            ADMIN_ID,
            f"📢 **فاتورة جديدة**\n👤 {user_id}\n💎 {plan_name}\n💰 ${amount_usd}\n🆔 {track_id}"
        )
    else:
        # فشل إنشاء الفاتورة
        error_msg = result.get('error', 'Unknown error')
        logger.error(f"OxaPay failed for user {user_id}: {error_msg}")
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("🏦 تحويل بنكي", callback_data=f"bank_{plan_name}"),
            types.InlineKeyboardButton(
                "💬 تواصل واتساب",
                url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
            ),
            types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}")
        )
        
        bot.edit_message_text(
            f"⚠️ **تعذر إنشاء فاتورة تلقائية**\n\n"
            f"يمكنك:\n"
            f"• استخدام التحويل البنكي ({amount_sdg:,} SDG)\n"
            f"• التواصل مع المطور للمساعدة",
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
        # تفعيل الاشتراك
        days = PLANS[plan_name]['days']
        db.add_sub(user_id, plan_name, days, 'OxaPay')
        db.reset_attempts(user_id)
        
        expires = datetime.fromtimestamp(time.time() + days * 86400)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "💬 تواصل مع المطور",
            url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
        ))
        
        bot.edit_message_text(
            f"🎉 **تم الدفع بنجاح!**\n\n"
            f"💎 الباقة: {plan_name}\n"
            f"📅 صالحة حتى: {expires.strftime('%Y-%m-%d')}\n\n"
            "شكراً لاشتراكك! 🌟",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        
        bot.send_message(
            ADMIN_ID,
            f"✅ **دفع ناجح**\n👤 {user_id}\n💎 {plan_name}\n🆔 {track_id}"
        )
    else:
        # إعادة عرض نفس الأزرار
        pay_url = f"https://oxapay.com/payment/{track_id}"
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("💳 ادفع الآن", url=pay_url))
        markup.add(types.InlineKeyboardButton("🔄 تحقق مجدداً", callback_data=f"check_{track_id}_{plan_name}"))
        markup.add(types.InlineKeyboardButton(
            "💬 تواصل واتساب",
            url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
        ))
        markup.add(types.InlineKeyboardButton("« رجوع", callback_data=f"plan_{plan_name}"))
        
        bot.edit_message_text(
            f"⏳ **لم يتم تأكيد الدفع بعد**\n\n"
            f"إذا دفعت بالفعل، انتظر لحظة وحاول مجدداً.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = message.from_user.id
    
    # فحص المحاولات
    attempts = db.get_attempts(user_id)
    if attempts and attempts[0] >= 5:
        markup = whatsapp_keyboard()
        bot.reply_to(
            message,
            "⛔ تجاوزت الحد الأقصى للمحاولات.\n\nللحصول على مساعدة، تواصل مع المطور:",
            reply_markup=markup
        )
        return
    
    wait = bot.reply_to(message, "🔍 جاري فحص الإشعار...")
    
    try:
        # تحميل الصورة
        file = bot.get_file(message.photo[-1].file_id)
        img_data = bot.download_file(file.file_path)
        img_b64 = base64.b64encode(img_data).decode('utf-8')
        
        # تحليل الصورة
        result = analyze_receipt(img_b64)
        db.inc_attempts(user_id)
        
        if result and result.get('valid') and result.get('account_match'):
            amount = float(result.get('amount', 0))
            tx_id = result.get('tx_id', f"TX_{user_id}_{int(time.time())}")
            
            if db.tx_exists(tx_id):
                markup = whatsapp_keyboard()
                bot.edit_message_text(
                    "❌ رقم العملية مستخدم مسبقاً\n\nللتواصل مع المطور:",
                    message.chat.id, 
                    wait.message_id,
                    reply_markup=markup
                )
                return
            
            plan_name = match_plan(amount)
            
            if plan_name:
                db.add_tx(tx_id, user_id, None)
                db.add_sub(user_id, plan_name, PLANS[plan_name]['days'], 'تحويل بنكي')
                db.reset_attempts(user_id)
                
                expires = datetime.fromtimestamp(time.time() + PLANS[plan_name]['days'] * 86400)
                
                markup = whatsapp_keyboard()
                
                bot.edit_message_text(
                    f"✅ **تم التفعيل!**\n\n"
                    f"💎 {plan_name}\n"
                    f"💰 {amount:,.0f} SDG\n"
                    f"📅 صالح حتى: {expires.strftime('%Y-%m-%d')}\n\n"
                    "🎉 مبروك! للتواصل مع المطور:",
                    message.chat.id, 
                    wait.message_id,
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
                
                bot.send_message(ADMIN_ID, f"✅ تفعيل بنكي: {user_id} | {plan_name} | {amount}")
            else:
                expected = "\n".join([f"• {n}: {i['sdg']:,} SDG" for n, i in PLANS.items()])
                markup = support_keyboard()
                
                bot.edit_message_text(
                    f"⚠️ المبلغ ({amount:,.0f}) غير مطابق لأي باقة.\n\n"
                    f"المبالغ المطلوبة:\n{expected}\n\n"
                    f"للتواصل مع المطور:",
                    message.chat.id, 
                    wait.message_id,
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
        else:
            markup = support_keyboard()
            bot.edit_message_text(
                f"❌ **رفض الإشعار**\n\n"
                f"• تأكد من التحويل للحساب: `{MY_ACCOUNT}`\n"
                f"• الصورة واضحة وغير معدلة\n"
                f"• يظهر رقم العملية بوضوح\n\n"
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
            "❌ حدث خطأ. حاول مجدداً أو تواصل مع المطور:",
            message.chat.id,
            wait.message_id,
            reply_markup=markup
        )

@bot.message_handler(commands=['support', 'help'])
def support_command(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        "💬 تواصل مع المطور عبر واتساب",
        url=f"https://wa.me/{DEVELOPER_WHATSAPP}"
    ))
    markup.add(types.InlineKeyboardButton(
        "🏠 الرجوع للقائمة الرئيسية",
        callback_data="renew"
    ))
    
    bot.send_message(
        message.chat.id,
        f"📞 **الدعم الفني**\n\n"
        f"للحصول على مساعدة فورية، تواصل مع المطور عبر واتساب:\n\n"
        f"📱 +{DEVELOPER_WHATSAPP}",
        reply_markup=markup,
        parse_mode="Markdown"
    )

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

# ==================== تشغيل ====================
print("=" * 50)
print("✅ بوت طقس السودان - يعمل الآن")
print(f"💳 OxaPay: جاهز وفعال")
print(f"🏦 بنكك: {MY_ACCOUNT}")
print(f"📱 واتساب المطور: +{DEVELOPER_WHATSAPP}")
print("=" * 50)

bot.polling(none_stop=True)
