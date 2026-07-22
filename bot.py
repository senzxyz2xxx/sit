import os
import time
import asyncio
import threading
from datetime import datetime, timezone

import discord
from discord.ext import commands
from flask import Flask, jsonify, render_template_string

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

# รองรับ prefix ทั้ง s. และ S.
def get_prefix(bot_, message):
    return commands.when_mentioned_or("s.", "S.")(bot_, message)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)

# state ที่ dashboard จะอ่านไปแสดง
state = {
    "bot_ready": False,
    "bot_user": None,
    "start_time": time.time(),
    "sessions": {},   # guild_id -> {guild_name, channel_name, channel_id, join_time, status}
}

RECONNECT_MAX_RETRY = 5
RECONNECT_DELAY_SEC = 5


# ---------------------------------------------------------------------------
# DISCORD BOT LOGIC
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    state["bot_ready"] = True
    state["bot_user"] = str(bot.user)
    print(f"[READY] ล็อกอินเป็น {bot.user}")


@bot.command(name="join")
async def join_cmd(ctx):
    """s.join หรือ S.join -> เข้าห้องเสียงที่ผู้เรียกใช้คำสั่งอยู่"""
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.reply("ต้องเข้าห้องเสียงก่อนถึงจะสั่งให้บอทเข้าตามได้ครับ")
        return

    channel = ctx.author.voice.channel
    try:
        vc = await channel.connect(reconnect=True, self_deaf=True)
    except discord.ClientException:
        vc = ctx.voice_client
        if vc and vc.channel.id != channel.id:
            await vc.move_to(channel)

    _record_join(ctx.guild, channel)
    await ctx.reply(f"เข้าห้อง **{channel.name}** แล้ว เริ่มนับเวลา ✅")


@bot.command(name="leave")
async def leave_cmd(ctx):
    """s.leave หรือ S.leave -> ออกจากห้องเสียง (สั่งออกเองได้เสมอ)"""
    vc = ctx.voice_client
    if vc is None:
        await ctx.reply("ตอนนี้บอทไม่ได้อยู่ในห้องเสียงครับ")
        return
    await vc.disconnect(force=True)
    _clear_session(ctx.guild.id)
    await ctx.reply("ออกจากห้องเสียงแล้ว 👋")


@bot.command(name="status")
async def status_cmd(ctx):
    """s.status หรือ S.status -> เช็คสถานะบอทในเซิร์ฟเวอร์นี้"""
    session = state["sessions"].get(ctx.guild.id)
    if not session:
        await ctx.reply("ตอนนี้บอทไม่ได้อยู่ในห้องเสียงของเซิร์ฟเวอร์นี้ครับ")
        return
    elapsed = _fmt_duration(time.time() - session["join_time"])
    await ctx.reply(
        f"📡 อยู่ในห้อง **{session['channel_name']}**\n"
        f"⏱️ เวลาที่สิงอยู่: {elapsed}\n"
        f"🌐 ดู dashboard ได้ที่เว็บที่ deploy ไว้"
    )


@bot.command(name="help")
async def help_cmd(ctx):
    text = (
        "**คำสั่งทั้งหมด (prefix: `s.` หรือ `S.`)**\n"
        "`s.join`   - เข้าห้องเสียงตามผู้เรียกคำสั่ง\n"
        "`s.leave`  - ออกจากห้องเสียง\n"
        "`s.status` - เช็คเวลาที่อยู่ในห้องเสียงตอนนี้\n"
        "`s.help`   - แสดงข้อความนี้\n"
    )
    await ctx.reply(text)


@bot.event
async def on_voice_state_update(member, before, after):
    """ตรวจจับตอนบอทหลุดจากห้องเสียง -> ตัดสินใจว่าจะ reconnect หรือไม่"""
    if member.id != bot.user.id:
        return

    guild = member.guild

    # กรณีบอทถูกย้ายห้อง (ไม่ใช่ถูกเตะออก) -> อัปเดต session เฉยๆ
    if after.channel is not None and before.channel is not None and after.channel != before.channel:
        _record_join(guild, after.channel, keep_start_time=False)
        return

    # กรณีบอทหลุดออกจากห้องเสียงไปเลย (after.channel is None)
    if before.channel is not None and after.channel is None:
        session = state["sessions"].get(guild.id)
        if session is None:
            return

        # เช็คว่าบอทยังมีสิทธิ์ Connect ในห้องเดิมอยู่ไหม
        # ถ้าแอดมินถอดสิทธิ์ Connect ออก แปลว่าตั้งใจเตะ -> เคารพสิทธิ์ ไม่ฝืนเข้า
        old_channel = before.channel
        me = guild.me
        perms = old_channel.permissions_for(me) if old_channel else None

        if perms is not None and not perms.connect:
            print(f"[{guild.name}] ผู้ดูแลถอดสิทธิ์ Connect -> ไม่ reconnect (เคารพผู้ดูแลเซิร์ฟเวอร์)")
            _clear_session(guild.id)
            return

        # ลอง reconnect แบบจำกัดจำนวนครั้ง (สมมติว่าหลุดเพราะเน็ต/ปัญหาโหนดเสียงชั่วคราว)
        asyncio.create_task(_try_reconnect(guild, old_channel))


