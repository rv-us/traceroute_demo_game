# app.py
import os
import sys
import json
import time
import re
import platform
import subprocess
import uuid
import sqlite3
from datetime import datetime, timedelta
from typing import Iterator, Optional, Tuple, Dict, List
from dataclasses import dataclass, asdict
from threading import Lock
import math

import requests
from flask import Flask, Response, render_template, request, jsonify, make_response
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-prod')
socketio = SocketIO(app, cors_allowed_origins="*")

# -------------------------
# Data models and state
# -------------------------

@dataclass
class Player:
    id: str
    display_name: str
    created_at: datetime

@dataclass
class Challenge:
    id: str
    city_name: str
    city_lat: float
    city_lon: float
    radius_km: float
    target_host: str
    start_time: datetime
    end_time: Optional[datetime]
    created_at: datetime

@dataclass
class RaceResult:
    player_id: str
    player_name: str
    challenge_id: str
    finish_time: datetime
    rank: int
    points: int
    final_hop_lat: float
    final_hop_lon: float
    final_hop_city: str
    final_hop_country: str
    total_hops: int
    trace_duration_seconds: float
    distance_from_target_km: float

# In-memory storage (could be replaced with a database)
players: Dict[str, Player] = {}
challenges: Dict[str, Challenge] = {}
race_results: List[RaceResult] = []
active_races: Dict[str, dict] = {}  # player_id -> {start_time, challenge_id, hops}

# Thread safety
players_lock = Lock()
challenges_lock = Lock()
results_lock = Lock()
races_lock = Lock()
db_lock = Lock()

# Database setup
DB_PATH = os.path.join(os.path.dirname(__file__), 'traceroute_race.db')

