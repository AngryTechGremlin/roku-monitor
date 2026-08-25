# roku-monitor

You use a Roku TV as one of your PC's monitors. This makes the TV behave like
one: **while the PC is putting a picture on its displays, the TV is on and on
your HDMI input; when the PC blanks its displays (idle, lock, suspend, logout,
shutdown) the TV turns off; when they come back, the TV comes back on the
right input.** No exceptions, no "smart" guessing.

One Python file, no dependencies, on **Linux and Windows**. It talks to the TV
over Roku's local HTTP control protocol (ECP, port 8060), and on the PC side it
asks the operating system itself whether it is putting out a picture — the
kernel's DRM state on Linux, the console display state on Windows — rather than
trusting a desktop environment's idea of "idle". So it works under GNOME, KDE,
Sway and X11, and on Windows 10/11.

Optional, on both platforms: **the PC's volume control drives the TV's own
volume** — full range, no remote on the desk, nothing attenuated twice. On
Linux the keys *and* the slider (`./install.sh --volume`); on Windows the
volume keys (`.\install.ps1 -Volume`). See *the volume* sections below.

## Requirements

**PC — Linux**
- A KMS graphics driver (anything modern: amdgpu, i915, nouveau, …). Tested on
  Ubuntu 26.04 / GNOME 50 (Wayland) / amdgpu.
- `python3` (3.10+). Optional but recommended: `python3-gi` (`sudo apt install
  python3-gi gir1.2-glib-2.0`) — stock on Ubuntu desktop — so the TV can be
  turned off *before* suspend/shutdown takes the network down. Without it the
  TV still follows your displays; it just may stay on across a suspend.
- `systemd --user` (any systemd desktop distro).
- For the optional volume mirror: PipeWire ≥ 1.0 with WirePlumber ≥ 0.5 and
  their CLI tools `pw-dump` and `wpctl` (packages `pipewire-bin`,
  `wireplumber` — stock on Ubuntu desktop; tested with 1.6.2 / 0.5.13).

**PC — Windows**
- Windows 10 or 11. Tested on Windows 11 Pro.
- Python 3.10+ — `winget install -e --id Python.Python.3.12`. The
  "python.exe" that ships in `WindowsApps` is only a Store shortcut; the
  installer detects and rejects it.
- Nothing else: the Windows backend is `ctypes` against the OS, and the daemon
  runs from a per-user logon task, so no administrator rights are needed.

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

### Linux

```bash
git clone https://github.com/AngryTechGremlin/roku-monitor ~/roku-monitor
cd ~/roku-monitor
./install.sh
```

The installer (no `sudo`) checks the prerequisites, symlinks the script to
`~/.local/bin/roku-monitor`, installs the user unit, creates
`~/.config/roku-monitor/env` from `.env.example`, runs a network
discovery so you can pick the TV (writing its IP, serial and MAC for you),
asks which input the PC is on, and enables the service. Turn the TV on before
running it — a Roku in deep sleep does not answer discovery.

Add `--volume` (now or later, same command) to make the PC's volume control
drive the TV's volume: it writes a small PipeWire drop-in
(`~/.config/pipewire/pipewire.conf.d/90-roku-monitor.conf`) that adds a
**"Roku TV"** output, restarts PipeWire once (sound pauses for a second),
sets `ROKU_VOLUME=true` and restarts the service. The TV must be on (or in
Fast-start standby) so the installer can see which HDMI sink feeds it.

### Windows

