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

COLORS = ["#FF6B6B","#4ECDC4","#45B7D1","#96CEB4","#F7DC6F","#DDA0DD","#98D8C8","#BB8FCE","#85C1E9","#F0A500"]

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
    if len(history[room]) > 200:
        history[room] = history[room][-200:]

# ── Chat ────────────────────────────────────────────────────

@socketio.on("join")
def on_join(data):
    uname = data["username"].strip()
    room  = data["room"].strip()
    sid   = request.sid
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
    sid   = request.sid
    color = rooms.get(room, {}).get(sid, {}).get("color", "#aaa")
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
    sid   = request.sid
    color = rooms.get(room, {}).get(sid, {}).get("color", "#aaa")
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
    """Toggle a reaction emoji on a message."""
    room     = data["room"]
    msg_id   = data["msg_id"]
    emoji    = data["emoji"]
    user_sid = request.sid          # use sid, not username — unique per connection
    for msg in history.get(room, []):
        if msg.get("id") == msg_id:
            if "reactions" not in msg:
                msg["reactions"] = {}
            sids_reacted = msg["reactions"].get(emoji, [])
            if user_sid in sids_reacted:
                sids_reacted.remove(user_sid)
            else:
                sids_reacted.append(user_sid)
            if sids_reacted:
                msg["reactions"][emoji] = sids_reacted
            elif emoji in msg["reactions"]:
                del msg["reactions"][emoji]
            break
    emit("reaction_update", {"msg_id": msg_id, "reactions": msg.get("reactions", {})}, to=room)

@socketio.on("typing")
def on_typing(data):
    emit("typing", {"sid": request.sid, "username": data["username"]}, to=data["room"], include_self=False)

@socketio.on("stop_typing")
def on_stop_typing(data):
    emit("stop_typing", {"sid": request.sid}, to=data["room"], include_self=False)

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
    # Cap at 500 strokes
    if len(boards[room]) > 500:
        boards[room] = boards[room][-500:]
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

def _remove(sid, room, username):
    if room not in rooms or sid not in rooms[room]:
        return
    del rooms[room][sid]
    if rooms[room]:
        evt = {"kind": "event", "text": f"{username} left the room", "ts": ts()}
        push_history(room, evt)
        emit("user_left", {"sid": sid, "username": username, "users": room_users(room), "ts": ts()}, to=room)
    else:
        del rooms[room]
        if room in history: del history[room]
        if room in boards:  del boards[room]

if __name__ == "__main__":
    print("deeepTalk by deeepak — http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
