import asyncio
import json
import os
import sys
import uuid
import websockets
from datetime import datetime, timezone
from collections import defaultdict, deque
from pathlib import Path
from websockets.http11 import Response
from websockets.datastructures import Headers

INDEX_PATH = Path(__file__).parent / "index.html"

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ws -> {name, room, color, joined_at, msg_count, avatar}
clients = {}
# room -> set of websockets
rooms = defaultdict(set)
# room -> owner name
room_owners = {}
# room -> set of admin names
room_admins = defaultdict(set)
# room -> password (str); отсутствие ключа = публичная комната
room_passwords = {}
# msg_id -> {emoji -> set(user_names)}
reactions = {}
# msg_id -> room
msg_room = {}
# msg_id -> author name
msg_author = {}
# room -> deque of message payload dicts (hard cap = MAX_HISTORY)
MAX_HISTORY = 1000
room_history = defaultdict(deque)
# msg_id -> message payload dict (shared with room_history entries for O(1) edits)
msg_data = {}
# Максимум комнат, которые сервер держит одновременно
MAX_ROOMS = 10

COLORS = [
    '#e94560', '#4caf50', '#2196f3', '#ff9800',
    '#9c27b0', '#00bcd4', '#ff5722', '#8bc34a',
    '#3f51b5', '#f06292', '#ffc107', '#1a936f',
]
MAX_AVATAR_SIZE = 150_000  # ~150 KB base64
ALLOWED_EMOJI = {'👍', '❤️', '😂', '😮', '😢', '👎'}


def pick_color(name):
    return COLORS[sum(ord(c) for c in name) % len(COLORS)]


def _forget_msg(msg_id):
    msg_data.pop(msg_id, None)
    msg_room.pop(msg_id, None)
    msg_author.pop(msg_id, None)
    reactions.pop(msg_id, None)


def store_message(room, payload):
    history = room_history[room]
    while len(history) >= MAX_HISTORY:
        old = history.popleft()
        _forget_msg(old["id"])
    history.append(payload)
    msg_data[payload["id"]] = payload


def now():
    return datetime.now(timezone.utc).isoformat()


async def broadcast_room(room, message, exclude=None):
    targets = [ws for ws in rooms[room] if ws != exclude]
    if targets:
        data = json.dumps(message)
        await asyncio.gather(*[ws.send(data) for ws in targets], return_exceptions=True)


async def send_to_room(room, message):
    targets = list(rooms[room])
    if targets:
        data = json.dumps(message)
        await asyncio.gather(*[ws.send(data) for ws in targets], return_exceptions=True)


async def broadcast_rooms_list():
    room_list = [
        {
            "name": r,
            "count": len(rooms.get(r, ())),
            "owner": room_owners[r],
            "private": r in room_passwords,
        }
        for r in room_owners
    ]
    data = json.dumps({"type": "rooms_list", "rooms": room_list, "online": len(clients)})
    if clients:
        await asyncio.gather(*[ws.send(data) for ws in list(clients.keys())], return_exceptions=True)


async def broadcast_room_users(room):
    admins = room_admins.get(room, set())
    owner = room_owners.get(room, "")
    users = [
        {
            "name": clients[ws]["name"],
            "color": clients[ws]["color"],
            "avatar": clients[ws]["avatar"],
            "admin": clients[ws]["name"] in admins,
        }
        for ws in rooms[room] if ws in clients
    ]
    data = json.dumps({
        "type": "room_users",
        "users": users,
        "owner": owner,
        "admins": sorted(admins),
    })
    await asyncio.gather(*[ws.send(data) for ws in list(rooms[room])], return_exceptions=True)


