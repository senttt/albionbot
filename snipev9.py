import os
import time
import requests
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from collections import deque, defaultdict

# ===== Load Config =====
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID_2V2 = int(os.getenv("CHANNEL_ID"))
CHANNEL_ID_5V5 = int(os.getenv("CHANNEL_5V5_ID"))
CHANNEL_ID_SUMMARY = int(os.getenv("CHANNEL_SUMMARY_ID"))

API_URL = "https://gameinfo-sgp.albiononline.com/api/gameinfo/events"
POLL_INTERVAL = 2
MAX_EVENT_AGE = 150  
SUMMARY_WINDOW = 4800  # 1.5 hours

# ===== Item Power Ranges and Requirements =====
IP_RANGE = {
    "2v2": (900, 1200),
    "5v5": (1000, 1300)
}
REQUIRED_KILLS = {
    "2v2": 2,
    "5v5": 5
}

# ===== Discord Client =====
intents = discord.Intents.default()
client = discord.Client(intents=intents)

# ===== Runtime State =====
SEEN_EVENTS = deque()
MATCH_LOG = {"2v2": {}, "5v5": {}}
WIN_LOG = {"2v2": set(), "5v5": set()}
PLAYER_EQUIPMENT = {}
MATCH_HISTORY = {"2v2": defaultdict(list), "5v5": defaultdict(list)}
last_event = {}