```powershell
winget install -e --id Python.Python.3.12   # if you do not have real Python yet
# download or copy this repo, then from its folder:
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Same flow: it finds a real Python, creates `%APPDATA%\roku-monitor\env`, lets
you pick the TV and input, then registers a **Scheduled Task** that starts the
daemon with `pythonw.exe` (no console window) at every logon and restarts it if
it ever dies. Add `-Volume` (now or later, same command) to make the volume
keys drive the TV's own volume while the Roku TV is the default output — no
files are added; it sets `ROKU_VOLUME=true` and restarts the daemon (brief TV
off/on blink). Logs go to `%LOCALAPPDATA%\roku-monitor\roku-monitor.log` —
there is no journal to write to.

The task must run in your interactive session: the daemon listens for
display-power messages on a hidden window, which only exists once you are
logged in. That is also why starting it over SSH does not work — the CLI
commands (`status`, `on`, `off`) do.

`.\uninstall.ps1 [-Purge]` reverses everything.

### If discovery finds nothing

Discovery uses multicast, and some networks (or access points) never deliver it
to a particular device — a TV can be perfectly reachable and still stay
invisible to a search. Point at it directly instead:

```bash
roku-monitor discover --ip <tv-ip>
```

That reads the TV and prints the exact `ROKU_TV_IP` / `ROKU_TV_SERIAL` /
`ROKU_TV_MAC` lines to paste into your config. Both installers offer the same
option. Note that on such a network Wake-on-LAN broadcasts will not reach the
TV either, so **Fast TV start really is required** there.

Then: `roku-monitor status` (what the daemon sees), `journalctl --user
-u roku-monitor -f` (what it does). `./uninstall.sh [--purge]` reverses
everything (`--purge` also deletes the config).

## Configuration

Linux: `~/.config/roku-monitor/env` (mode 0600). Windows:
`%APPDATA%\roku-monitor\env`. Read by both the CLI and the service, so they
never disagree. Every key is documented in [`.env.example`](.env.example). The
ones that matter:

| Key | Default | Why |
|---|---|---|
| `ROKU_TV_IP` | — | The TV. `discover` prints it. |
| `ROKU_TV_SERIAL` | — | Safety: refuse to command a *different* Roku if DHCP hands the TV's old IP to another one (easy to have four Rokus in a house). |
| `ROKU_TV_MAC` | — | Best-effort Wake-on-LAN if the TV went to deep sleep. Not a substitute for Fast TV start. |
| `ROKU_INPUT` | `hdmi3` | Which TV input the PC is on (`hdmi1..4`, `av1`, `tuner`). |
| `ROKU_CONNECTOR` | auto | *Linux only.* The PC's output that feeds the TV. Found automatically from the TV's EDID (vendor `RKU`); override with e.g. `HDMI-A-1` if you must. |
| `ROKU_ONLY_OFF_WHEN_ON_INPUT` | `false` | Opt-in courtesy for a *shared* TV: don't turn it off if it is showing something other than the PC's input when the PC blanks. Off by default — the TV is your monitor. |
| `ROKU_OFF_ON_SLEEP` / `ROKU_OFF_ON_STOP` | `true` | Turn the TV off before suspend / when the service stops (logout, `systemctl --user stop`). A `restart` is a stop+start, so expect a brief TV off/on blink; set `ROKU_OFF_ON_STOP=false` if that bothers you. |
| `ROKU_OFF_DELAY_S` | `10` | Grace after the displays go dark; cancelled if they light up again. Absorbs "blank, then you touch the mouse". |
| `ROKU_POWERON_TIMEOUT_S` | `45` | How long to wait for the TV to report it is on (a cold boot is ~30 s). |
| `ROKU_VOLUME` | `false` | The PC's volume control drives the TV's own volume. Linux: the "Roku TV" output's slider/mute mirror to the TV and back (`install.sh --volume` sets it; it changes the audio graph). Windows: the volume keys go to the TV while the Roku TV is the default output (`install.ps1 -Volume`; it installs a keyboard hook). |

## Commands

| Command | What it does |
|---|---|
| `roku-monitor run` | The daemon (what the service runs). |
| `roku-monitor discover` | SSDP scan; lists Rokus with name, kind, serial, MAC, power state. |
| `roku-monitor discover --ip <tv-ip>` | Read one TV directly, for networks that block multicast. |
| `roku-monitor status` | What the daemon sees: the display state, which monitor is the Roku, the TV's power mode + active input, the TV's volume, and the audio side (Linux: which sink feeds the TV and whether the "Roku TV" output is the default; Windows: which output the volume keys currently belong to). |
| `roku-monitor on` | One-shot "TV on + input" (exit 1 if it could not). |
| `roku-monitor off [--force]` | One-shot TV off (`--force` ignores the shared-TV guard). |

On Windows, run these as `python roku_monitor.py <command>` from the repo
folder (or add a shortcut of your own).

## How it works

Everything below the platform line is shared: the same debounce, the same
"look before you command" reconciler, the same Roku ECP calls.

### Linux: the kernel, per output

For each connector, `/sys/class/drm/<connector>/` exposes three files:

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

### Linux: the volume (optional)

**The problem.** An HDMI audio sink has no hardware volume (`route.hw-volume
= false`; ALSA offers only an on/off switch), so GNOME's slider and volume keys
on it only scale the samples in software. The TV keeps whatever volume it had,
and you end up either pinning the TV high and attenuating on the PC (losing
range and resolution, and blasting anything else the TV plays) or keeping a
remote on the desk. The fix is to make the PC's volume control *be* the TV's
volume.

**The PipeWire side: a "Roku TV" output that does not attenuate.**
`install.sh --volume` adds a drop-in ([`roku-monitor.pipewire.conf`](roku-monitor.pipewire.conf))
with a virtual sink named `roku-tv` ("Roku TV" in Settings → Sound) and a
`module-loopback` relay from its monitor ports to the real HDMI sink. With
PipeWire's default `monitor.channel-volumes = false` a sink's monitor carries
the raw, pre-volume, pre-mute signal, so moving the "Roku TV" slider changes
two numbers and not a single sample; the relay is volume-locked and the HDMI
sink is kept at 100 %. The daemon makes "Roku TV" the default output (only
ever replacing the raw HDMI sink, never a headset you picked), so the keys and
the slider act on it. The objects belong to the PipeWire session, not the
daemon: sound keeps flowing when the daemon is stopped or restarting, and
WirePlumber re-links the relay itself when the HDMI sink disappears and comes
back — which it does on *every* screen blank. (A side benefit: the default
output no longer flaps on blank, so `gsd-media-keys: Unable to get default
sink` stops appearing in the journal.) The relay is pinned with
`node.dont-fallback` + `node.linger` — never `node.dont-reconnect`, which
WirePlumber 0.5 answers by destroying the stream when its target vanishes —
and without the pin it would fall back to the default output, i.e. into
itself. Cost: one extra quantum of latency (~20 ms) and one PipeWire restart at
install.

**The TV side: a closed loop.** ECP can only press `VolumeUp` / `VolumeDown`
/ `VolumeMute`, but the TV reports its state at `/query/audio-device`
(`<global><volume>15</volume><muted>false</muted>`, undocumented but present
on Roku OS 15). So every slider change is: read the TV, send the difference as
key presses, re-read, correct — at most three rounds, then give up until the
next change. Measured on the TV this was built against: one press is one step
on the TV's 0–100 scale, 20 presses back to back are all honoured (~40 ms
each, so 0→100 takes about 4 s and the TV draws its own volume bar meanwhile),
`VolumeUp` unmutes, `VolumeDown` keeps a muted TV muted, and the TV mutes
itself at 0; a model that steps differently is corrected by the re-read. The
slider's percent maps 1:1 to the TV's number (PipeWire stores linear gain;
GNOME and `wpctl` show its cube root, and so does the daemon), so both
on-screen displays agree. Mute is the TV's mute; while the PC is muted only
the mute is mirrored and the volume follows at unmute — stepping down first
and unmuting last when it goes down, so the TV never comes back loud and ramps
down audibly.

**Who wins.** In between, the PC drives the TV. At sync points — daemon start,
every successful "TV on" (i.e. every unblank), the "Roku TV" output
(re)appearing — the TV's value is copied into the slider instead. That never
blasts a quiet room at login and bounds any drift from the remote; there is no
periodic polling of the TV (the README's "never re-asserts on a schedule" rule
holds for volume too). Volume is only ever sent while the last "TV on" succeeded
*and* the TV is showing the PC's input — a shared TV on Live TV is left alone —
and a volume key the TV merely *accepts* (HTTP 202, standby) stops the job.
All volume work runs on the same reconciler thread as power, power first, with
short timeouts (1 s) and one attempt per change, so it can never hold up the
pre-suspend "TV off".

**What you see.** GNOME's OSD as usual; the TV's own volume bar while it
catches up (key repeat is coalesced to the newest value, so holding the key
ramps smoothly); `journalctl --user -u roku-monitor` lines like `TV volume 15
-> 21`. Degraded mode worth knowing about: with the drop-in installed but the
service stopped, the slider is a slider that moves nothing and the TV stays
where it was (`roku-monitor status` shows both halves). To drop only the
volume piece: remove `~/.config/pipewire/pipewire.conf.d/90-roku-monitor.conf`,
`systemctl --user restart pipewire`, and set `ROKU_VOLUME=false`;
`./uninstall.sh` does the same as part of removing everything.

### Windows: the volume (optional)

Windows has the same underlying problem as Linux — the Roku's HDMI endpoint
reports no hardware volume (`IAudioEndpointVolume::QueryHardwareSupport` says
mute only), so the slider and keys just scale samples while the TV keeps its
own level — but none of the Linux machinery: there is no in-box virtual output
or pre-volume loopback to build a "Roku TV" device from, and third-party
virtual cables are kernel drivers, which this project will not depend on. So
Windows gets the other honest mechanism: **take the volume keys themselves.**

With `ROKU_VOLUME=true` the daemon installs a low-level keyboard hook (on its
own thread, so nothing the daemon does can stall it into Windows' silent-removal
timeout). The hook looks at exactly three keys — `VolumeUp`, `VolumeDown`,
`VolumeMute` — and nothing else, records nothing, and acts only **while the
Roku TV is Windows' default output** (checked at the daemon's poll interval,
1 s by default): each press is swallowed, so Windows neither changes its own volume nor
shows its flyout, and is forwarded to the TV in order over the same closed-loop
ECP path as Linux. The TV's own volume bar is the feedback; holding the key
ramps at the TV's own rate (~25 steps/s, a small queue bounds the lag). The
keys keep their remote semantics: Up unmutes, Down keeps a muted TV muted,
Mute toggles. Pick your headphones as the output and the keys are Windows'
again within a poll tick.

While the TV endpoint is the default, its Windows volume belongs to the
daemon: it is set to 100 % and unmuted when it becomes the default, and any
Windows-side change that leaks in afterwards — a mute or a lowered slider from
the lock screen, an elevated window, or the tray — springs back within a poll
tick, with one log line saying so. (The tray slider is therefore not a control
here; mirroring it *to the TV* instead is a possible follow-up.) Keys pressed
while an **elevated** window is focused or on the **lock screen** bypass the
hook by Windows security design and briefly behave like plain Windows volume
keys; volume presses while the TV is off or showing another input are dropped
with one log line, same as Linux. Volume traffic uses 1 s timeouts and yields
to power work at every step, so at worst one in-flight request (~1 s) sits in
front of the ~2 s pre-suspend "TV off" budget. To turn the mirror off:
`.\install.ps1 -Volume:$false` (or set `ROKU_VOLUME=false` in
`%APPDATA%\roku-monitor\env` and restart the task).

### Windows: the console display state

Windows has no per-monitor power state — the console blanks as a whole — and no
call that answers "are the displays on right now". What it has is a
notification: register for `GUID_CONSOLE_DISPLAY_STATE` and the OS tells you
whenever the console display turns **off (0)**, **on (1)** or **dimmed (2)**,
starting with the current value the moment you register. That is the same
question the DRM files answer on Linux, asked of the OS instead of the kernel,
and it is deliberately *not* the per-session variant, which would follow a
virtual RDP display rather than the panel your TV is plugged into. Dimmed
counts as on — the GPU is still scanning out.

The daemon owns a hidden top-level window to receive it (a message-only window
would be tidier but does not receive broadcast messages like `WM_ENDSESSION`),
and also registers for suspend/resume notifications, which Modern Standby
machines otherwise never deliver. `status` additionally lists the active
monitors via `QueryDisplayConfig` and flags the Roku by its EDID vendor — for
your information only, never for the power decision.

Timing note: locking Windows does **not** blank the screen immediately; the
hidden "console lock display off timeout" (1 minute by default) does. If you
want the TV to follow a lock faster, lower it:
`powercfg /setacvalueindex SCHEME_CURRENT SUB_VIDEO VIDEOCONLOCK 15` then
`powercfg /setactive SCHEME_CURRENT`.

### Shared: desired state, not commands

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

**Suspend and shutdown.** On Linux, with `python3-gi`, the daemon holds a
login1 *delay* inhibitor and turns the TV off the moment `PrepareForSleep` /
`PrepareForShutdown` fires — before NetworkManager takes the Wi-Fi down — then
releases the lock so the machine can sleep. On resume the displays light up
and the normal "driven" edge turns the TV back on (the retry ladder covers
the seconds until Wi-Fi is back). Logout stops the service (`PartOf=
graphical-session.target`), which turns the TV off; the next login turns it
on. Reboot is treated like shutdown: the TV goes off and comes back at login.

Windows is the same shape with a tighter budget: `PBT_APMSUSPEND` and
`WM_ENDSESSION` (shutdown *and* sign-out) trigger the same immediate off, but
Windows allows only about two seconds and offers no way to hold the system
back, so the off is a single fast request with no verification. If it loses
that race — the Wi-Fi went down first — the TV simply stays on and is
corrected the next time the displays come back.

## Troubleshooting

- **TV does not come on after being off a while** → Fast TV start is off (see
  *Requirements*); the TV has left the network. Check with `curl -m 3
  http://<tv-ip>:8060/query/device-info`. Wake it with the remote once and fix
  the setting. `ROKU_TV_MAC` enables a best-effort Wake-on-LAN, and
  `ROKU_WOL_BROADCAST=192.168.x.255` helps on hosts with many interfaces.
