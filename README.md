# roku-monitor-linux

You use a Roku TV as one of your PC's monitors. This makes the TV behave like
one: **while the PC is putting a picture on its displays, the TV is on and on
your HDMI input; when the PC blanks its displays (idle, lock, suspend, logout,
shutdown) the TV turns off; when they come back, the TV comes back on the
right input.** No exceptions, no "smart" guessing.

One Python file, no dependencies, runs as a `systemd --user` service. It talks
to the TV over Roku's local HTTP control protocol (ECP, port 8060) and reads
the PC side straight from the kernel (`/sys/class/drm`), so it works on any
Linux desktop — GNOME, KDE, Sway, X11 — not just the one it was written on.

## Requirements

**PC**
- Linux with a KMS graphics driver (anything modern: amdgpu, i915, nouveau,
  …). Tested on Ubuntu 26.04 / GNOME 50 (Wayland) / amdgpu.
- `python3` (3.10+). Optional but recommended: `python3-gi` (`sudo apt install
  python3-gi gir1.2-glib-2.0`) — stock on Ubuntu desktop — so the TV can be
  turned off *before* suspend/shutdown takes the network down. Without it the
  TV still follows your displays; it just may stay on across a suspend.
- `systemd --user` (any systemd desktop distro).

**TV** — set these once, on the TV itself:
- **Settings → System → Advanced system settings → Control by mobile apps →
  Network access = Default.** Roku OS 14.1+ defaults to *Limited*, which
  rejects every control command with HTTP 403.
- **Settings → System → Power → Fast TV start = On.** Without it the TV drops
  off the network ~10–15 minutes after it is turned off and nothing on the LAN
  can wake it (Wake-on-LAN over Wi-Fi is unreliable on Roku TVs). With it, the
  TV answers in warm standby and powers on in a second or two.
- **Settings → System → Power → Power on = HDMI 3** (your input). Then even
  the TV's own remote lands on the PC; the daemon's input switch becomes a
  belt-and-braces step.
- **Settings → System → Power → Auto power savings: untick "Turn off after 4
  hours".** That timer counts *remote-control* activity, which a PC user never
  generates, and would switch the TV off underneath you.
- Optional: tick one of the **HDMI-CEC** boxes (Settings → System → Control
  other devices). The PC's GPU has no CEC, so it is harmless, and several
  people report it keeps the TV's HDMI hot-plug asserted in standby — which
  stops your desktop from reshuffling windows when the TV goes off (see
  *How it works*).
- Give the TV a DHCP reservation on your router.

## Install

```bash
git clone https://github.com/AngryTechGremlin/roku-monitor-linux ~/roku-monitor-linux
cd ~/roku-monitor-linux
./install.sh
```

The installer (no `sudo`) checks the prerequisites, symlinks the script to
`~/.local/bin/roku-monitor-linux`, installs the user unit, creates
`~/.config/roku-monitor-linux/env` from `.env.example`, runs a network
discovery so you can pick the TV (writing its IP, serial and MAC for you),
asks which input the PC is on, and enables the service. Turn the TV on before
running it — a Roku in deep sleep does not answer discovery.

Then: `roku-monitor-linux status` (what the daemon sees), `journalctl --user
-u roku-monitor-linux -f` (what it does). `./uninstall.sh [--purge]` reverses
everything (`--purge` also deletes the config).

## Configuration

`~/.config/roku-monitor-linux/env`, mode 0600, read by both the CLI and the
service. Every key is documented in [`.env.example`](.env.example). The ones
that matter:

| Key | Default | Why |
|---|---|---|
| `ROKU_TV_IP` | — | The TV. `discover` prints it. |
| `ROKU_TV_SERIAL` | — | Safety: refuse to command a *different* Roku if DHCP hands the TV's old IP to another one (easy to have four Rokus in a house). |
| `ROKU_TV_MAC` | — | Best-effort Wake-on-LAN if the TV went to deep sleep. Not a substitute for Fast TV start. |
| `ROKU_INPUT` | `hdmi3` | Which TV input the PC is on (`hdmi1..4`, `av1`, `tuner`). |
| `ROKU_CONNECTOR` | auto | The PC's output that feeds the TV. Found automatically from the TV's EDID (vendor `RKU`); override with e.g. `HDMI-A-1` if you must. |
| `ROKU_ONLY_OFF_WHEN_ON_INPUT` | `false` | Opt-in courtesy for a *shared* TV: don't turn it off if it is showing something other than the PC's input when the PC blanks. Off by default — the TV is your monitor. |
| `ROKU_OFF_ON_SLEEP` / `ROKU_OFF_ON_STOP` | `true` | Turn the TV off before suspend / when the service stops (logout, `systemctl --user stop`). A `restart` is a stop+start, so expect a brief TV off/on blink; set `ROKU_OFF_ON_STOP=false` if that bothers you. |
| `ROKU_OFF_DELAY_S` | `10` | Grace after the displays go dark; cancelled if they light up again. Absorbs "blank, then you touch the mouse". |
| `ROKU_POWERON_TIMEOUT_S` | `45` | How long to wait for the TV to report it is on (a cold boot is ~30 s). |

## Commands

| Command | What it does |
|---|---|
| `roku-monitor-linux run` | The daemon (what the service runs). |
| `roku-monitor-linux discover` | SSDP scan; lists Rokus with name, kind, serial, MAC, power state. |
| `roku-monitor-linux status` | Every DRM connector with status/enabled/dpms, which one is the Roku, whether the PC is driving anything, and the TV's power mode + active input. |
| `roku-monitor-linux on` | One-shot "TV on + input" (exit 1 if it could not). |
| `roku-monitor-linux off [--force]` | One-shot TV off (`--force` ignores the shared-TV guard). |

## How it works

**The source of truth is the kernel, per output.** For each connector,
`/sys/class/drm/<connector>/` exposes three files:

| file | meaning |
|---|---|
| `status` | `connected` / `disconnected` — is a sink asserting hot-plug on that cable? |
| `enabled` | is the compositor using this output in its layout right now? |
| `dpms` | `On` / `Off` — is the GPU actually scanning out a picture on it? |

A connector is *driven* only when all three say yes. That is literally "the
PC is sending HDMI to this display", with no desktop in between. (Why all
three: when GNOME blanks, it disables the outputs, so `enabled` flips to
`disabled` while `dpms` also goes `Off`; and a connector that has never been
set up reports `dpms=On` by default, so `dpms` alone would lie.) The daemon
reads these files once a second — there is no kernel notification for DPMS
changes, and the read costs well under a millisecond.

The states you will see:

| situation | Roku connector reads |
|---|---|
| normal use | `connected / enabled / On` |
| desktop blanked the screens | `connected / disabled / Off` |
| output disabled in display settings | `connected / disabled / Off` (other outputs still `enabled / On`) |
| TV off long enough to drop hot-plug, or cable out | `disconnected / disabled / Off` |

**Why the power decision uses *all* connectors, not just the TV's.** A Roku
TV pulses HDMI hot-plug for about a second when it powers off, and a TV that
has gone to deep sleep (Fast TV start off) is reported to drop it entirely
after 10–15 minutes and raise it again on power-on. While hot-plug is down the
TV's connector reads `disconnected` whatever the PC wants — so "the TV's
output lit up" can never be the wake trigger (the TV would have to be on
first). The daemon therefore keys on *any output being driven*: when your
other monitor wakes, the TV is told to wake; when everything goes dark, the
TV goes dark. The TV's own connector is reported in `status` and the logs for
your information only.

Side effect you should know about: if the TV does drop hot-plug, your desktop
sees a monitor unplugged and may move windows onto the remaining screen (and
not move them back). That is your compositor reacting to the TV, not this
daemon. With Fast TV start on, the TV this was developed against kept
hot-plug asserted through every standby period observed (20 minutes of
sampling, standby stretches of up to 9 minutes) — so in the recommended
configuration the layout stays put; the CEC tip under *Requirements* is the
known mitigation if yours behaves differently.