def init_db():
    """Initialize the SQLite database with tables."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS players (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        conn.execute('''CREATE TABLE IF NOT EXISTS challenges (
            id TEXT PRIMARY KEY,
            city_name TEXT NOT NULL,
            city_lat REAL NOT NULL,
            city_lon REAL NOT NULL,
            radius_km REAL NOT NULL,
            target_host TEXT NOT NULL,
            start_time TIMESTAMP NOT NULL,
            end_time TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )''')
        
        conn.execute('''CREATE TABLE IF NOT EXISTS race_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            player_name TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            finish_time TIMESTAMP NOT NULL,
            rank INTEGER NOT NULL,
            points INTEGER NOT NULL,
            final_hop_lat REAL NOT NULL,
            final_hop_lon REAL NOT NULL,
            final_hop_city TEXT,
            final_hop_country TEXT,
            total_hops INTEGER NOT NULL,
            trace_duration_seconds REAL NOT NULL,
            distance_from_target_km REAL DEFAULT 0,
            FOREIGN KEY (player_id) REFERENCES players(id),
            FOREIGN KEY (challenge_id) REFERENCES challenges(id)
        )''')
        
        conn.commit()

# Initialize database on startup
init_db()

def load_data_from_db():
    """Load existing data from database into memory."""
    global players, challenges, race_results
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        
        # Load players
        for row in conn.execute('SELECT * FROM players'):
            player = Player(
                id=row['id'],
                display_name=row['display_name'],
                created_at=datetime.fromisoformat(row['created_at'])
            )
            players[player.id] = player
        
        # Load challenges
        for row in conn.execute('SELECT * FROM challenges WHERE is_active = 1'):
            challenge = Challenge(
                id=row['id'],
                city_name=row['city_name'],
                city_lat=row['city_lat'],
                city_lon=row['city_lon'],
                radius_km=row['radius_km'],
                target_host=row['target_host'],
                start_time=datetime.fromisoformat(row['start_time']),
                end_time=datetime.fromisoformat(row['end_time']) if row['end_time'] else None,
                created_at=datetime.fromisoformat(row['created_at'])
            )
            challenges[challenge.id] = challenge
        
        # Load race results
        for row in conn.execute('SELECT * FROM race_results ORDER BY finish_time'):
            try:
                distance = row['distance_from_target_km']
            except (KeyError, IndexError):
                distance = 0  # Default for old records without this field
                
            result = RaceResult(
                player_id=row['player_id'],
                player_name=row['player_name'],
                challenge_id=row['challenge_id'],
                finish_time=datetime.fromisoformat(row['finish_time']),
                rank=row['rank'],
                points=row['points'],
                final_hop_lat=row['final_hop_lat'],
                final_hop_lon=row['final_hop_lon'],
                final_hop_city=row['final_hop_city'] or '',
                final_hop_country=row['final_hop_country'] or '',
                total_hops=row['total_hops'],
                trace_duration_seconds=row['trace_duration_seconds'],
                distance_from_target_km=distance
            )
            race_results.append(result)

# Load existing data
load_data_from_db()

# -------------------------
# Utility: run traceroute
# -------------------------

IPV4_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")

def run_traceroute(target: str, max_hops: int = 30, timeout_ms: int = 700) -> Iterator[Tuple[int, Optional[str]]]:
    """
    Cross-platform traceroute using system tools.
    Yields tuples of (hop_number, ip) for each hop; ip may be None if unknown (*).
    """
    system = platform.system().lower()

    if "windows" in system:
        # tracert: -d no DNS, -h max hops, -w timeout(ms), -4 force IPv4
        cmd = ["tracert", "-d", "-h", str(max_hops), "-w", str(timeout_ms), "-4", target]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        hop = 0
        for line in proc.stdout:
            # lines like: "  1     2 ms     2 ms     2 ms  192.168.1.1"
            m = IPV4_RE.search(line)
            if line.strip().startswith("Tracing route") or line.strip().startswith("over a maximum") or "---" in line:
                continue
            if line.strip().startswith("Unable to resolve"):
                break
            if m:
                hop += 1
                yield hop, m.group(0)
            elif re.search(r"\s\*\s\*\s\*", line):
                hop += 1
                yield hop, None
        proc.wait()

    else:
        # Unix traceroute: -n numeric, -q 1 one probe per hop, -w timeout(sec), -m max hops, -4 IPv4
        cmd = ["traceroute", "-n", "-q", "1", "-w", str(max(1, timeout_ms // 1000)), "-m", str(max_hops), "-4", target]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        for line in proc.stdout:
            # lines like: " 1  192.168.1.1  1.123 ms"
            hop_match = re.match(r"\s*(\d+)\s+(.+)", line)
            if not hop_match:
                continue
            hop = int(hop_match.group(1))
            rest = hop_match.group(2)
            m = IPV4_RE.search(rest)
            if m:
                yield hop, m.group(0)
            else:
                yield hop, None
        proc.wait()


# -------------------------
# Utility: geolocation
# -------------------------

def geo_ip(ip: str) -> Optional[dict]:
    """
    Use ip-api.com for quick demo geolocation (free tier). Returns dict with lat, lon, city, country.
    NOTE: For production, obtain an API key service and add caching.
    """
    try:
        r = requests.get(f"http://ip-api.com/json/{ip}", timeout=5)
        data = r.json()
        if data.get("status") == "success":
            return {
                "lat": data.get("lat"),
                "lon": data.get("lon"),
                "city": data.get("city"),
                "country": data.get("country"),
                "org": data.get("org"),
                "query": data.get("query"),
            }
    except Exception:
        return None
    return None


def get_public_ip_info() -> Optional[dict]:
    try:
        ip = requests.get("https://api.ipify.org?format=json", timeout=5).json().get("ip")
        if not ip:
            return None
        g = geo_ip(ip)
        if g:
            g["ip"] = ip
        return g
    except Exception:
        return None


# -------------------------
# Utility: distance and challenge
# -------------------------

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points on Earth in kilometers."""
    R = 6371  # Earth's radius in kilometers
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    return R * c


def get_active_challenge() -> Optional[Challenge]:
    """Get the currently active challenge."""
    now = datetime.now()
    with challenges_lock:
        for challenge in challenges.values():
            if challenge.start_time <= now and (not challenge.end_time or now <= challenge.end_time):
                return challenge
    return None


def create_challenge(city_name: str, city_lat: float, city_lon: float, 
                    radius_km: float, target_host: str, duration_hours: int = 24) -> Challenge:
    """Create a new challenge."""
    challenge = Challenge(
        id=str(uuid.uuid4()),
        city_name=city_name,
        city_lat=city_lat,
        city_lon=city_lon,
        radius_km=radius_km,
        target_host=target_host,
        start_time=datetime.now(),
        end_time=datetime.now() + timedelta(hours=duration_hours),
        created_at=datetime.now()
    )
    with challenges_lock:
        challenges[challenge.id] = challenge
    
    # Save to database
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''INSERT INTO challenges 
                (id, city_name, city_lat, city_lon, radius_km, target_host, start_time, end_time, created_at, is_active) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (challenge.id, challenge.city_name, challenge.city_lat, challenge.city_lon,
                 challenge.radius_km, challenge.target_host, challenge.start_time.isoformat(),
                 challenge.end_time.isoformat() if challenge.end_time else None,
                 challenge.created_at.isoformat(), 1))
            conn.commit()
    
    return challenge


