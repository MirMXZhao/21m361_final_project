# Mouse to Logic MIDI Bridge

This project gives you a technical pipeline from mouse gestures to Logic Pro.
You decide the artistic mapping in Logic; this script just sends clean control data.

## What it sends

- `CC 1`: mouse speed
- `CC 2`: mouse X position
- `CC 3`: mouse Y position
- `CC 4`: click pressed state (127 down, 0 up)
- `CC 5`: scroll intensity

All CC numbers are configurable through command line flags.

## 1 Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2 Enable IAC Driver (macOS)

1. Open `Audio MIDI Setup` (Applications > Utilities).
2. Window > Show MIDI Studio.
3. Double-click `IAC Driver`.
4. Check `Device is online`.
5. Ensure at least one bus exists (e.g. `Bus 1`).

## 3 Check available MIDI ports

```bash
python mouse_to_logic.py --list-ports
```

Find the exact IAC port name (often `IAC Driver Bus 1`).

## 4 Run the bridge

```bash
python mouse_to_logic.py --port "IAC Driver Bus 1"
```

Press `Ctrl+C` to stop.

## 5 Receive data in Logic Pro

1. Create a software instrument track.
2. Set track MIDI input to the IAC bus (or `All`).
3. Insert your instrument/effect chain.
4. Use MIDI Learn / Controller Assignments in Logic to map incoming CCs.

## Useful flags

- `--cc-speed`, `--cc-x`, `--cc-y`, `--cc-click`, `--cc-scroll`
- `--speed-alpha` and `--scroll-alpha` for smoothing
- `--max-speed` to calibrate how fast motion reaches CC 127
- `--channel` for MIDI channel (0-15)
- `--heartbeat-ms` for control refresh rate

Example:

```bash
python mouse_to_logic.py \
  --port "IAC Driver Bus 1" \
  --cc-speed 16 \
  --cc-x 17 \
  --cc-y 18 \
  --cc-click 19 \
  --cc-scroll 20 \
  --max-speed 3000
```

## Notes

- This script captures global mouse events while running.
- The click signal is binary by default; if you want richer click features
  (double-click, hold duration, drag pressure proxies), we can add those next.

## Collaborative mode (separate MIDI channel per user)

Use these two scripts if multiple people should control Logic at once while
staying on separate MIDI channels.

- Host machine (running Logic): `collab_host.py`
- Participant machine (each user): `collab_client.py`

### Start host bridge (on Logic machine)

```bash
python collab_host.py --midi-port "IAC Driver Bus 1"
```

Optional fixed user-to-channel assignments:

```bash
python collab_host.py \
  --midi-port "IAC Driver Bus 1" \
  --user-channel alice:1 \
  --user-channel bob:2 \
  --user-channel carol:3
```

If not explicitly assigned, users are auto-assigned from `--channels`
(default `1,2,3,4,5,6,7,8`).

### Start participant client (on each user's machine)

```bash
python collab_client.py --host <HOST_IP> --user-id alice
```

Each client sends the same control set (`speed`, `x`, `y`, `click`, `scroll`);
the host writes those controls on that user's dedicated MIDI channel.

### Logic setup for channel-separated control

1. Create separate instrument tracks (or channel-strip targets) per performer.
2. Set each track's MIDI channel filter/input to the performer's assigned channel.
3. Use Logic controller assignments per track as desired.

## Browser link-sharing collaboration

If you want people to join with a URL (no Python client required), use the web host:

```bash
python collab_web_host.py --midi-port "IAC Driver Bus 1"
```

Share links in this format:

```text
http://<HOST_IP>:8080/?room=ensemble&user=alice
```

- `room`: collaboration room name (participants in same room see each other)
- `user`: participant ID (used for MIDI channel assignment)

When users open the link, they get:

- a live stage showing all participants' cursor positions
- a per-user MIDI channel assignment
- real-time mouse metrics sent to Logic through the host IAC/MIDI port

Optional fixed channel assignments:

```bash
python collab_web_host.py \
  --midi-port "IAC Driver Bus 1" \
  --user-channel alice:1 \
  --user-channel bob:2 \
  --user-channel carol:3
```

Notes:

- Open firewall/router ports as needed so remote users can reach your host.
- Browser users may need to click inside the stage area before interactions start.
