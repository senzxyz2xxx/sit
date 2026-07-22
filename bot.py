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

        # ถ้า discord.py ยังมี VoiceClient ของกิลด์นี้อยู่ (guild.voice_client ไม่ใช่ None)
        # แปลว่ามันกำลังจัดการ reconnect ของตัวเองอยู่แล้ว (เห็นใน log ว่ามี
        # "Starting voice handshake... attempt N" เองอัตโนมัติ) -> ห้ามยิง connect()
        # ซ้ำเข้าไปอีก ไม่งั้นจะมี session แย่งกัน 2 ชุดจนได้ error 4006 วนลูป
        if guild.voice_client is not None:
            print(f"[{guild.name}] discord.py กำลัง reconnect เองอยู่แล้ว -> ไม่ยุ่ง")
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

        # ถึงตรงนี้แปลว่าหลุดแบบสมบูรณ์จริงๆ (ไม่มี voice_client เหลืออยู่เลย) และยังมีสิทธิ์อยู่
        # ค่อยลอง reconnect เองแบบจำกัดจำนวนครั้ง
        asyncio.create_task(_try_reconnect(guild, old_channel))


async def _try_reconnect(guild, channel):
    for attempt in range(1, RECONNECT_MAX_RETRY + 1):
        await asyncio.sleep(RECONNECT_DELAY_SEC)

        # ถ้าระหว่างรอ มี voice_client โผล่มาแล้ว (เช่นคนสั่ง s.join เอง หรือ
        # discord.py auto-reconnect ของ session เดิมกลับมาทำงาน) ให้เลิกพยายามเอง
        if guild.voice_client is not None:
            print(f"[{guild.name}] มี voice_client อยู่แล้ว -> เลิกพยายาม reconnect เอง")
            return

        # เช็คสิทธิ์ใหม่ทุกรอบ เผื่อผู้ดูแลเพิ่งเปลี่ยนสิทธิ์
        me = guild.me
        perms = channel.permissions_for(me)
        if not perms.connect:
            print(f"[{guild.name}] ไม่มีสิทธิ์ Connect แล้ว -> หยุดพยายาม reconnect")
            _clear_session(guild.id)
            return

        try:
            await channel.connect(reconnect=True, self_deaf=True, timeout=15)
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Presence Console</title>
<meta http-equiv="refresh" content="8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #10141b;
    --panel: #1a212c;
    --panel-line: #2a3444;
    --amber: #e8a33d;
    --amber-dim: #6b5330;
    --mint: #4fd9bf;
    --red: #e8654a;
    --text: #e9edf3;
    --text-mute: #7f8ba0;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background:
      radial-gradient(circle at 15% 0%, rgba(232,163,61,0.06), transparent 45%),
      var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    padding: 2.5rem 1.5rem 3rem;
  }
  .wrap { max-width: 980px; margin: 0 auto; }

  .console-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 1rem;
    border-bottom: 1px solid var(--panel-line);
    padding-bottom: 1.4rem;
    margin-bottom: 1.8rem;
  }
  .eyebrow {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    letter-spacing: 0.18em;
    color: var(--text-mute);
    text-transform: uppercase;
    margin: 0 0 0.5rem;
  }
  .callsign {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: clamp(1.6rem, 4vw, 2.3rem);
    margin: 0;
    letter-spacing: -0.01em;
  }
  .status-block { text-align: right; }
  .led-row { display: flex; align-items: center; gap: 0.5rem; justify-content: flex-end; }
  .led {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--red);
    box-shadow: 0 0 0 rgba(0,0,0,0);
  }
  .led.on {
    background: var(--amber);
    box-shadow: 0 0 10px 2px rgba(232,163,61,0.55);
    animation: pulse 1.6s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.45; }
  }
  .status-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.82rem;
    letter-spacing: 0.06em;
  }
  .status-label.on { color: var(--amber); }
  .status-label.off { color: var(--red); }
  .uptime {
    font-family: 'JetBrains Mono', monospace;
    color: var(--text-mute);
    font-size: 0.78rem;
    margin-top: 0.35rem;
  }

  .grid {
    display: grid;
    grid-template-columns: 1.5fr 1fr;
    gap: 1.4rem;
  }
  @media (max-width: 760px) {
    .grid { grid-template-columns: 1fr; }
  }

  .panel-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--text-mute);
    margin: 0 0 0.7rem;
  }

  .strip {
    background: var(--panel);
    border: 1px solid var(--panel-line);
    border-left: 3px solid var(--amber);
    border-radius: 8px;
    padding: 1rem 1.15rem;
    margin-bottom: 0.8rem;
  }
  .strip-top { display: flex; justify-content: space-between; align-items: baseline; gap: 0.5rem; }
  .strip-room {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 1.05rem;
  }
  .strip-guild { color: var(--text-mute); font-size: 0.82rem; margin-top: 0.15rem; }
  .strip-time {
    font-family: 'JetBrains Mono', monospace;
    color: var(--mint);
    font-size: 0.95rem;
    white-space: nowrap;
  }
  .wave {
    display: flex;
    align-items: flex-end;
    gap: 3px;
    height: 20px;
    margin-top: 0.7rem;
  }
  .wave span {
    width: 3px;
    background: var(--amber-dim);
    border-radius: 2px;
    animation: bounce 1.1s ease-in-out infinite;
  }
  .wave span:nth-child(odd) { background: var(--amber); }
  .wave span:nth-child(1){height:40%;animation-delay:0s}
  .wave span:nth-child(2){height:80%;animation-delay:.1s}
  .wave span:nth-child(3){height:55%;animation-delay:.2s}
  .wave span:nth-child(4){height:95%;animation-delay:.3s}
  .wave span:nth-child(5){height:35%;animation-delay:.4s}
  .wave span:nth-child(6){height:70%;animation-delay:.5s}
  .wave span:nth-child(7){height:50%;animation-delay:.6s}
  .wave span:nth-child(8){height:85%;animation-delay:.7s}
  .wave span:nth-child(9){height:40%;animation-delay:.8s}
  .wave span:nth-child(10){height:65%;animation-delay:.9s}
  @keyframes bounce {
    0%, 100% { transform: scaleY(0.5); opacity: 0.7; }
    50% { transform: scaleY(1); opacity: 1; }
  }

  .empty {
    background: var(--panel);
    border: 1px dashed var(--panel-line);
    border-radius: 8px;
    padding: 1.8rem 1.2rem;
    text-align: center;
    color: var(--text-mute);
    font-size: 0.9rem;
  }
  .empty code {
    font-family: 'JetBrains Mono', monospace;
    color: var(--amber);
    background: rgba(232,163,61,0.08);
    padding: 0.15rem 0.4rem;
    border-radius: 4px;
  }

  .rack {
    background: var(--panel);
    border: 1px solid var(--panel-line);
    border-radius: 8px;
    padding: 0.4rem 1rem;
  }
  .port {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    padding: 0.75rem 0;
    border-bottom: 1px solid var(--panel-line);
  }
  .port:last-child { border-bottom: none; }
  .port-num {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: var(--text-mute);
    width: 1.6rem;
    flex-shrink: 0;
  }
  .port-body { flex: 1; }
  .port-name {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    color: var(--text);
  }
  .port-desc { font-size: 0.78rem; color: var(--text-mute); margin-top: 0.1rem; }
  .port-dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--mint);
    flex-shrink: 0;
    box-shadow: 0 0 6px 1px rgba(79,217,191,0.5);
  }

  .footer-note {
    margin-top: 1.6rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: var(--text-mute);
    text-align: center;
    letter-spacing: 0.04em;
  }