- **`HTTP 403`** in the log → *Control by mobile apps → Network access* is
  *Limited*. Set it to *Default*.
- **TV turns on but lands on the wrong input** → `roku-monitor status`
  shows what the TV reports; `curl http://<tv-ip>:8060/query/apps` lists the
  `tvinput.*` ids your model uses; match `ROKU_INPUT`.
- **"device at … has serial …, expected …"** → DHCP moved the TV. Run
  `discover` and update `ROKU_TV_IP` (then set a reservation).
- **TV blinks off/on when I restart the service** → expected (stop turns it
  off, start turns it on). `ROKU_OFF_ON_STOP=false` if you prefer.
- **Service is inactive after login** → `systemctl --user status
  roku-monitor`. Exit code 78 means the config is missing or still has
  placeholders. Did you run `install.sh` from inside a desktop session?
- **My windows jumped to the other monitor** → the TV dropped hot-plug while
  off; see *How it works* and the CEC tip.
- **Roku connector "not found"** (Linux) → the TV is off or on another input
  (its EDID is not readable then) — normal; it is cached once seen. If it is
  never found, set `ROKU_CONNECTOR`.
- **Volume keys do nothing to the TV** (Linux) → `roku-monitor status`:
  is the "Roku TV" output present and the default (`wpctl status`), is
  `ROKU_VOLUME=true`, is the service running (`journalctl --user -u
  roku-monitor -f` shows `TV volume 15 -> 21` per change)? A slider moved
  while the TV is off or on another input is deliberately not sent; it is
  re-synced from the TV at the next power-on.
