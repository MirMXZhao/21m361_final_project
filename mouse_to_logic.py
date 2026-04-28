#!/usr/bin/env python3
"""
Mouse-to-Logic MIDI bridge.

Captures mouse movement/click/scroll data and sends MIDI CC events to a virtual
MIDI port (for example: IAC Driver Bus 1), which Logic can listen to.

This script intentionally keeps mappings simple and explicit so you can change
them easily as your artistic design evolves.
"""

from __future__ import annotations

import argparse
import math
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import mido
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


class MidiBridge:
    def __init__(
        self,
        port_name: str,
        channel: int,
        speed_alpha: float,
        scroll_alpha: float,
        speed_cc: int,
        x_cc: int,
        y_cc: int,
        click_cc: int,
        scroll_cc: int,
        max_speed_px_s: float,
        heartbeat_ms: int,
    ) -> None:
        self.port_name = port_name
        self.channel = channel
        self.speed_alpha = speed_alpha
        self.scroll_alpha = scroll_alpha
        self.speed_cc = speed_cc
        self.x_cc = x_cc
        self.y_cc = y_cc
        self.click_cc = click_cc
        self.scroll_cc = scroll_cc
        self.max_speed_px_s = max_speed_px_s
        self.heartbeat_ms = heartbeat_ms

        self.state = MouseState()
        self.lock = threading.Lock()
        self.running = False
        self.out_port: Optional[mido.ports.BaseOutput] = None
        self.listener: Optional[mouse.Listener] = None
        self.heartbeat_thread: Optional[threading.Thread] = None

    def open(self) -> None:
        self.out_port = mido.open_output(self.port_name)
        self.running = True
        self.listener = mouse.Listener(
            on_move=self.on_move,
            on_click=self.on_click,
            on_scroll=self.on_scroll,
        )
        self.listener.start()
        self.heartbeat_thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

    def close(self) -> None:
        self.running = False
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        if self.out_port is not None:
            self.out_port.close()
            self.out_port = None

    def send_cc(self, cc: int, value: int) -> None:
        if self.out_port is None:
            return
        msg = mido.Message("control_change", channel=self.channel, control=cc, value=value)
        self.out_port.send(msg)

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
                screen_w, screen_h = self.get_screen_size()
            except Exception:
                screen_w, screen_h = 1920, 1080

            x_value = normalize_to_midi(x, 0, screen_w - 1)
            y_value = normalize_to_midi(y, 0, screen_h - 1)
            speed_value = normalize_to_midi(self.state.smoothed_speed, 0, self.max_speed_px_s)

        self.send_cc(self.x_cc, x_value)
        self.send_cc(self.y_cc, y_value)
        self.send_cc(self.speed_cc, speed_value)

    def on_click(self, _x: float, _y: float, button: mouse.Button, pressed: bool) -> None:
        with self.lock:
            self.state.click_state = 127 if pressed else 0
            click_value = self.state.click_state

        self.send_cc(self.click_cc, click_value)

        # Optional note trigger: disabled by design so you control mapping in Logic.
        # Keeping button reference prevents linter "unused var" in strict setups.
        _ = button

    def on_scroll(self, _x: float, _y: float, _dx: float, dy: float) -> None:
        with self.lock:
            scroll_mag = abs(dy) * 20.0
            self.state.smoothed_scroll = (
                self.scroll_alpha * scroll_mag + (1.0 - self.scroll_alpha) * self.state.smoothed_scroll
            )
            scroll_value = clamp_midi(self.state.smoothed_scroll)

        self.send_cc(self.scroll_cc, scroll_value)

    def heartbeat_loop(self) -> None:
        while self.running:
            with self.lock:
                speed_value = normalize_to_midi(self.state.smoothed_speed, 0, self.max_speed_px_s)
                click_value = self.state.click_state
                scroll_value = clamp_midi(self.state.smoothed_scroll)
            self.send_cc(self.speed_cc, speed_value)
            self.send_cc(self.click_cc, click_value)
            self.send_cc(self.scroll_cc, scroll_value)
            time.sleep(self.heartbeat_ms / 1000.0)

    @staticmethod
    def get_screen_size() -> tuple[int, int]:
        from AppKit import NSScreen

        frame = NSScreen.mainScreen().frame()
        return int(frame.size.width), int(frame.size.height)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send mouse metrics to Logic via MIDI CC.")
    parser.add_argument("--list-ports", action="store_true", help="List available MIDI output ports and exit.")
    parser.add_argument("--port", type=str, default="IAC Driver Bus 1", help="MIDI output port name.")
    parser.add_argument("--channel", type=int, default=0, help="MIDI channel 0-15 (Logic channel 1 is 0 here).")
    parser.add_argument("--speed-alpha", type=float, default=0.25, help="Smoothing for speed (0..1).")
    parser.add_argument("--scroll-alpha", type=float, default=0.35, help="Smoothing for scroll (0..1).")
    parser.add_argument("--max-speed", type=float, default=2600.0, help="Speed value mapped to CC 127.")
    parser.add_argument("--heartbeat-ms", type=int, default=40, help="CC refresh interval in milliseconds.")
    parser.add_argument("--cc-speed", type=int, default=1, help="CC number for mouse speed.")
    parser.add_argument("--cc-x", type=int, default=2, help="CC number for mouse X position.")
    parser.add_argument("--cc-y", type=int, default=3, help="CC number for mouse Y position.")
    parser.add_argument("--cc-click", type=int, default=4, help="CC number for click pressed/released.")
    parser.add_argument("--cc-scroll", type=int, default=5, help="CC number for scroll intensity.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_ports:
        ports = mido.get_output_names()
        if not ports:
            print("No MIDI output ports found.")
            return 1
        for name in ports:
            print(name)
        return 0

    if args.channel < 0 or args.channel > 15:
        print("Error: --channel must be 0..15")
        return 2

    bridge = MidiBridge(
        port_name=args.port,
        channel=args.channel,
        speed_alpha=args.speed_alpha,
        scroll_alpha=args.scroll_alpha,
        speed_cc=args.cc_speed,
        x_cc=args.cc_x,
        y_cc=args.cc_y,
        click_cc=args.cc_click,
        scroll_cc=args.cc_scroll,
        max_speed_px_s=args.max_speed,
        heartbeat_ms=args.heartbeat_ms,
    )

    def stop_handler(_sig: int, _frame: object) -> None:
        print("\nStopping bridge...")
        bridge.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    try:
        bridge.open()
    except IOError as exc:
        print(f"Failed to open MIDI port '{args.port}': {exc}")
        print("Tip: run with --list-ports and choose one exactly.")
        return 3

    print(f"Streaming mouse -> MIDI on port: {args.port}")
    print(
        "CC map: "
        f"speed={args.cc_speed}, x={args.cc_x}, y={args.cc_y}, "
        f"click={args.cc_click}, scroll={args.cc_scroll}"
    )
    print("Press Ctrl+C to stop.")

    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