# ===== Helper Functions =====
def fetch_events(limit=51):
    try:
        r = requests.get(f"{API_URL}?limit={limit}", timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        print("⚠️ Error fetching events:", e)
        return []

def check_ip_range(event, mode):
    ip_min, ip_max = IP_RANGE[mode]
    participants = event.get("Participants", [])
    victim = event.get("Victim", {})
    return (
        ip_min < victim.get("AverageItemPower", 0) < ip_max and
        all(ip_min < p.get("AverageItemPower", 0) < ip_max for p in participants)
    )

def clean_item_name(item_name):
    if item_name:
        for t in ["T3_", "T4_", "T5_", "T6_", "T7_"]:
            item_name = item_name.replace(t, "")
        item_name = item_name.split("@")[0]
        for s in ["_SET1", "_SET2", "_SET3"]:
            item_name = item_name.replace(s, "")
        item_name = item_name.replace("_", " ")
    return item_name

def get_equipment_pieces(entity):
    eq = entity.get("Equipment", {})
    return {
        "Weapon": clean_item_name(eq.get("MainHand", {}).get("Type")),
        "Head": clean_item_name(eq.get("Head", {}).get("Type")),
        "Armor": clean_item_name(eq.get("Armor", {}).get("Type")),
        "Shoes": clean_item_name(eq.get("Shoes", {}).get("Type"))
    }

def build_win_message(team, victims, timestamp, mode):
    team_names = sorted(team)
    victim_names = ', '.join(victims)
    equipment_lines = []
    for member_name in team_names:
        gear = PLAYER_EQUIPMENT.get(member_name)
        if gear:
            gear_str = f"\U0001f6e1\ufe0f {member_name}: {gear['Weapon']}, {gear['Head']}, {gear['Armor']}, {gear['Shoes']}"
        else:
            gear_str = f"\U0001f6e1\ufe0f {member_name}: *gear unknown*"
        equipment_lines.append(gear_str)
    gear_block = "\n".join(equipment_lines)
    return (
        f"\U0001f525 **{mode.upper()} Hellgate Win Detected!**\n"
        f"\U0001f3c6 **Winning Team:** {', '.join(team_names)}\n"
        f"\U0001f480 **Defeated Team:** {victim_names}\n"
        f"\U0001f552 **Timestamp:** {timestamp}\n"
        f"{gear_block}\n"
        f"================================="
    )

def purge_old():
    now = time.time()
    for mode in ["2v2", "5v5"]:
        expired = []
        for match_id, match in MATCH_LOG[mode].items():
            timeout_limit = MAX_EVENT_AGE + match["timeout_extension"]
            if now - match["start"] > timeout_limit:
                expired.append(match_id)
        for match_id in expired:
            match = MATCH_LOG[mode][match_id]
            elapsed = now - match["start"]
            progress = (len(match["victims"]) / REQUIRED_KILLS[mode]) * 100
            print(f"[{mode.upper()}] timed out after {int(elapsed // 60)}m {int(elapsed % 60)}s, group: {', '.join(sorted(match['team']))} | Progress: {int(progress)}%")
            del MATCH_LOG[mode][match_id]

def process_match(event, mode, channel):
    global last_event
    last_event = event
    current_time = time.time()
    timestamp = event['TimeStamp']
    group_members = [m["Name"] for m in event.get("GroupMembers", [])]
    killer = event["Killer"]["Name"]
    victim = event["Victim"]["Name"]
    participant_count = event.get("numberOfParticipants", 0)

    for p in event.get("Participants", []):
        PLAYER_EQUIPMENT[p["Name"]] = get_equipment_pieces(p)

    if not group_members or killer not in group_members:
        return

    team_key = frozenset(group_members)
    match_id = '_'.join(sorted(group_members))
    match = MATCH_LOG[mode].setdefault(match_id, {
        "team": team_key,
        "victims": set(),
        "start": current_time,
        "last_kill": current_time,
        "timeout_extension": 0
    })

    if victim not in match["victims"]:
        match["victims"].add(victim)
        match["last_kill"] = current_time
        match["timeout_extension"] += 30

    progress = (len(match["victims"]) / REQUIRED_KILLS[mode]) * 100
    print(f"[{mode.upper()}] {killer} killed {victim} | groupmates: {', '.join(sorted(match['team']))} | progress {int(progress)}% | timestamp: {timestamp} | numberOfParticipants: {participant_count}")

    if len(match["victims"]) >= REQUIRED_KILLS[mode]:
        win_key = (match["team"], frozenset(match["victims"]), timestamp)
        if win_key not in WIN_LOG[mode]:
            WIN_LOG[mode].add(win_key)
            msg = build_win_message(match["team"], match["victims"], timestamp, mode)
            del MATCH_LOG[mode][match_id]
            team_key_str = ','.join(sorted(match["team"]))
            MATCH_HISTORY[mode][team_key_str].append({"win": True, "timestamp": current_time})
            defeated_key = ','.join(sorted(match["victims"]))
            MATCH_HISTORY[mode][defeated_key].append({"win": False, "timestamp": current_time})
            return msg
    return None

@tasks.loop(seconds=POLL_INTERVAL)
async def poll_events():
    events = fetch_events()
    if not events:
        return

    now = time.time()
    new_events = [e for e in events if all(e['EventId'] != se[0] for se in SEEN_EVENTS)]

    for event in reversed(new_events):
        event_id = event['EventId']
        SEEN_EVENTS.append((event_id, now))

        group_size = event.get("groupMemberCount", 0)
        participants = event.get("numberOfParticipants", 0)

        if group_size == 2 and participants <= 2 and check_ip_range(event, "2v2"):
            msg = process_match(event, "2v2", CHANNEL_ID_2V2)
            if msg:
                channel = client.get_channel(CHANNEL_ID_2V2)
                if channel:
                    await channel.send(msg)

        if group_size == 5 and participants <= 5 and check_ip_range(event, "5v5"):
            msg = process_match(event, "5v5", CHANNEL_ID_5V5)
            if msg:
                channel = client.get_channel(CHANNEL_ID_5V5)
                if channel:
                    await channel.send(msg)

    purge_old()

@tasks.loop(minutes=10)
async def summary_loop():
    await send_summary()

async def send_summary():
    now = time.time()
    for mode in ["2v2", "5v5"]:
        for team in list(MATCH_HISTORY[mode].keys()):
            MATCH_HISTORY[mode][team] = [m for m in MATCH_HISTORY[mode][team] if now - m["timestamp"] <= SUMMARY_WINDOW]
            if not MATCH_HISTORY[mode][team]:
                del MATCH_HISTORY[mode][team]

        recent_teams = MATCH_HISTORY[mode]
        sorted_teams = sorted(recent_teams.items(), key=lambda x: len(x[1]), reverse=True)
        top_teams = sorted_teams[:5]

        lines = [
            f"🌀 **{mode.upper()} Hellgate Summary** (last 1 hr)",
            f"🔢 Unique Teams: {len(recent_teams)}"
        ]

        for team, matches in top_teams:
            total = len(matches)
            wins = sum(1 for m in matches if m["win"])
            winrate = (wins / total) * 100
            last_played = max(m["timestamp"] for m in matches)
            elapsed = int(now - last_played)
            lines.append(f"🛡️ {team} — {wins}/{total} wins ({winrate:.0f}%) — Last match: {elapsed // 60}m {elapsed % 60}s ago")

        message = "\n".join(lines)

        summary_channel = client.get_channel(CHANNEL_ID_SUMMARY)
        if summary_channel:
            try:
                await summary_channel.send(message)
            except discord.Forbidden:
                print(f"❌ Forbidden: Bot lacks permission to send to {summary_channel}")
            except Exception as e:
                print(f"❌ Error sending summary: {e}")

@client.event
async def on_ready():
    print(f"✅ Bot connected as {client.user}")
    poll_events.start()
    summary_loop.start()

client.run(TOKEN)