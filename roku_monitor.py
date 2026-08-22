#!/usr/bin/python3
"""roku-monitor-linux: make a Roku TV mirror what this PC's GPU is driving.

One rule, no exceptions: while the PC is putting a picture on its displays the
TV is one of those displays (on, and on the configured HDMI input); when the
PC stops driving its displays (idle blank, lock, suspend, logout, shutdown)
the TV is turned off.

Source of truth is the kernel, not the desktop: /sys/class/drm/<connector>/
{status,enabled,dpms} say whether the GPU is actually scanning out on each
output. That works on any Linux compositor (GNOME, KDE, Sway, X11) and needs
no D-Bus. The only optional desktop-ish dependency is python3-gi, used to hear
login1's PrepareForSleep/PrepareForShutdown so the TV goes off *before* the
network does. See README.md for the why behind each choice.

Subcommands: run | discover | status | on | off
"""

import argparse
import glob
import http.client
import ipaddress
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

APP = "roku-monitor-linux"
ECP_PORT = 8060
EX_CONFIG = 78  # sysexits.h: a config problem a restart cannot fix
SYS_DRM = "/sys/class/drm"
ON, OFF = "ON", "OFF"

log = logging.getLogger(APP)

# ROKU_INPUT value -> (ECP keypress key, app id as listed by /query/apps).
INPUTS = {
    "hdmi1": ("InputHDMI1", "tvinput.hdmi1"),
    "hdmi2": ("InputHDMI2", "tvinput.hdmi2"),
    "hdmi3": ("InputHDMI3", "tvinput.hdmi3"),
    "hdmi4": ("InputHDMI4", "tvinput.hdmi4"),
    "av1": ("InputAV1", "tvinput.cvbs"),
    "tuner": ("InputTuner", "tvinput.dtv"),
}

