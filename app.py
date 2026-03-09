from flask import Flask, request, send_from_directory
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_cors import CORS
from datetime import datetime
import pytz
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret123'
CORS(app, origins="*")
socketio = SocketIO(app, cors_allowed_origins="*")

rooms = {}

COLORS = ["#FF6B6B","#4ECDC4","#45B7D1","#96CEB4","#F7DC6F","#DDA0DD","#98D8C8","#BB8FCE","#85C1E9","#F0A500"]

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')  # same folder

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(BASE_DIR, path)

def room_users(room):
    return [{"username": u["username"], "color": u["color"]} for u in rooms.get(room, {}).values()]

def pick_color(room):
    used = {u["color"] for u in rooms.get(room, {}).values()}
    for c in COLORS:
        if c not in used:
            return c
    return COLORS[0]

def ts():
    IST = pytz.timezone("Asia/Kolkata")
    return datetime.now(IST).strftime("%H:%M")

@socketio.on("join")
def on_join(data):
    username = data["username"].strip()
    room = data["room"].strip()
    sid = request.sid
    join_room(room)
    if room not in rooms:
        rooms[room] = {}
    color = pick_color(room)
    rooms[room][sid] = {"username": username, "color": color}
    print(f"[JOIN] {username} -> #{room} (sid={sid})")
    emit("joined", {"room": room, "username": username, "color": color, "users": room_users(room), "ts": ts()})
    emit("user_joined", {"username": username, "color": color, "users": room_users(room), "ts": ts()}, to=room, include_self=False)

@socketio.on("leave")
def on_leave(data):
    _remove(request.sid, data["room"], data["username"])
    leave_room(data["room"])

@socketio.on("send_msg")
def on_msg(data):
    room = data["room"]
    sid = request.sid
    color = rooms.get(room, {}).get(sid, {}).get("color", "#aaa")
    print(f"[MSG] {data['username']}: {data['message']}")
    emit("new_msg", {"username": data["username"], "message": data["message"], "color": color, "ts": ts()}, to=room)

@socketio.on("typing")
def on_typing(data):
    emit("typing", {"username": data["username"]}, to=data["room"], include_self=False)

@socketio.on("stop_typing")
def on_stop_typing(data):
    emit("stop_typing", {"username": data["username"]}, to=data["room"], include_self=False)

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    for room_name, users in list(rooms.items()):
        if sid in users:
            uname = users[sid]["username"]
            _remove(sid, room_name, uname)
            break

def _remove(sid, room, username):
    if room in rooms and sid in rooms[room]:
        del rooms[room][sid]
        if not rooms[room]:
            del rooms[room]
        emit("user_left", {"username": username, "users": room_users(room), "ts": ts()}, to=room)
        print(f"[LEAVE] {username} left #{room}")

if __name__ == "__main__":
    print("deeepTalk by deeepak — running at http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
