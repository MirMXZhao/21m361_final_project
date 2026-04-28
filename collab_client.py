#!/usr/bin/env python3
"""
Collaborative participant client.

Captures local mouse data and streams it to a host bridge over WebSocket.
The host maps each user ID to a dedicated MIDI channel for Logic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

import websockets
from pynput import mouse


def clamp_midi(value: float) -> int:
    return max(0, min(127, int(round(value))))


def normalize_to_midi(value: float, min_value: float, max_value: float) -> int:
    if max_value <= min_value:
        return 0
    ratio = (value - min_value) / (max_value - min_value)
    return clamp_midi(ratio * 127.0)


@dataclass
class MouseState:
    last_x: Optional[float] = None
    last_y: Optional[float] = None
    last_time: Optional[float] = None
    smoothed_speed: float = 0.0
    smoothed_scroll: float = 0.0
    click_state: int = 0
    x_value: int = 0
    y_value: int = 0


class MouseCapture:
    def __init__(self, speed_alpha: float, scroll_alpha: float, max_speed: float) -> None:
        self.speed_alpha = speed_alpha
        self.scroll_alpha = scroll_alpha
        self.max_speed = max_speed
        self.state = MouseState()
        self.lock = threading.Lock()
        self.listener: Optional[mouse.Listener] = None

    def start(self) -> None:
        self.listener = mouse.Listener(
            on_move=self.on_move,
            on_click=self.on_click,
            on_scroll=self.on_scroll,
        )
        self.listener.start()

    def stop(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener = None

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            speed_value = normalize_to_midi(self.state.smoothed_speed, 0, self.max_speed)
            scroll_value = clamp_midi(self.state.smoothed_scroll)
            return {
                "speed": speed_value,
                "x": self.state.x_value,
                "y": self.state.y_value,
                "click": self.state.click_state,
                "scroll": scroll_value,
            }

    def on_move(self, x: float, y: float) -> None:
        now = time.perf_counter()
        with self.lock:
            if self.state.last_x is not None and self.state.last_y is not None and self.state.last_time is not None:
                dx = x - self.state.last_x
                dy = y - self.state.last_y
                dt = max(now - self.state.last_time, 1e-6)
                instant_speed = math.sqrt(dx * dx + dy * dy) / dt
                self.state.smoothed_speed = (
                    self.speed_alpha * instant_speed + (1.0 - self.speed_alpha) * self.state.smoothed_speed
                )

            self.state.last_x = x
            self.state.last_y = y
            self.state.last_time = now

            try:
                screen_w, screen_h = get_screen_size()
            except Exception:
                screen_w, screen_h = 1920, 1080

            self.state.x_value = normalize_to_midi(x, 0, screen_w - 1)
            self.state.y_value = normalize_to_midi(y, 0, screen_h - 1)

    def on_click(self, _x: float, _y: float, _button: mouse.Button, pressed: bool) -> None:
        with self.lock:
            self.state.click_state = 127 if pressed else 0

    def on_scroll(self, _x: float, _y: float, _dx: float, dy: float) -> None:
        with self.lock:
            scroll_mag = abs(dy) * 20.0
            self.state.smoothed_scroll = (
                self.scroll_alpha * scroll_mag + (1.0 - self.scroll_alpha) * self.state.smoothed_scroll
            )


def get_screen_size() -> tuple[int, int]:
    from AppKit import NSScreen

    frame = NSScreen.mainScreen().frame()
    return int(frame.size.width), int(frame.size.height)


async def run_client(args: argparse.Namespace) -> None:
    capture = MouseCapture(
        speed_alpha=args.speed_alpha,
        scroll_alpha=args.scroll_alpha,
        max_speed=args.max_speed,
    )
    capture.start()
    uri = f"ws://{args.host}:{args.port}"
    interval_s = args.send_ms / 1000.0

    print(f"Connecting to host at {uri} as user '{args.user_id}'...")
    try:
        async with websockets.connect(uri) as ws:
            hello = {"type": "hello", "user_id": args.user_id}
            await ws.send(json.dumps(hello))
            print("Connected. Streaming mouse metrics. Press Ctrl+C to stop.")
            while True:
                payload = {
                    "type": "metrics",
                    "user_id": args.user_id,
                    "values": capture.snapshot(),
                    "timestamp_ms": int(time.time() * 1000),
                }
                await ws.send(json.dumps(payload))
                await asyncio.sleep(interval_s)
    finally:
        capture.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collaborative mouse client for Logic bridge host.")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host bridge IP/hostname.")
    parser.add_argument("--port", type=int, default=8765, help="Host bridge WebSocket port.")
    parser.add_argument("--user-id", type=str, required=True, help="Participant ID (unique in session).")
    parser.add_argument("--send-ms", type=int, default=40, help="Send interval in milliseconds.")
    parser.add_argument("--speed-alpha", type=float, default=0.25, help="Smoothing for speed (0..1).")
    parser.add_argument("--scroll-alpha", type=float, default=0.35, help="Smoothing for scroll (0..1).")
    parser.add_argument("--max-speed", type=float, default=2600.0, help="Speed value mapped to 127.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.user_id.strip():
        print("Error: --user-id cannot be empty.")
        return 2
    try:
        asyncio.run(run_client(args))
    except KeyboardInterrupt:
        print("\nStopping client...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