**Desired state, not commands.** An edge in "displays driven?" becomes a
desired TV state (on or off) handed to one worker thread that owns all talk
with the TV. The worker first *looks* (`/query/device-info`,
`/query/active-app`) and only sends what changes something: `PowerOn`,
then `launch/tvinput.hdmi3` (falling back to the `InputHDMI3` key), verifying
via `active-app`; or `PowerOff`. Duplicate edges are therefore free, a quick
blank/unblank is harmless (the grace timer is cancelled and in-flight work
notices it is stale), and slow things — waiting up to 45 s for a cold boot,
Wi-Fi not yet up after resume — never block the sampler. Retries are bounded
(attempts at 0/3/8/15/30/60 s), then the daemon logs once and waits for the
next edge; it never loops at a TV that will not answer, and it never
re-asserts on a schedule (if you turn the TV off by hand while the PC is
awake, it stays off until the displays change state).

**Suspend and shutdown.** With `python3-gi` the daemon holds a login1 *delay*
inhibitor and turns the TV off the moment `PrepareForSleep` /
`PrepareForShutdown` fires — before NetworkManager takes the Wi-Fi down — then
releases the lock so the machine can sleep. On resume the displays light up
and the normal "driven" edge turns the TV back on (the retry ladder covers
the seconds until Wi-Fi is back). Logout stops the service (`PartOf=
graphical-session.target`), which turns the TV off; the next login turns it
on. Reboot is treated like shutdown: the TV goes off and comes back at login.

## Troubleshooting

- **TV does not come on after being off a while** → Fast TV start is off (see
  *Requirements*); the TV has left the network. Check with `curl -m 3
  http://<tv-ip>:8060/query/device-info`. Wake it with the remote once and fix
  the setting. `ROKU_TV_MAC` enables a best-effort Wake-on-LAN, and
  `ROKU_WOL_BROADCAST=192.168.x.255` helps on hosts with many interfaces.
- **`HTTP 403`** in the log → *Control by mobile apps → Network access* is
  *Limited*. Set it to *Default*.
- **TV turns on but lands on the wrong input** → `roku-monitor-linux status`
  shows what the TV reports; `curl http://<tv-ip>:8060/query/apps` lists the
  `tvinput.*` ids your model uses; match `ROKU_INPUT`.
- **"device at … has serial …, expected …"** → DHCP moved the TV. Run
  `discover` and update `ROKU_TV_IP` (then set a reservation).
- **TV blinks off/on when I restart the service** → expected (stop turns it
  off, start turns it on). `ROKU_OFF_ON_STOP=false` if you prefer.
- **Service is inactive after login** → `systemctl --user status
  roku-monitor-linux`. Exit code 78 means the config is missing or still has
  placeholders. Did you run `install.sh` from inside a desktop session?
- **Windows jumped to my other monitor** → the TV dropped hot-plug while off;
  see *How it works* and the CEC tip.
- **Roku connector "not found"** → the TV is off or on another input (its EDID
  is not readable then) — normal; it is cached once seen. If it is never
  found, set `ROKU_CONNECTOR`.

Handy checks:

```bash
for c in /sys/class/drm/card*-*; do [ -e $c/status ] && echo "$(basename $c) $(cat $c/status)/$(cat $c/enabled)/$(cat $c/dpms)"; done
journalctl --user -u roku-monitor-linux -f
systemd-inhibit --list                     # shows the daemon's delay lock
curl -s http://<tv-ip>:8060/query/active-app
curl -s -X POST http://<tv-ip>:8060/keypress/PowerOff
# GNOME: Super+L blanks within ~1s — the fastest real test. Or force it:
busctl --user set-property org.gnome.Mutter.DisplayConfig /org/gnome/Mutter/DisplayConfig \
  org.gnome.Mutter.DisplayConfig PowerSaveMode i 3; sleep 20; busctl --user set-property \
  org.gnome.Mutter.DisplayConfig /org/gnome/Mutter/DisplayConfig org.gnome.Mutter.DisplayConfig PowerSaveMode i 0
# (pair the two: moving the mouse does not undo a forced blank on GNOME)
```

## Development

`python3 -m unittest discover -s tests -v` (no TV, no desktop, no `gi`
needed). CI runs that plus `py_compile`, `--help`, and shellcheck on every
push. Conventional Commits. Never commit LAN addresses, serials or MACs —
tests use `192.0.2.x` documentation addresses, docs use `<tv-ip>`.

Ideas not built yet: a GNOME-only tweak to ignore the 15-second screen
wake GNOME does for notifications while locked; turning the TV off when only
*its* output is disabled in display settings; re-discovering the TV by serial
when its IP moves.

## License

MIT.