async def handler(websocket):
    clients[websocket] = {
        "name": "Аноним",
        "room": None,
        "color": "#aaa",
        "joined_at": now(),
        "msg_count": 0,
        "avatar": None,
    }
    print(f"[+] Подключён клиент. Всего: {len(clients)}")

    try:
        await broadcast_rooms_list()

        async for raw in websocket:
            msg = json.loads(raw)
            info = clients[websocket]

            if msg["type"] == "join":
                info["name"] = msg["name"]
                info["color"] = pick_color(msg["name"])
                if msg.get("avatar"):
                    data = msg["avatar"]
                    if data.startswith("data:image/") and len(data) <= MAX_AVATAR_SIZE:
                        info["avatar"] = data
                print(f"  {info['name']} подключился")

                await websocket.send(json.dumps({
                    "type": "my_profile",
                    "color": info["color"],
                }))

            elif msg["type"] == "set_avatar":
                data = msg.get("data", "")
                if data.startswith("data:image/") and len(data) <= MAX_AVATAR_SIZE:
                    info["avatar"] = data
                elif data == "":
                    info["avatar"] = None

                room = info["room"]
                if room:
                    await broadcast_room_users(room)

                await websocket.send(json.dumps({"type": "avatar_updated"}))

            elif msg["type"] == "join_room":
                name = info["name"]
                new_room = msg["room"].strip()
                password = (msg.get("password") or "").strip()
                old_room = info["room"]
                room_exists = new_room in room_owners

                if room_exists and new_room in room_passwords:
                    if password != room_passwords[new_room]:
                        await websocket.send(json.dumps({
                            "type": "join_error",
                            "room": new_room,
                            "reason": "bad_password",
                        }))
                        continue

                if not room_exists and len(room_owners) >= MAX_ROOMS:
                    await websocket.send(json.dumps({
                        "type": "join_error",
                        "room": new_room,
                        "reason": "too_many_rooms",
                        "limit": MAX_ROOMS,
                    }))
                    continue

                if old_room:
                    rooms[old_room].discard(websocket)
                    await broadcast_room(old_room, {
                        "type": "system",
                        "text": f"{name} покинул комнату",
                        "time": now()
                    })
                    if rooms[old_room]:
                        await broadcast_room_users(old_room)

                if not room_exists:
                    room_owners[new_room] = name
                    if password:
                        room_passwords[new_room] = password

                rooms[new_room].add(websocket)
                info["room"] = new_room
                print(f"  {name} → '{new_room}' (владелец: {room_owners[new_room]}"
                      f"{', 🔒' if new_room in room_passwords else ''})")

                await websocket.send(json.dumps({
                    "type": "room_joined",
                    "room": new_room,
                    "owner": room_owners[new_room],
                    "private": new_room in room_passwords,
                    "time": now()
                }))
                history = list(room_history.get(new_room, ()))
                if history:
                    await websocket.send(json.dumps({
                        "type": "history",
                        "messages": history,
                    }))
                await broadcast_room(new_room, {
                    "type": "system",
                    "text": f"{name} зашёл в комнату",
                    "time": now()
                }, exclude=websocket)
                await broadcast_room_users(new_room)
                await broadcast_rooms_list()

            elif msg["type"] == "message":
                room = info["room"]
                if not room:
                    continue
                t = now()
                msg_id = uuid.uuid4().hex[:8]
                reactions[msg_id] = {}
                msg_room[msg_id] = room
                msg_author[msg_id] = info["name"]
                info["msg_count"] += 1
                print(f"[{t}] [{room}] {info['name']}: {msg['text']}")
                payload = {
                    "type": "message",
                    "id": msg_id,
                    "name": info["name"],
                    "color": info["color"],
                    "avatar": info["avatar"],
                    "text": msg["text"],
                    "time": t,
                    "reactions": {},
                }
                store_message(room, payload)
                await send_to_room(room, payload)

            elif msg["type"] == "react":
                msg_id = msg.get("msg_id")
                emoji = msg.get("emoji")
                if not msg_id or emoji not in ALLOWED_EMOJI:
                    continue
                if msg_id not in reactions:
                    continue

                name = info["name"]
                room = msg_room.get(msg_id)
                if not room:
                    continue

                r = reactions[msg_id]
                prev_emoji = next((e for e, names in r.items() if name in names), None)
                if prev_emoji:
                    r[prev_emoji].discard(name)
                    if not r[prev_emoji]:
                        del r[prev_emoji]

                if prev_emoji != emoji:
                    r.setdefault(emoji, set()).add(name)

                snapshot = {e: list(names) for e, names in r.items()}
                if msg_id in msg_data:
                    msg_data[msg_id]["reactions"] = snapshot
                await send_to_room(room, {
                    "type": "reaction_update",
                    "msg_id": msg_id,
                    "reactions": snapshot,
                })

            elif msg["type"] == "delete_msg":
                msg_id = msg.get("msg_id")
                if not msg_id or msg_id not in msg_room:
                    continue
                room = msg_room[msg_id]
                if info["room"] != room:
                    continue
                name = info["name"]
                author = msg_author.get(msg_id)
                is_owner = room_owners.get(room) == name
                is_admin = name in room_admins.get(room, set())
                if name != author and not is_owner and not is_admin:
                    continue
                payload = msg_data.pop(msg_id, None)
                if payload is not None:
                    try:
                        room_history[room].remove(payload)
                    except ValueError:
                        pass
                msg_room.pop(msg_id, None)
                msg_author.pop(msg_id, None)
                reactions.pop(msg_id, None)
                await send_to_room(room, {
                    "type": "msg_deleted",
                    "msg_id": msg_id,
                    "by": name,
                })

            elif msg["type"] == "edit_msg":
                msg_id = msg.get("msg_id")
                new_text = (msg.get("text") or "").strip()
                if not msg_id or not new_text or msg_id not in msg_room:
                    continue
                room = msg_room[msg_id]
                if info["room"] != room:
                    continue
                if msg_author.get(msg_id) != info["name"]:
                    continue
                if msg_id in msg_data:
                    msg_data[msg_id]["text"] = new_text
                await send_to_room(room, {
                    "type": "msg_edited",
                    "msg_id": msg_id,
                    "text": new_text,
                })

            elif msg["type"] == "get_profile":
                target_name = msg.get("name")
                target = next(
                    (c for c in clients.values() if c["name"] == target_name), None
                )
                if target:
                    t_room = target["room"]
                    await websocket.send(json.dumps({
                        "type": "profile_data",
                        "name": target["name"],
                        "color": target["color"],
                        "avatar": target["avatar"],
                        "joined_at": target["joined_at"],
                        "msg_count": target["msg_count"],
                        "room": t_room or "—",
                        "is_owner": bool(t_room) and room_owners.get(t_room) == target["name"],
                        "is_admin": bool(t_room) and target["name"] in room_admins.get(t_room, set()),
                        "online": True,
                    }))

            elif msg["type"] in ("promote", "demote"):
                room = info["room"]
                target_name = (msg.get("name") or "").strip()
                if not room or not target_name:
                    continue
                if room_owners.get(room) != info["name"]:
                    continue
                if target_name == info["name"]:
                    continue

                target_in_room = any(
                    clients[ws]["name"] == target_name
                    for ws in rooms[room] if ws in clients
                )
                if not target_in_room:
                    continue

                admins = room_admins[room]
                if msg["type"] == "promote":
                    if target_name in admins:
                        continue
                    admins.add(target_name)
                    text = f"{target_name} назначен админом"
                else:
                    if target_name not in admins:
                        continue
                    admins.discard(target_name)
                    text = f"{target_name} больше не админ"

                await send_to_room(room, {
                    "type": "system",
                    "text": text,
                    "time": now()
                })
                await broadcast_room_users(room)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        info = clients.pop(websocket, {"name": "Аноним", "room": None})
        name, room = info["name"], info["room"]
        if room and websocket in rooms.get(room, set()):
            rooms[room].discard(websocket)
            if rooms[room]:
                await broadcast_room_users(room)
            await broadcast_room(room, {
                "type": "system",
                "text": f"{name} покинул комнату",
                "time": now()
            })
        await broadcast_rooms_list()
        print(f"[-] {name} отключился. Всего: {len(clients)}")


def http_response(status, reason, body, content_type="text/plain; charset=utf-8"):
    return Response(
        status, reason,
        Headers([
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Connection", "close"),
        ]),
        body,
    )


async def process_request(connection, request):
    """Отдаём index.html по HTTP, пропускаем WebSocket-апгрейд дальше."""
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    path = request.path.split("?", 1)[0]
    if path in ("/", "/index.html"):
        try:
            body = INDEX_PATH.read_bytes()
        except FileNotFoundError:
            return http_response(404, "Not Found", b"index.html not found\n")
        return http_response(200, "OK", body, "text/html; charset=utf-8")
    if path == "/health":
        return http_response(200, "OK", b"ok\n")
    if path == "/favicon.ico":
        return http_response(204, "No Content", b"")
    return http_response(404, "Not Found", b"not found\n")


async def main():
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 8765))
    print(f"Сервер на http://{host}:{port} (WebSocket + статика index.html)")
    async with websockets.serve(handler, host, port, process_request=process_request):
        await asyncio.Future()


asyncio.run(main())
