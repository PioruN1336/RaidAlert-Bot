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

# ================== CONFIG ==================
TOKEN        = os.getenv("BOT_TOKEN")
API_SECRET   = os.getenv("API_SECRET", "super_tajne_haslo")
PORT         = int(os.getenv("PORT", 8080))
CODE_EXPIRY  = int(os.getenv("LINK_CODE_EXPIRY", 300))

if not TOKEN:
    print("ERROR: BOT_TOKEN nie ustawiony!")
    exit(1)

LINKS_FILE = "links.json"

# ================== CACHE (ważne dla wydajności!) ==================
# Zamiast czytać plik przy każdym rajdzie
# trzymamy wszystko w pamięci RAM

_links_cache: dict = {}
_cache_dirty: bool = False

def _load_links_from_disk():
    global _links_cache
    if not os.path.exists(LINKS_FILE):
        _links_cache = {}
        _save_links_to_disk()
        return
    with open(LINKS_FILE, "r") as f:
        _links_cache = json.load(f)
    print(f"[Cache] Loaded {len(_links_cache)} links from disk.")

def _save_links_to_disk():
    global _cache_dirty
    with open(LINKS_FILE, "w") as f:
        json.dump(_links_cache, f, indent=2)
    _cache_dirty = False

def get_discord_id(steam_id: str):
    # Tylko RAM - bez I/O
    entry = _links_cache.get(str(steam_id))
    return int(entry["discord_id"]) if entry else None

def link_accounts(steam_id: str, discord_id: int):
    global _cache_dirty
    # Usuń stare linki dla tego discord ID
    to_remove = [
        k for k, v in _links_cache.items()
        if v["discord_id"] == str(discord_id)
    ]
    for k in to_remove:
        del _links_cache[k]

    _links_cache[str(steam_id)] = {
        "discord_id": str(discord_id),
        "linked_at": time.time()
    }
    _cache_dirty = True
    # Zapisz na dysk natychmiast po linkowaniu
    _save_links_to_disk()

def unlink_account(discord_id: int) -> bool:
    global _cache_dirty
    to_remove = [
        k for k, v in _links_cache.items()
        if v["discord_id"] == str(discord_id)
    ]
    if not to_remove:
        return False
    for k in to_remove:
        del _links_cache[k]
    _cache_dirty = True
    _save_links_to_disk()
    return True

# ================== PENDING CODES ==================
PENDING_CODES: dict = {}

def cleanup_codes():
    now = time.time()
    expired = [c for c, v in PENDING_CODES.items()
               if now - v["created"] > CODE_EXPIRY]
    for c in expired:
        del PENDING_CODES[c]
    return len(expired)

# ================== RATE LIMITING ==================
# Prosta ochrona przed spamem DM per user
_dm_cooldowns: dict = {}
DM_COOLDOWN_SECONDS = 30

def check_dm_cooldown(steam_id: str) -> bool:
    now = time.time()
    last = _dm_cooldowns.get(steam_id, 0)
    if now - last < DM_COOLDOWN_SECONDS:
        return False
    _dm_cooldowns[steam_id] = now
    return True

# ================== DISCORD BOT ==================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    _load_links_from_disk()
    print(f"[{datetime.now()}] Bot online: {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Sync error: {e}")

    # Uruchom auto-cleanup kodów co minutę
    bot.loop.create_task(auto_cleanup_task())

async def auto_cleanup_task():
    while True:
        await asyncio.sleep(60)
        removed = cleanup_codes()
        if removed > 0:
            print(f"[Cleanup] Removed {removed} expired codes.")

# ─── /link ───
@bot.tree.command(
    name="link",
    description="Link your Steam account to Discord for raid alerts.")
@app_commands.describe(code="The 8-character code from /linkdiscord in-game")
async def slash_link(interaction: discord.Interaction, code: str):
    cleanup_codes()
    code = code.strip().upper()

    if code not in PENDING_CODES:
        return await interaction.response.send_message(
            "❌ Invalid or expired code.\n"
            "Run `/linkdiscord` in-game to get a new one.",
            ephemeral=True)

    data = PENDING_CODES[code]
    if time.time() - data["created"] > CODE_EXPIRY:
        del PENDING_CODES[code]
        return await interaction.response.send_message(
            "❌ Code expired. Run `/linkdiscord` in-game again.",
            ephemeral=True)

    steam_id = data["steam_id"]
    link_accounts(steam_id, interaction.user.id)
    del PENDING_CODES[code]

    embed = discord.Embed(
        title="✅ Account Linked!",
        color=0x00FF00,
        description="You will now receive raid alerts as Discord DMs.")
    embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
    embed.add_field(name="Steam ID", value=f"`{steam_id}`", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)
    print(f"[Link] Steam {steam_id} -> Discord {interaction.user.id} ({interaction.user})")

# ─── /unlink ───
@bot.tree.command(
    name="unlink",
    description="Unlink your Discord from Steam. You will stop receiving raid alerts.")