- **No sound after `install.sh --volume`** → the relay names an HDMI sink
  that is not there: `pw-link -l | grep -A2 roku-tv-relay.playback` shows no
  link. Moved the TV to another HDMI port, or a profile change renamed the
  sink? `roku-monitor status` names the sink it finds and says when the
  relay points elsewhere; re-run `./install.sh --volume` with the TV on (it
  rewrites the drop-in) or edit `target.object` in
  `~/.config/pipewire/pipewire.conf.d/90-roku-monitor.conf` and
  `systemctl --user restart pipewire`.
- **TV volume overshoots or oscillates** → your model steps more than one per
  key; the log says `TV volume settled at N, wanted M`. The loop corrects
  itself within a step; please report the model.
- **Volume keys do nothing to the TV** (Windows) → `python roku_monitor.py
  status`: is the *default output* the Roku TV (that is the gate), is
  `ROKU_VOLUME=true`, is the task running? The log shows `volume keys drive
  the TV ...` at start and `TV volume 15 -> 21 (6 keys)` per burst. An
  elevated window in focus or the lock screen bypasses the hook (Windows
  security design) — the flyout appearing again is the tell.
- **Nothing happens on Windows** → check the log first
  (`%LOCALAPPDATA%\roku-monitor\roku-monitor.log`) and
  `Get-ScheduledTaskInfo -TaskName roku-monitor`. The daemon only sees display
  events inside your interactive session, so a copy started over SSH or in
  session 0 will never react. Remember the lock-screen delay above.
