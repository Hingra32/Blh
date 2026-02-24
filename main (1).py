import os
import socket
import dns.resolver
try:
    resolver = dns.resolver.Resolver()
    resolver.nameservers = ['8.8.8.8', '8.8.4.4']
    answers = resolver.resolve('api.telegram.org', 'A')
    telegram_ip = answers[0].to_text()
    print(f"✅ Telegram IP Resolved: {telegram_ip}")
    _orig_getaddrinfo = socket.getaddrinfo

    def new_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == 'api.telegram.org':
            return _orig_getaddrinfo(telegram_ip, port, family, type, proto, flags)
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    socket.getaddrinfo = new_getaddrinfo
except Exception as e:
    print(f"⚠️ DNS Fix Failed: {e}")
import telebot
from telebot import apihelper
apihelper.CONNECT_TIMEOUT = 30
apihelper.READ_TIMEOUT = 60
import random
import string
import threading
from telebot import types
from datetime import datetime, timedelta
import time
import traceback
import requests
import pymongo
import certifi
import re
from flask import Flask, request, jsonify

# ---------------- CONFIG & SECRETS ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI") 
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
except:
    print("⚠️ ADMIN_ID missing/invalid!")
    ADMIN_ID = 0

if not BOT_TOKEN or not MONGO_URI:
    raise ValueError("❌ Error: BOT_TOKEN or MONGO_URI missing!")

# ---------------- DATABASE CONNECTION ----------------
try:
    client = pymongo.MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client["TelegramBotDB"]

    users_col = db["users"]
    batches_col = db["batches"]
    tickets_col = db["tickets"]
    pro_proofs_col = db["pro_proofs"]
    settings_col = db["settings"]
    pending_payments_col = db["pending_payments"] # New Collection for Email Verification
    unclaimed_payments_col = db["unclaimed_payments"]
    redeems_col = db["redeems"]
    auto_delete_col = db["auto_delete"] # New for Scheduler
    verification_tokens_col = db["verification_tokens"] # New for Secure Verification
    
    # Auto-delete code when expiry time is reached
    redeems_col.create_index("expiry", expireAfterSeconds=0)
    
    # Auto-delete pending email requests after 48 hours
    pending_payments_col.create_index("created_at", expireAfterSeconds=172800)
    
    # Auto-delete verification tokens after 20 minutes (1200 seconds)
    verification_tokens_col.create_index("created_at", expireAfterSeconds=1200)
    
    print("✅ MongoDB Connected!")
except Exception as e:
    print(f"❌ DB Error: {e}")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
BOT_USERNAME = bot.get_me().username
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "default_secret_123")

# ---------------- COMMAND MENU SETTINGS ----------------
def set_bot_commands():
    try:
        # Default Commands for all users
        user_commands = [
            types.BotCommand("start", "Start the bot"),
            types.BotCommand("genpaid", "Generate paid file links"),
            types.BotCommand("genpublic", "Generate public file links"),
            types.BotCommand("shortner", "Generate personal shortener link"),
            types.BotCommand("proof", "Manage payment proofs"),
            types.BotCommand("redeem", "Redeem a code")
        ]
        bot.set_my_commands(user_commands, scope=types.BotCommandScopeDefault())

        # Admin Commands (User commands + Admin specific)
        if ADMIN_ID != 0:
            admin_commands = user_commands + [
                types.BotCommand("prm", "Generate premium file links"),
                types.BotCommand("broadcast", "Send broadcast messages"),
                types.BotCommand("alive", "Open admin control panel")
            ]
            bot.set_my_commands(admin_commands, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))
        print("✅ Command Menu Set!")
    except Exception as e:
        print(f"⚠️ Failed to set commands: {e}")

# Set commands on startup
set_bot_commands()

# ---------------- AUTO-DELETE SCHEDULER (RAM OPTIMIZED) ----------------
def deletion_worker():
    while True:
        try:
            now = datetime.now()
            # 1. Clean up Auto-Delete Messages
            pending = list(auto_delete_col.find({"delete_at": {"$lte": now}}))
            for task in pending:
                chat_id = task['chat_id']
                for mid in task['message_ids']:
                    try: bot.delete_message(chat_id, mid)
                    except: pass
                auto_delete_col.delete_one({"_id": task['_id']})
            
            # 2. Clean up Expired User Bonuses
            users_col.update_many(
                {"bonus_expiry": {"$lte": now}},
                {"$unset": {"bonus_percent": "", "bonus_expiry": ""}}
            )

            # 3. Clean up invalid Redeem IDs from users' used_redeems list
            # Get list of all currently active redeem IDs
            active_redeem_ids = [r['_id'] for r in redeems_col.find({}, {"_id": 1})]
            # Remove any ID from users' list that is NOT in the active list
            users_col.update_many(
                {},
                {"$pull": {"used_redeems": {"$nin": active_redeem_ids}}}
            )
            
        except Exception as e:
            print(f"❌ Scheduler Error: {e}")
        time.sleep(60) # Run every minute

threading.Thread(target=deletion_worker, daemon=True).start()

# ---------------- WEBHOOK SERVER (FLASK) ----------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running!"

# ---------------- WEBHOOK (SMART SAVE MODE) ----------------
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # SECRET SECURITY CHECK
        secret = request.args.get('secret')
        if secret != WEBHOOK_SECRET:
            return jsonify({"status": "unauthorized"}), 403

        data = request.json
        if not data: return jsonify({"status": "error"}), 400

        email = data.get('user_email', '').lower().strip()
        amount_str = str(data.get('amount', '0')).replace("Rs.", "").strip()
        amount_clean = re.sub(r'[^\d.]', '', amount_str)
        try: paid_amount = float(amount_clean)
        except: paid_amount = 0.0

        print(f"🔔 Webhook: Email={email}, Amt={paid_amount}", flush=True)

        # 1. Pehle check karo koi User wait kar raha hai kya?
        pending = pending_payments_col.find_one({"email": email})

        if pending:
            # --- NAYA LOGIC (Store Rupees directly) ---
            user_id = pending['user_id']
            
            # Check for Bonus Percentage
            u_data = users_col.find_one({"_id": user_id})
            bonus = u_data.get("bonus_percent", 0) if u_data else 0
            
            final_amount = paid_amount + (paid_amount * (bonus / 100))
            
            # paid_amount is already in Rupees (₹)
            add_credits(user_id, final_amount)

            credit_val = CREDIT_CONFIG.get("value", 1.0)
            credits_added_display = final_amount / credit_val

            bonus_str = f" (including {bonus}% bonus)" if bonus > 0 else ""
            try: bot.send_message(user_id, f"✅ *Payment Confirmed!*\n₹{paid_amount} received. {credits_added_display} Credits added to your wallet{bonus_str}.")
            except: pass
                
            pending_payments_col.delete_one({"_id": pending['_id']})
            return jsonify({"status": "success"}), 200
        
        else:
            # --- NAYA LOGIC (Agar User nahi mila to SAVE kar lo) ---
            print(f"💾 Saving Unclaimed Payment for {email}", flush=True)
            unclaimed_payments_col.insert_one({
                "email": email,
                "amount": paid_amount,
                "timestamp": datetime.now()
            })
            return jsonify({"status": "saved", "message": "Payment stored for later claim"}), 200

    except Exception as e:
        print(f"Error: {e}", flush=True)
        return jsonify({"status": "error"}), 500
def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# Start Flask in a separate thread
threading.Thread(target=run_flask, daemon=True).start()


# ---------------- IN-MEMORY STATE ----------------
user_states = {}             
user_support_state = {}      
active_chats = {}            
user_ticket_reply = {}       
active_user_code = {}        
last_broadcast_ids = []      

# ---------------- SETTINGS MANAGER ----------------
def get_setting(key, default):
    try:
        doc = settings_col.find_one({"_id": key})
        return doc["data"] if doc else default
    except: return default

def save_setting(key, data):
    try:
        settings_col.update_one({"_id": key}, {"$set": {"data": data}}, upsert=True)
    except: pass

# Load Configs
START_CONFIG = get_setting("start", {"text": "Hi {mention} ✨\nWelcome! Use buttons below.", "pic": None})
CHANNEL_CONFIG = get_setting("channel", {"active": True, "channels": []}) 
PLANS = get_setting("plans", {"7": 50, "15": 80, "1M": 120, "6M": 500})
DELETE_CONFIG = get_setting("delete", {"minutes": 30})
LOG_CHANNELS = get_setting("logs", {"data": None, "user": None})
SHORTNER_CONFIG = get_setting("shortner", {"shorteners": [], "validity": 12, "active": False, "tutorial": None})
CUSTOM_BTN_CONFIG = get_setting("custom_btn", {"text": None})
PAYMENT_LINK = get_setting("payment_link", "https://superprofile.bio/vp/p-payment") # Admin Payment Link
CREDIT_CONFIG = get_setting("credit", {"value": 1.0})

PLAN_DAYS = {"7": 7, "15": 15, "1M": 30, "6M": 180}

# ---------------- HELPERS ----------------
def save_user(user_id):
    try:
        if not users_col.find_one({"_id": user_id}):
            users_col.insert_one({
                "_id": user_id,
                "joined_at": datetime.now(),
                "is_banned": False,
                "premium_expiry": None,
                "verification_expiry": None,
                "upi_id": None,
                "credits": 0,
                "last_shortener_index": -1,
                "personal_shortener": {"api": None, "url": None},
                "bonus_percent": 0,
                "used_redeems": [],
                "support_reports": {"date": None, "count": 0}
            })
            log_to_user_channel(f"🆕 *New User Joined*\nID: `{user_id}`")
    except: pass

def get_credits(user_id):
    u = users_col.find_one({"_id": user_id})
    return u.get("credits", 0) if u else 0

def add_credits(user_id, amount):
    users_col.update_one({"_id": user_id}, {"$inc": {"credits": amount}}, upsert=True)

def is_banned(user_id):
    u = users_col.find_one({"_id": user_id})
    return u.get("is_banned", False) if u else False

def is_premium(user_id):
    if user_id == ADMIN_ID: return True
    try:
        u = users_col.find_one({"_id": user_id})
        if not u or not u.get("premium_expiry"): return False
        if isinstance(u["premium_expiry"], datetime):
            if datetime.now() > u["premium_expiry"]:
                users_col.update_one({"_id": user_id}, {"$set": {"premium_expiry": None}})
                return False
            return True
    except: pass
    return False

def get_premium_expiry(user_id):
    if user_id == ADMIN_ID: return "Lifetime"
    try:
        u = users_col.find_one({"_id": user_id})
        if u and u.get("premium_expiry"):
            if isinstance(u["premium_expiry"], datetime):
                return u["premium_expiry"].strftime("%d-%b-%Y %I:%M %p")
            return str(u["premium_expiry"])
    except: pass
    return "N/A"

def set_premium(user_id, days):
    expiry = datetime.now() + timedelta(days=days)
    users_col.update_one({"_id": user_id}, {"$set": {"premium_expiry": expiry}})

def is_verified(user_id):
    if user_id == ADMIN_ID: return True
    if is_premium(user_id): return True
    if not SHORTNER_CONFIG.get("active"): return True
    try:
        u = users_col.find_one({"_id": user_id})
        if u and u.get("verification_expiry"):
            if isinstance(u["verification_expiry"], datetime):
                if datetime.now() < u["verification_expiry"]: return True
    except: pass
    return False

def set_verification(user_id, hours):
    expiry = datetime.now() + timedelta(hours=hours)
    users_col.update_one({"_id": user_id}, {"$set": {"verification_expiry": expiry}}, upsert=True)

def get_user_upi(user_id):
    u = users_col.find_one({"_id": user_id})
    return u.get("upi_id") if u else None

def update_user_upi(user_id, upi):
    users_col.update_one({"_id": user_id}, {"$set": {"upi_id": upi}}, upsert=True)

def gen_code(length=6):
    while True:
        code = ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))
        if not batches_col.find_one({"_id": code}): return code

def get_short_link(destination_url, shortener=None):
    try:
        if not shortener or not shortener.get("api") or not shortener.get("url"): return destination_url
        api_url = f"https://{shortener['url']}/api?api={shortener['api']}&url={destination_url}"
        r = requests.get(api_url).json()
        if r.get("status") == "success" or "shortenedUrl" in r: return r.get("shortenedUrl")
        return destination_url
    except: return destination_url