def calculate_points(rank: int) -> int:
    """Calculate points based on rank."""
    return max(0, 10 - (rank - 1))


# -------------------------
# Routes
# -------------------------

@app.route("/")
def index():
    me = get_public_ip_info()
    
    # Check for player cookie
    player_id = request.cookies.get('player_id')
    player = None
    if player_id:
        with players_lock:
            player = players.get(player_id)
    
    # Get active challenge
    challenge = get_active_challenge()
    
    return render_template("index.html", 
                         me=me or {}, 
                         player=asdict(player) if player else None,
                         challenge=asdict(challenge) if challenge else None)


@app.route("/geo/<ip>")
def geo(ip):
    # small proxy to avoid CORS issues in the browser
    g = geo_ip(ip)
    if not g:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, "geo": g})


@app.route("/register", methods=["POST"])
def register():
    """Register a new player or update existing."""
    print("="*50)
    print(f"🔵 REGISTRATION REQUEST RECEIVED")
    print(f"Request data: {request.json}")
    print(f"Request cookies: {request.cookies}")
    
    data = request.json
    display_name = data.get("display_name", "").strip()
    
    if not display_name or len(display_name) > 30:
        print(f"❌ Invalid display name: '{display_name}'")
        return jsonify({"error": "Invalid display name"}), 400
    
    # Check for existing player
    player_id = request.cookies.get('player_id')
    print(f"🔍 Checking existing player: player_id={player_id}")
    
    with players_lock:
        if player_id and player_id in players:
            print(f"🔄 Updating existing player: {player_id[:8]}...")
            # Update existing player
            players[player_id].display_name = display_name
            player = players[player_id]
            
            # Update in database
            with db_lock:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute('UPDATE players SET display_name = ? WHERE id = ?',
                                (display_name, player_id))
                    conn.commit()
                    print(f"✅ Updated player in database: {display_name}")
        else:
            print(f"➕ Creating new player: {display_name}")
            # Create new player
            player_id = str(uuid.uuid4())
            player = Player(
                id=player_id,
                display_name=display_name,
                created_at=datetime.now()
            )
            players[player_id] = player
            print(f"🆔 Generated player ID: {player_id[:8]}...")
            
            # Save to database
            with db_lock:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute('INSERT INTO players (id, display_name, created_at) VALUES (?, ?, ?)',
                                (player.id, player.display_name, player.created_at.isoformat()))
                    conn.commit()
                    print(f"💾 Player saved to database: {player.display_name} (ID: {player.id[:8]}...)")
    
    print(f"✅ Registration successful: {player.display_name}")
    
    # Add player to scoreboard with 0 points if there's an active challenge
    challenge = get_active_challenge()
    print(f"🎯 Checking for active challenge: {challenge.city_name if challenge else 'None'}")
    
    if challenge:
        print(f"🏁 Active challenge found: {challenge.city_name}")
        # Check if player already has a result for this challenge
        with results_lock:
            existing_result = any(r for r in race_results if r.player_id == player_id and r.challenge_id == challenge.id)
            print(f"🔍 Checking existing result for player: {existing_result}")
            
            if not existing_result:
                print(f"➕ Adding placeholder result for {player.display_name}")
                # Add placeholder result with 0 points
                placeholder_result = RaceResult(
                    player_id=player_id,
                    player_name=player.display_name,
                    challenge_id=challenge.id,
                    finish_time=datetime.now(),
                    rank=999,  # Will be updated when they actually complete
                    points=0,
                    final_hop_lat=0,
                    final_hop_lon=0,
                    final_hop_city="",
                    final_hop_country="",
                    total_hops=0,
                    trace_duration_seconds=0,
                    distance_from_target_km=0
                )
                race_results.append(placeholder_result)
                print(f"📊 Added to race_results list. Total results: {len(race_results)}")
                
                # Save placeholder to database
                with db_lock:
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute('''INSERT INTO race_results 
                            (player_id, player_name, challenge_id, finish_time, rank, points,
                             final_hop_lat, final_hop_lon, final_hop_city, final_hop_country,
                             total_hops, trace_duration_seconds, distance_from_target_km)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (placeholder_result.player_id, placeholder_result.player_name, placeholder_result.challenge_id,
                             placeholder_result.finish_time.isoformat(), placeholder_result.rank, placeholder_result.points,
                             placeholder_result.final_hop_lat, placeholder_result.final_hop_lon,
                             placeholder_result.final_hop_city, placeholder_result.final_hop_country,
                             placeholder_result.total_hops, placeholder_result.trace_duration_seconds,
                             placeholder_result.distance_from_target_km))
                        conn.commit()
                        print(f"💾 Placeholder result saved to database")
                
                # Notify all clients that a new player joined
                print(f"📡 Sending WebSocket notification to all clients")
                result_dict = asdict(placeholder_result)
                result_dict['finish_time'] = placeholder_result.finish_time.isoformat()
                
                player_dict = asdict(player)
                player_dict['created_at'] = player.created_at.isoformat()
                
                try:
                    socketio.emit('player_joined', {
                        'player': player_dict,
                        'result': result_dict
                    })
                    print(f"✅ WebSocket notification sent successfully")
                except Exception as e:
                    print(f"❌ WebSocket notification failed: {e}")
            else:
                print(f"⚠️ Player already has a result for this challenge, skipping placeholder")
    else:
        print(f"⚠️ No active challenge found, skipping scoreboard entry")
    
    player_response_dict = asdict(player)
    player_response_dict['created_at'] = player.created_at.isoformat()
    
    response = jsonify({"player": player_response_dict})
    response.set_cookie('player_id', player_id, max_age=60*60*24*30)  # 30 days
    return response


@app.route("/challenge/create", methods=["POST"])
def create_challenge_route():
    """Create a new challenge (admin endpoint)."""
    # In production, add authentication here
    data = request.json
    
    try:
        challenge = create_challenge(
            city_name=data["city_name"],
            city_lat=float(data["city_lat"]),
            city_lon=float(data["city_lon"]),
            radius_km=float(data.get("radius_km", 50)),
            target_host=data["target_host"],
            duration_hours=int(data.get("duration_hours", 24))
        )
        
        # Notify all connected clients
        socketio.emit('new_challenge', asdict(challenge))
        
        return jsonify({"challenge": asdict(challenge)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/challenge/current")
def current_challenge():
    """Get the current active challenge."""
    challenge = get_active_challenge()
    if not challenge:
        return jsonify({"challenge": None})
    return jsonify({"challenge": asdict(challenge)})


@app.route("/challenge/toggle", methods=["POST"])
def toggle_challenge():
    """Toggle challenge mode on/off."""
    data = request.json
    enable = data.get("enable", True)
    
    with challenges_lock:
        if enable:
            # Re-enable all challenges
            with db_lock:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute('UPDATE challenges SET is_active = 1')
                    conn.commit()
            
            # Reload challenges
            load_data_from_db()
            message = "Challenge mode enabled"
        else:
            # Disable all challenges
            with db_lock:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute('UPDATE challenges SET is_active = 0')
                    conn.commit()
            
            # Clear active challenges from memory
            challenges.clear()
            message = "Challenge mode disabled"
    
    # Notify all clients
    socketio.emit('challenge_mode_changed', {'enabled': enable})
    
    return jsonify({"success": True, "message": message, "enabled": enable})


@app.route("/scoreboard")
def scoreboard():
    """Get current scoreboard for active challenge."""
    challenge = get_active_challenge()
    if not challenge:
        return jsonify({"error": "No active challenge"}), 404
    
    with results_lock:
        # Filter results for current challenge
        challenge_results = [r for r in race_results if r.challenge_id == challenge.id]
        
        # Separate completed races from placeholder entries
        completed_races = [r for r in challenge_results if r.points > 0]
        placeholder_entries = [r for r in challenge_results if r.points == 0]
        
        # Sort completed races by rank, then append placeholder entries
        completed_races.sort(key=lambda r: r.rank)
        placeholder_entries.sort(key=lambda r: r.player_name)  # Sort by name for consistency
        
        # Combine results - completed races first, then waiting players
        sorted_results = completed_races + placeholder_entries
        
        # Convert to dict with proper datetime serialization
        results_dict = []
        for r in sorted_results:
            result_dict = asdict(r)
            result_dict['finish_time'] = r.finish_time.isoformat()
            # Add status to distinguish between completed and waiting
            result_dict['status'] = 'completed' if r.points > 0 else 'waiting'
            results_dict.append(result_dict)
    
    challenge_dict = asdict(challenge)
    challenge_dict['start_time'] = challenge.start_time.isoformat()
    if challenge.end_time:
        challenge_dict['end_time'] = challenge.end_time.isoformat()
    challenge_dict['created_at'] = challenge.created_at.isoformat()
    
    return jsonify({
        "challenge": challenge_dict,
        "results": results_dict,
        "timestamp": datetime.now().isoformat()
    })


@app.route("/test/race_result", methods=["POST"])
def test_race_result():
    """Test endpoint to manually add a race result."""
    data = request.json
    player_name = data.get("player_name", "Test Player")
    
    challenge = get_active_challenge()
    if not challenge:
        return jsonify({"error": "No active challenge"}), 400
    
    # Create a test result
    with results_lock:
        existing_results = [r for r in race_results if r.challenge_id == challenge.id]
        rank = len(existing_results) + 1
        
        result = RaceResult(
            player_id="test-player-id",
            player_name=player_name,
            challenge_id=challenge.id,
            finish_time=datetime.now(),
            rank=rank,
            points=calculate_points(rank),
            final_hop_lat=35.6762,  # Tokyo coordinates
            final_hop_lon=139.6503,
            final_hop_city="Tokyo",
            final_hop_country="Japan", 
            total_hops=15,
            trace_duration_seconds=30.5
        )
        race_results.append(result)
        
        # Save to database
        with db_lock:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute('''INSERT INTO race_results 
                    (player_id, player_name, challenge_id, finish_time, rank, points,
                     final_hop_lat, final_hop_lon, final_hop_city, final_hop_country,
                     total_hops, trace_duration_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (result.player_id, result.player_name, result.challenge_id,
                     result.finish_time.isoformat(), result.rank, result.points,
                     result.final_hop_lat, result.final_hop_lon,
                     result.final_hop_city, result.final_hop_country,
                     result.total_hops, result.trace_duration_seconds))
                conn.commit()
        
        # Notify all clients
        result_dict = asdict(result)
        result_dict['finish_time'] = result.finish_time.isoformat()
        socketio.emit('race_finished', {
            'result': result_dict,
            'distance_km': 0
        })
        
        return jsonify({"success": True, "result": result_dict})


@app.route("/player/check")
def check_player():
    """Check if current player is registered."""
    player_id = request.cookies.get('player_id')
    if not player_id:
        return jsonify({"registered": False, "message": "No player ID cookie"})
    
    with players_lock:
        player = players.get(player_id)
        if player:
            return jsonify({"registered": True, "player": asdict(player)})
        else:
            return jsonify({"registered": False, "message": "Player not found in memory"})


@app.route("/debug")
def debug_info():
    """Debug endpoint to check system state."""
    with players_lock:
        player_count = len(players)
    
    with challenges_lock:
        active_challenge = get_active_challenge()
        challenge_count = len(challenges)
    
    with results_lock:
        result_count = len(race_results)
    
    with races_lock:
        active_race_count = len(active_races)
    
    return jsonify({
        "players": player_count,
        "challenges": challenge_count,
        "active_challenge": asdict(active_challenge) if active_challenge else None,
        "race_results": result_count,
        "active_races": active_race_count,
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH)
    })