</style>
</head>
<body>
<div class="wrap">

  <div class="console-head">
    <div>
      <p class="eyebrow">Voice Presence Console</p>
      <h1 class="callsign">{{ bot_user or "ยังไม่ได้เชื่อมต่อ" }}</h1>
    </div>
    <div class="status-block">
      <div class="led-row">
        <span class="led {% if bot_ready %}on{% endif %}"></span>
        <span class="status-label {% if bot_ready %}on{% else %}off{% endif %}">
          {% if bot_ready %}ON AIR{% else %}OFFLINE{% endif %}
        </span>
      </div>
      <div class="uptime">PROCESS UPTIME · {{ process_uptime }}</div>
    </div>
  </div>

  <div class="grid">
    <div>
      <p class="panel-label">ห้องเสียงที่กำลังสิงอยู่ ({{ sessions|length }})</p>

      {% if sessions %}
        {% for s in sessions %}
        <div class="strip">
          <div class="strip-top">
            <div>
              <div class="strip-room">{{ s.channel_name }}</div>
              <div class="strip-guild">{{ s.guild_name }}</div>
            </div>
            <div class="strip-time">{{ s.duration }}</div>
          </div>
          <div class="wave">
            <span></span><span></span><span></span><span></span><span></span>
            <span></span><span></span><span></span><span></span><span></span>
          </div>
        </div>
        {% endfor %}
      {% else %}
        <div class="empty">
          ยังไม่มีห้องเสียงที่บอทเข้าอยู่ตอนนี้<br>
          พิมพ์ <code>s.join</code> หรือ <code>S.join</code> ในห้องเสียงที่ต้องการ
        </div>
      {% endif %}
    </div>

    <div>
      <p class="panel-label">แผงคำสั่ง / ฟีเจอร์</p>
      <div class="rack">
        {% for f in features %}
        <div class="port">
          <span class="port-num">{{ "%02d"|format(loop.index) }}</span>
          <div class="port-body">
            <div class="port-name">{{ f.name }}</div>
            <div class="port-desc">{{ f.desc }}</div>
          </div>
          <span class="port-dot"></span>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>

  <p class="footer-note">PREFIX: s. หรือ S. &nbsp;·&nbsp; รีเฟรชอัตโนมัติทุก 8 วินาที &nbsp;·&nbsp; /api/status สำหรับ JSON</p>
</div>
</body>
</html>
"""


FEATURES = [
    {"name": "s.join", "desc": "เข้าห้องเสียงตามผู้เรียกคำสั่ง"},
    {"name": "s.leave", "desc": "ออกจากห้องเสียงทันที"},
    {"name": "s.status", "desc": "เช็คห้อง/เวลาที่สิงอยู่ตอนนี้"},
    {"name": "s.help", "desc": "แสดงคำสั่งทั้งหมด"},
    {"name": "auto-reconnect", "desc": "ต่อกลับอัตโนมัติถ้าหลุดเพราะเน็ต (สูงสุด 5 ครั้ง)"},
    {"name": "respect-kick", "desc": "ไม่ฝืนกลับเข้าห้อง ถ้าแอดมินถอดสิทธิ์ Connect"},
    {"name": "/api/status", "desc": "ข้อมูลสถานะแบบ JSON สำหรับเชื่อมต่อระบบอื่น"},
]


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
        features=FEATURES,
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