- **The TV can be pinged from one PC but not another** → some access points
  stop delivering broadcast frames to a device, which breaks ARP (and so all
  traffic) from machines that have not already learned its address, plus
  discovery and Wake-on-LAN. Symptom: `Test-Connection` says
  `DestinationHostUnreachable` and `Get-NetNeighbor` shows `Incomplete`, while
  another machine talks to the TV fine. Rejoining the TV's Wi-Fi usually fixes
  it; a persistent neighbor entry works around it:
  `netsh interface ipv4 add neighbors "Wi-Fi" <tv-ip> <tv-mac-with-dashes> store=persistent`
  (pair it with a DHCP reservation, or it goes stale).

Handy checks:

```bash
# Linux
for c in /sys/class/drm/card*-*; do [ -e $c/status ] && echo "$(basename $c) $(cat $c/status)/$(cat $c/enabled)/$(cat $c/dpms)"; done
journalctl --user -u roku-monitor -f
systemd-inhibit --list                     # shows the daemon's delay lock
curl -s http://<tv-ip>:8060/query/active-app
curl -s -X POST http://<tv-ip>:8060/keypress/PowerOff
curl -s http://<tv-ip>:8060/query/audio-device | grep -A2 '<global>'   # the TV's volume
wpctl status; pw-link -l | grep -A2 roku-tv          # the "Roku TV" output and its relay
pw-dump -m -N | grep -n '"channelVolumes"'           # slider moves as the daemon sees them
# GNOME: Super+L blanks within ~1s — the fastest real test. Or force it:
busctl --user set-property org.gnome.Mutter.DisplayConfig /org/gnome/Mutter/DisplayConfig \
  org.gnome.Mutter.DisplayConfig PowerSaveMode i 3; sleep 20; busctl --user set-property \
  org.gnome.Mutter.DisplayConfig /org/gnome/Mutter/DisplayConfig org.gnome.Mutter.DisplayConfig PowerSaveMode i 0
# (pair the two: moving the mouse does not undo a forced blank on GNOME)
```

