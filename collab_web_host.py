#!/usr/bin/env python3
"""
Collaborative web host for shareable link sessions.

Features:
- Browser participants join via URL query params (room/user).
- Real-time participant cursor visualization in browser.
- Per-user MIDI channel assignment for Logic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import mido
from aiohttp import WSMsgType, web


@dataclass
class MidiMapping:
    speed_cc: int
    x_cc: int
    y_cc: int
    click_cc: int
    scroll_cc: int


@dataclass
class ParticipantState:
    user_id: str
    channel: int
    values: dict[str, int] = field(default_factory=lambda: {"x": 0, "y": 0, "speed": 0, "click": 0, "scroll": 0})
    updated_ms: int = 0


@dataclass
class RoomState:
    participants: dict[str, ParticipantState] = field(default_factory=dict)
    sockets: set[web.WebSocketResponse] = field(default_factory=set)
    used_channels: set[int] = field(default_factory=set)


class SessionServer:
    def __init__(
        self,
        midi_port: str,
        default_channels: list[int],
        explicit_assignments: dict[str, int],
        mapping: MidiMapping,
        debug_print_ms: int,
        state_broadcast_ms: int,
    ) -> None:
        self.mapping = mapping
        self.default_channels = default_channels
        self.explicit_assignments = explicit_assignments
        self.rooms: dict[str, RoomState] = {}
        self.out_port = mido.open_output(midi_port)
        self.debug_print_ms = debug_print_ms
        self.state_broadcast_ms = state_broadcast_ms

    def close(self) -> None:
        self.out_port.close()

    def get_room(self, room_id: str) -> RoomState:
        if room_id not in self.rooms:
            self.rooms[room_id] = RoomState()
        return self.rooms[room_id]

    def assign_channel(self, room: RoomState, user_id: str) -> int:
        if user_id in room.participants:
            return room.participants[user_id].channel
        if user_id in self.explicit_assignments:
            channel = self.explicit_assignments[user_id]
        else:
            channel = next((ch for ch in self.default_channels if ch not in room.used_channels), None)
            if channel is None:
                raise RuntimeError("No available MIDI channels left for this room.")
        room.used_channels.add(channel)
        room.participants[user_id] = ParticipantState(user_id=user_id, channel=channel)
        return channel

    def update_metrics(self, room_id: str, user_id: str, values: dict[str, Any]) -> None:
        room = self.get_room(room_id)
        if user_id not in room.participants:
            self.assign_channel(room, user_id)
        participant = room.participants[user_id]
        sanitized = {
            "x": max(0, min(127, int(values.get("x", 0)))),
            "y": max(0, min(127, int(values.get("y", 0)))),
            "speed": max(0, min(127, int(values.get("speed", 0)))),
            "click": max(0, min(127, int(values.get("click", 0)))),
            "scroll": max(0, min(127, int(values.get("scroll", 0)))),
        }
        participant.values = sanitized
        participant.updated_ms = int(time.time() * 1000)
        self.send_midi(participant.channel, sanitized)

    def send_midi(self, channel: int, values: dict[str, int]) -> None:
        self.send_cc(channel, self.mapping.speed_cc, values["speed"])
        self.send_cc(channel, self.mapping.x_cc, values["x"])
        self.send_cc(channel, self.mapping.y_cc, values["y"])
        self.send_cc(channel, self.mapping.click_cc, values["click"])
        self.send_cc(channel, self.mapping.scroll_cc, values["scroll"])

    def send_cc(self, channel: int, cc: int, value: int) -> None:
        msg = mido.Message("control_change", channel=channel, control=max(0, min(127, cc)), value=value)
        self.out_port.send(msg)

    async def broadcast_state(self, room_id: str) -> None:
        room = self.get_room(room_id)
        if not room.sockets:
            return
        payload = {
            "type": "state",
            "room": room_id,
            "participants": {
                user_id: {
                    "channel": state.channel + 1,
                    "x": state.values["x"],
                    "y": state.values["y"],
                    "speed": state.values["speed"],
                    "click": state.values["click"],
                    "scroll": state.values["scroll"],
                    "updated_ms": state.updated_ms,
                }
                for user_id, state in room.participants.items()
            },
        }
        text = json.dumps(payload)
        stale: list[web.WebSocketResponse] = []
        for ws in room.sockets:
            try:
                await ws.send_str(text)
            except Exception:
                stale.append(ws)
        for ws in stale:
            room.sockets.discard(ws)

    async def debug_print_loop(self) -> None:
        if self.debug_print_ms <= 0:
            return
        while True:
            now_ms = int(time.time() * 1000)
            printed_any = False
            for room_id, room in self.rooms.items():
                if not room.participants:
                    continue
                if not printed_any:
                    print(f"[debug {time.strftime('%H:%M:%S')}] latest mouse metrics")
                    printed_any = True
                print(f"  room={room_id}")
                for user_id, state in sorted(room.participants.items()):
                    age_ms = max(0, now_ms - state.updated_ms) if state.updated_ms else -1
                    values = state.values
                    print(
                        "    "
                        f"user={user_id} ch={state.channel + 1} "
                        f"x={values['x']:>3} y={values['y']:>3} "
                        f"speed={values['speed']:>3} click={values['click']:>3} "
                        f"scroll={values['scroll']:>3} age_ms={age_ms}"
                    )
            await asyncio.sleep(self.debug_print_ms / 1000.0)

    async def state_broadcast_loop(self) -> None:
        if self.state_broadcast_ms <= 0:
            return
        while True:
            room_ids = [room_id for room_id, room in self.rooms.items() if room.sockets]
            for room_id in room_ids:
                await self.broadcast_state(room_id)
            await asyncio.sleep(self.state_broadcast_ms / 1000.0)


def parse_channel_list(text: str) -> list[int]:
    channels: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if not 1 <= value <= 16:
            raise ValueError("Channels must be in range 1..16.")
        channels.append(value - 1)
    deduped = list(dict.fromkeys(channels))
    if not deduped:
        raise ValueError("At least one default channel is required.")
    return deduped


def parse_user_channel_pairs(pairs: list[str]) -> dict[str, int]:
    assignments: dict[str, int] = {}
    for pair in pairs:
        if ":" not in pair:
            raise ValueError(f"Invalid --user-channel format: '{pair}'. Use user:channel")
        user_id, channel_text = pair.split(":", 1)
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("User ID in --user-channel cannot be empty.")
        channel_one_based = int(channel_text.strip())
        if not 1 <= channel_one_based <= 16:
            raise ValueError("MIDI channels must be in range 1..16.")
        assignments[user_id] = channel_one_based - 1
    return assignments


async def index_handler(_: web.Request) -> web.Response:
    return web.FileResponse("web/index.html")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    server: SessionServer = request.app["session_server"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    room_id = "main"
    user_id = ""
    joined = False

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                msg_type = data.get("type")
                if msg_type == "hello":
                    room_id = str(data.get("room", "main")).strip() or "main"
                    user_id = str(data.get("user_id", "")).strip()
                    if not user_id:
                        await ws.send_json({"type": "error", "message": "Missing user_id"})
                        continue
                    room = server.get_room(room_id)
                    channel = server.assign_channel(room, user_id)
                    room.sockets.add(ws)
                    joined = True
                    await ws.send_json({"type": "joined", "room": room_id, "user_id": user_id, "channel": channel + 1})
                    await server.broadcast_state(room_id)
                elif msg_type == "metrics":
                    if not joined:
                        continue
                    values = data.get("values", {})
                    if isinstance(values, dict):
                        server.update_metrics(room_id, user_id, values)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        room = server.get_room(room_id)
        room.sockets.discard(ws)
    return ws


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shareable-link collaborative host with per-user MIDI channels.")
    parser.add_argument("--list-ports", action="store_true", help="List MIDI output ports and exit.")
    parser.add_argument("--midi-port", type=str, default="IAC Driver Bus 1", help="MIDI output port name.")
    parser.add_argument("--http-host", type=str, default="0.0.0.0", help="HTTP bind host.")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP bind port.")
    parser.add_argument(
        "--channels",
        type=str,
        default="1,2,3,4,5,6,7,8",
        help="Comma-separated default channels for auto-assignment (1..16).",
    )
    parser.add_argument(
        "--user-channel",
        action="append",
        default=[],
        help="Optional fixed assignment: --user-channel alice:1",
    )
    parser.add_argument("--cc-speed", type=int, default=1, help="CC number for speed.")
    parser.add_argument("--cc-x", type=int, default=2, help="CC number for x.")
    parser.add_argument("--cc-y", type=int, default=3, help="CC number for y.")
    parser.add_argument("--cc-click", type=int, default=4, help="CC number for click.")
    parser.add_argument("--cc-scroll", type=int, default=5, help="CC number for scroll.")
    parser.add_argument(
        "--debug-print-ms",
        type=int,
        default=0,
        help="Print latest participant mouse values every N ms; 0 disables.",
    )
    parser.add_argument(
        "--state-broadcast-ms",
        type=int,
        default=80,
        help="Broadcast browser visualization state every N ms; 0 disables.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.list_ports:
        for name in mido.get_output_names():
            print(name)
        return 0

    default_channels = parse_channel_list(args.channels)
    assignments = parse_user_channel_pairs(args.user_channel)
    mapping = MidiMapping(
        speed_cc=args.cc_speed,
        x_cc=args.cc_x,
        y_cc=args.cc_y,
        click_cc=args.cc_click,
        scroll_cc=args.cc_scroll,
    )

    try:
        server = SessionServer(
            midi_port=args.midi_port,
            default_channels=default_channels,
            explicit_assignments=assignments,
            mapping=mapping,
            debug_print_ms=args.debug_print_ms,
            state_broadcast_ms=args.state_broadcast_ms,
        )
    except IOError as exc:
        print(f"Failed to open MIDI port '{args.midi_port}': {exc}")
        print("Tip: run with --list-ports and choose one exactly.")
        return 2

    app = web.Application()
    app["session_server"] = server
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", ws_handler)

    async def start_background_tasks(app: web.Application) -> None:
        app["debug_task"] = asyncio.create_task(server.debug_print_loop())
        app["state_task"] = asyncio.create_task(server.state_broadcast_loop())

    async def cleanup_background_tasks(app: web.Application) -> None:
        for key in ("debug_task", "state_task"):
            task = app.get(key)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    print(f"Web session host: http://{args.http_host}:{args.http_port}")
    print(f"MIDI output: {args.midi_port}")
    print("Share links like:")
    print(f"  http://<HOST_IP>:{args.http_port}/?room=ensemble&user=alice")
    if args.debug_print_ms > 0:
        print(f"Debug print interval: {args.debug_print_ms} ms")
    print(f"Browser state broadcast interval: {args.state_broadcast_ms} ms")
    print("Press Ctrl+C to stop.")

    try:
        web.run_app(app, host=args.http_host, port=args.http_port, handle_signals=True)
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
