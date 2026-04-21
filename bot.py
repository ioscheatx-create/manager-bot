import os
import json
import asyncio
import math
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import firebase_admin
from firebase_admin import credentials, firestore

# --- Environment Variables ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
FIREBASE_JSON_STR = os.environ.get("FIREBASE_JSON")

# --- Firebase Initialization ---
firebase_cert = json.loads(FIREBASE_JSON_STR)
cred = credentials.Certificate(firebase_cert)
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Bot Initialization ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Helper Functions ---
def get_global_settings():
    doc = db.collection('config').document('bot_settings').get()
    if doc.exists: return doc.to_dict()
    default = {"dm_text": "Welcome to the group!", "btn_text": "Our Channel", "btn_url": "https://t.me/telegram"}
    db.collection('config').document('bot_settings').set(default)
    return default

def get_dm_keyboard(settings):
    builder = InlineKeyboardBuilder()
    builder.button(text=settings.get("btn_text"), url=settings.get("btn_url"))
    return builder.as_markup()

# --- 1. Bot Added to Group (Initialize in DB) ---
@dp.my_chat_member()
async def bot_added_to_group(update: types.ChatMemberUpdated):
    # When bot is promoted to admin, save group to database
    if update.new_chat_member.status == "administrator":
        chat_id = str(update.chat.id)
        group_ref = db.collection('groups').document(chat_id)
        if not group_ref.get().exists:
            group_ref.set({
                "title": update.chat.title,
                "pending_users": [],
                "users_left": 0,
                "auto_accept": False
            })

# --- 2. Track Users Leaving ---
@dp.chat_member()
async def on_user_leave(update: types.ChatMemberUpdated):
    # Triggers when a user leaves or is kicked
    if update.new_chat_member.status in ["left", "kicked"]:
        chat_id = str(update.chat.id)
        db.collection('groups').document(chat_id).set({"users_left": firestore.Increment(1)}, merge=True)

# --- 3. Handling Join Requests ---
@dp.chat_join_request()
async def handle_join_request(update: types.ChatJoinRequest):
    chat_id = str(update.chat.id)
    user_id = update.from_user.id
    
    group_ref = db.collection('groups').document(chat_id)
    group_doc = group_ref.get()
    
    # Initialize if missing
    if not group_doc.exists:
        group_ref.set({"title": update.chat.title, "pending_users": [user_id], "users_left": 0, "auto_accept": False})
        auto_accept = False
    else:
        auto_accept = group_doc.to_dict().get("auto_accept", False)
        group_ref.update({
            "title": update.chat.title, 
            "pending_users": firestore.ArrayUnion([user_id])
        })

    # If Auto-Accept is ON for THIS specific group
    if auto_accept:
        settings = get_global_settings()
        try:
            await bot.send_message(user_id, settings.get("dm_text"), reply_markup=get_dm_keyboard(settings))
        except Exception:
            pass # Ignore if blocked
        
        await update.approve()
        group_ref.update({"pending_users": firestore.ArrayRemove([user_id])})

# --- 4. Main Admin Panel ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return

    builder = InlineKeyboardBuilder()
    builder.button(text="📂 Manage Groups", callback_data="groups_page_0")
    builder.button(text="✉️ Set DM Text", callback_data="help_dm_text")
    builder.button(text="🔗 Set Button Link", callback_data="help_btn")
    builder.adjust(1)
    
    await message.answer("🛠 **Main Admin Panel**\nSelect an option:", reply_markup=builder.as_markup(), parse_mode="Markdown")