def check_force_join(user_id):
    if user_id == ADMIN_ID: return True, []
    if not CHANNEL_CONFIG.get("active") or not CHANNEL_CONFIG.get("channels"): return True, []
    missing = []
    for ch in CHANNEL_CONFIG["channels"]:
        try:
            status = bot.get_chat_member(ch['id'], user_id).status
            if status not in ['creator', 'administrator', 'member']: missing.append(ch)
        except: pass
    if missing: return False, missing
    return True, []

# ---------------- LOGGING ----------------
def log_to_data_channel(text, files=None):
    cid = LOG_CHANNELS.get("data")
    if not cid: return
    try:
        bot.send_message(cid, text)
        if files:
            for f in files:
                ftype, fid = f['type'], f['id']
                if ftype == 'text': bot.send_message(cid, f"📝 Text: {fid}")
                elif ftype == 'photo': bot.send_photo(cid, fid)
                elif ftype == 'video': bot.send_video(cid, fid)
                elif ftype == 'document': bot.send_document(cid, fid)
                elif ftype == 'audio': bot.send_audio(cid, fid)
    except: pass

def log_to_user_channel(text):
    cid = LOG_CHANNELS.get("user")
    if not cid: return
    try: bot.send_message(cid, text)
    except: pass

# ---------------- CUSTOM BUTTON PARSER ----------------
def get_custom_markup():
    btn_text = CUSTOM_BTN_CONFIG.get("text")
    if not btn_text: return None
    markup = types.InlineKeyboardMarkup()
    try:
        matches = re.findall(r'\[(.*?)\]\[buttonurl:(.*?)\]', btn_text)
        row_btns = []
        for name, url in matches:
            row_btns.append(types.InlineKeyboardButton(name, url=url))
            if len(row_btns) == 2:
                markup.add(*row_btns)
                row_btns = []
        if row_btns: markup.add(*row_btns)
    except: pass
    return markup

# ---------------- AUTO DELETE ----------------
def schedule_delete(chat_id, message_ids):
    delay_mins = DELETE_CONFIG.get("minutes", 30)
    delete_at = datetime.now() + timedelta(minutes=delay_mins)
    auto_delete_col.insert_one({
        "chat_id": chat_id,
        "message_ids": message_ids,
        "delete_at": delete_at
    })

def send_batch_content(user_id, code):
    batch = batches_col.find_one({"_id": code})
    if not batch: return False

    time_str = "30 Minutes" if DELETE_CONFIG["minutes"] == 30 else "2 Hours"
    note_msg = bot.send_message(user_id, f"⚠️ *IMPORTANT NOTE*\n\nFiles will be *Auto-Deleted* in *{time_str}*.\nPlease Forward/Save them!")
    sent_ids = [note_msg.message_id]
    custom_kb = get_custom_markup()

    for f in batch['files']:
        ftype, fid = f['type'], f['id']
        try:
            m = None
            if ftype == "photo": m = bot.send_photo(user_id, fid, reply_markup=custom_kb)
            elif ftype == "video": m = bot.send_video(user_id, fid, reply_markup=custom_kb)
            elif ftype == "audio": m = bot.send_audio(user_id, fid, reply_markup=custom_kb)
            elif ftype == "text": m = bot.send_message(user_id, fid, reply_markup=custom_kb)
            else: m = bot.send_document(user_id, fid, reply_markup=custom_kb)
            if m: sent_ids.append(m.message_id)
            time.sleep(0.2)
        except: pass
    schedule_delete(user_id, sent_ids)
    return True

# ---------------- START LOGIC ----------------
@bot.message_handler(commands=["start"])
def start_command(message):
    user_id = message.from_user.id
    save_user(user_id)
    if is_banned(user_id): return
    args = message.text.split()

    # --- NEW SECURE VERIFICATION HANDLER ---
    if len(args) > 1 and args[1].startswith("v_"):
        token = args[1]
        session = verification_tokens_col.find_one({"_id": token})
        
        if not session:
            bot.send_message(user_id, "❌ *Link Expired or Invalid!*\nPlease generate a new verification link.")
            return
        
        if session["user_id"] != user_id:
            bot.send_message(user_id, "⚠️ *Access Denied!*\nThis verification link was not generated for you.")
            return
        
        # Mark user as verified
        hours = SHORTNER_CONFIG.get("validity", 12)
        set_verification(user_id, hours)
        
        # One-time use: Delete token
        verification_tokens_col.delete_one({"_id": token})
        
        bot.send_message(user_id, f"✅ *Verification Successful!*\nYou now have access for {hours} hours. Click 'Try Again' on your previous menu to get your files.")
        return

    if len(args) > 1 and args[1].startswith("verify_"):
        # Deprecated but kept for safety during transition
        real_code = args[1].replace("verify_", "")
        hours = SHORTNER_CONFIG.get("validity", 12)
        set_verification(user_id, hours)
        bot.send_message(user_id, f"✅ *Verified for {hours} hours!*")
        process_link(user_id, real_code)
        return

    if len(args) > 1 and args[1].startswith("sl_"):
        real_code = args[1].replace("sl_", "")
        process_link(user_id, real_code, bypass_verification=True)
        return

    if len(args) == 1:
        send_custom_welcome(user_id)
        return

    is_joined, missing = check_force_join(user_id)
    if not is_joined:
        active_user_code[user_id] = f"PENDING_START_{args[1]}"
        markup = types.InlineKeyboardMarkup(row_width=1)
        for ch in missing:
            url = f"https://t.me/{ch['username']}" if ch.get('username') else "https://t.me/"
            markup.add(types.InlineKeyboardButton(f"📢 Join {ch['title']}", url=url))
        markup.add(types.InlineKeyboardButton("✅ Verify Joined", callback_data="verify_join"))
        bot.send_message(user_id, "⚠️ *Please join our channels to access the content!*", reply_markup=markup)
        return

    process_link(user_id, args[1])

@bot.callback_query_handler(func=lambda c: c.data == "verify_join")
def verify_join_cb(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    is_joined, _ = check_force_join(uid)
    if is_joined:
        bot.delete_message(uid, call.message.message_id)
        saved = active_user_code.get(uid, "")
        if saved.startswith("PENDING_START_"):
            process_link(uid, saved.split("_")[-1])
        else:
            send_custom_welcome(uid)
    else:
        bot.answer_callback_query(call.id, "❌ You haven't joined all channels!", show_alert=True)

def send_custom_welcome(user_id):
    markup = types.InlineKeyboardMarkup() # Initialize to prevent UnboundLocalError
    try:
        user = bot.get_chat(user_id)
        safe_name = user.first_name.replace("_", "\\_").replace("*", "\\*").replace("[", "").replace("]", "")
        mention = f"[{safe_name}](tg://user?id={user_id})"
        text = START_CONFIG["text"].replace("{mention}", mention)

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("💳 My Credits", callback_data="user_menu_credits"),
        )
        markup.add(
            types.InlineKeyboardButton("Dashboard 👤", callback_data="user_dashboard"),
            types.InlineKeyboardButton("👑 Premium Status", callback_data="user_menu_prem"),
            types.InlineKeyboardButton("📞 Contact", callback_data="user_menu_supp"),
        )
        custom_kb = get_custom_markup()
        if custom_kb:
            for row in custom_kb.keyboard: markup.keyboard.append(row)

        if START_CONFIG["pic"]: 
            bot.send_photo(user_id, START_CONFIG["pic"], caption=text, reply_markup=markup)
        else: 
            bot.send_message(user_id, text, reply_markup=markup)
    except Exception as e:
        print(f"Error in send_custom_welcome: {e}")
        try: bot.send_message(user_id, "Welcome!", reply_markup=markup, parse_mode=None)
        except: pass

def process_link(user_id, code, bypass_verification=False):
    batch = batches_col.find_one({"_id": code})
    if not batch:
        bot.send_message(user_id, "❌ *Link Expired or Invalid*")
        return

    owner = batch.get('owner_id', ADMIN_ID)
    btype = batch.get('type')
    price = batch.get('price')

    if btype == 'premium':
        if is_premium(user_id):
            bot.send_message(user_id, "✅ *Premium Unlocked!*")
            send_batch_content(user_id, code)
        else:
            bot.send_message(user_id, "🔒 *Premium Content*", reply_markup=get_plan_kb())

    elif btype in ['public', 'normal', 'shortner_link']:
        if not bypass_verification and not is_verified(user_id):
            shorteners = SHORTNER_CONFIG.get("shorteners", [])
            if not shorteners:
                send_batch_content(user_id, code)
                return
            
            u = users_col.find_one({"_id": user_id})
            last_index = u.get("last_shortener_index", -1) if u else -1
            next_index = (last_index + 1) % len(shorteners)
            users_col.update_one({"_id": user_id}, {"$set": {"last_shortener_index": next_index}})
            
            selected_shortener = shorteners[next_index]
            
            # --- 5-LINK POOL & RANDOMIZATION ---
            # Generate a unique session token for this user
            session_token = f"v_{gen_code(8)}"
            
            # Use the pool of links from the shortener slot (if we had pre-gen)
            # For now, we generate one dynamically but secure it with session_token
            bot_url = f"https://t.me/{BOT_USERNAME}?start={session_token}"
            short_link = get_short_link(bot_url, selected_shortener)
            
            # Save session to DB (Auto-deleted after 20 mins)
            verification_tokens_col.insert_one({
                "_id": session_token,
                "user_id": user_id,
                "created_at": datetime.now()
            })
            
            # Professional Caption
            caption = (
                "🛡 *Access Token Expired*\n"
                "Your Access Token has expired. Please renew it and try again.\n\n"
                "⏳ *Token Validity:* 12 hours\n\n"
                "ℹ️ _This is an ads-based access token. If you pass 1 access token, "
                "you can access messages from sharable links for the next 12 hours._"
            )
            
            origin_url = f"https://t.me/{BOT_USERNAME}?start={code}"
            
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                types.InlineKeyboardButton("Verify 🔓", url=short_link),
                types.InlineKeyboardButton("Try Again 🔄", url=origin_url)
            )
            if SHORTNER_CONFIG.get("tutorial"): 
                kb.add(types.InlineKeyboardButton("How to Verify ❓", url=SHORTNER_CONFIG["tutorial"]))
            
            bot.send_message(user_id, caption, reply_markup=kb, parse_mode="Markdown")
            return
        send_batch_content(user_id, code)

    elif btype in ['sale', 'special']:
                        # LOGIC CHANGED FOR AUTO PAYMENT IF ADMIN
        if owner == ADMIN_ID:
            credit_val = CREDIT_CONFIG.get("value", 1.0)
            credits_display = round(price / credit_val, 2)
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                types.InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_sale|{code}"),
                types.InlineKeyboardButton("❌ Cancel", callback_data="user_main_back")
            )
            bot.send_message(user_id, f"*💰 Buy This File*\n\nCost: {credits_display} Credits\n\nAre you sure you want to unlock this file?", 
                             reply_markup=kb)
        else:
            upi_info = get_user_upi(owner) or "❌ Owner UPI Not Set"
            if upi_info.startswith("❌"):
                bot.send_message(user_id, "❌ Owner hasn't set payment details.")
                return
            active_user_code[user_id] = code
            bot.send_message(user_id, f"*💰 Paid Content*\nPrice: ₹{price}\n\n*Pay to:* `{upi_info}`\n\nSend Screenshot to unlock.")

# ---------------- USER MENUS & DASHBOARD ----------------
@bot.message_handler(commands=["shortner", "shortener"])
def cmd_shortner(message):
    uid = message.from_user.id
    if is_banned(uid): return
    
    # Check if user has set their shortener
    u = users_col.find_one({"_id": uid})
    s = u.get("personal_shortener", {})
    if not s.get("api") or not s.get("url"):
        bot.send_message(uid, "❌ *Shortener Not Set!*\nPehle Dashboard -> Shortener me apni API aur Domain set karein.")
        return

    user_states[uid] = {'state': 'batch_collect', 'type': 'shortner_link', 'owner': uid, 'files': []}
    bot.send_message(uid, "*🔗 Shortener Link Mode*\nSend files now. Click Done when finished.", reply_markup=done_kb())

