import os
import json
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import firebase_admin
from firebase_admin import credentials, firestore

# --- Environment Variables ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
FIREBASE_JSON_STR = os.environ.get("FIREBASE_JSON") # Firebase credentials as a JSON string

# --- Firebase Initialization ---
firebase_cert = json.loads(FIREBASE_JSON_STR)
cred = credentials.Certificate(firebase_cert)
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Bot Initialization ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Helper function to get bot settings
def get_settings():
    doc = db.collection('config').document('bot_settings').get()
    if doc.exists:
        return doc.to_dict()
    # Default settings
    default = {"auto_accept": False, "dm_text": "Welcome to the group!", "btn_text": "Our Channel", "btn_url": "https://t.me/telegram"}
    db.collection('config').document('bot_settings').set(default)
    return default

# Helper function to create DM keyboard
def get_dm_keyboard(settings):
    builder = InlineKeyboardBuilder()
    builder.button(text=settings.get("btn_text"), url=settings.get("btn_url"))
    return builder.as_markup()

# --- 1. Handling Join Requests ---
@dp.chat_join_request()
async def handle_join_request(update: types.ChatJoinRequest):
    chat_id = str(update.chat.id)
    user_id = update.from_user.id
    settings = get_settings()

    # Update Group Info in DB
    group_ref = db.collection('groups').document(chat_id)
    group_doc = group_ref.get()
    
    # Get total member count (Async Telegram API call)
    member_count = await bot.get_chat_member_count(update.chat.id)
    
    if not group_doc.exists:
        group_ref.set({"title": update.chat.title, "pending_users": [user_id], "member_count": member_count})
    else:
        group_ref.update({
            "title": update.chat.title, 
            "member_count": member_count,
            "pending_users": firestore.ArrayUnion([user_id])
        })

    # If Auto-Accept is ON
    if settings.get("auto_accept"):
        try:
            # Send DM First
            await bot.send_message(user_id, settings.get("dm_text"), reply_markup=get_dm_keyboard(settings))
        except Exception:
            pass # Ignore if user blocked the bot
        
        # Accept instantly
        await update.approve()
        # Remove from pending list
        group_ref.update({"pending_users": firestore.ArrayRemove([user_id])})

# --- 2. Admin Panel ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    settings = get_settings()
    status = "🟢 ON" if settings.get("auto_accept") else "🔴 OFF"
    
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Toggle Auto-Accept: {status}", callback_data="toggle_auto")
    builder.button(text="Manage Groups", callback_data="manage_groups")
    builder.button(text="Set DM Text", callback_data="help_dm_text")
    builder.button(text="Set Button Link", callback_data="help_btn")
    builder.adjust(1)
    
    await message.answer("🛠 **Admin Panel**\nChoose an option below:", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "toggle_auto")
async def toggle_auto_accept(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    settings = get_settings()
    new_status = not settings.get("auto_accept")
    db.collection('config').document('bot_settings').update({"auto_accept": new_status})
    await callback.answer(f"Auto-Accept is now {'ON' if new_status else 'OFF'}", show_alert=True)
    
    # Refresh Admin Panel
    await admin_panel(callback.message)

@dp.callback_query(F.data == "manage_groups")
async def manage_groups(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    groups = db.collection('groups').stream()
    builder = InlineKeyboardBuilder()
    
    text = "📊 **Group Statistics:**\n\n"
    for group in groups:
        data = group.to_dict()
        pending_count = len(data.get("pending_users", []))
        text += f"🔹 **{data.get('title')}**\nMembers: {data.get('member_count', 0)} | Pending Requests: {pending_count}\n"
        if pending_count > 0:
            builder.button(text=f"Accept All in {data.get('title')[:10]}...", callback_data=f"accept_all_{group.id}")
            
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# --- 3. Accept All Logic ---
@dp.callback_query(F.data.startswith("accept_all_"))
async def accept_all_users(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    chat_id = callback.data.replace("accept_all_", "")
    group_ref = db.collection('groups').document(chat_id)
    doc = group_ref.get()
    
    if not doc.exists:
        return await callback.answer("Group not found.", show_alert=True)
        
    pending_users = doc.to_dict().get("pending_users", [])
    if not pending_users:
        return await callback.answer("No pending users.", show_alert=True)

    await callback.answer("Processing... Sending DMs and accepting.", show_alert=False)
    settings = get_settings()
    dm_text = settings.get("dm_text")
    keyboard = get_dm_keyboard(settings)

    for user_id in pending_users:
        try:
            # Send DM
            await bot.send_message(user_id, dm_text, reply_markup=keyboard)
        except Exception:
            pass # User might have blocked the bot, ignore and continue
        
        try:
            # Approve Request
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            print(f"Failed to approve {user_id}: {e}")
            
    # Clear the database array
    group_ref.update({"pending_users": []})
    await callback.message.answer(f"✅ Successfully accepted {len(pending_users)} users!")

# --- 4. Setup Commands for DM Customization ---
@dp.message(Command("setdm"))
async def set_dm(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    new_text = message.text.replace("/setdm ", "")
    if new_text == "/setdm":
        return await message.answer("Use format: `/setdm Your new welcome message`", parse_mode="Markdown")
    db.collection('config').document('bot_settings').update({"dm_text": new_text})
    await message.answer("✅ DM Text updated successfully!")

@dp.message(Command("setbutton"))
async def set_btn(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    # Expects format: /setbutton ButtonName | https://link.com
    parts = message.text.replace("/setbutton ", "").split("|")
    if len(parts) != 2:
        return await message.answer("Use format: `/setbutton Button Text | https://yourlink.com`", parse_mode="Markdown")
    
    db.collection('config').document('bot_settings').update({
        "btn_text": parts[0].strip(),
        "btn_url": parts[1].strip()
    })
    await message.answer("✅ Button updated successfully!")

@dp.callback_query(F.data.in_(["help_dm_text", "help_btn"]))
async def show_help(callback: types.CallbackQuery):
    if callback.data == "help_dm_text":
        await callback.message.answer("To set the DM text, send a message like this:\n`/setdm Welcome to our VIP Group! Read the rules.`", parse_mode="Markdown")
    else:
        await callback.message.answer("To set the button, send a message like this:\n`/setbutton Join Channel | https://t.me/yourchannel`", parse_mode="Markdown")

# --- Main Entry Point ---
async def main():
    print("Bot is starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())