# --- 5. Paginated Group List ---
@dp.callback_query(F.data.startswith("groups_page_"))
async def list_groups(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    page = int(callback.data.split("_")[2])
    items_per_page = 5
    
    # Get all groups
    docs = list(db.collection('groups').stream())
    
    if len(docs) == 0:
        return await callback.message.edit_text("No groups found. Add the bot to a group as an admin first!")

    total_pages = math.ceil(len(docs) / items_per_page)
    
    # Slice for current page
    current_docs = docs[page * items_per_page : (page + 1) * items_per_page]
    
    builder = InlineKeyboardBuilder()
    text = f"📊 **Select a Group to Manage (Page {page+1}/{total_pages}):**\n\n"
    
    for doc in current_docs:
        data = doc.to_dict()
        title = data.get('title', 'Unknown Group')
        pending = len(data.get("pending_users", []))
        
        text += f"🔹 **{title}** (Pending: {pending})\n"
        builder.button(text=f"⚙️ Manage {title[:15]}...", callback_data=f"manage_{doc.id}")

    builder.adjust(1)
    
    # Pagination Buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"groups_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"groups_page_{page+1}"))
    
    if nav_buttons:
        builder.row(*nav_buttons)
        
    builder.row(InlineKeyboardButton(text="🔙 Back to Main", callback_data="main_admin"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "main_admin")
async def back_to_main(callback: types.CallbackQuery):
    # Reuse the admin message command logic via callback
    await callback.message.delete()
    await admin_panel(callback.message)

# --- 6. Per-Group Management Panel ---
@dp.callback_query(F.data.startswith("manage_"))
async def group_dashboard(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    chat_id = callback.data.replace("manage_", "")
    doc = db.collection('groups').document(chat_id).get()
    
    if not doc.exists:
        return await callback.answer("Group not found.", show_alert=True)
        
    data = doc.to_dict()
    title = data.get("title", "Unknown")
    pending_count = len(data.get("pending_users", []))
    users_left = data.get("users_left", 0)
    auto_accept = data.get("auto_accept", False)
    
    # Live member count from Telegram API
    try:
        member_count = await bot.get_chat_member_count(int(chat_id))
    except Exception:
        member_count = "Unknown (Bot kicked?)"

    # ADDED: Display the Group ID here so you can copy it for the /group command
    text = (
        f"📊 **Statistics for {title}**\n"
        f"🆔 **Group ID:** `{chat_id}`\n\n"
        f"👥 **Total Members:** {member_count}\n"
        f"⏳ **Pending Requests:** {pending_count}\n"
        f"🚪 **Total Users Left:** {users_left}\n"
        f"⚙️ **Auto-Accept:** {'🟢 ON' if auto_accept else '🔴 OFF'}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text=f"Toggle Auto-Accept ({'ON' if auto_accept else 'OFF'})", callback_data=f"toggleaa_{chat_id}")
    
    if pending_count > 0:
        builder.button(text=f"✅ Accept All ({pending_count})", callback_data=f"acceptall_{chat_id}")
        
    builder.button(text="🔙 Back to Group List", callback_data="groups_page_0")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# --- 7. Group Actions (Toggle & Accept All) ---
@dp.callback_query(F.data.startswith("toggleaa_"))
async def toggle_group_aa(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    chat_id = callback.data.replace("toggleaa_", "")
    
    doc_ref = db.collection('groups').document(chat_id)
    current_status = doc_ref.get().to_dict().get("auto_accept", False)
    doc_ref.update({"auto_accept": not current_status})
    
    # Refresh the specific group dashboard
    callback.data = f"manage_{chat_id}" 
    await group_dashboard(callback)

@dp.callback_query(F.data.startswith("acceptall_"))
async def accept_all_users(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    chat_id = callback.data.replace("acceptall_", "")
    group_ref = db.collection('groups').document(chat_id)
    doc = group_ref.get()
    
    pending_users = doc.to_dict().get("pending_users", [])
    if not pending_users:
        return await callback.answer("No pending users.", show_alert=True)

    await callback.answer("Processing... Sending DMs and accepting.", show_alert=False)
    settings = get_global_settings()
    dm_text = settings.get("dm_text")
    keyboard = get_dm_keyboard(settings)

    for user_id in pending_users:
        try:
            await bot.send_message(user_id, dm_text, reply_markup=keyboard)
        except Exception:
            pass 
        try:
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            print(f"Failed to approve {user_id}: {e}")
            
    group_ref.update({"pending_users": []})
    
    # Refresh the specific group dashboard
    callback.data = f"manage_{chat_id}"
    await group_dashboard(callback)

# --- 8. DM Text Commands ---
@dp.message(Command("setdm"))
async def set_dm(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    new_text = message.text.replace("/setdm ", "")
    if new_text == "/setdm": return await message.answer("Use format: `/setdm Your new welcome message`", parse_mode="Markdown")
    db.collection('config').document('bot_settings').update({"dm_text": new_text})
    await message.answer("✅ DM Text updated successfully!")

@dp.message(Command("setbutton"))
async def set_btn(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    parts = message.text.replace("/setbutton ", "").split("|")
    if len(parts) != 2: return await message.answer("Use format: `/setbutton Button Text | https://yourlink.com`", parse_mode="Markdown")
    db.collection('config').document('bot_settings').update({"btn_text": parts[0].strip(), "btn_url": parts[1].strip()})
    await message.answer("✅ Button updated successfully!")

@dp.callback_query(F.data.in_(["help_dm_text", "help_btn"]))
async def show_help(callback: types.CallbackQuery):
    if callback.data == "help_dm_text":
        await callback.message.answer("To set the DM text, send a message like this:\n`/setdm Welcome to our VIP Group! Read the rules.`", parse_mode="Markdown")
    else:
        await callback.message.answer("To set the button, send a message like this:\n`/setbutton Join Channel | https://t.me/yourchannel`", parse_mode="Markdown")

# --- 9. Fix Unknown Group Names ---
@dp.message(Command("sync"))
async def sync_groups(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    msg = await message.answer("🔄 Syncing group names with Telegram... please wait.")
    docs = db.collection('groups').stream()
    count = 0
    
    for doc in docs:
        chat_id = doc.id
        try:
            # Fetch live chat info from Telegram
            chat = await bot.get_chat(chat_id)
            # Update Firebase with the real title
            db.collection('groups').document(chat_id).update({"title": chat.title})
            count += 1
            await asyncio.sleep(0.5) # Pause briefly to prevent Telegram rate limits
        except Exception as e:
            print(f"Could not sync {chat_id}: {e}")
            
    await msg.edit_text(f"✅ Successfully fixed the names of {count} groups!")

# --- 10. Broadcast Commands ---
@dp.message(Command("all"))
async def broadcast_all(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    text_to_send = message.text.replace("/all", "").strip()
    if not text_to_send:
        return await message.answer("Please provide a message. Format:\n`/all Hello everyone!`", parse_mode="Markdown")
        
    msg = await message.answer("⏳ Broadcasting message to all groups...")
    docs = db.collection('groups').stream()
    
    success = 0
    failed = 0
    
    for doc in docs:
        chat_id = doc.id
        try:
            await bot.send_message(chat_id, text_to_send)
            success += 1
            await asyncio.sleep(0.1) # Anti-spam delay so Telegram doesn't block the bot
        except Exception as e:
            print(f"Failed to send to {chat_id}: {e}")
            failed += 1
            
    await msg.edit_text(f"✅ **Broadcast Complete!**\n\n🟢 Sent to: {success} groups\n🔴 Failed: {failed} groups", parse_mode="Markdown")

@dp.message(Command("group"))
async def broadcast_group(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    # Expects format: /group -1001234567890 Hello group!
    parts = message.text.split(" ", 2)
    if len(parts) < 3:
        return await message.answer("Please use the format:\n`/group [group_id] Your message here`\n\n*Tip: Go to /admin -> Manage Groups to find your Group ID.*", parse_mode="Markdown")
        
    chat_id = parts[1]
    text_to_send = parts[2]
    
    try:
        await bot.send_message(chat_id, text_to_send)
        await message.answer(f"✅ Message sent successfully to group `{chat_id}`", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Failed to send message to `{chat_id}`. Make sure the bot is an admin there.\nError: {e}", parse_mode="Markdown")

# --- Main Entry Point ---
async def main():
    print("Bot is starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())