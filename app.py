import eventlet
eventlet.monkey_patch()

from flask import Flask, request, send_from_directory
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_cors import CORS
from datetime import datetime
import pytz, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret123'
CORS(app, origins="*")
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=10 * 1024 * 1024)

rooms   = {}   # rooms[room]  = { sid: {username, color} }

import sqlite3
import json
import requests

try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_AVAILABLE = True
except ImportError:
    CLOUDINARY_AVAILABLE = False

DB_PATH = os.path.join(BASE_DIR, "chat.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            room TEXT,
            kind TEXT,
            sid TEXT,
            username TEXT,
            sender_token TEXT,
            message TEXT,
            image TEXT,
            color TEXT,
            ts TEXT,
            reply_to TEXT,
            reactions TEXT,
            read_by TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS strokes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT,
            stroke_json TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_room_history(room):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM messages WHERE room = ? ORDER BY ts ASC LIMIT 50", (room,))
    rows = cursor.fetchall()
    conn.close()
    
    msgs = []
    for r in rows:
        reply_to = json.loads(r["reply_to"]) if r["reply_to"] else None
        reactions = json.loads(r["reactions"]) if r["reactions"] else {}
        read_by = json.loads(r["read_by"]) if r["read_by"] else []
        msgs.append({
            "id": r["id"],
            "kind": r["kind"],
            "sid": r["sid"],
            "username": r["username"],
            "sender_token": r["sender_token"],
            "message": r["message"],
            "image": r["image"],
            "color": r["color"],
            "ts": r["ts"],
            "replyTo": reply_to,
            "reactions": reactions,
            "readBy": read_by
        })
    return msgs

def save_message(room, msg):
    conn = get_db_connection()
    cursor = conn.cursor()
    reply_to_json = json.dumps(msg.get("replyTo")) if msg.get("replyTo") else None
    reactions_json = json.dumps(msg.get("reactions", {}))
    read_by_json = json.dumps(msg.get("readBy", []))
    cursor.execute("""
        INSERT INTO messages (id, room, kind, sid, username, sender_token, message, image, color, ts, reply_to, reactions, read_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (msg["id"], room, msg["kind"], msg["sid"], msg["username"], msg.get("sender_token", ""),
          msg.get("message", ""), msg.get("image", ""), msg["color"], msg["ts"], reply_to_json, reactions_json, read_by_json))
    
    # Prune to 50 messages
    cursor.execute("""
        DELETE FROM messages WHERE room = ? AND id NOT IN (
            SELECT id FROM messages WHERE room = ? ORDER BY ts DESC LIMIT 50
        )
    """, (room, room))
    conn.commit()
    conn.close()

def update_message_reactions(room, msg_id, reactions):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE messages SET reactions = ? WHERE room = ? AND id = ?", (json.dumps(reactions), room, msg_id))
    conn.commit()
    conn.close()

def get_room_board(room):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT stroke_json FROM strokes WHERE room = ? ORDER BY id ASC LIMIT 300", (room,))
    rows = cursor.fetchall()
    conn.close()
    return [json.loads(r["stroke_json"]) for r in rows]

def save_stroke(room, stroke):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO strokes (room, stroke_json) VALUES (?, ?)", (room, json.dumps(stroke)))
    cursor.execute("""
        DELETE FROM strokes WHERE room = ? AND id NOT IN (
            SELECT id FROM strokes WHERE room = ? ORDER BY id DESC LIMIT 300
        )
    """, (room, room))
    conn.commit()
    conn.close()

def clear_board(room):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM strokes WHERE room = ?", (room,))
    conn.commit()
    conn.close()

def undo_stroke(room, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, stroke_json FROM strokes WHERE room = ? ORDER BY id DESC", (room,))
    rows = cursor.fetchall()
    target_id = None
    for r in rows:
        stroke = json.loads(r["stroke_json"])
        if stroke.get("by") == username:
            target_id = r["id"]
            break
    if target_id is not None:
        cursor.execute("DELETE FROM strokes WHERE id = ?", (target_id,))
    conn.commit()
    conn.close()

def upload_image(base64_str):
    if CLOUDINARY_AVAILABLE and os.environ.get("CLOUDINARY_URL"):
        try:
            res = cloudinary.uploader.upload(base64_str)
            return res.get("secure_url")
        except Exception as e:
            print("Cloudinary upload failed, falling back:", e)
            
    # Fallback to Catbox
    try:
        import base64
        if "," in base64_str:
            base64_data = base64_str.split(",")[1]
        else:
            base64_data = base64_str
        img_data = base64.b64decode(base64_data)
        
        files = {'fileToUpload': ('image.jpg', img_data, 'image/jpeg')}
        data = {'reqtype': 'fileupload'}
        res = requests.post("https://catbox.moe/user/api.php", data=data, files=files, timeout=20)
        if res.status_code == 200 and res.text.startswith("http"):
            return res.text.strip()
    except Exception as e:
        print("Catbox upload failed:", e)
        
    return None

COLORS = [
    "#FF4B4B",  # vivid red
    "#FF9500",  # orange
    "#FFD600",  # yellow
    "#4CD964",  # green
    "#00C7BE",  # teal
    "#0A84FF",  # blue
    "#5E5CE6",  # indigo
    "#BF5AF2",  # purple
    "#FF2D78",  # pink
    "#00B386",  # emerald
    "#FF6B00",  # deep orange
    "#00CFFF",  # cyan
]

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/<path:path>')
def statics(path):
    return send_from_directory(BASE_DIR, path)

def room_users(room):
    return [{"sid": sid, "username": u["username"], "color": u["color"]} for sid, u in rooms.get(room, {}).items()]

def pick_color(room):
    used = {u["color"] for u in rooms.get(room, {}).values()}
    for c in COLORS:
        if c not in used:
            return c
    return COLORS[0]

def ts():
    return datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%H:%M")

@socketio.on("read_all")
def on_read_all(data):
    room = data.get("room")
    uname = data.get("username")
    if not room or not uname:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, read_by FROM messages WHERE room = ?", (room,))
    rows = cursor.fetchall()
    
    updates = {}
    for r in rows:
        msg_id = r["id"]
        read_by = json.loads(r["read_by"]) if r["read_by"] else []
        if uname not in read_by:
            read_by.append(uname)
            cursor.execute("UPDATE messages SET read_by = ? WHERE room = ? AND id = ?", (json.dumps(read_by), room, msg_id))
            updates[msg_id] = read_by
            
    if updates:
        conn.commit()
        emit("msg_read_update", {"updates": updates}, to=room)
    conn.close()

# ── Chat ────────────────────────────────────────────────────

@socketio.on("join")
def on_join(data):
    uname  = data["username"].strip()
    room   = data["room"].strip()
    token  = data.get("token", "").strip()
    sid    = request.sid
    join_room(room)
    if room not in rooms:
        rooms[room] = {}
    
    # Check if username is already taken by someone else (different token)
    username_taken = False
    for osid, u in rooms[room].items():
        if u["username"].lower() == uname.lower() and u.get("token") != token:
            username_taken = True
            break
            
    if username_taken:
        emit("join_error", {"msg": f"Username '{uname}' is already active in this room. Please choose a different name."})
        return
    
    is_reconnecting = False
    old_sids = []
    if token:
        for osid, u in rooms[room].items():
            if u.get("token") == token:
                is_reconnecting = True
                old_sids.append(osid)
            
    for osid in old_sids:
        rooms[room].pop(osid, None)

    if not token:
        import uuid
        token = f"{uname.replace(' ', '_')}_{uuid.uuid4().hex[:8]}"

    color = pick_color(room)
    rooms[room][sid] = {"username": uname, "color": color, "token": token}

    emit("joined", {
        "room": room, "username": uname, "color": color,
        "sid": sid, "token": token,
        "users": room_users(room), "ts": ts(),
        "history": get_room_history(room),
        "board":   get_room_board(room)
    })
    if room in games:
        g = games[room]
        for p in g.get('players', []):
            if p['name'] == uname:
                if g.get('host') == p['sid']:
                    g['host'] = sid
                p['sid'] = sid
        emit("alq_state", g)

    if not is_reconnecting:
        emit("user_joined", {"username": uname, "color": color, "users": room_users(room), "ts": ts()}, to=room, include_self=False)

@socketio.on("leave")
def on_leave(data):
    _remove(request.sid, data["room"], data["username"])
    leave_room(data["room"])

@socketio.on("send_msg")
def on_msg(data):
    room  = data["room"]
    sid    = request.sid
    color  = rooms.get(room, {}).get(sid, {}).get("color", "#aaa")
    token  = rooms.get(room, {}).get(sid, {}).get("token", "")
    uname  = data["username"]
    msg = {
        "kind": "msg",
        "id": f"{request.sid}_{ts()}_{uname}_{os.urandom(4).hex()}",
        "sid": request.sid,
        "username": uname,
        "sender_token": token,
        "message": data.get("message", ""),
        "color": color,
        "ts": ts(),
        "replyTo": data.get("replyTo", None),
        "reactions": {},
        "readBy": [uname]
    }
    save_message(room, msg)
    emit("new_msg", msg, to=room)

@socketio.on("send_image")
def on_image(data):
    room  = data["room"]
    sid    = request.sid
    color  = rooms.get(room, {}).get(sid, {}).get("color", "#aaa")
    token  = rooms.get(room, {}).get(sid, {}).get("token", "")
    uname  = data["username"]
    
    # Upload image externally
    uploaded_url = upload_image(data["image"])
    
    msg = {
        "kind": "image",
        "id": f"{request.sid}_{ts()}_{uname}_{os.urandom(4).hex()}",
        "sid": request.sid,
        "username": uname,
        "sender_token": token,
        "image": uploaded_url if uploaded_url else data["image"],
        "color": color,
        "ts": ts(),
        "replyTo": data.get("replyTo", None),
        "reactions": {},
        "readBy": [uname]
    }
    save_message(room, msg)
    emit("new_msg", msg, to=room)

@socketio.on("react_msg")
def on_react(data):
    """One reaction per user per message — toggling same emoji removes it, picking new one replaces old."""
    room     = data["room"]
    msg_id   = data["msg_id"]
    emoji    = data["emoji"]
    username = rooms.get(room, {}).get(request.sid, {}).get("username", "Unknown")
    for msg in history.get(room, []):
        if msg.get("id") == msg_id:
            if "reactions" not in msg:
                msg["reactions"] = {}
            # Check if user already reacted with THIS emoji → toggle off
            already_on_this = username in msg["reactions"].get(emoji, [])
            # Remove user from ALL emojis first (one reaction per user)
            for e in list(msg["reactions"].keys()):
                if username in msg["reactions"][e]:
                    msg["reactions"][e].remove(username)
                if not msg["reactions"][e]:
                    del msg["reactions"][e]
            # If they weren't on this emoji before, add them now
            if not already_on_this:
                msg["reactions"].setdefault(emoji, []).append(username)
            break
    emit("reaction_update", {"msg_id": msg_id, "reactions": msg.get("reactions", {})}, to=room)

@socketio.on("typing")
def on_typing(data):
    emit("typing", {"sid": request.sid, "username": data["username"]}, to=data["room"], include_self=False)

@socketio.on("stop_typing")
def on_stop_typing(data):
    emit("stop_typing", {"sid": request.sid}, to=data["room"], include_self=False)

# ── Alquerque Game ──────────────────────────────────────────
games = {}

# Standard Alquerque: p1=0..11, empty=12, p2=13..24
INIT_BOARD = ['p1']*12 + [None] + ['p2']*12

def neighbors(idx):
    """All valid (neighbor_idx, dr, dc) from idx on 5x5 grid with 8-directional connections"""
    r,c = divmod(idx,5)
    result = []
    for dr in (-1,0,1):
        for dc in (-1,0,1):
            if dr==0 and dc==0: continue
            nr,nc = r+dr, c+dc
            if 0<=nr<5 and 0<=nc<5:
                result.append((nr*5+nc, dr, dc))
    return result

def get_moves(board, idx, chain_idx=None):
    """Get valid moves for piece at idx. chain_idx restricts to that piece if set."""
    piece = board[idx]
    if not piece: return []
    if chain_idx is not None and chain_idx != idx: return []
    opp = 'p2' if piece=='p1' else 'p1'
    moves = []
    for nidx,dr,dc in neighbors(idx):
        if board[nidx] is None and chain_idx is None:
            moves.append({'type':'slide','from':idx,'to':nidx,'cap':None})
        elif board[nidx] == opp:
            er,ec = nidx//5+dr, nidx%5+dc
            if 0<=er<5 and 0<=ec<5:
                land = er*5+ec
                if board[land] is None:
                    moves.append({'type':'capture','from':idx,'to':land,'cap':nidx})
    return moves

def all_player_moves(board, player, chain_idx=None):
    moves = []
    for i in range(25):
        if board[i] == player:
            moves.extend(get_moves(board, i, chain_idx))
    return moves

def check_winner(board):
    if not any(x=='p1' for x in board): return 'p2'
    if not any(x=='p2' for x in board): return 'p1'
    return None

@socketio.on('alq_new_or_join')
def on_alq_new_or_join(data):
    """Called by game.html — joins socket room, creates or joins game."""
    room  = data.get('room','').strip()
    uname = data.get('username','?').strip()
    sid   = request.sid
    if not room: return
    join_room(room)
    # If no game exists, create one
    if room not in games or games[room]['status'] == 'finished':
        games[room] = {
            'status':'lobby', 'host':sid,
            'players':[{'sid':sid,'name':uname,'role':'p1'}],
            'board':None,'turn':None,
            'captured':{'p1':0,'p2':0},
            'chain':None,'winner':None
        }
    else:
        g = games[room]
        # Already in game?
        if not any(p['sid']==sid for p in g['players']):
            if len(g['players'])<2 and g['status']=='lobby':
                g['players'].append({'sid':sid,'name':uname,'role':'p2'})
    emit('alq_state', games[room], to=room)

@socketio.on('alq_new')
def on_alq_new(data):
    room = data.get('room'); sid = request.sid
    uname = rooms.get(room,{}).get(sid,{}).get('username','?')
    if room in games and games[room]['status'] in ('lobby','playing'):
        emit('alq_error',{'msg':'A game is already active'}); return
    games[room] = {
        'status':'lobby', 'host':sid,
        'players':[{'sid':sid,'name':uname,'role':'p1'}],
        'board':None, 'turn':None,
        'captured':{'p1':0,'p2':0},
        'chain':None, 'winner':None
    }
    emit('alq_state', games[room], to=room)

@socketio.on('alq_join')
def on_alq_join(data):
    room = data.get('room'); sid = request.sid
    uname = rooms.get(room,{}).get(sid,{}).get('username','?')
    g = games.get(room)
    if not g or g['status']!='lobby': return
    if any(p['sid']==sid for p in g['players']): return
    if len(g['players'])>=2: emit('alq_error',{'msg':'Game full'}); return
    g['players'].append({'sid':sid,'name':uname,'role':'p2'})
    emit('alq_state', g, to=room)

@socketio.on('alq_start')
def on_alq_start(data):
    room = data.get('room'); sid = request.sid
    g = games.get(room)
    if not g or g['status']!='lobby' or g['host']!=sid: return
    if len(g['players'])<2: emit('alq_error',{'msg':'Need 2 players'}); return
    import copy
    g.update({'status':'playing','board':copy.copy(INIT_BOARD),
              'turn':'p1','captured':{'p1':0,'p2':0},'chain':None,'winner':None})
    emit('alq_state', g, to=room)

@socketio.on('alq_move')
def on_alq_move(data):
    room=data.get('room'); sid=request.sid
    fi,ti = data.get('from'), data.get('to')
    g = games.get(room)
    if not g or g['status']!='playing': return
    turn = g['turn']
    player = next((p for p in g['players'] if p['role']==turn), None)
    if not player or player['sid']!=sid: return
    valid = get_moves(g['board'], fi, g['chain'])
    mv = next((m for m in valid if m['to']==ti), None)
    if not mv: return
    b = g['board']
    b[ti]=b[fi]; b[fi]=None
    if mv['cap'] is not None:
        b[mv['cap']]=None
        g['captured'][turn]+=1
        w = check_winner(b)
        if w:
            g['status']='finished'; g['winner']=w
            emit('alq_state',g,to=room); return
        # Check chain captures
        chains = [m for m in get_moves(b,ti,ti) if m['type']=='capture']
        if chains:
            g['chain']=ti
            emit('alq_state',g,to=room); return
    g['chain']=None
    g['turn']='p2' if turn=='p1' else 'p1'
    emit('alq_state',g,to=room)

@socketio.on('alq_stop_chain')
def on_alq_stop_chain(data):
    room=data.get('room'); sid=request.sid
    g=games.get(room)
    if not g or g['status']!='playing' or g['chain'] is None: return
    turn=g['turn']
    player=next((p for p in g['players'] if p['role']==turn),None)
    if not player or player['sid']!=sid: return
    g['chain']=None
    g['turn']='p2' if turn=='p1' else 'p1'
    emit('alq_state',g,to=room)

@socketio.on('alq_resign')
def on_alq_resign(data):
    room=data.get('room'); sid=request.sid
    g=games.get(room)
    if not g or g['status']!='playing': return
    p=next((x for x in g['players'] if x['sid']==sid),None)
    if not p: return
    g['status']='finished'
    g['winner']='p2' if p['role']=='p1' else 'p1'
    emit('alq_state',g,to=room)

@socketio.on('alq_close')
def on_alq_close(data):
    room=data.get('room')
    if room in games: del games[room]
    emit('alq_closed',{},to=room)

# ── Whiteboard ──────────────────────────────────────────────

@socketio.on("wb_cursor")
def on_wb_cursor(data):
    """Broadcast live cursor position — no storage, pure relay."""
    room = data["room"]
    emit("wb_cursor", data, to=room, include_self=False)

@socketio.on("wb_img_move")
def on_wb_img_move(data):
    """Relay image drag/resize to others and update stored board."""
    room   = data.get("room")
    stroke = data.get("stroke")
    if not room or not stroke:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, stroke_json FROM strokes WHERE room = ?", (room,))
    rows = cursor.fetchall()
    for r in rows:
        s = json.loads(r["stroke_json"])
        if s.get("tool")=="wbimage" and s.get("src")==stroke.get("src") and s.get("by")==stroke.get("by"):
            s["x"]=stroke["x"]; s["y"]=stroke["y"]
            s["w"]=stroke["w"]; s["h"]=stroke["h"]
            cursor.execute("UPDATE strokes SET stroke_json = ? WHERE id = ?", (json.dumps(s), r["id"]))
            break
    conn.commit()
    conn.close()
    emit("wb_img_move", data, to=room, include_self=False)

@socketio.on("wb_img_delete")
def on_wb_img_delete(data):
    """Remove a specific image from the board."""
    room = data.get("room")
    if not room: return
    src = data.get("src")
    by = data.get("by")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, stroke_json FROM strokes WHERE room = ?", (room,))
    rows = cursor.fetchall()
    for r in rows:
        s = json.loads(r["stroke_json"])
        if s.get("tool")=="wbimage" and s.get("src")==src and s.get("by")==by:
            cursor.execute("DELETE FROM strokes WHERE id = ?", (r["id"],))
            break
    conn.commit()
    conn.close()
    emit("wb_img_delete", data, to=room, include_self=False)

@socketio.on("wb_segment")
def on_wb_segment(data):
    """Live pen/eraser segment — broadcast only, not stored."""
    room = data["room"]
    emit("wb_segment", data, to=room, include_self=False)

@socketio.on("wb_stroke")
def on_wb_stroke(data):
    """Completed stroke — broadcast + store."""
    room   = data["room"]
    stroke = data["stroke"]
    save_stroke(room, stroke)
    emit("wb_stroke", {"stroke": stroke}, to=room, include_self=False)

@socketio.on("wb_clear")
def on_wb_clear(data):
    room = data["room"]
    clear_board(room)
    emit("wb_clear", {}, to=room, include_self=False)

@socketio.on("wb_undo")
def on_wb_undo(data):
    """Remove last stroke for this room."""
    room = data["room"]
    sid  = request.sid
    uname = rooms.get(room, {}).get(sid, {}).get("username", "")
    undo_stroke(room, uname)
    emit("wb_state", {"board": get_room_board(room)}, to=room)

# ── Disconnect ─────────────────────────────────────────────

@socketio.on("disconnect")
def on_dc():
    sid = request.sid
    for rn, users in list(rooms.items()):
        if sid in users:
            username = users[sid]["username"]
            eventlet.spawn(deferred_remove, sid, rn, username)
            break

def delayed_cleanup(room):
    eventlet.sleep(180)
    if room in rooms and not rooms[room]:
        rooms.pop(room, None)
        games.pop(room, None)
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM messages WHERE room = ?", (room,))
            cursor.execute("DELETE FROM strokes WHERE room = ?", (room,))
            conn.commit()
            conn.close()
        except Exception as e:
            print("DB cleanup failed:", e)

def deferred_remove(sid, room, username):
    eventlet.sleep(15)  # 15s grace period for unexpected drops
    if room not in rooms:
        return
    token = None
    if sid in rooms[room]:
        token = rooms[room][sid].get("token")
    token_still_connected = False
    if token:
        token_still_connected = any(u.get("token") == token for osid, u in rooms[room].items() if osid != sid)
    if sid in rooms[room]:
        del rooms[room][sid]
    if not token_still_connected:
        if rooms[room]:
            emit("user_left", {"sid": sid, "username": username, "users": room_users(room), "ts": ts()}, to=room)
        else:
            eventlet.spawn(delayed_cleanup, room)

def _remove(sid, room, username):
    if room not in rooms or sid not in rooms[room]:
        return
    del rooms[room][sid]
    if rooms[room]:
        emit("user_left", {"sid": sid, "username": username, "users": room_users(room), "ts": ts()}, to=room)
    else:
        eventlet.spawn(delayed_cleanup, room)

if __name__ == "__main__":
    print("deeepTalk by deeepak — http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