@bot.message_handler(commands=["redeem"])
def cmd_redeem(message):
    uid = message.from_user.id
    if is_banned(uid): return
    args = message.text.split()
    if len(args) < 2:
        usage_text = (
            "🏷 *How to Redeem a Code*\n\n"
            "To use a gift code or promotional voucher, please use the following format:\n\n"
            "👉 `/redeem YOUR_CODE_HERE`\n\n"
            "*Example:*\n"
            "`/redeem WELCOME100`\n\n"
            "💡 _Note: Codes are case-sensitive and can only be used once._"
        )
        bot.send_message(uid, usage_text, parse_mode="Markdown")
        return
    
    code = args[1].strip()
    redeem = redeems_col.find_one({"_id": code})
    
    if not redeem:
        bot.send_message(uid, "❌ *Invalid or Expired Code!*")
        return
    
    # Check expiry
    if datetime.now() > redeem['expiry']:
        bot.send_message(uid, "❌ *This code has expired!*")
        return
    
    # Check if used
    u = users_col.find_one({"_id": uid})
    if code in u.get("used_redeems", []):
        bot.send_message(uid, "❌ *You have already used this code!*")
        return
    
    # Process Redeem
    credits_to_add = redeem.get("credits", 0)
    bonus_to_set = redeem.get("bonus", 0)
    expiry_time = redeem.get("expiry") # Keep the same expiry as the code
    
    credit_val = CREDIT_CONFIG.get("value", 1.0)
    rs_to_add = credits_to_add * credit_val
    
    add_credits(uid, rs_to_add)
    if bonus_to_set > 0:
        users_col.update_one({"_id": uid}, {"$set": {"bonus_percent": bonus_to_set, "bonus_expiry": expiry_time}})
    
    # Mark as used
    users_col.update_one({"_id": uid}, {"$push": {"used_redeems": code}})
    
    msg = "✅ *Redeem Successful!*\n\n"
    if credits_to_add > 0: msg += f"💰 Added: `{credits_to_add}` Credits\n"
    if bonus_to_set > 0: msg += f"🎁 Bonus Set: `{bonus_to_set}%` extra on next purchases!"
    
    bot.send_message(uid, msg, parse_mode="Markdown")

@bot.message_handler(commands=["genpaid"])
def cmd_genpaid(message):
    uid = message.from_user.id
    if is_banned(uid): return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_input_process"))
    if uid == ADMIN_ID:
        user_states[uid] = {'state': 'waiting_price', 'owner': ADMIN_ID, 'type': 'sale'}
        bot.send_message(uid, "💰 *Admin Paid Mode*\nEnter Price (in Credits):", reply_markup=kb)
    elif is_premium(uid):
        user_states[uid] = {'state': 'waiting_price', 'owner': uid, 'type': 'special'}
        bot.send_message(uid, "💰 *Pro Paid Mode*\nEnter Price (₹):", reply_markup=kb)
    else:
        bot.send_message(uid, "❌ *Premium Required!*", reply_markup=get_plan_kb())

@bot.message_handler(commands=["genpublic"])
def cmd_genpublic(message):
    uid = message.from_user.id
    if is_banned(uid): return
    if uid == ADMIN_ID:
        user_states[uid] = {'state': 'batch_collect', 'type': 'public', 'owner': ADMIN_ID, 'files': []}
        bot.send_message(uid, "*🔓 Admin Public Mode*\nSend files now. Click Done.", reply_markup=done_kb())
    elif is_premium(uid):
        user_states[uid] = {'state': 'batch_collect', 'type': 'normal', 'owner': uid, 'files': []}
        bot.send_message(uid, "*🔓 Pro Public Mode*\nSend files now. Click Done.", reply_markup=done_kb())
    else:
        bot.send_message(uid, "❌ *Premium Required!*", reply_markup=get_plan_kb())

@bot.message_handler(commands=["prm"])
def cmd_prm(message):
    uid = message.from_user.id
    if is_banned(uid): return
    if uid == ADMIN_ID:
        user_states[uid] = {'state': 'batch_collect', 'type': 'premium', 'owner': ADMIN_ID, 'files': []}
        bot.send_message(uid, "*👑 Admin Premium Mode*\nSend files now. Click Done.", reply_markup=done_kb())
    else:
        bot.send_message(uid, "❌ *Admin Only Command!*")

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast_direct(message):
    uid = message.from_user.id
    if uid != ADMIN_ID: return
    
    # Show broadcast menu
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("All Users", callback_data="bc_all"), 
        types.InlineKeyboardButton("P+ (Premium)", callback_data="bc_prem"), 
        types.InlineKeyboardButton("🗑 Del 1h", callback_data="bc_del_1h"), 
        types.InlineKeyboardButton("🗑 Del 12h", callback_data="bc_del_12h"), 
        types.InlineKeyboardButton("🗑 Del All", callback_data="bc_del_all"), 
        types.InlineKeyboardButton("🔙 Back", callback_data="close_panel")
    )
    bot.send_message(uid, "*📢 Broadcast Menu*", reply_markup=kb, parse_mode="Markdown")

@bot.message_handler(commands=["alive"])
def alive_cmd(message):
    if message.from_user.id == ADMIN_ID:
        send_admin_panel(message.from_user.id)

# --- PROOF COMMAND ---
@bot.message_handler(commands=["proof"])
def cmd_proof(message):
    uid = message.from_user.id

    # 1. Check karein ki user Premium hai ya Admin
    if uid != ADMIN_ID and not is_premium(uid):
        bot.send_message(uid, "❌ *Premium Required!*")
        return

    # 2. Database se proofs dhoondo
    proofs = list(pro_proofs_col.find({"owner_id": uid}))

    if not proofs:
        bot.send_message(uid, "📂 *Koi naya Proof nahi hai.* (No pending proofs)")
        return

    bot.send_message(uid, f"🔄 Fetching {len(proofs)} pending proofs...")

    # 3. Saare proofs dikhao (Same button logic ke sath)
    for data in proofs:
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Success", callback_data=f"proof_ok|{data['_id']}"),
            types.InlineKeyboardButton("❌ Reject", callback_data=f"proof_no|{data['_id']}")
        )

        cap = (f"📩 *Payment Proof*\n\n"
               f"👤 UserID: `{data['user_id']}`\n"
               f"💰 Price: ₹{data.get('price', 'N/A')}\n"
               f"📂 Code: `{data.get('code', 'N/A')}`")

        try:
            bot.send_photo(uid, data['photo'], caption=cap, reply_markup=kb)
        except:
            bot.send_message(uid, f"{cap}\n[Photo Failed]", reply_markup=kb)


def send_user_dashboard(user_id, chat_id, message_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("⚡ Gen Link", callback_data="gen_pro_menu"),
        types.InlineKeyboardButton("Set Payment", callback_data="pay_pro_menu"),
        types.InlineKeyboardButton("Proof 📸", callback_data="manual_proof_menu"),
        types.InlineKeyboardButton("Back 🔙", callback_data="user_main_back")
    )
    try:
        bot.edit_message_text("*👤 User Dashboard*", chat_id, message_id, reply_markup=kb)
    except:
        bot.send_message(chat_id, "*👤 User Dashboard*", reply_markup=kb)

def get_plan_kb():
    credit_val = CREDIT_CONFIG.get("value", 1.0)
    kb = types.InlineKeyboardMarkup(row_width=2)
    for k, v in PLANS.items():
        credits_display = round(v / credit_val, 2)
        kb.add(types.InlineKeyboardButton(f"{k} Days - {credits_display} Credits", callback_data=f"buy_plan|{k}"))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="user_main_back"))
    return kb

# ---------------- ADMIN PANEL ----------------
def send_admin_panel(admin_id, msg_id_to_edit=None):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔗 Gen Link", callback_data="gen_shortcut"),
        types.InlineKeyboardButton("📢 Broadcast", callback_data="panel_broadcast"),
        types.InlineKeyboardButton("🎁 Redeem System", callback_data="panel_redeem"),
        types.InlineKeyboardButton("📨 Reports", callback_data="panel_reports"),
        types.InlineKeyboardButton("📝 Log Channels", callback_data="panel_logs"),
        types.InlineKeyboardButton("⚙️ Settings", callback_data="panel_settings"),
        types.InlineKeyboardButton("📊 Status", callback_data="panel_stats")
    )
    text = "*🤖 Admin Control Panel*"
    if msg_id_to_edit: 
        try: bot.edit_message_text(text, admin_id, msg_id_to_edit, reply_markup=kb)
        except: bot.send_message(admin_id, text, reply_markup=kb)
    else: bot.send_message(admin_id, text, reply_markup=kb)

def send_settings_panel(admin_id, msg_id_to_edit):
    t_str = "30 Mins" if DELETE_CONFIG["minutes"] == 30 else "2 Hours"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💳 Credit System", callback_data="panel_credits"),
        types.InlineKeyboardButton("Token Verification", callback_data="panel_token"),
    )
    kb.add(
        types.InlineKeyboardButton("Start Msg 💬", callback_data="panel_start_msg"),
        types.InlineKeyboardButton("Force Join ➕", callback_data="panel_force"),
        types.InlineKeyboardButton("Custom Button 🔘", callback_data="panel_custom_btn"),
        types.InlineKeyboardButton(f"⏱ Set Time ({t_str})", callback_data="panel_timer"),
        types.InlineKeyboardButton("🚫 Ban/Unban", callback_data="panel_ban"),
        types.InlineKeyboardButton("💲 Edit Plans", callback_data="panel_plans"),
        types.InlineKeyboardButton("🔗 Edit Payment Link", callback_data="panel_payment_link"),
        types.InlineKeyboardButton("🔙 Back", callback_data="close_panel")
    )
    bot.edit_message_text("*⚙️ Settings Menu*", admin_id, msg_id_to_edit, reply_markup=kb)