async def slash_unlink(interaction: discord.Interaction):
    removed = unlink_account(interaction.user.id)
    if removed:
        await interaction.response.send_message(
            "✅ Account unlinked. You will no longer receive raid DMs.",
            ephemeral=True)
    else:
        await interaction.response.send_message(
            "❌ You don't have a linked account.",
            ephemeral=True)

# ─── /linkstatus ───
@bot.tree.command(
    name="linkstatus",
    description="Check if your Discord is linked to a Steam account.")
async def slash_linkstatus(interaction: discord.Interaction):
    found = None
    for steam_id, data in _links_cache.items():
        if data["discord_id"] == str(interaction.user.id):
            found = steam_id
            break

    if found:
        linked_at = _links_cache[found].get("linked_at", 0)
        date_str = datetime.fromtimestamp(linked_at).strftime("%Y-%m-%d %H:%M")
        await interaction.response.send_message(
            f"✅ **Linked!**\n"
            f"Steam ID: `{found}`\n"
            f"Linked at: {date_str}",
            ephemeral=True)
    else:
        await interaction.response.send_message(
            "❌ Not linked. Use `/linkdiscord` in-game first.",
            ephemeral=True)

# ================== API SERVER ==================

async def handle_raid_alert(request: web.Request):
    if request.headers.get("Authorization") != f"Bearer {API_SECRET}":
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    owner_steam = str(data.get("owner_steam_id", ""))

    # Rate limiting po stronie bota (dodatkowa warstwa)
    if not check_dm_cooldown(owner_steam):
        return web.json_response({
            "status": "cooldown",
            "reason": "DM cooldown active"
        })

    # Pobierz Discord ID z cache (bez I/O!)
    discord_id = get_discord_id(owner_steam)
    if not discord_id:
        return web.json_response({
            "status": "no_link",
            "reason": "Steam not linked to Discord"
        })

    try:
        user = bot.get_user(discord_id)
        if user is None:
            user = await bot.fetch_user(discord_id)
    except discord.NotFound:
        return web.json_response({
            "status": "error",
            "reason": "Discord user not found"
        }, status=404)
    except Exception as e:
        return web.json_response({
            "status": "error",
            "reason": str(e)
        }, status=500)

    embed = discord.Embed(
        title="🚨 RAID ALERT 🚨",
        color=0xFF0000,
        timestamp=datetime.utcnow())

    embed.add_field(
        name="⚔️ Raider",
        value=f"{data.get('raider_name', 'Unknown')}\n`{data.get('raider_steam_id', '?')}`",
        inline=True)

    embed.add_field(
        name="🧱 Object",
        value=f"{data.get('object_name', 'Unknown')}\n({data.get('object_type', '?')})",
        inline=True)

    embed.add_field(
        name="💥 Damage",
        value=str(data.get("damage", 0)),
        inline=True)

    embed.add_field(
        name="🔫 Origin",
        value=data.get("damage_origin", "Unknown"),
        inline=True)

    embed.add_field(
        name="📍 Location",
        value=data.get("location", "Unknown"),
        inline=False)

    embed.set_footer(text=f"🖥️ {data.get('server_name', 'Server')}")

    try:
        await user.send(embed=embed)
        print(f"[DM] Sent raid alert to {user} (owner of {data.get('object_name')})")
        return web.json_response({"status": "sent"})
    except discord.Forbidden:
        return web.json_response({
            "status": "dm_blocked",
            "reason": "User has DMs disabled"
        })
    except Exception as e:
        return web.json_response({"status": "error", "reason": str(e)}, status=500)

async def handle_generate_code(request: web.Request):
    if request.headers.get("Authorization") != f"Bearer {API_SECRET}":
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data   = await request.json()
        steam  = str(data["steam_id"])
        code   = secrets.token_hex(4).upper()

        # Usuń stary kod dla tego steama
        old = [c for c, v in PENDING_CODES.items() if v["steam_id"] == steam]
        for c in old:
            del PENDING_CODES[c]

        PENDING_CODES[code] = {"steam_id": steam, "created": time.time()}
        print(f"[Code] Generated {code} for Steam {steam}")
        return web.json_response({"code": code})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def handle_stats(request: web.Request):
    """Endpoint do sprawdzania statystyk bota."""
    if request.headers.get("Authorization") != f"Bearer {API_SECRET}":
        return web.json_response({"error": "Unauthorized"}, status=401)

    return web.json_response({
        "linked_accounts": len(_links_cache),
        "pending_codes":   len(PENDING_CODES),
        "dm_cooldowns":    len(_dm_cooldowns),
        "bot_latency_ms":  round(bot.latency * 1000, 2)
    })

# ================== START ==================

async def main():
    app = web.Application()
    app.router.add_post("/raid-alert",     handle_raid_alert)
    app.router.add_post("/generate-code",  handle_generate_code)
    app.router.add_get("/stats",           handle_stats)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[API] HTTP server running on port {PORT}")

    async with bot:
        await main()
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