DEFAULTS = {
    "ROKU_TV_IP": "",
    "ROKU_TV_SERIAL": "",
    "ROKU_TV_MAC": "",
    "ROKU_INPUT": "hdmi3",
    "ROKU_CONNECTOR": "",
    "ROKU_ONLY_OFF_WHEN_ON_INPUT": "false",
    "ROKU_OFF_ON_SLEEP": "true",
    "ROKU_OFF_ON_STOP": "true",
    "ROKU_POLL_S": "1",
    "ROKU_OFF_DELAY_S": "10",
    "ROKU_ECP_TIMEOUT_S": "3",
    "ROKU_POWERON_TIMEOUT_S": "45",
    "ROKU_WOL_BROADCAST": "255.255.255.255",
    "ROKU_LOG_LEVEL": "INFO",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def parse_env_file(text):
    """KEY=VALUE lines; '#' comments, blank lines, optional 'export ', quotes."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def default_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP, "env")


def config_candidates(explicit=None):
    if explicit:
        return [explicit]
    if os.environ.get("ROKU_CONFIG"):
        return [os.environ["ROKU_CONFIG"]]
    repo_env = os.path.join(os.path.dirname(os.path.realpath(__file__)), ".env")
    return [default_config_path(), repo_env]


def input_spec(name):
    key = (name or "").strip().lower()
    if key not in INPUTS:
        raise ConfigError(f"ROKU_INPUT={name!r} is not one of {', '.join(INPUTS)}")
    return INPUTS[key]


def _as_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, values, source=None):
        self.source = source
        v = dict(DEFAULTS)
        for k, val in values.items():
            # A pristine copy of .env.example still has '<tv-ip>' style
            # placeholders; treat them as unset so we fail with a clear message
            # instead of trying to reach a host literally called "<tv-ip>".
            if k in DEFAULTS and not str(val).strip().startswith("<"):
                v[k] = str(val).strip()
        self.tv_ip = v["ROKU_TV_IP"]
        self.serial = v["ROKU_TV_SERIAL"]
        self.mac = v["ROKU_TV_MAC"]
        if self.mac:
            try:
                magic_packet(self.mac)
            except ValueError as e:
                raise ConfigError(f"ROKU_TV_MAC: {e}")
        self.input_name = v["ROKU_INPUT"]
        self.input_key, self.input_app = input_spec(self.input_name)
        self.connector = v["ROKU_CONNECTOR"]
        self.only_off_when_on_input = _as_bool(v["ROKU_ONLY_OFF_WHEN_ON_INPUT"])
        self.off_on_sleep = _as_bool(v["ROKU_OFF_ON_SLEEP"])
        self.off_on_stop = _as_bool(v["ROKU_OFF_ON_STOP"])
        try:
            self.poll_s = max(0.2, float(v["ROKU_POLL_S"]))
            self.off_delay_s = max(0.0, float(v["ROKU_OFF_DELAY_S"]))
            self.ecp_timeout = max(0.5, float(v["ROKU_ECP_TIMEOUT_S"]))
            self.poweron_timeout = max(5.0, float(v["ROKU_POWERON_TIMEOUT_S"]))
        except ValueError as e:
            raise ConfigError(f"numeric setting is not a number: {e}")
        self.wol_broadcast = v["ROKU_WOL_BROADCAST"]
        self.log_level = v["ROKU_LOG_LEVEL"].upper()

    @classmethod
    def load(cls, explicit=None):
        values, source = {}, None
        for path in config_candidates(explicit):
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    values = parse_env_file(f.read())
                source = path
                break
            if explicit:
                raise ConfigError(f"config file not found: {path}")
        # Process environment always wins (that is how a systemd drop-in or a
        # one-off `ROKU_TV_IP=... roku-monitor-linux status` overrides things).
        for k in DEFAULTS:
            if k in os.environ:
                values[k] = os.environ[k]
        return cls(values, source)

    def require_tv(self):
        if not self.tv_ip:
            raise ConfigError(
                "ROKU_TV_IP is not set. Run `roku-monitor-linux discover` and put the "
                f"TV's IP (and serial/MAC) in {default_config_path()}")


# ---------------------------------------------------------------------------
# Kernel DRM: what is the GPU driving right now?
# ---------------------------------------------------------------------------

def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def edid_vendor(edid):
    """Three-letter PNP manufacturer ID from EDID bytes 8-9 (e.g. 'RKU')."""
    if len(edid) < 10:
        return ""
    v = (edid[8] << 8) | edid[9]
    return "".join(chr(64 + ((v >> s) & 0x1F)) for s in (10, 5, 0))


def edid_name(edid):
    """Monitor name from the 0xFC descriptor block, if present."""
    for i in range(54, 126, 18):
        block = edid[i:i + 18]
        if len(block) == 18 and block[:3] == b"\x00\x00\x00" and block[3] == 0xFC:
            return block[5:18].decode("ascii", "replace").strip()
    return ""


def is_roku_edid(edid):
    return edid_vendor(edid) == "RKU" or "roku" in edid_name(edid).lower()


class Connector:
    __slots__ = ("name", "status", "enabled", "dpms")

    def __init__(self, name, status, enabled, dpms):
        self.name, self.status, self.enabled, self.dpms = name, status, enabled, dpms

    @property
    def driven(self):
        # All three are required. Under atomic modesetting a blanked output
        # reads connected/disabled/Off, and a connector that never went through
        # a modeset reads dpms=On (DRM_MODE_DPMS_ON == 0) while disabled.
        return self.status == "connected" and self.enabled == "enabled" and self.dpms == "On"

    def __repr__(self):
        return f"{self.name} {self.status}/{self.enabled}/{self.dpms}"


def list_connectors(root=SYS_DRM):
    out = []
    for path in sorted(glob.glob(os.path.join(root, "card*-*"))):
        name = os.path.basename(path)
        if "-Writeback-" in name or not os.path.exists(os.path.join(path, "status")):
            continue
        out.append(Connector(name, _read_text(os.path.join(path, "status")),
                             _read_text(os.path.join(path, "enabled")),
                             _read_text(os.path.join(path, "dpms"))))
    return out


def find_roku_connector(root=SYS_DRM, override=""):
    for path in sorted(glob.glob(os.path.join(root, "card*-*"))):
        name = os.path.basename(path)
        if "-Writeback-" in name:
            continue
        if override:
            if name.endswith("-" + override) or name == override:
                return name
            continue
        if is_roku_edid(_read_bytes(os.path.join(path, "edid"))):
            return name
    return None


class Sample:
    __slots__ = ("pc_on", "roku", "roku_name", "connectors")

    def __init__(self, pc_on, roku, roku_name, connectors):
        self.pc_on, self.roku, self.roku_name, self.connectors = pc_on, roku, roku_name, connectors


class DisplaySampler:
    """Reads sysfs once per call. Cheap (a handful of tiny files)."""

    def __init__(self, root=SYS_DRM, override=""):
        self.root = root
        self.override = override
        self.roku_name = None  # cached: the EDID vanishes while the TV has HPD low

    def sample(self):
        if self.roku_name is None:
            self.roku_name = find_roku_connector(self.root, self.override)
        connectors = list_connectors(self.root)
        pc_on = any(c.driven for c in connectors)
        roku = "unknown"
        if self.roku_name:
            match = [c for c in connectors if c.name == self.roku_name]
            if not match or match[0].status != "connected":
                roku = "absent"
            else:
                roku = "driven" if match[0].driven else "idle"
        return Sample(pc_on, roku, self.roku_name, connectors)


# ---------------------------------------------------------------------------
# Roku ECP (External Control Protocol): plain HTTP on port 8060
# ---------------------------------------------------------------------------

class EcpError(Exception):
    pass


class EcpUnreachable(EcpError):
    """Timeout / no route: TV asleep, unplugged, or wrong IP."""


class EcpRefused(EcpError):
    """TCP RST: host is up but nothing listens on 8060 (ECP disabled?)."""


class EcpHttpError(EcpError):
    def __init__(self, code, path):
        super().__init__(f"{path} -> HTTP {code}")
        self.code = code


class WrongDevice(EcpError):
    """The device at ROKU_TV_IP is not the TV we were configured for."""


def _xml(xml_text):
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise EcpError(f"unparseable ECP response: {e}") from None


def parse_device_info(xml_text):
    root = _xml(xml_text)
    return {child.tag: (child.text or "").strip() for child in root}


def parse_active_app(xml_text):
    """Returns (app_id or None, name). Shapes seen on Roku OS 15:
    <active-app><app>Roku</app></active-app>                         (home)
    <active-app><app id="tvinput.hdmi3" type="tvin">HDMI 3</app>…    (input)
    …<screensaver id="55545" type="ssvr">Roku City</screensaver>     (sibling)
    """
    root = _xml(xml_text)
    app = root.find("app")
    if app is None:
        return None, "Unknown"
    return app.get("id"), (app.text or "").strip() or "Unknown"


def parse_apps(xml_text):
    root = _xml(xml_text)
    return [(a.get("id", ""), (a.text or "").strip()) for a in root.findall("app")]


class Roku:
    def __init__(self, ip, timeout=3.0, expected_serial=""):
        self.ip = ip
        self.timeout = timeout
        self.expected_serial = expected_serial
        self._lock = threading.Lock()  # the TV's tiny HTTP server dislikes overlap

    def _req(self, method, path, timeout=None):
        url = f"http://{self.ip}:{ECP_PORT}{path}"
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        with self._lock:
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                raise EcpHttpError(e.code, path) from None
            except urllib.error.URLError as e:
                if isinstance(e.reason, ConnectionRefusedError):
                    raise EcpRefused(f"{path}: connection refused") from None
                raise EcpUnreachable(f"{path}: {e.reason}") from None
            except ConnectionRefusedError:
                raise EcpRefused(f"{path}: connection refused") from None
            except (OSError, TimeoutError, http.client.HTTPException) as e:  # socket.timeout is an OSError
                raise EcpUnreachable(f"{path}: {type(e).__name__}: {e}") from None

    def device_info(self, timeout=None):
        info = parse_device_info(self._req("GET", "/query/device-info", timeout))
        serial = info.get("serial-number", "")
        if self.expected_serial and serial and serial != self.expected_serial:
            raise WrongDevice(
                f"device at {self.ip} has serial {serial}, expected {self.expected_serial} "
                "(DHCP moved the TV? run `discover` and fix ROKU_TV_IP)")
        return info

    def power_mode(self, timeout=None):
        return self.device_info(timeout).get("power-mode", "Unknown")

    def active_app(self, timeout=None):
        return parse_active_app(self._req("GET", "/query/active-app", timeout))

    def apps(self):
        return parse_apps(self._req("GET", "/query/apps"))

    def keypress(self, key, timeout=None):
        self._req("POST", "/keypress/" + urllib.parse.quote(key), timeout)

    def launch(self, app_id, timeout=None):
        self._req("POST", "/launch/" + urllib.parse.quote(app_id), timeout)


# ---------------------------------------------------------------------------
# Wake-on-LAN (best effort; Roku TVs on Wi-Fi are reported to ignore it) and
# SSDP discovery
# ---------------------------------------------------------------------------

def magic_packet(mac):
    clean = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(clean) != 12:
        raise ValueError(f"bad MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(clean) * 16


def wol_targets(tv_ip, broadcast="255.255.255.255"):
    addrs = [broadcast]
    try:
        # Directed broadcast guessed as /24 — best effort alongside the limited
        # broadcast and a unicast copy (ARP may still know the TV).
        net = ipaddress.ip_network(f"{tv_ip}/24", strict=False)
        addrs.append(str(net.broadcast_address))
    except ValueError:
        pass
    if tv_ip:
        addrs.append(tv_ip)
    seen, out = set(), []
    for a in addrs:
        for port in (9, 7):
            if (a, port) not in seen:
                seen.add((a, port))
                out.append((a, port))
    return out


def send_wol(mac, tv_ip="", broadcast="255.255.255.255"):
    pkt = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr, port in wol_targets(tv_ip, broadcast):
            try:
                s.sendto(pkt, (addr, port))
            except OSError as e:
                log.debug("WoL to %s:%s failed: %s", addr, port, e)


SSDP_ADDR, SSDP_PORT = "239.255.255.250", 1900
MSEARCH = ("M-SEARCH * HTTP/1.1\r\n"
           f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
           'MAN: "ssdp:discover"\r\n'
           "ST: roku:ecp\r\n"
           "MX: 3\r\n\r\n").encode()


def parse_ssdp_response(text, fallback_ip=""):
    if "roku:ecp" not in text.lower():
        return None
    m = re.search(r"LOCATION:\s*http://([\d.]+):8060", text, re.I)
    return m.group(1) if m else (fallback_ip or None)


def ssdp_discover(timeout=4.0):
    """IPs of Rokus answering M-SEARCH. Sent three times: SSDP is UDP and
    single packets get lost. Rokus in deep sleep do not answer at all."""
    found = set()
    sends = [0.0, 0.6, 1.2]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 0))
        s.settimeout(0.3)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            while sends and time.monotonic() - t0 >= sends[0]:
                try:
                    s.sendto(MSEARCH, (SSDP_ADDR, SSDP_PORT))
                except OSError as e:
                    log.debug("SSDP send failed: %s", e)
                sends.pop(0)
            try:
                data, (ip, _) = s.recvfrom(4096)
            except socket.timeout:
                continue
            hit = parse_ssdp_response(data.decode("utf-8", "replace"), ip)
            if hit:
                found.add(hit)
    return sorted(found, key=lambda a: tuple(int(p) for p in a.split(".")))


# ---------------------------------------------------------------------------
# The two routines: "TV on and on our input" and "TV off"
# ---------------------------------------------------------------------------

def _sleep_unless(seconds, stale):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stale():
            return False
        time.sleep(max(0.0, min(0.25, end - time.monotonic())))
    return not stale()


def _describe_power_mode_failure(exc):
    if isinstance(exc, EcpHttpError) and exc.code == 403:
        return ("TV refused the command (HTTP 403): on the TV set Settings > System > "
                "Advanced system settings > Control by mobile apps > Network access = Default")
    if isinstance(exc, EcpRefused):
        return ("TV is up but nothing answers on port 8060 — is 'Control by mobile apps' "
                "disabled, or is this not a Roku?")
    return str(exc)


def ensure_on(roku, cfg, stale=lambda: False):
    """One attempt. Returns True when the TV is on and showing cfg.input_app."""
    try:
        mode = roku.power_mode()
    except EcpUnreachable as e:
        log.info("TV unreachable (%s) — sending PowerOn%s", e, " + WoL" if cfg.mac else "")
        mode = None
    except WrongDevice:
        raise
    except EcpError as e:
        log.warning("%s", _describe_power_mode_failure(e))
        return False

    if mode != "PowerOn":
        if cfg.mac:
            send_wol(cfg.mac, cfg.tv_ip, cfg.wol_broadcast)
        try:
            roku.keypress("PowerOn")
            log.info("sent PowerOn (TV was %s)", mode or "unreachable")
        except EcpError as e:
            log.debug("PowerOn not delivered yet: %s", e)
        deadline = time.monotonic() + cfg.poweron_timeout
        last_wol = time.monotonic()
        while True:
            if not _sleep_unless(2.0, stale):
                return False
            try:
                mode = roku.power_mode()
                if mode == "PowerOn":
                    break
                roku.keypress("PowerOn")
            except EcpUnreachable:
                if cfg.mac and time.monotonic() - last_wol >= 10:
                    send_wol(cfg.mac, cfg.tv_ip, cfg.wol_broadcast)
                    last_wol = time.monotonic()
            except WrongDevice:
                raise
            except EcpError as e:
                log.warning("%s", _describe_power_mode_failure(e))
                return False
            if time.monotonic() >= deadline:
                log.warning("TV did not report PowerOn within %.0fs (last seen: %s)",
                            cfg.poweron_timeout, mode or "unreachable")
                return False
        log.info("TV is on")
        # The TV passes through its Home screen right after power-on; an
        # input command sent immediately can lose that race.
        if not _sleep_unless(2.0, stale):
            return False

    for attempt in range(3):
        try:
            app_id, name = roku.active_app()
            if app_id == cfg.input_app:
                log.info("TV is on %s (%s)", cfg.input_name.upper(), name)
                return True
            log.info("TV is on '%s' — switching to %s", name, cfg.input_name.upper())
            if attempt == 0:
                roku.launch(cfg.input_app)
            else:
                roku.keypress(cfg.input_key)
        except EcpError as e:
            log.warning("input switch failed: %s", _describe_power_mode_failure(e))
            return False
        end = time.monotonic() + 8.0
        while time.monotonic() < end:
            if not _sleep_unless(1.0, stale):
                return False
            try:
                if roku.active_app()[0] == cfg.input_app:
                    log.info("TV switched to %s", cfg.input_name.upper())
                    return True
            except EcpError:
                pass
    log.warning("TV is on but would not switch to %s", cfg.input_name.upper())
    return False


def ensure_off(roku, cfg, stale=lambda: False, force=False, urgent=False):
    """Returns True when the TV is off (or already was). `urgent` is the
    pre-suspend/stop path: short timeouts, one try, no verification.

    Always read power-mode before sending PowerOff: the TV answers device-info
    in ~30 ms, but holds a PowerOff sent while it is already in standby for
    ~5 s — which would burn the whole urgent budget for nothing."""
    t = 1.5 if urgent else None
    mode, err = None, None
    for i in range(1 if urgent else 2):
        try:
            mode = roku.power_mode(timeout=t)
            break
        except EcpUnreachable as e:
            err = e
            if i == 0 and not urgent:
                time.sleep(1.0)
        except WrongDevice:
            raise
        except EcpError as e:
            log.warning("%s", _describe_power_mode_failure(e))
            return False
    if mode is None:
        log.info("TV unreachable (%s) — treating as already off", err)
        return True
    if mode != "PowerOn":
        log.info("TV already off (%s)", mode)
        return True
    if cfg.only_off_when_on_input and not force:
        try:
            app_id, name = roku.active_app(timeout=t)
        except EcpError as e:
            log.warning("could not read active app (%s) — leaving the TV on", e)
            return True
        if app_id != cfg.input_app:
            log.info("TV is on '%s' (%s), not %s — leaving it on (ROKU_ONLY_OFF_WHEN_ON_INPUT)",
                     name, app_id or "no id", cfg.input_name.upper())
            return True
    try:
        roku.keypress("PowerOff", timeout=t)
        log.info("sent PowerOff%s", " (urgent)" if urgent else "")
    except EcpError as e:
        log.warning("PowerOff failed: %s", _describe_power_mode_failure(e))
        return False
    if urgent:
        return True
    for _ in range(2):
        if not _sleep_unless(2.0, stale):
            return True
        try:
            if roku.power_mode() != "PowerOn":
                log.info("TV is off")
                return True
            roku.keypress("PowerOff")
        except EcpUnreachable:
            return True
        except WrongDevice:
            raise
        except EcpError as e:
            log.warning("PowerOff verify failed: %s", e)
            return False
    log.warning("TV still reports PowerOn after PowerOff")
    return False


# ---------------------------------------------------------------------------
# Reconciler: one worker thread owns all TV I/O; bounded retries, never loops
# ---------------------------------------------------------------------------

ON_LADDER = (0, 3, 8, 15, 30, 60)  # seconds between attempts; covers Wi-Fi coming back after resume


class Reconciler(threading.Thread):
    def __init__(self, roku, cfg):
        super().__init__(daemon=True, name="reconciler")
        self.roku, self.cfg = roku, cfg
        self.cv = threading.Condition()
        self.target = None
        self.urgent = False
        self.gen = 0
        self.done = threading.Event()
        self.done.set()
        self.last_off_at = None
        self.fatal = None  # exit code the main loop should use (WrongDevice)

    def request(self, target, urgent=False):
        with self.cv:
            self.target, self.urgent = target, urgent
            self.gen += 1
            self.done.clear()
            self.cv.notify()

    def wait_done(self, timeout):
        return self.done.wait(timeout)

    def run(self):
        while True:
            with self.cv:
                while self.target is None:
                    self.cv.wait()
                target, urgent, gen = self.target, self.urgent, self.gen
                self.target = None
            try:
                self._reconcile(target, urgent, lambda: self.gen != gen)
            except WrongDevice as e:
                log.critical("%s", e)
                self.fatal = EX_CONFIG
            except Exception:
                log.exception("reconcile %s failed", target)
            with self.cv:
                if self.target is None:
                    self.done.set()

    def _reconcile(self, target, urgent, stale):
        if target == OFF:
            if urgent:
                if ensure_off(self.roku, self.cfg, stale, urgent=True):
                    self.last_off_at = time.monotonic()
                return
            ok = ensure_off(self.roku, self.cfg, stale)
            if not ok and _sleep_unless(30, stale):
                ok = ensure_off(self.roku, self.cfg, stale)
            if ok:
                self.last_off_at = time.monotonic()
            return
        for delay in ON_LADDER:
            if delay and not _sleep_unless(delay, stale):
                return
            if stale():
                return
            if ensure_on(self.roku, self.cfg, stale):
                return
            log.info("retrying TV on in a moment")
        log.warning("giving up on TV on until the displays change again — "
                    "is the TV powered, on the network, with 'Fast TV start' enabled?")


# ---------------------------------------------------------------------------
# Daemon: sample, debounce, hand off; optional login1 hooks via Gio
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self, cfg, sampler, reconciler):
        self.cfg, self.sampler, self.reconciler = cfg, sampler, reconciler
        self.committed = None      # what we last asked the TV to be
        self.off_samples = 0
        self.off_deadline = None
        self.last_roku = None
        self.sleeping = False
        self.shutdown_seen = False
        self.stop_event = threading.Event()
        self.exit_code = 0
        self.inhibit_fd = None
        self.sysbus = None
        self.loop = None

    # -- sampling -----------------------------------------------------------

    def tick(self):
        try:
            self._tick()
        except Exception:
            log.exception("sampler tick failed")
        if self.reconciler.fatal is not None:
            self.exit_code = self.reconciler.fatal
            self._quit()
            return False  # removes the GLib timeout source
        return True

    def _tick(self):
        s = self.sampler.sample()
        if s.roku != self.last_roku:
            if self.last_roku is not None:
                log.info("Roku connector %s is now %s", s.roku_name, s.roku)
            self.last_roku = s.roku
        if self.sleeping:
            return
        now = time.monotonic()
        if s.pc_on:
            self.off_samples = 0
            if self.off_deadline is not None:
                log.info("displays came back — TV stays on")
                self.off_deadline = None
            if self.committed != ON:
                self._commit(ON, "displays on")
        else:
            if self.committed == OFF:
                return
            self.off_samples += 1
            if self.off_samples >= 2 and self.off_deadline is None:
                self.off_deadline = now + self.cfg.off_delay_s
                log.info("displays off — TV off in %.0fs unless they come back", self.cfg.off_delay_s)
            if self.off_deadline is not None and now >= self.off_deadline:
                self.off_deadline = None
                self._commit(OFF, "displays still off")

    def _commit(self, target, why):
        self.committed = target
        log.info("%s -> TV %s", why, "on + " + self.cfg.input_name.upper() if target == ON else "off")
        self.reconciler.request(target)

    def _initial(self):
        s = self.sampler.sample()
        self.last_roku = s.roku
        log.info("start: displays %s; Roku connector %s (%s); TV %s input %s",
                 "ON" if s.pc_on else "OFF", s.roku_name or "not found", s.roku,
                 self.cfg.tv_ip, self.cfg.input_name.upper())
        for c in s.connectors:
            log.debug("  %r", c)
        self._commit(ON if s.pc_on else OFF, "startup")

    # -- stop / sleep / shutdown --------------------------------------------

    def _urgent_off(self, why):
        # Skip only if the last thing we did was an OFF and it was moments ago
        # (shutdown fires PrepareForShutdown and then SIGTERM back to back).
        recent = (self.committed == OFF and self.reconciler.last_off_at is not None
                  and time.monotonic() - self.reconciler.last_off_at < 20)
        if recent:
            log.info("%s: TV was turned off moments ago, nothing to do", why)
            return
        self.off_deadline = None
        self.committed = OFF
        self.reconciler.request(OFF, urgent=True)
        if not self.reconciler.wait_done(3.0):
            log.info("%s: TV off still in flight after 3s, continuing", why)

    def on_term(self):
        if not self.stop_event.is_set():
            if self.shutdown_seen:
                log.info("SIGTERM during shutdown — already handled")
            elif self.cfg.off_on_stop:
                self._urgent_off("stopping")
            else:
                log.info("stopping — leaving the TV as it is (ROKU_OFF_ON_STOP=false)")
        self._quit()

    def on_int(self):
        log.info("interrupted — leaving the TV as it is")
        self._quit()

    def _quit(self):
        self.stop_event.set()
        if self.loop is not None:
            self.loop.quit()

    # -- login1 (optional) ----------------------------------------------------

    def _setup_login1(self, Gio, GLib):
        self._Gio, self._GLib = Gio, GLib
        self.sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        for member in ("PrepareForSleep", "PrepareForShutdown"):
            self.sysbus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager",
                                         member, "/org/freedesktop/login1", None,
                                         Gio.DBusSignalFlags.NONE, self._on_login1, member)
        self._take_inhibitor()
        log.info("login1 hooks active (TV goes off before suspend/shutdown)")

    def _take_inhibitor(self):
        Gio, GLib = self._Gio, self._GLib
        try:
            ret, fds = self.sysbus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager", "Inhibit",
                GLib.Variant("(ssss)", ("sleep:shutdown", APP, "Turning the Roku TV off", "delay")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 3000, None, None)
            # steal the fd so the UnixFDList's own copy does not keep the lock alive
            self.inhibit_fd = fds.steal_fds()[ret.unpack()[0]]
        except Exception as e:  # polkit denial, logind absent, ...
            log.warning("could not take a login1 delay inhibitor (%s); TV may stay on across suspend", e)
            self.inhibit_fd = None

    def _release_inhibitor(self):
        if self.inhibit_fd is not None:
            try:
                os.close(self.inhibit_fd)
            except OSError:
                pass
            self.inhibit_fd = None

    def _on_login1(self, conn, sender, path, iface, member, params, _ud):
        (start,) = params.unpack()
        try:
            if member == "PrepareForShutdown":
                if start:
                    self.shutdown_seen = True
                    self.sleeping = True
                    log.info("system is shutting down")
                    self._urgent_off("shutdown")
                return
            if start:
                self.sleeping = True
                log.info("system is going to sleep")
                if self.cfg.off_on_sleep:
                    self._urgent_off("suspend")
                else:
                    log.info("ROKU_OFF_ON_SLEEP=false — leaving the TV on")
            else:
                log.info("system resumed")
                self.sleeping = False
                self.off_samples = 0
                self.off_deadline = None
                self._take_inhibitor()
        finally:
            if start:
                self._release_inhibitor()  # this is what lets the machine actually sleep

    # -- main loops -----------------------------------------------------------

    def run(self):
        self.reconciler.start()
        self._initial()
        try:
            import gi  # noqa: F401
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except (ImportError, ValueError):
            log.warning("python3-gi not available: suspend/shutdown hooks disabled "
                        "(TV will still follow the displays). Install python3-gi to enable them.")
            return self._run_plain()
        return self._run_glib(Gio, GLib)

    def _run_plain(self):
        signal.signal(signal.SIGTERM, lambda *_: self.on_term())
        signal.signal(signal.SIGINT, lambda *_: self.on_int())
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(self.cfg.poll_s)
        return self.exit_code

    def _run_glib(self, Gio, GLib):
        self.loop = GLib.MainLoop()
        try:
            self._setup_login1(Gio, GLib)
        except Exception as e:
            log.warning("login1 hooks unavailable (%s)", e)
        GLib.timeout_add(int(self.cfg.poll_s * 1000), self.tick)

        def handler(fn):
            def cb(*_):
                fn()
                return GLib.SOURCE_REMOVE
            return cb

        try:
            from gi.repository import GLibUnix
            add = GLibUnix.signal_add
        except (ImportError, ValueError):
            add = GLib.unix_signal_add
        add(GLib.PRIORITY_HIGH, signal.SIGTERM, handler(self.on_term))
        add(GLib.PRIORITY_HIGH, signal.SIGINT, handler(self.on_int))
        self.loop.run()
        self._release_inhibitor()
        return self.exit_code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logging(level):
    logging.basicConfig(stream=sys.stderr, format="%(levelname)s %(message)s",
                        level=getattr(logging, level, logging.INFO))


def cmd_discover(args, cfg):
    print("Searching for Rokus (SSDP, ~4s)... TVs in deep sleep do not answer; turn the TV on first.")
    ips = ssdp_discover()
    if not ips:
        print("No Roku answered. Is the TV on and on the same network?")
        return 1
    rows = []
    for ip in ips:
        try:
            info = Roku(ip, timeout=3.0).device_info()
        except EcpError as e:
            rows.append((ip, "?", f"(no ECP answer: {e})", "", "", ""))
            continue
        name = info.get("user-device-name") or info.get("friendly-device-name") or "?"
        kind = "TV" if info.get("is-tv") == "true" else info.get("model-name", "player")
        rows.append((ip, name, kind, info.get("serial-number", ""),
                     info.get("wifi-mac") or info.get("ethernet-mac") or "", info.get("power-mode", "")))
    w = max(len(r[1]) for r in rows)
    print(f"\n{'#':>2}  {'IP':<15} {'Name':<{w}}  {'Kind':<22} {'Serial':<14} {'MAC':<17} Power")
    for i, r in enumerate(rows, 1):
        # '-' for empty cells keeps the last three columns parseable (install.sh reads them)
        print(f"{i:>2}  {r[0]:<15} {r[1]:<{w}}  {r[2]:<22} {r[3] or '-':<14} {r[4] or '-':<17} {r[5] or '-'}")
    print(f"\nPut the TV you use as a monitor into {default_config_path()}:")
    for r in rows:
        if r[2] == "TV":
            print(f"  # {r[1]}\n  ROKU_TV_IP={r[0]}\n  ROKU_TV_SERIAL={r[3]}\n  ROKU_TV_MAC={r[4]}")
    return 0


def cmd_status(args, cfg):
    sampler = DisplaySampler(override=cfg.connector)
    s = sampler.sample()
    print(f"config: {cfg.source or '(defaults + environment)'}")
    print(f"PC displays driven: {'YES' if s.pc_on else 'no'}")
    for c in s.connectors:
        tag = "  <- Roku" if c.name == s.roku_name else ""
        print(f"  {c.name:<18} {c.status:<13} {c.enabled:<9} {c.dpms:<4} {'driven' if c.driven else ''}{tag}")
    print(f"Roku connector: {s.roku_name or 'not found (set ROKU_CONNECTOR)'} -> {s.roku}")
    if not cfg.tv_ip:
        print("TV: ROKU_TV_IP not set")
        return 1
    roku = Roku(cfg.tv_ip, cfg.ecp_timeout, cfg.serial)
    try:
        info = roku.device_info()
        app_id, name = roku.active_app()
    except EcpError as e:
        print(f"TV {cfg.tv_ip}: {e}")
        return 1
    on_input = app_id == cfg.input_app
    print(f"TV {cfg.tv_ip}: {info.get('user-device-name') or info.get('friendly-device-name')} "
          f"({info.get('model-name')}, serial {info.get('serial-number')}), power-mode {info.get('power-mode')}, "
          f"active app '{name}' ({app_id or 'no id'}){' = our input' if on_input else ''}")
    print(f"Wanted input: {cfg.input_name.upper()} ({cfg.input_app}); "
          f"mirror rule: displays on -> TV on+{cfg.input_name.upper()}, displays off -> TV off"
          f"{' (only if on our input)' if cfg.only_off_when_on_input else ''}")
    return 0


def cmd_on(args, cfg):
    cfg.require_tv()
    roku = Roku(cfg.tv_ip, cfg.ecp_timeout, cfg.serial)
    return 0 if ensure_on(roku, cfg) else 1


def cmd_off(args, cfg):
    cfg.require_tv()
    roku = Roku(cfg.tv_ip, cfg.ecp_timeout, cfg.serial)
    return 0 if ensure_off(roku, cfg, force=args.force) else 1


def cmd_run(args, cfg):
    cfg.require_tv()
    roku = Roku(cfg.tv_ip, cfg.ecp_timeout, cfg.serial)
    sampler = DisplaySampler(override=cfg.connector)
    daemon = Daemon(cfg, sampler, Reconciler(roku, cfg))
    return daemon.run()


def main(argv=None):
    p = argparse.ArgumentParser(prog=APP, description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help=f"env file (default: {default_config_path()} or ./.env)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="follow the displays (what the systemd service runs)")
    sub.add_parser("discover", help="list Rokus on the LAN with IP/serial/MAC")
    sub.add_parser("status", help="show connector state, TV state and what the daemon would do")
    sub.add_parser("on", help="turn the TV on and switch to the configured input")
    off = sub.add_parser("off", help="turn the TV off")
    off.add_argument("--force", action="store_true", help="ignore ROKU_ONLY_OFF_WHEN_ON_INPUT")
    args = p.parse_args(argv)

    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        _setup_logging("INFO")
        log.error("%s", e)
        return EX_CONFIG
    _setup_logging("DEBUG" if args.verbose else cfg.log_level)
    if args.cmd == "run":
        log.info("%s starting (config: %s)", APP, cfg.source or "environment only")
    try:
        return {"run": cmd_run, "discover": cmd_discover, "status": cmd_status,
                "on": cmd_on, "off": cmd_off}[args.cmd](args, cfg)
    except (ConfigError, WrongDevice) as e:
        log.error("%s", e)
        return EX_CONFIG
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