# ---------------- ROUTER ----------------
@bot.callback_query_handler(func=lambda c: True)
def router_callback(call):
    # 1. Sabse Pehle Loading Band Karein
    try: bot.answer_callback_query(call.id)
    except: pass 

    # 2. Variables Set Karein
    action = call.data
    uid = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
                # ==========================================
    #  FINAL USER DASHBOARD & PAYMENT SYSTEM
    # ==========================================

    # --- 1. USER DASHBOARD (Smooth Edit) ---
    if action == "user_dashboard":
        if uid in user_states: del user_states[uid]
        if is_premium(uid) or uid == ADMIN_ID:
            text = "👤 *User Dashboard*\n\nSelect an option:"
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                types.InlineKeyboardButton("⚡ Gen Link", callback_data="gen_pro_menu"),
                types.InlineKeyboardButton("🔗 Shortener", callback_data="user_short_menu"),
                types.InlineKeyboardButton("💳 Set Payment", callback_data="pay_pro_menu"),
                types.InlineKeyboardButton("📸 Proof", callback_data="manual_proof_menu"),
                types.InlineKeyboardButton("🔙 Back", callback_data="user_main_back")
            )
            # Try Edit first, Fallback to Delete-Send
            try: bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
            except: 
                try: bot.delete_message(chat_id, msg_id)
                except: pass
                bot.send_message(chat_id, text, reply_markup=kb)
        else:
            # Non-Premium Logic
            try: bot.edit_message_text("❌ *Premium Required*\n\nSelect a plan to access:", chat_id, msg_id, reply_markup=get_plan_kb())
            except: bot.send_message(chat_id, "❌ *Premium Required*\n\nSelect a plan to access:", reply_markup=get_plan_kb())
        return

    # --- 2. PREMIUM STATUS ---
    if action == "user_menu_prem":
        prem_active = is_premium(uid)
        status = "✅ Active" if prem_active else "❌ Inactive"
        exp = get_premium_expiry(uid)
        text = f"👑 *Premium Status*\n\n🔹 Status: {status}\n⏳ Expires: {exp}"
        
        kb = types.InlineKeyboardMarkup()
        if not prem_active:
            kb.add(types.InlineKeyboardButton("💎 Buy Premium", callback_data="show_plans"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="user_main_back"))
        
        try: bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
        except: 
            try: bot.delete_message(chat_id, msg_id)
            except: pass
            bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
        return

    # --- 3. SHOW PLANS ---
    if action == "show_plans":
        if uid in active_user_code: del active_user_code[uid]
        try: bot.edit_message_text("💎 *Select a Premium Plan:*", chat_id, msg_id, parse_mode="Markdown", reply_markup=get_plan_kb())
        except: bot.send_message(chat_id, "💎 *Select a Premium Plan:*", parse_mode="Markdown", reply_markup=get_plan_kb())
        return

    # --- 4. BACK BUTTON (Universal Logic) ---
    if action == "user_main_back":
        if uid in active_user_code: del active_user_code[uid]
        user = call.from_user
        safe_name = user.first_name.replace("_", "\\_").replace("*", "\\*").replace("[", "").replace("]", "")
        mention = f"[{safe_name}](tg://user?id={uid})"
        
        text_content = START_CONFIG["text"].replace("{mention}", mention)
        pic = START_CONFIG.get("pic")

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("💳 My Credits", callback_data="user_menu_credits"),
        )
        markup.add(
            types.InlineKeyboardButton("Dashboard 👤", callback_data="user_dashboard"),
            types.InlineKeyboardButton("👑 Premium Status", callback_data="user_menu_prem"),
            types.InlineKeyboardButton("📞 Contact", callback_data="user_menu_supp"),
        )
        custom_kb = get_custom_markup()
        if custom_kb:
            for row in custom_kb.keyboard: markup.keyboard.append(row)

        if pic:
            try: bot.delete_message(chat_id, msg_id)
            except: pass
            bot.send_photo(chat_id, pic, caption=text_content, reply_markup=markup)
        else:
            try: bot.edit_message_text(text_content, chat_id, msg_id, parse_mode="Markdown", reply_markup=markup)
            except:
                try: bot.delete_message(chat_id, msg_id)
                except: pass
                bot.send_message(chat_id, text_content, reply_markup=markup)
        return

    # --- 5. CONTACT SUPPORT ---
    if action == "user_menu_supp":
        user_support_state[uid] = True
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Back / Cancel", callback_data="cancel_input_process"))
        bot.edit_message_text("📝 *Describe Your Issue:*\n\nPlease write your message or send a screenshot. Our support team will get back to you soon.", chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
        return

    # --- 6. BUY PLAN INVOICE (With VIEWING State) ---
    if action.startswith("buy_plan|"):
        plan = action.split("|")[1]
        price_rs = PLANS.get(plan, 0)
        credit_val = CREDIT_CONFIG.get("value", 1.0)
        req_credits = round(price_rs / credit_val, 2)
        
        text = (f"*💎 Buy Premium Plan: {plan} Days*\n"
                f"💰 Cost: {req_credits} Credits\n\n"
                f"Are you sure you want to buy this plan?")
        
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_plan|{plan}"),
            types.InlineKeyboardButton("❌ Cancel", callback_data="show_plans")
        )
        
        bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
        return

    if action.startswith("confirm_plan|"):
        plan = action.split("|")[1]
        req_rs = PLANS.get(plan, 0)
        credit_val = CREDIT_CONFIG.get("value", 1.0)
        
        current_rs = get_credits(uid)
        if current_rs >= req_rs:
            add_credits(uid, -req_rs)
            days = PLAN_DAYS.get(plan, 0)
            set_premium(uid, days)
            bot.answer_callback_query(call.id, "🎉 Plan Activated successfully!", show_alert=True)
            try: bot.delete_message(chat_id, msg_id)
            except: pass
            send_custom_welcome(uid)
        else:
            req_credits = round(req_rs / credit_val, 2)
            current_credits = round(current_rs / credit_val, 2)
            bot.answer_callback_query(call.id, "❌ Insufficient Credits!", show_alert=True)
            text = f"❌ *Insufficient Credits*\n\nYou need {req_credits} Credits but have {current_credits}."
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("💰 Buy Credits", callback_data="buy_credits"))
            kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="show_plans"))
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
        return
        
    if action.startswith("confirm_sale|"):
        code = action.split("|")[1]
        batch = batches_col.find_one({"_id": code})
        if not batch: return
        req_rs = batch.get('price', 0)
        credit_val = CREDIT_CONFIG.get("value", 1.0)
        
        current_rs = get_credits(uid)
        if current_rs >= req_rs:
            add_credits(uid, -req_rs)
            send_batch_content(uid, code)
            bot.answer_callback_query(call.id, "✅ File Delivered!", show_alert=True)
            try: bot.delete_message(chat_id, msg_id)
            except: pass
        else:
            req_credits = round(req_rs / credit_val, 2)
            current_credits = round(current_rs / credit_val, 2)
            bot.answer_callback_query(call.id, "❌ Insufficient Credits!", show_alert=True)
            text = f"❌ *Insufficient Credits*\n\nYou need {req_credits} Credits but have {current_credits}."
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("💰 Buy Credits", callback_data="buy_credits"))
            kb.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="user_main_back"))
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
        return

    # --- 1A. MY CREDITS ---
    if action == "user_menu_credits":
        if uid in active_user_code: del active_user_code[uid]
        balance_rs = get_credits(uid)
        credit_val = CREDIT_CONFIG.get("value", 1.0)
        credits_display = round(balance_rs / credit_val, 2)
        text = f"💳 *My Wallet*\n\n💰 Balance: `{credits_display}` Credits"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("💰 Buy Credits", callback_data="buy_credits"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="user_main_back"))
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
        except:
            try: bot.edit_message_text(text, chat_id, msg_id, parse_mode=None, reply_markup=kb)
            except: 
                try: bot.send_message(chat_id, text, reply_markup=kb)
                except: pass
        return

    # --- 1B. BUY CREDITS INVOICE ---
    if action == "buy_credits":
        active_user_code[uid] = "CREDIT_VIEWING_0_0"
        credit_val = CREDIT_CONFIG.get("value", 1.0)
        text = (f"*💰 Buy Credits*\n\n"
                f"1 Credit = ₹{credit_val}\n"
                f"You can pay ANY amount. Credits will be added accordingly.\n\n"
                f"1. Click 'Pay Now'.\n"
                f"2. Come back & click 'I Have Paid'.")
        
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("💳 Pay Now", url=PAYMENT_LINK))
        kb.add(types.InlineKeyboardButton("✅ I Have Paid", callback_data="i_have_paid"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="user_menu_credits"))
        
        bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
        return

    # --- 7. I HAVE PAID (Switch to PENDING State) ---
    if action == "i_have_paid":
        session = active_user_code.get(uid)
        if not session:
            # Agar restart ki wajah se session udd gaya
            bot.answer_callback_query(call.id, "❌ Session Expired. Please Start Again.")
            return

        # Magic: Switch VIEWING -> PENDING (Ab bot Email lega)
        active_user_code[uid] = session.replace("VIEWING", "PENDING")

        kb = types.InlineKeyboardMarkup()
        # Back button will go to 'step_back_to_invoice'
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="step_back_to_invoice"))
        
        bot.edit_message_text("📧 *Enter Payment Email:*\n\nPlease provide the email address used during payment for verification.", chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
        return

    # --- 8. STEP BACK TO INVOICE (Switch back to VIEWING) ---
    if action == "step_back_to_invoice":
        session = active_user_code.get(uid)
        if not session:
            send_custom_welcome(uid)
            return

        # Magic: Switch PENDING -> VIEWING (Ab bot random text ignore karega)
        active_user_code[uid] = session.replace("PENDING", "VIEWING")
        
        parts = session.split("_")
        type_ = parts[0]
        name = parts[2]
        try: price = parts[3]
        except: price = "0"
        
        # Reconstruct Invoice
        if type_ == "CREDIT":
            credit_val = CREDIT_CONFIG.get("value", 1.0)
            text = (f"*💰 Buy Credits*\n\n"
                    f"1 Credit = ₹{credit_val}\n"
                    f"You can pay ANY amount.\n\n"
                    f"1. Click 'Pay Now'.\n"
                    f"2. Click 'I Have Paid'.")
            back_cb = "user_menu_credits"
        else:
            text = "Process Cancelled"
            back_cb = "user_main_back"

        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("💳 Pay Now", url=PAYMENT_LINK))
        kb.add(types.InlineKeyboardButton("✅ I Have Paid", callback_data="i_have_paid"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data=back_cb))

        bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown", reply_markup=kb)
        return

    # --- 9. CANCEL PROCESS (Cleanup) ---
    if action == "cancel_input_process":
        if uid in active_user_code: del active_user_code[uid]
        if uid in user_support_state: del user_support_state[uid]
        if uid in user_states: del user_states[uid]
        
        user = call.from_user
        safe_name = user.first_name.replace("_", "\\_").replace("*", "\\*").replace("[", "").replace("]", "")
        mention = f"[{safe_name}](tg://user?id={uid})"
        
        text_content = START_CONFIG["text"].replace("{mention}", mention)
        pic = START_CONFIG.get("pic")

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("💳 My Credits", callback_data="user_menu_credits"),
        )
        markup.add(
            types.InlineKeyboardButton("Dashboard 👤", callback_data="user_dashboard"),
            types.InlineKeyboardButton("👑 Premium Status", callback_data="user_menu_prem"),
            types.InlineKeyboardButton("📞 Contact", callback_data="user_menu_supp"),
        )
        custom_kb = get_custom_markup()
        if custom_kb:
            for row in custom_kb.keyboard: markup.keyboard.append(row)

        if pic:
            try: bot.delete_message(chat_id, msg_id)
            except: pass
            bot.send_photo(chat_id, pic, caption=text_content, reply_markup=markup)
        else:
            try: bot.edit_message_text(text_content, chat_id, msg_id, parse_mode="Markdown", reply_markup=markup)
            except:
                try: bot.delete_message(chat_id, msg_id)
                except: pass
                bot.send_message(chat_id, text_content, reply_markup=markup)
        return
    # --- 9. GEN LINK HELPERS (Jo delete ho gaye the unhe wapas lagaya) ---
    if action == "gen_special":
        process_gen_command(uid, chat_id, "special")
        return
    if action == "gen_normal":
        process_gen_command(uid, chat_id, "normal")
        return

    if action == "panel_payment_link":
        bot.send_message(uid, f"🔗 *Current Payment Link:*\n`{PAYMENT_LINK}`\n\nSend new link to edit:", reply_markup=types.ForceReply())
        user_states[uid] = "WAIT_PAYMENT_LINK"
        return
    # Gen Link Logic (Shared)
    if action == "gen_shortcut":
        process_gen_command(uid, chat_id, msg_id)
        return

    # Pro User Handlers
    if action == "gen_pro_menu":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✨ Special (Paid)", callback_data="gen_pro_special"), types.InlineKeyboardButton("🔓 Normal (Free)", callback_data="gen_pro_normal"), types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard"))
        bot.edit_message_text("*Select Link Type:*", chat_id, msg_id, reply_markup=kb)
        return
    if action == "gen_pro_special":
        user_states[uid] = {'state': 'waiting_price', 'owner': uid, 'type': 'special'}
        bot.edit_message_text("💰 Enter Price (₹):", chat_id, msg_id)
        return
    if action == "gen_pro_normal":
        user_states[uid] = {'state': 'batch_collect', 'type': 'normal', 'owner': uid, 'files': []}
        bot.edit_message_text("*Normal Mode (Free)*\nSend files now. Click Done.", chat_id, msg_id, reply_markup=done_kb())
        return
    if action == "pay_pro_menu":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✏️ Set", callback_data="pay_pro_set"), types.InlineKeyboardButton("👀 See", callback_data="pay_pro_see"), types.InlineKeyboardButton("🗑 Delete", callback_data="pay_pro_del"), types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard"))
        bot.edit_message_text("*💳 Payment Settings*", chat_id, msg_id, reply_markup=kb)
        return
    if action == "pay_pro_set":
        user_states[uid] = {'state': 'waiting_upi'}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="pay_pro_menu"))
        bot.edit_message_text("Send your UPI ID:", chat_id, msg_id, reply_markup=kb)
        return
    if action == "pay_pro_see":
        bot.send_message(uid, f"Info: {get_user_upi(uid) or 'Not Set'}")
        return
    if action == "pay_pro_del":
        update_user_upi(uid, None)
        bot.send_message(uid, "Deleted.")
        return

    # User Personal Shortener Menu
    if action == "user_short_menu":
        u = users_col.find_one({"_id": uid})
        s = u.get("personal_shortener", {})
        
        if s.get("api"):
            status = "✅ Active"
            details = f"🔗 *Current Settings:*\n🌐 Domain: `{s.get('url')}`\n🔑 API: `{s.get('api')}`"
        else:
            status = "❌ Not Set"
            details = "_No shortener configured yet._"
            
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✏️ Set API/Domain", callback_data="user_short_set"),
            types.InlineKeyboardButton("🗑 Delete", callback_data="user_short_del")
        )
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard"))
        
        txt = f"*🔗 Personal Shortener Settings*\n\nStatus: {status}\n\n{details}\n\nSet your own shortener to generate links with `/shortner`."
        bot.edit_message_text(txt, chat_id, msg_id, reply_markup=kb, parse_mode="Markdown")
        return

    if action == "user_short_set":
        user_states[uid] = {'state': 'waiting_user_short_api'}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_user_short"))
        bot.edit_message_text("Send your Shortener API Key:", chat_id, msg_id, reply_markup=kb)
        return

    if action == "cancel_user_short":
        if uid in user_states: del user_states[uid]
        # Refresh the Personal Shortener Menu
        u = users_col.find_one({"_id": uid})
        s = u.get("personal_shortener", {})
        if s.get("api"):
            status = "✅ Active"
            details = f"🔗 *Current Settings:*\n🌐 Domain: `{s.get('url')}`\n🔑 API: `{s.get('api')}`"
        else:
            status = "❌ Not Set"
            details = "_No shortener configured yet._"
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✏️ Set API/Domain", callback_data="user_short_set"),
            types.InlineKeyboardButton("🗑 Delete", callback_data="user_short_del")
        )
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard"))
        txt = f"*🔗 Personal Shortener Settings*\n\nStatus: {status}\n\n{details}\n\nSet your own shortener to generate links with `/shortner`."
        bot.edit_message_text(txt, chat_id, msg_id, reply_markup=kb, parse_mode="Markdown")
        return

    if action == "user_short_see":
        # Removed as requested
        return

    if action == "user_short_del":
        users_col.update_one({"_id": uid}, {"$set": {"personal_shortener": {"api": None, "url": None}}})
        bot.answer_callback_query(call.id, "✅ Shortener deleted!", show_alert=True)
        # Refresh menu
        router_callback(call) 
        return

    # Pro Proof Log (MANUAL PROOF MENU)
    if action == "manual_proof_menu":
        proofs = list(pro_proofs_col.find({"owner_id": uid}))
        if not proofs:
            bot.answer_callback_query(call.id, "❌ No pending proofs found!")
            return

        bot.answer_callback_query(call.id, "Fetching proofs...")

        for data in proofs:
            kb = types.InlineKeyboardMarkup()
            kb.add(
                types.InlineKeyboardButton("✅ Success", callback_data=f"proof_ok|{data['_id']}"),
                types.InlineKeyboardButton("❌ Reject", callback_data=f"proof_no|{data['_id']}")
            )

            cap = (f"📩 *Payment Proof*\n\n"
                   f"👤 UserID: `{data['user_id']}`\n"
                   f"💰 Price: ₹{data.get('price', 'N/A')}\n"
                   f"📂 Code: `{data.get('code', 'N/A')}`")

            try:
                bot.send_photo(uid, data['photo'], caption=cap, reply_markup=kb)
            except:
                bot.send_message(uid, f"{cap}\n[Photo Failed]", reply_markup=kb)
        return

      # SUCCESS / REJECT BUTTONS
    if action.startswith("proof_ok|") or action.startswith("proof_no|"):
        act, pid = action.split("|")
        proof = pro_proofs_col.find_one({"_id": pid})

        if not proof:
            bot.answer_callback_query(call.id, "This process is already completed.")
            try: bot.delete_message(chat_id, msg_id)
            except: pass
            return

        buyer_id = proof['user_id']

        if act == "proof_ok":
            # 1. File bhejo
            send_batch_content(buyer_id, proof['code'])
            
            # 2. Buyer ko batao
            try: bot.send_message(buyer_id, "✅ *Payment Accepted!*\nThe requested files have been delivered.")
            except: pass
            
            # 3. Seller Message Update (FIXED HERE)
            try:
                bot.edit_message_caption("✅ ACCEPTED & DELIVERED", chat_id, msg_id)
            except:
                # Agar Caption edit na ho (matlab ye text message hai), to Text edit karo
                bot.edit_message_text("✅ ACCEPTED & DELIVERED", chat_id, msg_id)
        
        else: # Reject Logic
            # 1. Buyer ko batao
            try: bot.send_message(buyer_id, "❌ *Payment Rejected!*\nThe payment could not be verified. Please check your details and try again.")
            except: pass
            
            # 2. Seller Message Update (FIXED HERE)
            try:
                bot.edit_message_caption("❌ REJECTED", chat_id, msg_id)
            except:
                bot.edit_message_text("❌ REJECTED", chat_id, msg_id)

        # Database se delete aur cleanup
        pro_proofs_col.delete_one({"_id": pid})
        time.sleep(2)
        try: bot.delete_message(chat_id, msg_id)
        except: pass
        return


        # --- BATCH SAVE (DONE BUTTON) WITH CLEANUP ---
    if action == "batch_save":
        state = user_states.get(uid)

        # 1. Session Check
        if not state or not state.get('files'):
            bot.answer_callback_query(call.id, "❌ Session Expired!", show_alert=True)
            try: bot.delete_message(chat_id, msg_id)
            except: pass
            return

        bot.answer_callback_query(call.id, "Generating Link...")

        # 2. Data Collect
        code = gen_code()
        batch_type = state.get('type', 'normal')
        price = state.get('price', 0)
        owner_id = state.get('owner', uid)
        files = state['files']
        
        # --- CLEANUP START (Ye naya magic hai) ---
        # Jo buttons delete hone se reh gaye, unhe ab uda do
        btn_ids = state.get('btn_ids', [])
        
        def clean_old_buttons(c_id, ids, current_msg_id):
            for mid in ids:
                if mid == current_msg_id: continue # Done button ko mat udana
                try: 
                    bot.delete_message(c_id, mid)
                    time.sleep(0.05) # Thoda sa gap taaki Telegram block na kare
                except: pass
        
        # Background mein delete karo
        threading.Thread(target=clean_old_buttons, args=(chat_id, btn_ids, msg_id)).start()
        # --- CLEANUP END ---

        # 3. Database Save
        batches_col.insert_one({
            '_id': code,
            'type': batch_type,
            'price': price,
            'owner_id': owner_id,
            'files': files,
            'created_at': datetime.now()
        })

        # 4. Response Message
        if batch_type == "shortner_link":
            # Generate shortened link using USER'S personal shortener
            u = users_col.find_one({"_id": uid})
            s = u.get("personal_shortener", {})
            bot_start_link = f"https://t.me/{BOT_USERNAME}?start=sl_{code}"
            final_link = get_short_link(bot_start_link, s)
            msg = (f"✅ *Shortener Link Generated!*\n\n"
                   f"🔗 `{final_link}`\n"
                   f"📂 Files: {len(files)}\n"
                   f"⚠️ *Note:* This link will bypass bot's global verification.")
        else:
            link = f"https://t.me/{BOT_USERNAME}?start={code}"
            warning = ""
            if batch_type == 'special' and not get_user_upi(owner_id):
                warning = "\n⚠️ *Warning:* UPI ID missing!"

            msg = (f"✅ *Link Generated!*\n"
                   f"🔗 `{link}`\n"
                   f"📂 Files: {len(files)}\n"
                   f"💰 Price: ₹{price}"
                   f"{warning}")

        # Current message ko edit karke Link dikha do
        try: bot.edit_message_text(msg, chat_id, msg_id)
        except: bot.send_message(chat_id, msg)

        # 5. Clear Memory
        del user_states[uid]
        return
    # --- ADMIN CHECK (Iske neeche sirf Admin logic rahega) ---
    if uid != ADMIN_ID: return 

    if action == "panel_settings":
        send_settings_panel(uid, msg_id)
        return
    if action == "close_panel":
        send_admin_panel(uid, msg_id)
        return

    # Custom Button
    if action == "panel_custom_btn":
        kb = types.InlineKeyboardMarkup()
        btn = CUSTOM_BTN_CONFIG.get("text")
        if not btn:
            kb.add(types.InlineKeyboardButton("➕ Add Button", callback_data="cb_add"))
        else:
            kb.add(types.InlineKeyboardButton("👀 See", callback_data="cb_see"), types.InlineKeyboardButton("🗑 Remove", callback_data="cb_rem"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings"))

        msg = (
            "🔘 *Custom Button Settings*\n\n"
            "Format to Copy & Send:\n"
            "`[Button Name][buttonurl:https://link.com]`\n\n"
            "Double Button:\n"
            "`[Btn 1][buttonurl:link][Btn 2][buttonurl:link]`"
        )
        bot.edit_message_text(msg, chat_id, msg_id, reply_markup=kb)
        return

    if action == "cb_add":
        user_states[uid] = {'state': 'waiting_custom_btn'}
        bot.send_message(uid, "Send your button text format:")
        return

    if action == "cb_see":
        markup = get_custom_markup()
        bot.send_message(uid, f"Current Button Text:\n`{CUSTOM_BTN_CONFIG.get('text')}`\n\nPreview Below:", reply_markup=markup)
        return

    if action == "cb_rem":
        CUSTOM_BTN_CONFIG["text"] = None
        save_setting("custom_btn", CUSTOM_BTN_CONFIG)
        bot.send_message(uid, "✅ Custom button removed.")
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➕ Add Button", callback_data="cb_add"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings"))
        bot.send_message(uid, "Refreshed Panel:", reply_markup=kb)
        return

    # Force Join
    if action == "panel_force":
        status = "✅ Active" if CHANNEL_CONFIG.get("active") else "❌ Inactive"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for idx, ch in enumerate(CHANNEL_CONFIG.get("channels", [])):
            kb.add(types.InlineKeyboardButton(f"📺 {ch['title']}", callback_data=f"fj_view_{idx}"))
        kb.add(types.InlineKeyboardButton("+ Add Channel +", callback_data="fj_add"))
        kb.add(types.InlineKeyboardButton(f"Force Join: {status}", callback_data="fj_toggle"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings"))
        bot.edit_message_text("*❪ SET CHANNEL ❫*", chat_id, msg_id, reply_markup=kb)
        return
    if action == "fj_toggle":
        CHANNEL_CONFIG["active"] = not CHANNEL_CONFIG.get("active", False)
        save_setting("channel", CHANNEL_CONFIG)
        status = "✅ Active" if CHANNEL_CONFIG.get("active") else "❌ Inactive"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for idx, ch in enumerate(CHANNEL_CONFIG.get("channels", [])):
            kb.add(types.InlineKeyboardButton(f"📺 {ch['title']}", callback_data=f"fj_view_{idx}"))
        kb.add(types.InlineKeyboardButton("+ Add Channel +", callback_data="fj_add"))
        kb.add(types.InlineKeyboardButton(f"Force Join: {status}", callback_data="fj_toggle"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings"))
        bot.edit_message_text("*❪ SET CHANNEL ❫*", chat_id, msg_id, reply_markup=kb)
        return
    if action == "fj_add":
        user_states[uid] = {'state': 'waiting_fj_forward'}
        bot.send_message(uid, "Forward a message from your channel:")
        return
    if action.startswith("fj_view_"):
        idx = int(action.split("_")[2])
        try:
            ch = CHANNEL_CONFIG["channels"][idx]
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🗑 Remove Channel", callback_data=f"fj_rem_{idx}"))
            kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="panel_force"))
            txt = f"Title: {ch['title']}\nID: `{ch['id']}`"
            bot.edit_message_text(txt, chat_id, msg_id, reply_markup=kb)
        except: pass
        return
    if action.startswith("fj_rem_"):
        idx = int(action.split("_")[2])
        try:
            del CHANNEL_CONFIG["channels"][idx]
            save_setting("channel", CHANNEL_CONFIG)
            bot.send_message(uid, "Removed.")
        except: pass
        return

    # GEN LINK ADMIN
    if action.startswith("gen_adm_"):
        btype = action.split("_")[2] 
        if btype == "sale":
            user_states[uid] = {'state': 'waiting_price', 'owner': ADMIN_ID, 'type': 'sale'}
            bot.edit_message_text("💰 Enter Price (in Credits):", chat_id, msg_id)
        else:
            b_type_map = {'prem': 'premium', 'pub': 'public'}
            user_states[uid] = {'state': 'batch_collect', 'type': b_type_map[btype], 'owner': ADMIN_ID, 'files': []}
            bot.edit_message_text(f"*{btype.title()} Mode*\nSend files now. Click Done.", chat_id, msg_id, reply_markup=done_kb())
        return

    # REDEEM SYSTEM
    if action == "panel_redeem":
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("➕ Create Code", callback_data="redeem_create"),
            types.InlineKeyboardButton("🗑 Delete Code", callback_data="redeem_delete"),
            types.InlineKeyboardButton("📜 List All", callback_data="redeem_list"),
            types.InlineKeyboardButton("🔙 Back", callback_data="close_panel")
        )
        bot.edit_message_text("*🎁 Redeem System Menu*", chat_id, msg_id, reply_markup=kb)
        return

    if action == "redeem_create":
        user_states[uid] = {'state': 'waiting_redeem_name', 'msg_id': msg_id}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_redeem"))
        bot.edit_message_text("⌨️ *Step 1:* Enter Code Name (e.g. WELCOME50):", chat_id, msg_id, reply_markup=kb, parse_mode="Markdown")
        return

    if action == "redeem_delete":
        user_states[uid] = {'state': 'waiting_redeem_del'}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_redeem"))
        bot.edit_message_text("🗑 Enter Code Name to Delete:", chat_id, msg_id, reply_markup=kb)
        return

    if action == "cancel_admin_redeem":
        if uid in user_states: del user_states[uid]
        # Go back to Redeem System menu
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("➕ Create Code", callback_data="redeem_create"),
            types.InlineKeyboardButton("🗑 Delete Code", callback_data="redeem_delete"),
            types.InlineKeyboardButton("📜 List All", callback_data="redeem_list"),
            types.InlineKeyboardButton("🔙 Back", callback_data="close_panel")
        )
        bot.edit_message_text("*🎁 Redeem System Menu*", chat_id, msg_id, reply_markup=kb)
        return

    if action == "redeem_list":
        redeems = list(redeems_col.find())
        if not redeems:
            bot.answer_callback_query(call.id, "❌ No active codes!")
            return
        
        txt = "📜 *Active Redeem Codes:*\n\n"
        for r in redeems:
            exp = r['expiry'].strftime("%d-%b %I:%M %p")
            txt += f"🔹 `{r['_id']}`\n   💰 {r.get('credits', 0)} Cr | 🎁 {r.get('bonus', 0)}%\n   ⌛ Exp: {exp}\n\n"
        
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="panel_redeem"))
        bot.edit_message_text(txt, chat_id, msg_id, reply_markup=kb)
        return

    # BAN SYSTEM
    if action == "panel_ban":
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("🚫 Ban User", callback_data="ban_add"),
               types.InlineKeyboardButton("✅ Unban User", callback_data="ban_remove"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings"))
        banned_count = users_col.count_documents({"is_banned": True})
        bot.edit_message_text(f"*🚫 User Ban System*\nBanned: {banned_count}", chat_id, msg_id, reply_markup=kb)
    elif action == "ban_add":
        user_states[uid] = {'state': 'waiting_ban_id'}
        bot.send_message(uid, "Send User ID to BAN:")
    elif action == "ban_remove":
        user_states[uid] = {'state': 'waiting_unban_id'}
        bot.send_message(uid, "Send User ID to UNBAN:")

    # CREDIT SYSTEM
    if action == "panel_credits":
        credit_val = CREDIT_CONFIG.get("value", 1.0)
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton(f"💲 Set Credit Value (₹{credit_val})", callback_data="credit_set_val"),
            types.InlineKeyboardButton("➕ Add Credit Manually", callback_data="credit_add_manual"),
            types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings")
        )
        bot.edit_message_text("*💳 Credit System Configuration*", chat_id, msg_id, reply_markup=kb)
        return
        
    if action == "credit_set_val":
        user_states[uid] = {'state': 'waiting_credit_val'}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_credit"))
        bot.edit_message_text("Send new value for 1 Credit (in ₹):", chat_id, msg_id, reply_markup=kb)
        return
        
    if action == "credit_add_manual":
        user_states[uid] = {'state': 'waiting_credit_user'}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_credit"))
        bot.edit_message_text("Send User ID to add credits:", chat_id, msg_id, reply_markup=kb)
        return

    if action == "cancel_admin_credit":
        if uid in user_states: del user_states[uid]
        # Go back to Credit System menu
        credit_val = CREDIT_CONFIG.get("value", 1.0)
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton(f"💲 Set Credit Value (₹{credit_val})", callback_data="credit_set_val"),
            types.InlineKeyboardButton("➕ Add Credit Manually", callback_data="credit_add_manual"),
            types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings")
        )
        bot.edit_message_text("*💳 Credit System Configuration*", chat_id, msg_id, reply_markup=kb)
        return

    # TOKEN
    if action == "panel_token":
        status = "✅ On" if SHORTNER_CONFIG.get("active") else "❌ Off"
        validity = SHORTNER_CONFIG.get("validity", 12)
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("Manage Shorteners", callback_data="tok_short_list"),
            types.InlineKeyboardButton(f"Verify Time ({validity}h)", callback_data="tok_time"),
            types.InlineKeyboardButton(f"Status ({status})", callback_data="tok_onoff"),
            types.InlineKeyboardButton("Verify Tutorial", callback_data="tok_tut"),
            types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings")
        )
        bot.edit_message_text("*🔐 Token Verification Settings*", chat_id, msg_id, reply_markup=kb)
        return

    if action == "tok_short_list":
        shorteners = SHORTNER_CONFIG.get("shorteners", [])
        kb = types.InlineKeyboardMarkup(row_width=1)
        for i in range(4):
            # Check if this slot exists
            if i < len(shorteners):
                s = shorteners[i]
                btn_txt = f"S{i+1}: {s['url'][:15]}... (Edit)"
            else:
                btn_txt = f"S{i+1}: Not Set (Add)"
            kb.add(types.InlineKeyboardButton(btn_txt, callback_data=f"tok_edit_{i}"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="panel_token"))
        
        # Show list in caption
        list_txt = "🔗 *Shorteners List:*\n\n"
        for i in range(4):
            if i < len(shorteners):
                s = shorteners[i]
                list_txt += f"{i+1}. `{s['url']}`\n   API: `{s['api'][:10]}...`\n"
            else:
                list_txt += f"{i+1}. Not Set\n"
        
        bot.edit_message_text(list_txt, chat_id, msg_id, reply_markup=kb)
        return

    if action.startswith("tok_edit_"):
        idx = int(action.split("_")[-1])
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✏️ Set", callback_data=f"tok_set_{idx}"), 
               types.InlineKeyboardButton("🗑 Delete", callback_data=f"tok_del_{idx}"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="tok_short_list"))
        bot.edit_message_text(f"Manage Shortener Slot {idx+1}:", chat_id, msg_id, reply_markup=kb)
        return

    if action.startswith("tok_set_"):
        idx = int(action.split("_")[-1])
        user_states[uid] = {'state': 'waiting_tok_api_multi', 'idx': idx}
        bot.send_message(uid, f"Send API Key for Slot {idx+1}:")
        return

    if action.startswith("tok_del_"):
        idx = int(action.split("_")[-1])
        shorteners = SHORTNER_CONFIG.get("shorteners", [])
        if idx < len(shorteners):
            del shorteners[idx]
            SHORTNER_CONFIG["shorteners"] = shorteners
            save_setting("shortner", SHORTNER_CONFIG)
            bot.send_message(uid, f"Shortener {idx+1} removed.")
        return
    
    if action == "tok_time":
        current = SHORTNER_CONFIG.get("validity", 12)
        # Cycle 3 -> 6 -> 12 -> 24 -> 3
        nxt = 6 if current == 3 else 12 if current == 6 else 24 if current == 12 else 3
        SHORTNER_CONFIG["validity"] = nxt
        save_setting("shortner", SHORTNER_CONFIG)
        
        status = "✅ On" if SHORTNER_CONFIG.get("active") else "❌ Off"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("Manage Shorteners", callback_data="tok_short_list"),
            types.InlineKeyboardButton(f"Verify Time ({nxt}h)", callback_data="tok_time"),
            types.InlineKeyboardButton(f"Status ({status})", callback_data="tok_onoff"),
            types.InlineKeyboardButton("Verify Tutorial", callback_data="tok_tut"),
            types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings")
        )
        bot.edit_message_text("*🔐 Token Verification Settings*", chat_id, msg_id, reply_markup=kb)
        return

    if action == "tok_onoff":
        SHORTNER_CONFIG["active"] = not SHORTNER_CONFIG.get("active", False)
        save_setting("shortner", SHORTNER_CONFIG)
        
        status = "✅ On" if SHORTNER_CONFIG.get("active") else "❌ Off"
        validity = SHORTNER_CONFIG.get("validity", 12)
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("Manage Shorteners", callback_data="tok_short_list"),
            types.InlineKeyboardButton(f"Verify Time ({validity}h)", callback_data="tok_time"),
            types.InlineKeyboardButton(f"Status ({status})", callback_data="tok_onoff"),
            types.InlineKeyboardButton("Verify Tutorial", callback_data="tok_tut"),
            types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings")
        )
        bot.edit_message_text("*🔐 Token Verification Settings*", chat_id, msg_id, reply_markup=kb)
        return

    if action == "tok_tut":
        user_states[uid] = {'state': 'waiting_tok_tut'}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="panel_token"))
        bot.edit_message_text("Send Tutorial Link:", chat_id, msg_id, reply_markup=kb)
        return

    # LOGS
    if action == "panel_logs":
        d_status = "✅ Set" if LOG_CHANNELS["data"] else "❌ Not Set"
        u_status = "✅ Set" if LOG_CHANNELS["user"] else "❌ Not Set"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton(f"📝 Set Data Log ({d_status})", callback_data="log_set_data"),
            types.InlineKeyboardButton(f"👤 Set User Log ({u_status})", callback_data="log_set_user"),
            types.InlineKeyboardButton("🔙 Back", callback_data="close_panel")
        )
        bot.edit_message_text("*📝 Log Channels Settings*", chat_id, msg_id, reply_markup=kb)
    elif action == "log_set_data":
        user_states[uid] = {'state': 'waiting_log_data'}
        bot.send_message(uid, "Forward message from Data Log Channel:")
    elif action == "log_set_user":
        user_states[uid] = {'state': 'waiting_log_user'}
        bot.send_message(uid, "Forward message from User Log Channel:")

    # Reports
    elif action == "panel_reports":
        count = tickets_col.count_documents({})
        if count == 0:
            bot.send_message(uid, "✅ No active reports.")
        else:
            bot.send_message(uid, f"📋 *Active Reports: {count}*")
            for t in list(tickets_col.find()):
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("✅ Fix", callback_data=f"fix|{t['_id']}"), types.InlineKeyboardButton("↩️ Reply", callback_data=f"reply|{t['_id']}"))
                
                user_name = t.get('username')
                uname_str = f"@{user_name}" if user_name else "N/A"
                txt = f"#{t['_id']} - {uname_str}\n{t['text']}"
                
                try:
                    if t['photo']: bot.send_photo(uid, t['photo'], caption=txt, reply_markup=kb, parse_mode="Markdown")
                    else: bot.send_message(uid, txt, reply_markup=kb, parse_mode="Markdown")
                except:
                    # Full Fallback
                    try:
                        if t['photo']: bot.send_photo(uid, t['photo'], caption=txt, reply_markup=kb, parse_mode=None)
                        else: bot.send_message(uid, txt, reply_markup=kb, parse_mode=None)
                    except Exception as e:
                        print(f"Error sending report {t['_id']}: {e}")

    # Broadcast
    elif action == "panel_broadcast":
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("All Users", callback_data="bc_all"), types.InlineKeyboardButton("P+ (Premium)", callback_data="bc_prem"), types.InlineKeyboardButton("🗑 Del 1h", callback_data="bc_del_1h"), types.InlineKeyboardButton("🗑 Del 12h", callback_data="bc_del_12h"), types.InlineKeyboardButton("🗑 Del All", callback_data="bc_del_all"), types.InlineKeyboardButton("🔙 Back", callback_data="close_panel"))
        bot.edit_message_text("*📢 Broadcast Menu*", chat_id, msg_id, reply_markup=kb)
    elif action == "bc_all":
        user_states[uid] = {'state': 'broadcast_input', 'target': 'all'}
        bot.send_message(uid, "📢 Send Msg for *ALL*:")
    elif action == "bc_prem":
        user_states[uid] = {'state': 'broadcast_input', 'target': 'prem'}
        bot.send_message(uid, "📢 Send Msg for *P+*:")
    elif action.startswith("bc_del_"):
        threading.Thread(target=perform_broadcast_delete, args=(uid, action)).start()
        bot.answer_callback_query(call.id, "Deletion Started in Background.")

    # Settings
    elif action == "panel_start_msg":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📝 Text", callback_data="st_text_menu"), types.InlineKeyboardButton("🖼 Pic", callback_data="st_pic_menu"), types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings"))
        bot.edit_message_text("Customize Start Message:", chat_id, msg_id, reply_markup=kb)
    elif action == "st_text_menu":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✏️ Edit", callback_data="st_text_edit"), types.InlineKeyboardButton("👀 See", callback_data="st_text_see"), types.InlineKeyboardButton("🔙 Back", callback_data="panel_start_msg"))
        bot.edit_message_text("Manage Start Text:", chat_id, msg_id, reply_markup=kb)
    elif action == "st_text_edit":
        user_states[uid] = {'state': 'waiting_start_text'}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="panel_start_msg"))
        bot.send_message(uid, "Send new text:", reply_markup=kb)
    elif action == "st_text_see":
        bot.send_message(uid, f"Current:\n{START_CONFIG['text']}")
    elif action == "st_pic_menu":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🖼 Set", callback_data="st_pic_set"), types.InlineKeyboardButton("🗑 Delete", callback_data="st_pic_del"), types.InlineKeyboardButton("🔙 Back", callback_data="panel_start_msg"))
        bot.edit_message_text("Manage Start Picture:", chat_id, msg_id, reply_markup=kb)
    elif action == "st_pic_set":
        user_states[uid] = {'state': 'waiting_start_pic'}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="panel_start_msg"))
        bot.send_message(uid, "Send new photo:", reply_markup=kb)
    elif action == "st_pic_del":
        START_CONFIG["pic"] = None
        save_setting("start", START_CONFIG)
        bot.send_message(uid, "Pic Deleted!")
    elif action == "panel_force":
        # Handled in new block
        pass
    elif action == "panel_plans":
        credit_val = CREDIT_CONFIG.get("value", 1.0)
        kb = types.InlineKeyboardMarkup()
        for p in PLANS: 
            credits_display = round(PLANS[p] / credit_val, 2)
            kb.add(types.InlineKeyboardButton(f"{p} - {credits_display} Credits", callback_data=f"ep|{p}"))
        bot.send_message(uid, "Select Plan to Edit:", reply_markup=kb)
    elif action == "panel_timer":
        DELETE_CONFIG["minutes"] = 120 if DELETE_CONFIG["minutes"] == 30 else 30
        save_setting("delete", DELETE_CONFIG)
        t_str = "30 Minutes" if DELETE_CONFIG["minutes"] == 30 else "2 Hours"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔄 Change", callback_data="panel_timer"), types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings"))
        bot.edit_message_text(f"Auto Delete Time: *{t_str}*", chat_id, msg_id, reply_markup=kb)
    elif action == "panel_stats":
        user_count = users_col.count_documents({})
        reports_count = tickets_col.count_documents({})
        banned_count = users_col.count_documents({"is_banned": True})
        prem_count = users_col.count_documents({"premium_expiry": {"$gt": datetime.now()}})
        
        msg = f"📊 *Bot Status*\n\n👥 Total Users: `{user_count}`\n📨 Reports: `{reports_count}`\n🚫 Banned: `{banned_count}`\n👑 Active Pro: `{prem_count}`"
        bot.send_message(uid, msg)

    # Actions
    if action.startswith("fix|"):
        mid = action.split("|")[1]
        t = tickets_col.find_one({"_id": mid})
        if t:
            try: bot.edit_message_reply_markup(t['user_id'], user_ticket_reply.get(t['user_id']), reply_markup=None)
            except: pass

            bot.send_message(t['user_id'], "✅ Your report has been resolved.")
            if t['user_id'] in active_chats: del active_chats[t['user_id']]
            tickets_col.delete_one({"_id": mid})
            bot.delete_message(ADMIN_ID, call.message.message_id)
    if action.startswith("reply|"):
        mid = action.split("|")[1]
        t = tickets_col.find_one({"_id": mid})
        if t:
            user_states[ADMIN_ID] = {'state': 'reply_ticket', 'uid': t['user_id'], 'tid': mid}
            bot.send_message(ADMIN_ID, "Type reply:")

    # User Reply to Admin
    if action.startswith("usr_reply|"):
        tid = action.split("|")[1]
        user_states[uid] = {'state': 'waiting_user_reply', 'tid': tid}
        bot.send_message(uid, "Type your reply to Admin:")
        return