```powershell
# Windows
Get-Content "$env:LOCALAPPDATA\roku-monitor\roku-monitor.log" -Tail 20 -Wait -Encoding UTF8
Get-ScheduledTaskInfo -TaskName roku-monitor
python roku_monitor.py status
Invoke-WebRequest "http://<tv-ip>:8060/query/active-app" -UseBasicParsing | Select-Object -Expand Content
powercfg /q SCHEME_CURRENT SUB_VIDEO VIDEOCONLOCK   # lock-to-blank delay
(Invoke-RestMethod "http://<tv-ip>:8060/query/audio-device").'audio-device'.global   # the TV's volume
Select-String 'TV volume|default output|keyboard hook' "$env:LOCALAPPDATA\roku-monitor\roku-monitor.log" | Select-Object -Last 10
```

## Development

`python3 -m unittest discover -s tests -v` — no TV, no desktop, no `gi`, no
PipeWire (the volume mirror's three subprocess seams are swapped for fakes and
its parser is fed captured `pw-dump` shapes), and the Windows decision logic is
covered on Linux too (its ctypes layer is a thin forwarder around a pure
function). CI runs the suite on Ubuntu *and* Windows
runners, plus `py_compile`, `--help`, shellcheck and a PowerShell parse check.
Conventional Commits. Never commit LAN addresses, serials or MACs — tests use
`192.0.2.x` documentation addresses, docs use `<tv-ip>`.

Ideas not built yet: a GNOME-only tweak to ignore the 15-second screen wake
GNOME does for notifications while locked; turning the TV off when only *its*
output is disabled in display settings; re-discovering the TV by serial when
its IP moves; a slow TV poll so the slider follows the remote live (today it
catches up at the next power-on); shipping the drop-in with
`monitor.channel-volumes = true` and flipping it at runtime so the slider
still attenuates in software while the daemon is down.

## License

MIT.
