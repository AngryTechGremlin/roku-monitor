# roku-monitor-linux: Ways of Working

A single-purpose daemon: make a Roku TV mirror what this PC's GPU is driving.
One file, zero dependencies, one rule (displays on → TV on + input; displays
off → TV off). Keep it that way.

## Principles (inherited from the homecast projects)
- **Simplicity & minimalism.** Prefer deleting to adding. A knob needs a
  concrete reason written next to it in `.env.example` and README.
- **Code explains how; docs explain why.** Every behaviour change gets a
  "why" line in the README (what it fixes, what it trades off).
- **No personal data in tracked files.** No LAN IPs, MACs, serials, real
  names or hostnames — not in code defaults, docs, tests or examples. Use
  placeholders (`<tv-ip>`, `192.0.2.x` documentation addresses in tests).
  Real values live in `~/.config/roku-monitor-linux/env` (0600, outside the
  repo) or the gitignored `.env`; household notes in the gitignored `pi.md`.
  Grep before every push: `git grep -nE '192\.168\.|([0-9a-f]{2}:){5}[0-9a-f]{2}'`.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `chore:`).

## Project rules
- `roku_monitor.py` is the whole program. Python ≥ 3.10, **stdlib only**;
  `gi` (PyGObject) is imported lazily and only inside the daemon's `run` path
  for the login1 suspend/shutdown hook. Everything else — and all tests —
  must work without it (CI has no `python3-gi`).
- The source of truth for "is the PC driving its displays" is
  `/sys/class/drm/<connector>/{status,enabled,dpms}`; a connector is *driven*
  only when all three say so. Do not swap this for a desktop-specific signal.
- TV power decisions key off the **global** "any connector driven" edge, never
  the Roku connector alone (Roku TVs drop HDMI hot-plug minutes after power-off).
- All TV I/O goes through the single reconciler thread; retries are bounded
  ladders that end by waiting for the next edge. Never loop forever at a TV.
- This project is standalone: it does not import from or call the homecast
  stack at runtime.
- Tests: `python3 -m unittest discover -s tests -v`. Shell: shellcheck-clean,
  `set -euo pipefail`, no `sudo`.