@app.route("/download")
def download_results():
    """Download complete results as JSON."""
    challenge = get_active_challenge()
    if not challenge:
        # Get most recent challenge
        with challenges_lock:
            if not challenges:
                return jsonify({"error": "No challenges found"}), 404
            challenge = max(challenges.values(), key=lambda c: c.created_at)
    
    with results_lock:
        challenge_results = [r for r in race_results if r.challenge_id == challenge.id]
        challenge_results.sort(key=lambda r: r.rank)
        results_dict = [asdict(r) for r in challenge_results]
    
    data = {
        "challenge": asdict(challenge),
        "results": results_dict,
        "export_time": datetime.now().isoformat()
    }
    
    response = make_response(json.dumps(data, indent=2))
    response.headers["Content-Disposition"] = f"attachment; filename=traceroute_race_{challenge.id[:8]}.json"
    response.headers["Content-Type"] = "application/json"
    return response


@app.route("/stream")
def stream():
    """
    Server-Sent Events stream. Client calls: /stream?target=8.8.8.8
    We run traceroute and stream each hop as an SSE 'message' with JSON payload.
    Now includes race logic for challenges.
    """
    target = request.args.get("target", "").strip()
    if not target:
        return "Missing target", 400
    
    # Check if this is a race
    player_id = request.cookies.get('player_id')
    challenge = get_active_challenge()
    is_race = False
    race_start_time = None
    
    print("="*60)
    print(f"🏁 TRACEROUTE REQUEST: target={target}")
    print(f"🆔 Player ID from cookie: {player_id[:8] + '...' if player_id else 'None'}")
    print(f"🎯 Active challenge: {challenge.city_name if challenge else 'None'}")
    print(f"🎯 Challenge target: {challenge.target_host if challenge else 'None'}")
    print(f"🎯 Target matches: {target == challenge.target_host if challenge else 'N/A'}")
    
    if player_id and challenge:
        # Check if player exists in memory first
        with players_lock:
            if player_id in players:
                is_race = True
                race_start_time = time.time()
                print(f"✅ RACE MODE ACTIVATED! (any target allowed)")
                print(f"✅ Player found: {players[player_id].display_name}")
                
                # Record race start
                with races_lock:
                    active_races[player_id] = {
                        'start_time': race_start_time,
                        'challenge_id': challenge.id,
                        'hops': []
                    }
                    print(f"📊 Race started and recorded. Active races: {len(active_races)}")
            else:
                print(f"⚠️ Player ID exists but not found in memory: {player_id[:8]}... (probably old cookie)")
                print(f"⚪ Falling back to regular traceroute mode")
    
    if not is_race:
        print(f"⚪ Regular traceroute mode")
        reasons = []
        if not player_id:
            reasons.append("No player ID")
        elif player_id and challenge:
            with players_lock:
                if player_id not in players:
                    reasons.append("Invalid/old player ID")
        if not challenge:
            reasons.append("No active challenge")
        
        if reasons:
            print(f"   Reasons: {', '.join(reasons)}")

    def event_stream():
        # preamble event
        init = {"type": "start", "target": target, "ts": time.time(), "is_race": is_race}
        if is_race:
            challenge_dict = asdict(challenge)
            challenge_dict['start_time'] = challenge.start_time.isoformat()
            if challenge.end_time:
                challenge_dict['end_time'] = challenge.end_time.isoformat()
            challenge_dict['created_at'] = challenge.created_at.isoformat()
            init["challenge"] = challenge_dict
        yield f"data: {json.dumps(init)}\n\n"
        
        last_hop_geo = None
        total_hops = 0
        
        for hop, ip in run_traceroute(target):
            total_hops = hop
            payload = {"type": "hop", "hop": hop}
            if ip:
                payload["ip"] = ip
                # do geolocation server-side so the browser just renders
                g = geo_ip(ip)
                if g:
                    payload["geo"] = g
                    last_hop_geo = g
                    
                    # Record hop for race
                    if is_race and player_id:
                        with races_lock:
                            if player_id in active_races:
                                active_races[player_id]['hops'].append({
                                    'hop': hop,
                                    'ip': ip,
                                    'geo': g
                                })
            else:
                payload["ip"] = None
                
            yield f"data: {json.dumps(payload)}\n\n"
        
        # Race completion logic
        print("="*60)
        print(f"🏁 RACE COMPLETION CHECK")
        print(f"   is_race: {is_race}")
        print(f"   player_id: {player_id[:8] + '...' if player_id else 'None'}")
        print(f"   last_hop_geo: {bool(last_hop_geo)}")
        print(f"   challenge: {bool(challenge)}")
        
        if is_race and player_id and last_hop_geo and challenge:
            print(f"✅ All conditions met for race completion check")
            print(f"🌍 Last hop geo data: {last_hop_geo}")
            print(f"🎯 Challenge: {challenge.city_name}")
            
            race_end_time = time.time()
            duration = race_end_time - race_start_time
            
            # Check if within target radius
            distance = haversine_distance(
                last_hop_geo['lat'], last_hop_geo['lon'],
                challenge.city_lat, challenge.city_lon
            )
            
            print(f"📏 DISTANCE CALCULATION:")
            print(f"   Player: {player_id[:8]}...")
            print(f"   Distance: {distance:.2f}km")
            print(f"   Required: ≤ {challenge.radius_km}km")
            print(f"   Last hop: {last_hop_geo.get('city')}, {last_hop_geo.get('country')} ({last_hop_geo['lat']}, {last_hop_geo['lon']})")
            print(f"   Target: {challenge.city_name} ({challenge.city_lat}, {challenge.city_lon})")
            print(f"   SUCCESS: {distance <= challenge.radius_km}")
        else:
            print(f"❌ Race completion check failed:")
            if not is_race:
                print(f"   - Not in race mode")
            if not player_id:
                print(f"   - No player ID")
            if not last_hop_geo:
                print(f"   - No geolocation data for final hop")
            if not challenge:
                print(f"   - No active challenge")
        
        if is_race and player_id and last_hop_geo and challenge:
            if distance <= challenge.radius_km:
                print(f"🎉 RACE WON! Player finished within bounds")
                # Success! Record the result
                with players_lock:
                    player = players.get(player_id)
                    player_name = player.display_name if player else "Unknown"
                    print(f"👤 Player found: {player_name}")
                
                with results_lock:
                    # Calculate rank based on completed races only
                    completed_results = [r for r in race_results if r.challenge_id == challenge.id and r.points > 0]
                    rank = len(completed_results) + 1
                    print(f"🏆 Calculated rank: {rank} (based on {len(completed_results)} completed races)")
                    
                    # Check if player already has a placeholder entry
                    placeholder_index = None
                    for i, r in enumerate(race_results):
                        if r.player_id == player_id and r.challenge_id == challenge.id and r.points == 0:
                            placeholder_index = i
                            break
                    
                    print(f"🔍 Placeholder check: {'Found at index ' + str(placeholder_index) if placeholder_index is not None else 'No placeholder found'}")
                    
                    result = RaceResult(
                        player_id=player_id,
                        player_name=player_name,
                        challenge_id=challenge.id,
                        finish_time=datetime.now(),
                        rank=rank,
                        points=calculate_points(rank),
                        final_hop_lat=last_hop_geo['lat'],
                        final_hop_lon=last_hop_geo['lon'],
                        final_hop_city=last_hop_geo.get('city', ''),
                        final_hop_country=last_hop_geo.get('country', ''),
                        total_hops=total_hops,
                        trace_duration_seconds=duration,
                        distance_from_target_km=distance
                    )
                    
                    print(f"📝 Created race result: {player_name} rank #{rank} with {result.points} points")
                    
                    # Replace placeholder or add new result
                    if placeholder_index is not None:
                        race_results[placeholder_index] = result
                        print(f"🔄 Replaced placeholder entry at index {placeholder_index}")
                    else:
                        race_results.append(result)
                        print(f"➕ Added new race result entry")
                    
                    print(f"📊 Total race results now: {len(race_results)}")
                    
                    # Save to database (update existing placeholder or insert new)
                    try:
                        with db_lock:
                            with sqlite3.connect(DB_PATH) as conn:
                                if placeholder_index is not None:
                                    print(f"💾 Updating existing database entry for placeholder")
                                    # Update existing placeholder entry
                                    rows_affected = conn.execute('''UPDATE race_results SET 
                                        finish_time = ?, rank = ?, points = ?,
                                        final_hop_lat = ?, final_hop_lon = ?, final_hop_city = ?, final_hop_country = ?,
                                        total_hops = ?, trace_duration_seconds = ?, distance_from_target_km = ?
                                        WHERE player_id = ? AND challenge_id = ? AND points = 0''',
                                        (result.finish_time.isoformat(), result.rank, result.points,
                                         result.final_hop_lat, result.final_hop_lon, result.final_hop_city, result.final_hop_country,
                                         result.total_hops, result.trace_duration_seconds, result.distance_from_target_km,
                                         result.player_id, result.challenge_id)).rowcount
                                    print(f"📊 Database rows updated: {rows_affected}")
                                else:
                                    print(f"💾 Inserting new database entry")
                                    # Insert new result
                                    conn.execute('''INSERT INTO race_results 
                                        (player_id, player_name, challenge_id, finish_time, rank, points,
                                         final_hop_lat, final_hop_lon, final_hop_city, final_hop_country,
                                         total_hops, trace_duration_seconds, distance_from_target_km)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                        (result.player_id, result.player_name, result.challenge_id,
                                         result.finish_time.isoformat(), result.rank, result.points,
                                         result.final_hop_lat, result.final_hop_lon,
                                         result.final_hop_city, result.final_hop_country,
                                         result.total_hops, result.trace_duration_seconds, result.distance_from_target_km))
                                    print(f"📊 New result inserted into database")
                                conn.commit()
                                print(f"✅ Database commit successful")
                    except Exception as e:
                        print(f"❌ Database error: {e}")
                    
                    print(f"🎯 Race result saved: {player_name} finished in position {rank} with {result.points} points")
                
                # Notify all clients of new result
                result_dict = asdict(result)
                result_dict['finish_time'] = result.finish_time.isoformat()
                socketio.emit('race_finished', {
                    'result': result_dict,
                    'distance_km': round(distance, 2)
                })
                
                # Send success event to this client
                success_payload = {
                    'type': 'race_success',
                    'rank': rank,
                    'points': calculate_points(rank),
                    'distance_km': round(distance, 2),
                    'duration_seconds': duration,
                    'final_location': f"{last_hop_geo.get('city', 'Unknown')}, {last_hop_geo.get('country', 'Unknown')}"
                }
                yield f"data: {json.dumps(success_payload)}\n\n"
            else:
                print(f"❌ RACE FAILED - Too far from target")
                
                # Update placeholder with distance but no points
                with players_lock:
                    player = players.get(player_id)
                    player_name = player.display_name if player else "Unknown"
                
                with results_lock:
                    # Find and update placeholder entry
                    for i, r in enumerate(race_results):
                        if r.player_id == player_id and r.challenge_id == challenge.id and r.points == 0:
                            # Update placeholder with attempt data
                            race_results[i] = RaceResult(
                                player_id=player_id,
                                player_name=player_name,
                                challenge_id=challenge.id,
                                finish_time=datetime.now(),
                                rank=999,  # No rank for failed attempts
                                points=0,  # No points for failed attempts
                                final_hop_lat=last_hop_geo['lat'],
                                final_hop_lon=last_hop_geo['lon'],
                                final_hop_city=last_hop_geo.get('city', ''),
                                final_hop_country=last_hop_geo.get('country', ''),
                                total_hops=total_hops,
                                trace_duration_seconds=race_end_time - race_start_time,
                                distance_from_target_km=distance
                            )
                            
                            # Update in database
                            with db_lock:
                                with sqlite3.connect(DB_PATH) as conn:
                                    conn.execute('''UPDATE race_results SET 
                                        finish_time = ?, total_hops = ?, trace_duration_seconds = ?, distance_from_target_km = ?
                                        WHERE player_id = ? AND challenge_id = ? AND points = 0''',
                                        (race_results[i].finish_time.isoformat(), total_hops, 
                                         race_results[i].trace_duration_seconds, distance,
                                         player_id, challenge.id))
                                    conn.commit()
                            break
                    
                    print(f"📊 Updated failed attempt: {player_name} - {distance:.2f}km away")
                
                # Failed - too far from target
                fail_payload = {
                    'type': 'race_failed',
                    'distance_km': round(distance, 2),
                    'required_radius_km': challenge.radius_km,
                    'final_location': f"{last_hop_geo.get('city', 'Unknown')}, {last_hop_geo.get('country', 'Unknown')}"
                }
                yield f"data: {json.dumps(fail_payload)}\n\n"
        else:
            # Only provide distance feedback for actually registered players
            if player_id and last_hop_geo and challenge:
                # Double-check that player actually exists in database/memory
                with players_lock:
                    if player_id in players:
                        distance = haversine_distance(
                            last_hop_geo['lat'], last_hop_geo['lon'],
                            challenge.city_lat, challenge.city_lon
                        )
                        print(f"📏 Registered player distance feedback: {distance:.2f}km from mystery location")
                        feedback_payload = {
                            'type': 'distance_feedback',
                            'distance_km': round(distance, 2),
                            'final_location': f"{last_hop_geo.get('city', 'Unknown')}, {last_hop_geo.get('country', 'Unknown')}"
                        }
                        yield f"data: {json.dumps(feedback_payload)}\n\n"
                    else:
                        print(f"⚪ Old player cookie detected but player not registered - no feedback provided")
            elif not player_id:
                print(f"⚪ Unregistered user - no distance feedback provided")
        
        # Clean up active race
        if is_race and player_id:
            with races_lock:
                active_races.pop(player_id, None)
        
        yield f"data: {json.dumps({'type':'end', 'target': target, 'ts': time.time()})}\n\n"

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # for nginx
        "Connection": "keep-alive",
    }
    return Response(event_stream(), headers=headers)


# -------------------------
# Entry
# -------------------------

if __name__ == "__main__":
    # Create a sample challenge for demo (Tokyo) if no active challenges exist
    active_challenge = get_active_challenge()
    if not active_challenge:
        print("No active challenge found. Creating default Tokyo challenge...")
        create_challenge(
            city_name="Tokyo, Japan",
            city_lat=35.6762,
            city_lon=139.6503,
            radius_km=100,
            target_host="8.8.8.8",
            duration_hours=24
        )
        print("Default challenge created!")
    else:
        print(f"Active challenge found: {active_challenge.city_name}")
    
    # Allow binding to all interfaces for LAN demo
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    debug = bool(os.environ.get("DEBUG", "1") == "1")
    print(f"Starting server on {host}:{port}")
    socketio.run(app, host=host, port=port, debug=debug)