async def _try_reconnect(guild, channel):
    for attempt in range(1, RECONNECT_MAX_RETRY + 1):
        await asyncio.sleep(RECONNECT_DELAY_SEC)

        # เช็คสิทธิ์ใหม่ทุกรอบ เผื่อผู้ดูแลเพิ่งเปลี่ยนสิทธิ์
        me = guild.me
        perms = channel.permissions_for(me)
        if not perms.connect:
            print(f"[{guild.name}] ไม่มีสิทธิ์ Connect แล้ว -> หยุดพยายาม reconnect")
            _clear_session(guild.id)
            return

        try:
            await channel.connect(reconnect=True, self_deaf=True)
            _record_join(guild, channel)
            print(f"[{guild.name}] reconnect สำเร็จ (ครั้งที่ {attempt})")
            return
        except Exception as e:
            print(f"[{guild.name}] reconnect ล้มเหลวครั้งที่ {attempt}: {e}")

    print(f"[{guild.name}] reconnect ครบ {RECONNECT_MAX_RETRY} ครั้งแล้วยังไม่สำเร็จ -> เลิกพยายาม")
    _clear_session(guild.id)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def _record_join(guild, channel, keep_start_time=None):
    existing = state["sessions"].get(guild.id)
    join_time = existing["join_time"] if (existing and keep_start_time is False and existing.get("channel_id") == channel.id) else time.time()
    if existing and existing.get("channel_id") == channel.id:
        join_time = existing["join_time"]
    else:
        join_time = time.time()

    state["sessions"][guild.id] = {
        "guild_name": guild.name,
        "channel_name": channel.name,
        "channel_id": channel.id,
        "join_time": join_time,
        "status": "connected",
    }


def _clear_session(guild_id):
    state["sessions"].pop(guild_id, None)


def _fmt_duration(seconds):
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    return f"{h}ชม. {m}นาที {s}วิ"


# ---------------------------------------------------------------------------
# WEB DASHBOARD (Flask)
# ---------------------------------------------------------------------------
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<title>Voice Presence Bot - Dashboard</title>
<meta http-equiv="refresh" content="5">
<style>
  body { font-family: sans-serif; background:#1e1f22; color:#e3e5e8; padding:2rem; }
  h1 { color:#5865f2; }
  .card { background:#2b2d31; border-radius:10px; padding:1rem 1.5rem; margin-bottom:1rem; }
  .ok { color:#3ba55d; font-weight:bold; }
  .bad { color:#ed4245; font-weight:bold; }
  table { width:100%; border-collapse:collapse; }
  td, th { padding:6px 10px; text-align:left; border-bottom:1px solid #3f4147; }
  .muted { color:#949ba4; font-size:0.85rem; }
</style>
</head>
<body>
  <h1>🎧 Voice Presence Bot - Dashboard</h1>
  <div class="card">
    <p>สถานะบอท: {% if bot_ready %}<span class="ok">ONLINE</span>{% else %}<span class="bad">OFFLINE</span>{% endif %}</p>
    <p>บัญชีบอท: {{ bot_user or "-" }}</p>
    <p>เวลา uptime ของโปรเซส: {{ process_uptime }}</p>
    <p class="muted">Prefix: s. หรือ S. | คำสั่ง: s.join, s.leave, s.status, s.help</p>
  </div>

  <div class="card">
    <h3>ห้องเสียงที่บอทกำลังสิงอยู่</h3>
    {% if sessions %}
    <table>
      <tr><th>เซิร์ฟเวอร์</th><th>ห้องเสียง</th><th>เวลาที่สิง</th></tr>
      {% for s in sessions %}
      <tr>
        <td>{{ s.guild_name }}</td>
        <td>{{ s.channel_name }}</td>
        <td>{{ s.duration }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p class="muted">ตอนนี้บอทยังไม่ได้เข้าห้องเสียงไหนเลย</p>
    {% endif %}
  </div>

  <div class="card muted">
    หน้านี้รีเฟรชอัตโนมัติทุก 5 วินาที
  </div>
</body>
</html>
"""


@app.route("/")
def dashboard():
    sessions = []
    for gid, s in state["sessions"].items():
        sessions.append({
            "guild_name": s["guild_name"],
            "channel_name": s["channel_name"],
            "duration": _fmt_duration(time.time() - s["join_time"]),
        })
    return render_template_string(
        DASHBOARD_HTML,
        bot_ready=state["bot_ready"],
        bot_user=state["bot_user"],
        process_uptime=_fmt_duration(time.time() - state["start_time"]),
        sessions=sessions,
    )


@app.route("/health")
def health():
    """ใช้สำหรับ Render health check หรือ external uptime pinger"""
    return jsonify({"status": "ok", "bot_ready": state["bot_ready"]})


@app.route("/api/status")
def api_status():
    sessions = []
    for gid, s in state["sessions"].items():
        sessions.append({
            "guild_id": gid,
            "guild_name": s["guild_name"],
            "channel_name": s["channel_name"],
            "channel_id": s["channel_id"],
            "join_time_utc": datetime.fromtimestamp(s["join_time"], tz=timezone.utc).isoformat(),
            "duration_seconds": int(time.time() - s["join_time"]),
        })
    return jsonify({
        "bot_ready": state["bot_ready"],
        "bot_user": state["bot_user"],
        "process_uptime_seconds": int(time.time() - state["start_time"]),
        "sessions": sessions,
    })


def run_flask():
    app.run(host="0.0.0.0", port=PORT)


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("กรุณาตั้งค่า ENV VAR ชื่อ DISCORD_TOKEN ก่อนรัน")

    # รัน Flask dashboard บน thread แยก เพื่อไม่ให้บล็อก event loop ของ discord.py
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    bot.run(TOKEN)
