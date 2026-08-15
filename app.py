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
history = {}   # history[room] = [ msg, ... ]
boards  = {}   # boards[room]  = [ stroke, ... ]  (persistent until room destroyed)

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

def push_history(room, msg):
    if room not in history:
        history[room] = []
    history[room].append(msg)
    if len(history[room]) > 50:
        history[room] = history[room][-50:]

# ── Chat ────────────────────────────────────────────────────

@socketio.on("join")
def on_join(data):
    uname  = data["username"].strip()
    room   = data["room"].strip()
    sid    = request.sid
    join_room(room)
    if room not in rooms:
        rooms[room] = {}
    color = pick_color(room)
    rooms[room][sid] = {"username": uname, "color": color}

    emit("joined", {
        "room": room, "username": uname, "color": color,
        "sid": sid,
        "users": room_users(room), "ts": ts(),
        "history": history.get(room, []),
        "board":   boards.get(room, [])
    })
    if room in games:
        emit("alq_state", games[room])

    evt = {"kind": "event", "text": f"{uname} joined the room", "ts": ts()}
    push_history(room, evt)
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
    msg = {
        "kind": "msg",
        "id": f"{request.sid}_{ts()}_{len(history.get(room,[]))}",
        "sid": request.sid,
        "username": data["username"],
        "message": data.get("message", ""),
        "color": color,
        "ts": ts(),
        "replyTo": data.get("replyTo", None),
        "reactions": {}
    }
    push_history(room, msg)
    emit("new_msg", msg, to=room)

@socketio.on("send_image")
def on_image(data):
    room  = data["room"]
    sid    = request.sid
    color  = rooms.get(room, {}).get(sid, {}).get("color", "#aaa")
    msg = {
        "kind": "image",
        "id": f"{request.sid}_{ts()}_{len(history.get(room,[]))}",
        "sid": request.sid,
        "username": data["username"],
        "image": data["image"],
        "color": color,
        "ts": ts(),
        "replyTo": data.get("replyTo", None),
        "reactions": {}
    }
    push_history(room, msg)
    emit("new_msg", msg, to=room)

@socketio.on("react_msg")
def on_react(data):
    """One reaction per user per message — toggling same emoji removes it, picking new one replaces old."""
    room     = data["room"]
    msg_id   = data["msg_id"]
    emoji    = data["emoji"]
    user_sid = request.sid
    for msg in history.get(room, []):
        if msg.get("id") == msg_id:
            if "reactions" not in msg:
                msg["reactions"] = {}
            # Check if user already reacted with THIS emoji → toggle off
            already_on_this = user_sid in msg["reactions"].get(emoji, [])
            # Remove user from ALL emojis first (one reaction per user)
            for e in list(msg["reactions"].keys()):
                if user_sid in msg["reactions"][e]:
                    msg["reactions"][e].remove(user_sid)
                if not msg["reactions"][e]:
                    del msg["reactions"][e]
            # If they weren't on this emoji before, add them now
            if not already_on_this:
                msg["reactions"].setdefault(emoji, []).append(user_sid)
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
    for s in boards.get(room, []):
        if s.get("tool")=="wbimage" and s.get("src")==stroke.get("src") and s.get("by")==stroke.get("by"):
            s["x"]=stroke["x"]; s["y"]=stroke["y"]
            s["w"]=stroke["w"]; s["h"]=stroke["h"]
            break
    emit("wb_img_move", data, to=room, include_self=False)

@socketio.on("wb_img_delete")
def on_wb_img_delete(data):
    """Remove a specific image from the board."""
    room = data.get("room")
    if not room: return
    boards[room] = [s for s in boards.get(room, [])
                    if not (s.get("tool")=="wbimage" and
                            s.get("src")==data.get("src") and
                            s.get("by")==data.get("by"))]
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
    if room not in boards:
        boards[room] = []
    boards[room].append(stroke)
    # Cap at 300 strokes
    if len(boards[room]) > 300:
        boards[room] = boards[room][-300:]
    emit("wb_stroke", {"stroke": stroke}, to=room, include_self=False)

@socketio.on("wb_clear")
def on_wb_clear(data):
    room = data["room"]
    boards[room] = []
    emit("wb_clear", {}, to=room, include_self=False)

@socketio.on("wb_undo")
def on_wb_undo(data):
    """Remove last stroke for this room."""
    room = data["room"]
    sid  = request.sid
    uname = rooms.get(room, {}).get(sid, {}).get("username", "")
    # Remove last stroke by this user
    if room in boards:
        for i in range(len(boards[room])-1, -1, -1):
            if boards[room][i].get("by") == uname:
                boards[room].pop(i)
                break
    emit("wb_state", {"board": boards.get(room, [])}, to=room)

# ── Disconnect ─────────────────────────────────────────────

@socketio.on("disconnect")
def on_dc():
    sid = request.sid
    for rn, users in list(rooms.items()):
        if sid in users:
            _remove(sid, rn, users[sid]["username"])
            break

def delayed_cleanup(room):
    eventlet.sleep(180)
    if room in rooms and not rooms[room]:
        rooms.pop(room, None)
        history.pop(room, None)
        boards.pop(room, None)
        games.pop(room, None)

def _remove(sid, room, username):
    if room not in rooms or sid not in rooms[room]:
        return
    del rooms[room][sid]
    if rooms[room]:
        evt = {"kind": "event", "text": f"{username} left the room", "ts": ts()}
        push_history(room, evt)
        emit("user_left", {"sid": sid, "username": username, "users": room_users(room), "ts": ts()}, to=room)
    else:
        eventlet.spawn(delayed_cleanup, room)

if __name__ == "__main__":
    print("deeepTalk by deeepak — http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