def process_gen_command(user_id, chat_id, message_id=None):
    if is_banned(user_id): return
    if user_id == ADMIN_ID:
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("💰 Sale", callback_data="gen_adm_sale"), types.InlineKeyboardButton("👑 Premium", callback_data="gen_adm_prem"), types.InlineKeyboardButton("🔓 Public", callback_data="gen_adm_pub"), types.InlineKeyboardButton("🔙 Back", callback_data="close_panel"))
        text = "*⚡ Admin Link Generator*"
    elif is_premium(user_id):
        # Pro User -> Open Pro Menu
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("⚡ Gen Link", callback_data="gen_pro_menu"), types.InlineKeyboardButton("💳 Set Payment", callback_data="pay_pro_menu"), types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard"))
        text = "*💎 Pro Member Panel*"
    else:
        # Free User -> Show Plans
        text = "*❌ Premium Required*"
        kb = get_plan_kb()

    if message_id: bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
    else: bot.send_message(chat_id, text, reply_markup=kb)

# ---------------- INPUT HANDLERS ----------------
@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'audio', 'animation', 'voice'])
def handle_inputs(message):
    uid = message.from_user.id
    if is_banned(uid): return

    # 1. USER REPLY TO ADMIN (Button Triggered)
    state = user_states.get(uid)
    if isinstance(state, dict) and state.get('state') == 'waiting_user_reply':
        tid = state['tid']
        # Forward to Admin
        bot.send_message(ADMIN_ID, f"📩 *User Reply (#{tid}):*\n{message.text or '[Media]'}")
        if message.content_type != 'text': bot.copy_message(ADMIN_ID, uid, message.message_id)
        bot.send_message(uid, "✅ Sent.")
        del user_states[uid]
        return

    # 2. SUPPORT REQUEST
    if uid in user_support_state:
        del user_support_state[uid]
        
        # --- RATE LIMIT CHECK (3 reports per 24h) ---
        u = users_col.find_one({"_id": uid})
        today_str = datetime.now().strftime("%Y-%m-%d")
        sr = u.get("support_reports", {"date": None, "count": 0})
        
        if sr["date"] == today_str:
            if sr["count"] >= 3:
                bot.send_message(uid, "🚫 *Daily Limit Reached*\n\nYou have already sent 3 reports today. Please try again tomorrow to avoid spamming.")
                return
            new_count = sr["count"] + 1
        else:
            new_count = 1
            
        # Update User Data
        users_col.update_one({"_id": uid}, {"$set": {"support_reports": {"date": today_str, "count": new_count}}})
        # --- END RATE LIMIT ---

        tid = gen_code(4)
        txt = message.caption or message.text or "No Text"
        pid = message.photo[-1].file_id if message.photo else None
        tickets_col.insert_one({'_id': tid, 'user_id': uid, 'username': message.from_user.username, 'text': txt, 'photo': pid, 'status': 'open'})
        bot.send_message(ADMIN_ID, f"⚠️ *New Report #{tid}* from @{message.from_user.username}\nCheck Admin Panel -> Reports")
        bot.send_message(uid, f"✅ Report #{tid} Submitted. Our team will review it soon.")
        return

     # Text Input Logic (Admin Link Update - Ye same rahega)
    if user_states.get(uid) == "WAIT_PAYMENT_LINK" and uid == ADMIN_ID:
        global PAYMENT_LINK
        PAYMENT_LINK = message.text
        save_setting("payment_link", message.text)
        user_states[uid] = None
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="panel_settings"))
        bot.send_message(uid, f"✅ *Payment Link Updated:*\n`{message.text}`", reply_markup=kb)
        return

            # Process pending payments email (Strict Logic: Only if PENDING)
    session = active_user_code.get(uid)
    text = message.text
    
    # CHECK: Sirf tab andar jao jab 'PENDING' state ho (VIEWING ho to ignore karo)
    if session and "PENDING" in session and text:

        # --- EMAIL VALIDATION ---
        if "@" not in text or "." not in text or len(text) < 5:
            # Error Message with Back Button
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🔙 Back / Cancel", callback_data="cancel_input_process"))
            bot.send_message(message.chat.id, "⚠️ *Invalid Email!*\nPlease enter a valid email address (e.g. name@gmail.com).", reply_markup=kb)
            return

        # --- PAYMENT CHECK ---
        email = text.strip().lower()

        existing_payment = unclaimed_payments_col.find_one({"email": email})

        # Scenario A: Payment Pehle se aayi hui hai
        if existing_payment:
            paid_amount = float(existing_payment['amount'])
            credit_val = CREDIT_CONFIG.get("value", 1.0)
            credits_added = paid_amount / credit_val
            
            add_credits(uid, credits_added)
            
            bot.send_message(uid, f"✅ *Payment Found!*\n₹{paid_amount} received. {credits_added} Credits added.", reply_markup=types.ReplyKeyboardRemove())
            unclaimed_payments_col.delete_one({"_id": existing_payment['_id']})
            del active_user_code[uid]
            return
        
        # Scenario B: Payment abhi nahi aayi (Tracking Start)
        else:
            pending_payments_col.insert_one({
                "user_id": uid, "email": email, "type": "credit", 
                "created_at": datetime.now()
            })
            
            # Message sent WITHOUT Buttons (User confuse na ho)
            bot.send_message(uid, f"✅ *Payment Tracking Started*\nEmail: `{email}`\nWaiting for confirmation...")
            
            # State Delete taaki user dobara 'Invalid Email' na face kare
            del active_user_code[uid] 
            return
 # 4. ADMIN INPUTS
    if isinstance(state, dict) and uid == ADMIN_ID:
        st = state['state']

        if st == 'reply_ticket':
            target_uid = state['uid']
            tid = state['tid']
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("↩️ Reply to Admin", callback_data=f"usr_reply|{tid}"))
            m = bot.send_message(target_uid, f"📩 *Admin Reply:*\n{message.text}", reply_markup=kb)
            user_ticket_reply[target_uid] = m.message_id # Save ID to remove later
            bot.send_message(uid, "✅ Reply Sent with Button.")
            del user_states[uid]
            return

        if st == 'broadcast_input':
            target = state.get('target')

            # Count users
            total_users = users_col.count_documents({})
            if target == 'prem':
                users = users_col.find({"premium_expiry": {"$gt": datetime.now()}})
            else:
                users = users_col.find({})

            sent_msg = bot.send_message(ADMIN_ID, f"🚀 Broadcasting to ~{total_users} users...")
            count = 0

            for u in users:
                try:
                    m = None
                    if message.content_type == 'text': m = bot.send_message(u["_id"], message.text)
                    elif message.content_type == 'photo': m = bot.send_photo(u["_id"], message.photo[-1].file_id, caption=message.caption)
                    elif message.content_type == 'video': m = bot.send_video(u["_id"], message.video.file_id, caption=message.caption)
                    elif message.content_type == 'document': m = bot.send_document(u["_id"], message.document.file_id, caption=message.caption)

                    if m: last_broadcast_ids.append((u["_id"], m.message_id, datetime.now()))
                    count += 1
                    time.sleep(0.04) # Rate limit
                except: pass

            bot.edit_message_text(f"✅ Broadcast Complete: {count} users.", ADMIN_ID, sent_msg.message_id)
            del user_states[uid]
            return

        if st == 'waiting_fj_forward':
            if message.forward_from_chat:
                new_ch = {'id': message.forward_from_chat.id, 'title': message.forward_from_chat.title, 'username': message.forward_from_chat.username}
                cl = CHANNEL_CONFIG.get("channels", [])
                cl.append(new_ch)
                CHANNEL_CONFIG["channels"] = cl
                save_setting("channel", CHANNEL_CONFIG)
                bot.send_message(uid, f"✅ Added: {new_ch['title']}")
            else:
                bot.send_message(uid, "❌ Forward from a channel please.")
            del user_states[uid]
            return

        if st == 'waiting_custom_btn':
            CUSTOM_BTN_CONFIG["text"] = message.text
            save_setting("custom_btn", CUSTOM_BTN_CONFIG)
            bot.send_message(uid, "✅ Button Set.")
            del user_states[uid]
            return

        if st == 'waiting_ban_id':
            try: users_col.update_one({"_id": int(message.text)}, {"$set": {"is_banned": True}}); bot.send_message(uid, "Banned.")
            except: pass
            del user_states[uid]; return
        if st == 'waiting_unban_id':
            try: users_col.update_one({"_id": int(message.text)}, {"$set": {"is_banned": False}}); bot.send_message(uid, "Unbanned.")
            except: pass
            del user_states[uid]; return
        if st in ['waiting_log_data', 'waiting_log_user']:
            try:
                cid = message.forward_from_chat.id if message.forward_from_chat else int(message.text)
                key = 'data' if st == 'waiting_log_data' else 'user'
                LOG_CHANNELS[key] = cid
                save_setting("logs", LOG_CHANNELS)
                bot.send_message(uid, f"✅ Log Channel Set: `{cid}`")
            except: pass
            del user_states[uid]; return
        if st == 'waiting_tok_api_multi':
            idx = state['idx']
            # Temp save in state
            state['api'] = message.text
            state['state'] = 'waiting_tok_url_multi'
            bot.send_message(uid, f"✅ API Saved for Slot {idx+1}. Now Send Domain (e.g. mdiskshortner.link):")
            return
        if st == 'waiting_tok_url_multi':
            idx = state['idx']
            api = state['api']
            url = message.text
            
            shorteners = SHORTNER_CONFIG.get("shorteners", [])
            # If index is beyond current list, append. Otherwise update.
            new_s = {"api": api, "url": url}
            if idx < len(shorteners):
                shorteners[idx] = new_s
            else:
                # Add placeholders if needed
                while len(shorteners) < idx:
                    shorteners.append({"api": None, "url": None})
                shorteners.append(new_s)
            
            SHORTNER_CONFIG["shorteners"] = shorteners
            save_setting("shortner", SHORTNER_CONFIG)
            bot.send_message(uid, f"✅ Shortener Slot {idx+1} Configured!")
            del user_states[uid]; return
        if st == 'waiting_tok_tut':
            SHORTNER_CONFIG["tutorial"] = message.text
            save_setting("shortner", SHORTNER_CONFIG)
            bot.send_message(uid, "✅ Tutorial Set!")
            del user_states[uid]; return
        if st == 'waiting_start_text':
            START_CONFIG["text"] = message.text; save_setting("start", START_CONFIG); bot.send_message(uid, "Updated."); del user_states[uid]; return
        if st == 'waiting_start_pic':
            if message.photo: START_CONFIG["pic"] = message.photo[-1].file_id; save_setting("start", START_CONFIG); bot.send_message(uid, "Updated.")
            del user_states[uid]; return
        if st == 'edit_plan_price':
            try: 
                credit_val = CREDIT_CONFIG.get("value", 1.0)
                price_rs = float(message.text) * credit_val
                PLANS[state['plan']] = price_rs
                save_setting("plans", PLANS)
                bot.send_message(uid, f"✅ Updated to {message.text} Credits (₹{price_rs}).")
            except: bot.send_message(uid, "Invalid Number.")
            del user_states[uid]; return

        if st == 'waiting_credit_val':
            try:
                val = float(message.text)
                if val <= 0:
                    bot.send_message(uid, "❌ *Error:* Credit value must be greater than 0.")
                    return
                CREDIT_CONFIG["value"] = val
                save_setting("credit", CREDIT_CONFIG)
                bot.send_message(uid, f"✅ 1 Credit = ₹{val} Set.")
            except: bot.send_message(uid, "Invalid Number.")
            del user_states[uid]; return
            
        if st == 'waiting_credit_user':
            try:
                target_uid = int(message.text)
                user_states[uid] = {'state': 'waiting_credit_amount', 'target': target_uid}
                bot.send_message(uid, "Send amount of Credits to add:")
            except: 
                bot.send_message(uid, "Invalid User ID.")
                del user_states[uid]
            return
            
        if st == 'waiting_credit_amount':
            try:
                credit_val = CREDIT_CONFIG.get("value", 1.0)
                amt_credits = float(message.text)
                amt_rs = amt_credits * credit_val
                add_credits(state['target'], amt_rs)
                bot.send_message(uid, f"✅ Added {amt_credits} Credits (₹{amt_rs}) to User {state['target']}.")
                try: bot.send_message(state['target'], f"🎁 Admin added {amt_credits} Credits to your wallet!")
                except: pass
            except: bot.send_message(uid, "Invalid Amount.")
            del user_states[uid]; return

        if st == 'waiting_redeem_name':
            code = message.text.strip().upper()
            try: bot.delete_message(uid, message.message_id) # Cleanup admin text
            except: pass
            
            if redeems_col.find_one({"_id": code}):
                bot.send_message(uid, "❌ This code already exists! Try another.")
                return
            
            user_states[uid]['code'] = code
            user_states[uid]['state'] = 'waiting_redeem_credits'
            
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_redeem"))
            bot.edit_message_text(f"Code: `{code}`\n\n💰 *Step 2:* How many CREDITS should it give? (0 for none):", uid, state['msg_id'], reply_markup=kb, parse_mode="Markdown")
            return

        if st == 'waiting_redeem_credits':
            try:
                cr = float(message.text)
                try: bot.delete_message(uid, message.message_id) # Cleanup
                except: pass
                
                user_states[uid]['credits'] = cr
                user_states[uid]['state'] = 'waiting_redeem_bonus'
                
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_redeem"))
                bot.edit_message_text(f"Code: `{state['code']}`\nCredits: `{cr}`\n\n🎁 *Step 3:* Percentage Bonus for future purchases? (0 for none):", uid, state['msg_id'], reply_markup=kb, parse_mode="Markdown")
            except: bot.send_message(uid, "Invalid Number. Try again.")
            return

        if st == 'waiting_redeem_bonus':
            try:
                bonus = float(message.text)
                try: bot.delete_message(uid, message.message_id) # Cleanup
                except: pass
                
                user_states[uid]['bonus'] = bonus
                user_states[uid]['state'] = 'waiting_redeem_time'
                
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_redeem"))
                bot.edit_message_text(f"Code: `{state['code']}`\nCredits: `{state['credits']}`\nBonus: `{bonus}%`\n\n⏳ *Step 4:* Valid for how many HOURS? (e.g. 24):", uid, state['msg_id'], reply_markup=kb, parse_mode="Markdown")
            except: bot.send_message(uid, "Invalid Number. Try again.")
            return

        if st == 'waiting_redeem_time':
            try:
                hours = int(message.text)
                try: bot.delete_message(uid, message.message_id) # Cleanup
                except: pass
                
                code = state['code']
                credits = state['credits']
                bonus = state['bonus']
                expiry = datetime.now() + timedelta(hours=hours)
                
                redeems_col.insert_one({
                    '_id': code,
                    'credits': credits,
                    'bonus': bonus,
                    'expiry': expiry,
                    'created_at': datetime.now()
                })
                
                msg = (f"✅ *Redeem Code Created!*\n\n"
                       f"🔹 Code: `{code}`\n"
                       f"💰 Credits: {credits}\n"
                       f"🎁 Bonus: {bonus}%\n"
                       f"⌛ Exp: {hours} hours")
                
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("🔙 Back to Redeem Menu", callback_data="panel_redeem"))
                bot.edit_message_text(msg, uid, state['msg_id'], reply_markup=kb, parse_mode="Markdown")
                del user_states[uid]
            except: bot.send_message(uid, "Invalid Number. Try again.")
            return

        if st == 'waiting_redeem_del':
            code = message.text.strip().upper()
            if redeems_col.delete_one({"_id": code}).deleted_count > 0:
                bot.send_message(uid, f"✅ Code `{code}` deleted.")
            else:
                bot.send_message(uid, "❌ Code not found!")
            del user_states[uid]; return

            # Gen Link (Pro/Admin) - SMART ID COLLECTOR
    if state and state.get('state') == 'batch_collect':
        # File Type & ID Nikalo
        ftype = 'text' if message.text else 'photo' if message.photo else 'video' if message.video else 'document' if message.document else 'audio'
        
        # ID safe tarike se nikalo
        fid = None
        if message.text: fid = message.text
        elif message.photo: fid = message.photo[-1].file_id
        elif message.video: fid = message.video.file_id
        elif message.document: fid = message.document.file_id
        elif message.audio: fid = message.audio.file_id
        elif message.voice: fid = message.voice.file_id
        elif message.animation: fid = message.animation.file_id
        
        if not fid: return # Agar sticker ya kuch aur hai to ignore karo

        # 1. File List mein add karo
        state['files'].append({'type': ftype, 'id': fid})

        # 2. Button List check karo (Agar nahi hai to nayi banao)
        if 'btn_ids' not in state: state['btn_ids'] = []

        # 3. Koshish karo purana delete karne ki (Agar load kam ho)
        last_mid = state.get('last_msg_id')
        if last_mid:
            try: bot.delete_message(message.chat.id, last_mid)
            except: pass 

        # 4. Naya Button Bhejo
        msg = bot.send_message(message.chat.id, f"✅ Added ({len(state['files'])}) files.", reply_markup=done_kb())
        
        # 5. ID Save karo (List mein bhi, aur Last ID mein bhi)
        state['last_msg_id'] = msg.message_id
        state['btn_ids'].append(msg.message_id) # <--- Ye zaroori line hai
        return
    if state and state.get('state') == 'waiting_price':
        try:
            # FIX: Use regex to extract first number found in string (handles "100rs", "price 50", etc)
            nums = re.findall(r'\d+', message.text)
            if not nums: raise ValueError
            val_input = int(nums[0])

            user_states[uid]['state'] = 'batch_collect'
            user_states[uid]['files'] = []
            
            if state.get('owner') == ADMIN_ID:
                credit_val = CREDIT_CONFIG.get("value", 1.0)
                price_rs = val_input * credit_val
                user_states[uid]['price'] = price_rs
                bot.send_message(uid, f"✅ Price: {val_input} Credits (₹{price_rs})\n*Send content now.*", reply_markup=done_kb())
            else:
                user_states[uid]['price'] = val_input
                bot.send_message(uid, f"✅ Price: ₹{val_input}\n*Send content now.*", reply_markup=done_kb())
        except: bot.send_message(uid, "Invalid Number. Send numbers only.")
        return

    if state and state.get('state') == 'waiting_upi':
        update_user_upi(uid, message.text)
        bot.send_message(uid, "✅ UPI Set!")
        del user_states[uid]
        return

    if state and state.get('state') == 'waiting_user_short_api':
        user_states[uid]['api'] = message.text
        user_states[uid]['state'] = 'waiting_user_short_url'
        bot.send_message(uid, "✅ API Saved. Now Send Domain (e.g. mdiskshortner.link):")
        return

    if state and state.get('state') == 'waiting_user_short_url':
        api = user_states[uid]['api']
        url = message.text
        users_col.update_one({"_id": uid}, {"$set": {"personal_shortener": {"api": api, "url": url}}})
        bot.send_message(uid, "✅ Personal Shortener Configured!")
        del user_states[uid]
        return

    # PROOF UPLOAD (Only for Pro users selling to other users now)
    if message.photo and not state:
        session = active_user_code.get(uid)
        if session:
            # Admin wale automated process ko ignore karein (SALE_... ya PLAN_...)
            if not session.startswith("PLAN_") and not session.startswith("SALE_"):
                code = session
                batch = batches_col.find_one({"_id": code})
                if batch:
                    owner_id = batch.get('owner_id')

                    # Agar Owner ADMIN nahi hai (Matlab Premium User hai)
                    if owner_id != ADMIN_ID:
                        price = batch.get('price', 0) # <--- Ye line IMPORTANT hai (Price nikalna)

                        pid = f"pro_{uid}_{gen_code(3)}"

                        # Data save karte waqt 'price' zaroor save karein
                        pro_proofs_col.insert_one({
                            '_id': pid, 
                            'owner_id': owner_id, 
                            'user_id': uid, 
                            'username': message.from_user.username, 
                            'code': code, 
                            'price': price, # <--- Yahan price database me ja raha hai
                            'photo': message.photo[-1].file_id,
                            'timestamp': datetime.now()
                        })

                        bot.send_message(uid, "✅ *Proof Sent!*\nSeller verify karke file bhej dega.")
                        try:
                            bot.send_message(owner_id, "🔔 *New Payment Proof!*\nCheck Dashboard -> Proof 📸")
                        except: pass

            # Code use hone ke baad session clear karein
            del active_user_code[uid]
            return

def done_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ DONE", callback_data="batch_save"))
    kb.add(types.InlineKeyboardButton("❌ Cancel Process", callback_data="cancel_input_process"))
    return kb

def perform_broadcast_delete(admin_id, action):
    cutoff = datetime.now()
    if "1h" in action: cutoff -= timedelta(hours=1)
    elif "12h" in action: cutoff -= timedelta(hours=12)
    else: cutoff -= timedelta(days=365) 
    count = 0
    # Copy list to iterate safely
    for i, (uid, mid, ts) in enumerate(list(last_broadcast_ids)):
        if ts > cutoff:
            try: bot.delete_message(uid, mid); count += 1
            except: pass
            # Remove from original list (using value, not index to be safe)
            try: last_broadcast_ids.remove((uid, mid, ts))
            except: pass

    bot.send_message(admin_id, f"🗑 Deleted {count} messages.")

# ---------------- RUN ----------------
print("🤖 Bot Started...")
try:
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
except Exception as e:
    print(f"Error: {e}")
    traceback.print_exc()
