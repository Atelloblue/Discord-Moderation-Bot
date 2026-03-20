import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import asyncio
import json
import re
import unicodedata
import logging
from datetime import datetime
from datetime import timedelta

DB_WARNS = "warns.db"
DB_LOGCONFIG = "logconfig.db"
DB_AUTOMOD = "automod_presets.db"
BADWORDS_FILE = "badwords.json"

logging.basicConfig(level=logging.INFO)

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.automod_cache = {}
        self.badwords_cache = []

    async def setup_hook(self):
        await self.init_dbs()
        await self.load_badwords()
        await self.tree.sync()
        logging.info("Bot ready with DBs and commands synced!")

    async def init_dbs(self):
        tables = [
            (DB_WARNS, "CREATE TABLE IF NOT EXISTS warns (guild_id INTEGER, user_id INTEGER, moderator_id INTEGER, reason TEXT, timestamp TEXT)"),
            (DB_LOGCONFIG, "CREATE TABLE IF NOT EXISTS logconfig (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)"),
            (DB_AUTOMOD, "CREATE TABLE IF NOT EXISTS automod_presets (guild_id INTEGER PRIMARY KEY, mention_spam INTEGER DEFAULT 0, bad_words INTEGER DEFAULT 0, links INTEGER DEFAULT 0)")
        ]
        for dbf, q in tables:
            async with aiosqlite.connect(dbf) as db:
                await db.execute(q)
                await db.commit()

    async def load_badwords(self):
        try:
            with open(BADWORDS_FILE, "r", encoding="utf-8") as f:
                self.badwords_cache = [self.normalize_text(w) for w in json.load(f).get("badwords", [])]
        except Exception:
            self.badwords_cache = []

    @staticmethod
    def normalize_text(txt: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", txt) if not unicodedata.combining(c)).lower()

bot = MyBot()

# ----------------- HELP COMMAND -----------------
@bot.tree.command(name="help", description="Shows a list of all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ Bot Command Help",
        description="Detailed list of all available slash commands. Use them by typing `/` followed by the command name.",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="🛠️ Moderation",
        value=(
            "`/kick [member] [reason]` - Kick a member\n"
            "`/ban [member] [reason]` - Ban a member\n"
            "`/softban [member] [reason]` - Ban and immediately unban\n"
            "`/timeout [member] [duration] [reason]` - Time out a member\n"
            "`/mute [member] [reason] [duration]` - Add the Muted role\n"
            "`/unmute [member]` - Remove the Muted role\n"
            "`/nick [member] [nickname]` - Change a member's name\n"
            "`/role [member] [role] [action:add/remove]` - Manage roles"
        ),
        inline=False
    )

    embed.add_field(
        name="⚠️ Warning System",
        value=(
            "`/warn [member] [reason]` - Give a member a warning\n"
            "`/warns [member]` - View a member's warning history\n"
            "`/clearwarns [member]` - Delete all warnings for a member"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Management & Config",
        value=(
            "`/logconfig [channel]` - Set the logging channel\n"
            "`/automod toggle [preset] [on/off]` - Manage filters\n"
            "`/lock [channel]` - Prevent users from sending messages\n"
            "`/unlock [channel]` - Allow users to send messages"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 Info & Tools",
        value=(
            "`/serverinfo` - Display information about this server\n"
            "`/userinfo [member]` - Display information about a user\n"
            "`/ping` - Check bot latency\n"
            "`/embed [title] [description] ...` - Create a custom embed"
        ),
        inline=False
    )

    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------- UTILITIES -----------------
async def get_muted_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name="Muted")
    if not role:
        role = await guild.create_role(name="Muted")
        for ch in guild.channels:
            await ch.set_permissions(role, send_messages=False, speak=False, add_reactions=False)
    return role

async def get_log_channel(guild: discord.Guild):
    async with aiosqlite.connect(DB_LOGCONFIG) as db:
        async with db.execute("SELECT channel_id FROM logconfig WHERE guild_id=?", (guild.id,)) as cur:
            row = await cur.fetchone()
            return guild.get_channel(row[0]) if row else None

async def log_embed(guild: discord.Guild, title: str, desc: str, color: discord.Color = discord.Color.blurple()):
    ch = await get_log_channel(guild)
    if ch:
        try:
            embed = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.utcnow())
            await ch.send(embed=embed)
        except Exception:
            logging.warning(f"Missing perms in {ch.name}")

# ----------------- AUTOMOD -----------------
async def get_automod_presets(gid: int) -> dict:
    if gid in bot.automod_cache:
        return bot.automod_cache[gid]
    async with aiosqlite.connect(DB_AUTOMOD) as db:
        async with db.execute("SELECT mention_spam, bad_words, links FROM automod_presets WHERE guild_id=?", (gid,)) as cur:
            row = await cur.fetchone()
            presets = {"mention_spam": bool(row[0]), "bad_words": bool(row[1]), "links": bool(row[2])} if row else {"mention_spam": False, "bad_words": False, "links": False}
            if not row:
                await db.execute("INSERT OR REPLACE INTO automod_presets(guild_id) VALUES(?)", (gid,))
                await db.commit()
    bot.automod_cache[gid] = presets
    return presets

async def set_automod_preset(gid: int, preset: str, value: bool):
    async with aiosqlite.connect(DB_AUTOMOD) as db:
        await db.execute(f"UPDATE automod_presets SET {preset}=? WHERE guild_id=?", (int(value), gid))
        await db.commit()
    bot.automod_cache[gid] = bot.automod_cache.get(gid, {})
    bot.automod_cache[gid][preset] = value

# ----------------- EVENTS -----------------
@bot.event
async def on_ready():
    logging.info(f"{bot.user} online!")

@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.guild:
        return
    presets = await get_automod_presets(msg.guild.id)
    content = bot.normalize_text(msg.content)
    if presets.get("mention_spam") and len(msg.mentions) > 5:
        await msg.delete()
        await log_embed(msg.guild, "🚫 Mention Spam", f"{msg.author.mention} sent too many mentions.", discord.Color.red())
        return
    if presets.get("bad_words") and any(re.search(rf"\b{re.escape(w)}\b", content) for w in bot.badwords_cache):
        await msg.delete()
        await log_embed(msg.guild, "🚫 Bad Word", f"{msg.author.mention} used a banned word.", discord.Color.red())
        return
    if presets.get("links") and re.search(r"https?://", msg.content):
        await msg.delete()
        await log_embed(msg.guild, "🚫 Link", f"{msg.author.mention} sent a link.", discord.Color.red())

@bot.event
async def on_member_join(member: discord.Member):
    await log_embed(member.guild, "👋 Member Joined", f"{member.mention} joined.", discord.Color.green())

@bot.event
async def on_member_remove(member: discord.Member):
    await log_embed(member.guild, "👋 Member Left", f"{member.mention} left.", discord.Color.orange())

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    await log_embed(channel.guild, "📂 Channel Created", f"{channel.mention} created.", discord.Color.green())

@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    await log_embed(channel.guild, "📂 Channel Deleted", f"{channel.name} deleted.", discord.Color.red())

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.nick != after.nick:
        await log_embed(after.guild, "✏️ Nick Changed", f"{before.mention} changed nickname to {after.nick}.", discord.Color.blurple())

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot:
        return
    await log_embed(before.guild, "✏️ Message Edited", f"{before.author.mention} edited.\nBefore: {before.content}\nAfter: {after.content}", discord.Color.orange())

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    await log_embed(message.guild, "🗑️ Message Deleted", f"{message.author.mention} deleted: {message.content}", discord.Color.red())

@bot.event
async def on_guild_role_create(role: discord.Role):
    await log_embed(role.guild, "🔹 Role Created", f"{role.name} created.", discord.Color.green())

@bot.event
async def on_guild_role_delete(role: discord.Role):
    await log_embed(role.guild, "🔹 Role Deleted", f"{role.name} deleted.", discord.Color.red())

@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    await log_embed(after.guild, "🔹 Role Updated", f"`{before.name}` updated to `{after.name}`.", discord.Color.blurple())

# ----------------- ERROR HANDLING -----------------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    msg = "❌ You don't have perms." if isinstance(error, app_commands.MissingPermissions) else "❌ Bot missing perms." if isinstance(error, app_commands.BotMissingPermissions) else f"❌ Error: {error}"
    await interaction.response.send_message(msg, ephemeral=True)
    logging.exception(error)

# ----------------- LOG CONFIG -----------------
@bot.tree.command(name="logconfig", description="Set the log channel")
@app_commands.describe(channel="The channel to use for logs")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def logconfig(interaction: discord.Interaction, channel: discord.TextChannel):
    async with aiosqlite.connect(DB_LOGCONFIG) as db:
        await db.execute("INSERT OR REPLACE INTO logconfig(guild_id, channel_id) VALUES (?, ?)", (interaction.guild.id, channel.id))
        await db.commit()
    await interaction.response.send_message(f"Log channel set to {channel.mention}.", ephemeral=True)

# ----------------- WARNING SYSTEM -----------------

@bot.tree.command(name="warn", description="Warn a member")
@app_commands.describe(
    member="The member to warn", 
    reason="Reason for the warning",
    hidden="If True, your name won't be shown in the public warning message"
)
@app_commands.checks.has_permissions(kick_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", hidden: bool = False):
    # Log to Database (Internal logs always keep the real moderator ID)
    async with aiosqlite.connect(DB_WARNS) as db:
        await db.execute("INSERT INTO warns(guild_id,user_id,moderator_id,reason,timestamp) VALUES(?,?,?,?,?)",
                         (interaction.guild.id, member.id, interaction.user.id, reason, datetime.utcnow().isoformat()))
        await db.commit()

    # Determine display name for the moderator
    moderator_display = "Staff Member" if hidden else interaction.user.mention

    # Create Public Embed
    embed = discord.Embed(
        title="⚠️ Warning Issued",
        description=f"{member.mention} has been officially warned.",
        color=discord.Color.orange(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Moderator", value=moderator_display, inline=True)
    embed.set_footer(text=f"User ID: {member.id}")
    embed.set_thumbnail(url=member.display_avatar.url)

    # Send message (Not ephemeral so the community/user sees the warning)
    await interaction.response.send_message(content=member.mention, embed=embed)
    
    # Internal Log (Always shows who it actually was)
    await log_embed(interaction.guild, "⚠️ Member Warned", f"{member.mention} warned by {interaction.user.mention}. Reason: {reason}", discord.Color.orange())

@bot.tree.command(name="warns", description="View member warnings")
@app_commands.describe(member="Member")
@app_commands.checks.has_permissions(kick_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    async with aiosqlite.connect(DB_WARNS) as db:
        async with db.execute("SELECT moderator_id,reason,timestamp FROM warns WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id)) as cur:
            rows = await cur.fetchall()
            
    if not rows:
        await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
        return

    embed = discord.Embed(title=f"Warnings for {member}", color=discord.Color.orange())
    for idx, (mod, r, ts) in enumerate(rows, 1):
        # Format the timestamp for better readability
        date_obj = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
        embed.add_field(name=f"Warning #{idx}", value=f"**By:** <@{mod}>\n**Reason:** {r}\n**Date:** {date_obj}", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clearwarns", description="Clear all warnings for a member")
@app_commands.describe(member="Member", reason="Reason for clearing these warnings")
@app_commands.checks.has_permissions(kick_members=True)
async def clearwarns(interaction: discord.Interaction, member: discord.Member, reason: str = "Issue resolved"):
    async with aiosqlite.connect(DB_WARNS) as db:
        await db.execute("DELETE FROM warns WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        await db.commit()

    # Create Embed for clearing warnings
    embed = discord.Embed(
        title="✅ Warnings Cleared",
        description=f"All warnings for {member.mention} have been removed.",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Operator", value=interaction.user.mention, inline=True)
    embed.add_field(name="Reason", value=reason, inline=True)
    
    await interaction.response.send_message(content=member.mention, embed=embed)
    
    # Log the action
    await log_embed(interaction.guild, "🧹 Warnings Cleared", f"Warnings for {member.mention} cleared by {interaction.user.mention}. Reason: {reason}", discord.Color.green())

# ----------------- MODERATION ACTION -----------------
async def moderation_action(interaction: discord.Interaction, member: discord.Member, action: str, reason: str = "No reason provided", duration: int = 0, role_obj: discord.Role = None):
    try:
        if action == "kick":
            await member.kick(reason=reason)
            msg = f"{member.mention} kicked. Reason: {reason}"
        elif action == "ban":
            await member.ban(reason=reason)
            msg = f"{member.mention} banned. Reason: {reason}"
        elif action == "softban":
            await member.ban(reason=reason)
            await member.unban(reason=reason)
            msg = f"{member.mention} softbanned. Reason: {reason}"
        elif action == "mute":
            role = await get_muted_role(interaction.guild)
            await member.add_roles(role, reason=reason)
            msg = f"{member.mention} muted. Reason: {reason}"
            if duration > 0:
                await asyncio.sleep(duration * 60)
                await member.remove_roles(role)
                await interaction.channel.send(f"{member.mention} unmuted after {duration} mins.")
        elif action == "unmute":
            role = await get_muted_role(interaction.guild)
            if role in member.roles:
                await member.remove_roles(role)
                msg = f"{member.mention} unmuted."
            else:
                msg = f"{member.mention} is not muted."
        elif action == "nick":
            await member.edit(nick=reason)
            msg = f"{member.mention}'s nickname changed to {reason}."
        elif action == "role" and role_obj:
            if reason == "add":
                await member.add_roles(role_obj)
                msg = f"Role {role_obj.mention} added to {member.mention}."
            else:
                await member.remove_roles(role_obj)
                msg = f"Role {role_obj.mention} removed from {member.mention}."
        await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        logging.exception(e)
        await interaction.response.send_message("Failed.", ephemeral=True)

# ----------------- MODERATION COMMANDS -----------------
def make_mod_command(name: str):
    @app_commands.command(name=name, description=f"{name.capitalize()} a member")
    async def command(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", role_obj: discord.Role = None, action: str = None):
        param = reason if name != "role" else action
        await moderation_action(interaction, member, name.lower(), param, role_obj=role_obj)
    return command

for cmd_name in ["kick", "ban", "softban", "mute", "unmute", "nick", "role"]:
    bot.tree.add_command(make_mod_command(cmd_name))

# ----------------- TIMEOUT -----------------
@bot.tree.command(name="timeout", description="Timeout a member")
@app_commands.describe(member="Member to timeout", duration="Duration in minutes", reason="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, duration: int, reason: str = "No reason provided"):
    try:
        await member.timeout(duration=timedelta(minutes=duration), reason=reason)
        await interaction.response.send_message(f"{member.mention} timed out for {duration} minutes. Reason: {reason}", ephemeral=True)
    except Exception as e:
        logging.exception(e)
        await interaction.response.send_message("Failed to timeout member.", ephemeral=True)

# ----------------- LOCK/UNLOCK CHANNEL -----------------
@bot.tree.command(name="lock", description="Lock a text channel")
@app_commands.describe(channel="Channel to lock")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    channel = channel or interaction.channel
    try:
        await channel.set_permissions(interaction.guild.default_role, send_messages=False)
        await interaction.response.send_message(f"{channel.mention} is now locked.", ephemeral=True)
    except Exception as e:
        logging.exception(e)
        await interaction.response.send_message("Failed to lock channel.", ephemeral=True)

@bot.tree.command(name="unlock", description="Unlock a text channel")
@app_commands.describe(channel="Channel to unlock")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    channel = channel or interaction.channel
    try:
        await channel.set_permissions(interaction.guild.default_role, send_messages=True)
        await interaction.response.send_message(f"{channel.mention} is now unlocked.", ephemeral=True)
    except Exception as e:
        logging.exception(e)
        await interaction.response.send_message("Failed to unlock channel.", ephemeral=True)

# ----------------- EMBED CREATOR -----------------
@bot.tree.command(name="embed", description="Create a custom embed")
@app_commands.describe(
    title="Embed title",
    description="Embed description",
    color="Hex color (#RRGGBB)",
    author="Author name",
    footer="Footer text"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def embed(interaction: discord.Interaction, title: str, description: str, color: str = "#2f3136", author: str = None, footer: str = None):
    try:
        color = int(color.lstrip("#"), 16)
        e = discord.Embed(title=title, description=description, color=color)
        if author:
            e.set_author(name=author)
        if footer:
            e.set_footer(text=footer)
        await interaction.response.send_message(embed=e)
    except Exception as e:
        logging.exception(e)
        await interaction.response.send_message("Failed to create embed.", ephemeral=True)

# ----------------- INFO COMMANDS -----------------
@bot.tree.command(name="serverinfo", description="Server info")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    e = discord.Embed(title=g.name, color=discord.Color.blue())
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    e.add_field(name="ID", value=g.id)
    e.add_field(name="Owner", value=g.owner)
    e.add_field(name="Members", value=g.member_count)
    e.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d %H:%M:%S"))
    await interaction.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="userinfo", description="User info")
@app_commands.describe(member="Optional member")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    e = discord.Embed(title=str(member), color=discord.Color.green())
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="ID", value=member.id)
    e.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S"))
    e.add_field(name="Created", value=member.created_at.strftime("%Y-%m-%d %H:%M:%S"))
    e.add_field(name="Roles", value=", ".join([r.mention for r in member.roles[1:]]) or "None")
    await interaction.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="ping", description="Bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! 🏓 {round(bot.latency * 1000)}ms", ephemeral=True)

# ----------------- AUTOMOD GROUP -----------------
class AutoMod(app_commands.Group):
    def __init__(self):
        super().__init__(name="automod", description="Manage AutoMod presets")

    @app_commands.command(name="toggle", description="Enable/disable preset")
    @app_commands.describe(preset="Preset", state="On or Off")
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="Mention Spam", value="mention_spam"),
            app_commands.Choice(name="Bad Words", value="bad_words"),
            app_commands.Choice(name="Links", value="links")
        ],
        state=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off")
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def toggle(self, interaction: discord.Interaction, preset: app_commands.Choice[str], state: app_commands.Choice[str]):
        try:
            await set_automod_preset(interaction.guild.id, preset.value, state.value == "on")
            await interaction.response.send_message(f"✅ Preset `{preset.name}` turned {state.name}.", ephemeral=True)
        except Exception as e:
            logging.exception(e)
            await interaction.response.send_message("Failed.", ephemeral=True)

bot.tree.add_command(AutoMod())

# ----------------- RUN BOT -----------------
bot.run("BOT_TOKEN_HERE")
