# roku-monitor: Ways of Working

A single-purpose daemon: make a Roku TV mirror what this PC's GPU is driving.
One file, zero dependencies, one rule (displays on → TV on + input; displays
off → TV off). Keep it that way. The one opt-in extension (Linux,
`ROKU_VOLUME`) is the same idea for sound: the PC's volume control *is* the
TV's volume — same reconciler thread, same bounded-retry rule, no second rule.

## Principles (inherited from the homecast projects)
- **Simplicity & minimalism.** Prefer deleting to adding. A knob needs a
  concrete reason written next to it in `.env.example` and README.
- **Code explains how; docs explain why.** Every behaviour change gets a
  "why" line in the README (what it fixes, what it trades off).
- **No personal data in tracked files.** No LAN IPs, MACs, serials, real
  names or hostnames — not in code defaults, docs, tests or examples. Use
  placeholders (`<tv-ip>`, `192.0.2.x` documentation addresses in tests).
  Real values live in `~/.config/roku-monitor/env` (0600, outside the
  repo) or the gitignored `.env`; household notes in the gitignored `pi.md`.
  Grep before every push: `git grep -nE '192\.168\.|([0-9a-f]{2}:){5}[0-9a-f]{2}'`.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `chore:`).

## Project rules
- `roku_monitor.py` is the whole program, on both platforms. Python ≥ 3.10,
  **stdlib only**; `gi` (PyGObject) is imported lazily and only inside the
  daemon's `run` path for the login1 suspend/shutdown hook, and the Windows
  backend is `ctypes` only. Everything else — and all tests — must work
  without either (CI has no `python3-gi`, and the tests run on both runners).
  The only external programs are PipeWire's own `pw-dump` and `wpctl`, used
  solely by the volume mirror behind three swappable seams; tests never spawn
  them.
- **The source of truth is the OS's own answer to "am I scanning out", never a
  desktop environment's intent.** On Linux that is
  `/sys/class/drm/<connector>/{status,enabled,dpms}` — *driven* only when all
  three say so. On Windows it is `GUID_CONSOLE_DISPLAY_STATE`. Do not swap
  either for a compositor- or session-specific signal.
- Windows decision logic lives in the pure `handle_win_event()` so it stays
  testable on Linux; the ctypes `WndProc` is only a forwarder.
- TV power decisions key off the **global** "any connector driven" edge, never
  the Roku connector alone (Roku TVs drop HDMI hot-plug minutes after power-off).
- All TV I/O goes through the single reconciler thread; retries are bounded
  ladders that end by waiting for the next edge. Never loop forever at a TV.
  Volume jobs are the same: one attempt, short timeouts, at most
  `VOLUME_ROUNDS` send/re-read rounds, then wait for the next slider change.
- The volume mirror is closed-loop: read the TV (`/query/audio-device`), send
  the delta, re-read. Never keep a guessed TV volume in a state file. At sync
  points (start, TV on, output reappears) the TV wins; in between the PC does.
- The "Roku TV" PipeWire output is session-owned (`roku-monitor.pipewire.conf`
  drop-in), never created by the daemon: sound must not depend on it running.
- This project is standalone: it does not import from or call the homecast
  stack at runtime.
- Tests: `python3 -m unittest discover -s tests -v`. Shell: shellcheck-clean,
  `set -euo pipefail`, no `sudo`.
