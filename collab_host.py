#!/usr/bin/env python3
"""
Collaborative host bridge.

Accepts mouse metrics from multiple clients over WebSocket and routes each
user's controls to a dedicated MIDI channel in Logic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from dataclasses import dataclass

import mido
import websockets
from websockets.server import WebSocketServerProtocol


@dataclass
class MidiMapping:
    speed_cc: int
    x_cc: int
    y_cc: int
    click_cc: int
    scroll_cc: int


class CollaborativeMidiBridge:
    def __init__(
        self,
        midi_port: str,
        explicit_assignments: dict[str, int],
        mapping: MidiMapping,
    ) -> None:
        self.midi_port_name = midi_port
        self.explicit_assignments = explicit_assignments
        self.mapping = mapping

        self.out_port = mido.open_output(midi_port)
        self.channel_by_user: dict[str, int] = {}
        self.used_channels: set[int] = set(explicit_assignments.values())
        self.lock = threading.Lock()

    def close(self) -> None:
        self.out_port.close()

    def get_or_assign_channel(self, user_id: str) -> int:
        with self.lock:
            if user_id in self.channel_by_user:
                return self.channel_by_user[user_id]

            join_index = len(self.channel_by_user)
            if user_id in self.explicit_assignments:
                channel = self.explicit_assignments[user_id]
                if channel in self.used_channels:
                    raise RuntimeError(
                        f"MIDI channel {channel + 1} is already taken; "
                        f"fix --user-channel for '{user_id}' or remove conflicting assignment."
                    )
            else:
                channel = join_index
                while channel <= 15 and channel in self.used_channels:
                    channel += 1
                if channel > 15:
                    raise RuntimeError("No available MIDI channels left for new users (max 16).")

            self.channel_by_user[user_id] = channel
            self.used_channels.add(channel)
            print(
                f"Assigned user '{user_id}' -> MIDI channel {channel + 1} "
                f"(join order {join_index + 1})"
            )
            return channel

    def send_metrics(self, user_id: str, values: dict[str, int]) -> None:
        channel = self.get_or_assign_channel(user_id)
        self.send_cc(channel, self.mapping.speed_cc, values.get("speed", 0))
        self.send_cc(channel, self.mapping.x_cc, values.get("x", 0))
        self.send_cc(channel, self.mapping.y_cc, values.get("y", 0))
        self.send_cc(channel, self.mapping.click_cc, values.get("click", 0))
        self.send_cc(channel, self.mapping.scroll_cc, values.get("scroll", 0))

    def send_cc(self, channel: int, cc: int, value: int) -> None:
        msg = mido.Message(
            "control_change",
            channel=channel,
            control=max(0, min(127, cc)),
            value=max(0, min(127, int(value))),
        )
        self.out_port.send(msg)


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


async def serve_client(ws: WebSocketServerProtocol, bridge: CollaborativeMidiBridge) -> None:
    user_id = None
    try:
        async for raw in ws:
            data = json.loads(raw)
            msg_type = data.get("type")
            if msg_type == "hello":
                user_id = str(data.get("user_id", "")).strip()
                if not user_id:
                    await ws.send(json.dumps({"type": "error", "message": "Missing user_id"}))
                    continue
                try:
                    bridge.get_or_assign_channel(user_id)
                    await ws.send(json.dumps({"type": "ok", "user_id": user_id}))
                except RuntimeError as exc:
                    await ws.send(json.dumps({"type": "error", "message": str(exc)}))
            elif msg_type == "metrics":
                payload_user = str(data.get("user_id", "")).strip()
                if not payload_user:
                    continue
                values = data.get("values", {})
                if isinstance(values, dict):
                    bridge.send_metrics(payload_user, values)
            else:
                await ws.send(json.dumps({"type": "error", "message": "Unknown message type"}))
    except websockets.ConnectionClosed:
        pass
    finally:
        if user_id is not None:
            print(f"Disconnected: {user_id}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host collaborative users and bridge to Logic MIDI channels.")
    parser.add_argument("--list-ports", action="store_true", help="List MIDI output ports and exit.")
    parser.add_argument("--midi-port", type=str, default="IAC Driver Bus 1", help="MIDI output port name.")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="WebSocket bind host.")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket bind port.")
    parser.add_argument(
        "--user-channel",
        action="append",
        default=[],
        help="Explicit assignment, repeatable: --user-channel alice:1 --user-channel bob:2",
    )
    parser.add_argument("--cc-speed", type=int, default=1, help="CC number for speed.")
    parser.add_argument("--cc-x", type=int, default=2, help="CC number for x.")
    parser.add_argument("--cc-y", type=int, default=3, help="CC number for y.")
    parser.add_argument("--cc-click", type=int, default=4, help="CC number for click.")
    parser.add_argument("--cc-scroll", type=int, default=5, help="CC number for scroll.")
    return parser.parse_args()


async def run_server(args: argparse.Namespace) -> int:
    if args.list_ports:
        for name in mido.get_output_names():
            print(name)
        return 0

    assignments = parse_user_channel_pairs(args.user_channel)
    mapping = MidiMapping(
        speed_cc=args.cc_speed,
        x_cc=args.cc_x,
        y_cc=args.cc_y,
        click_cc=args.cc_click,
        scroll_cc=args.cc_scroll,
    )

    try:
        bridge = CollaborativeMidiBridge(
            midi_port=args.midi_port,
            explicit_assignments=assignments,
            mapping=mapping,
        )
    except IOError as exc:
        print(f"Failed to open MIDI port '{args.midi_port}': {exc}")
        print("Tip: run with --list-ports and choose one exactly.")
        return 2

    print(f"Host bridge listening on ws://{args.host}:{args.port}")
    print(f"MIDI out: {args.midi_port}")
    print("Auto-assign: 1st new user -> MIDI channel 1, 2nd -> channel 2, ... (per session).")
    if assignments:
        friendly = {k: v + 1 for k, v in assignments.items()}
        print(f"Explicit user channels: {friendly}")
    print("Press Ctrl+C to stop.")

    async with websockets.serve(lambda ws: serve_client(ws, bridge), args.host, args.port):
        try:
            while True:
                await asyncio.sleep(1.0)
        finally:
            bridge.close()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_server(args))
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nStopping host bridge...")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
