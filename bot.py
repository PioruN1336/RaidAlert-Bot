import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import asyncio
import secrets
import time
from aiohttp import web
from datetime import datetime

# ================== CONFIG ZMIENNE ŚRODOWISKOWE ==================
TOKEN = os.getenv("BOT_TOKEN")
API_SECRET = os.getenv("API_SECRET", "super_tajne_haslo_123")
PORT = int(os.getenv("PORT", 8080))
LINK_CODE_EXPIRY = int(os.getenv("LINK_CODE_EXPIRY", 300))

if not TOKEN:
    print("ERROR: BOT_TOKEN nie został ustawiony!")
    exit(1)

# ================== BAZY DANYCH ==================
LINKS_FILE = "links.json"
PENDING_CODES = {}

def load_links():
    if not os.path.exists(LINKS_FILE):
        save_links({})
    with open(LINKS_FILE, "r") as f:
        return json.load(f)

def save_links(data):
    with open(LINKS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_discord_id(steam_id: str):
    links = load_links()
    entry = links.get(str(steam_id))
    return int(entry["discord_id"]) if entry else None

def link_accounts(steam_id: str, discord_id: int):
    links = load_links()
    links = {k: v for k, v in links.items() if v["discord_id"] != str(discord_id)}
    links[str(steam_id)] = {"discord_id": str(discord_id), "linked_at": time.time()}
    save_links(links)

# ================== DISCORD BOT ==================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"[{datetime.now()}] Bot online jako {bot.user}")
    try:
        await bot.tree.sync()
        print("Komendy slash zostały zsynchronizowane.")
    except Exception as e:
        print(f"Błąd sync: {e}")

# Komenda /link
@bot.tree.command(name="link", description="Link Steam z Discordem")
@app_commands.describe(code="Kod który dostałeś w grze")
async def link(interaction: discord.Interaction, code: str):
    code = code.strip().upper()
    
    if code not in PENDING_CODES:
        return await interaction.response.send_message("❌ Nieprawidłowy lub przedawniony kod.", ephemeral=True)
    
    data = PENDING_CODES[code]
    if time.time() - data["created"] > LINK_CODE_EXPIRY:
        del PENDING_CODES[code]
        return await interaction.response.send_message("❌ Kod wygasł. Wpisz w grze /linkdiscord ponownie.", ephemeral=True)

    link_accounts(data["steam_id"], interaction.user.id)
    del PENDING_CODES[code]

    await interaction.response.send_message(
        f"✅ **Pomyślnie połączono!**\n"
        f"Twoje konto Steam zostało powiązane z Discordem.\n"
        f"Od teraz będziesz dostawał powiadomienia o rajdach na priv.", 
        ephemeral=True
    )
    print(f"Linked: Steam {data['steam_id']} -> Discord {interaction.user.id}")

# ================== API (dla pluginu Unturned) ==================
async def raid_alert(request: web.Request):
    if request.headers.get("Authorization") != f"Bearer {API_SECRET}":
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except:
        return web.json_response({"error": "Bad JSON"}, status=400)

    owner_steam = str(data.get("owner_steam_id", ""))
    discord_id = get_discord_id(owner_steam)

    if not discord_id:
        return web.json_response({"status": "no_link"})

    user = bot.get_user(discord_id) or await bot.fetch_user(discord_id)
    if not user:
        return web.json_response({"status": "user_not_found"})

    embed = discord.Embed(title="🚨 RAID ALERT 🚨", color=0xFF0000)
    embed.add_field(name="Raider", value=f"{data.get('raider_name')}\n`{data.get('raider_steam_id')}`", inline=True)
    embed.add_field(name="Object", value=f"{data.get('object_name')} ({data.get('object_type')})", inline=True)
    embed.add_field(name="Damage", value=str(data.get('damage', 0)), inline=True)
    embed.add_field(name="Origin", value=data.get('damage_origin', 'Unknown'), inline=True)
    embed.add_field(name="Location", value=data.get('location', 'Unknown'), inline=False)
    embed.set_footer(text=data.get("server_name", "Unturned Server"))

    try:
        await user.send(embed=embed)
        return web.json_response({"status": "sent"})
    except:
        return web.json_response({"status": "dm_blocked"})

async def generate_code(request: web.Request):
    if request.headers.get("Authorization") != f"Bearer {API_SECRET}":
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
        steam_id = str(data["steam_id"])
        code = secrets.token_hex(4).upper()
        PENDING_CODES[code] = {"steam_id": steam_id, "created": time.time()}
        return web.json_response({"code": code})
    except:
        return web.json_response({"error": "Bad request"}, status=400)

# ================== URUCHOMIENIE ==================
async def create_app():
    app = web.Application()
    app.router.add_post("/raid-alert", raid_alert)
    app.router.add_post("/generate-code", generate_code)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)