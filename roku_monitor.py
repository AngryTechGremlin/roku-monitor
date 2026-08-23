#!/usr/bin/python3
"""roku-monitor: make a Roku TV mirror what this PC's GPU is driving.

One rule, no exceptions: while the PC is putting a picture on its displays the
TV is one of those displays (on, and on the configured HDMI input); when the
PC stops driving its displays (idle blank, lock, suspend, logout, shutdown)
the TV is turned off.

The source of truth is the OS's own answer to "am I scanning out a picture",
never a desktop environment's intent:

  Linux   /sys/class/drm/<connector>/{status,enabled,dpms} — per output, from
          the kernel; works under any compositor (GNOME, KDE, Sway, X11).
          python3-gi is optional and only used to hear login1's
          PrepareForSleep/PrepareForShutdown so the TV goes off *before* the
          network does.
  Windows GUID_CONSOLE_DISPLAY_STATE power-setting notifications — the
          console (physical) display state, delivered to a hidden window,
          plus the suspend/session-end messages. ctypes only, no pip deps.

Optional, Linux only (ROKU_VOLUME=true): the PC's volume control is the TV's
volume. A "Roku TV" PipeWire output (roku-monitor.pipewire.conf) carries the
sound to the HDMI sink unattenuated; the daemon mirrors its slider to the TV
with VolumeUp/VolumeDown/VolumeMute, verified against the TV's own reading
(/query/audio-device). Only `pw-dump` and `wpctl` are used (subprocesses).

See README.md for the why behind each choice.

Subcommands: run | discover | status | on | off
"""

import argparse
import glob
import http.client
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

APP = "roku-monitor"
ECP_PORT = 8060
EX_CONFIG = 78  # sysexits.h: a config problem a restart cannot fix
SYS_DRM = "/sys/class/drm"
ON, OFF = "ON", "OFF"
IS_WIN = sys.platform == "win32"
# How long the pre-suspend/stop off may take. Windows allows only ~2 s at
# PBT_APMSUSPEND and offers no delay-inhibitor equivalent to logind's.
URGENT_OFF_BUDGET = 1.8 if IS_WIN else 3.0

# Volume mirror (Linux): the PipeWire node names created by
# roku-monitor.pipewire.conf, and the bounds of one slider -> TV job.
PW_SINK = "roku-tv"                         # the virtual "Roku TV" output GNOME controls
PW_RELAY_CAPTURE = "roku-tv-relay.capture"  # loopback: PW_SINK monitor -> ...
PW_RELAY_PLAYBACK = "roku-tv-relay.playback"  # ... -> the real HDMI sink (target.object)
SYNC = "SYNC"              # reconciler job: PC slider := TV volume ("TV wins")
VOLUME_ROUNDS = 3          # per job: send keys, re-read the TV, correct; then give up
VOLUME_KEY_GAP_S = 0.0     # measured: 20 keypresses back to back, none dropped (~40 ms each)
VOLUME_SETTLE_S = 0.1      # the TV reports the new value at once; a small margin anyway
VOLUME_TIMEOUT_S = 1.0     # per request, so a volume job never eats the urgent-off budget
PW_DUMP_LADDER = (0, 3, 8, 15, 30, 60)  # seconds between pw-dump respawns (PipeWire restart)

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
    "ROKU_VOLUME": "false",
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
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP, "env")


def default_log_path():
    """Windows has no journal, so `run` also writes a rotating file here."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.path.join(base, APP, APP + ".log")


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
        self.volume = _as_bool(v["ROKU_VOLUME"])  # Linux only; needs install.sh --volume

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
        # one-off `ROKU_TV_IP=... roku-monitor status` overrides things).
        for k in DEFAULTS:
            if k in os.environ:
                values[k] = os.environ[k]
        return cls(values, source)

    def require_tv(self):
        if not self.tv_ip:
            raise ConfigError(
                "ROKU_TV_IP is not set. Run `roku-monitor discover` and put the "
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
# Windows: the console display state is the OS's own "am I lighting the
# monitors" answer. There is no per-output power state on Windows (the console
# blanks as a whole) and no API to read it on demand — you register for
# notifications and the current value arrives immediately. Everything here is
# ctypes; nothing in this section is imported or touched on Linux.
# ---------------------------------------------------------------------------

# MONITOR_DISPLAY_STATE values carried by GUID_CONSOLE_DISPLAY_STATE.
DISPLAY_OFF, DISPLAY_ON, DISPLAY_DIMMED = 0, 1, 2

# Event kinds handed to handle_win_event(). Keeping them as plain strings lets
# the decision logic be tested on any platform.
EV_DISPLAY = "display"      # value: MONITOR_DISPLAY_STATE
EV_SUSPEND = "suspend"
EV_RESUME = "resume"
EV_ENDSESSION = "endsession"  # value: True if logging off rather than shutting down
EV_CLOSE = "close"


def pnp_vendor(code):
    """Decode an EDID/PNP manufacturer id (3 x 5-bit letters) to e.g. 'RKU'.

    Windows' DISPLAYCONFIG edidManufactureId holds the same 16 bits as the
    EDID, but byte-swapped on little-endian, so callers try both orders.
    """
    return "".join(chr(64 + ((code >> s) & 0x1F)) for s in (10, 5, 0))


def is_roku_target(edid_code, friendly_name):
    if "roku" in (friendly_name or "").lower():
        return True
    swapped = ((edid_code & 0xFF) << 8) | (edid_code >> 8)
    return "RKU" in (pnp_vendor(edid_code), pnp_vendor(swapped))


def handle_win_event(daemon, kind, value=None):
    """Pure decision logic for the Windows event loop (no ctypes, testable).

    Mirrors the login1 handlers on Linux: the display state drives the normal
    debounce path, while suspend/session-end take the urgent off path.
    """
    cfg = daemon.cfg
    if kind == EV_DISPLAY:
        on = value != DISPLAY_OFF  # dimmed still means the GPU is scanning out
        daemon.sampler.set_display_on(on)
        if not daemon.sleeping:
            daemon.tick()
        return True
    if kind == EV_SUSPEND:
        daemon.sleeping = True
        log.info("system is going to sleep")
        if cfg.off_on_sleep:
            daemon._urgent_off("suspend")
        else:
            log.info("ROKU_OFF_ON_SLEEP=false - leaving the TV on")
        return True
    if kind == EV_RESUME:
        log.info("system resumed")
        daemon.sleeping = False
        daemon.off_samples = 0
        daemon.off_deadline = None
        return True
    if kind == EV_ENDSESSION:
        daemon.shutdown_seen = True
        daemon.sleeping = True
        log.info("session ending (%s)", "sign-out" if value else "shutdown")
        if cfg.off_on_stop:
            daemon._urgent_off("sign-out" if value else "shutdown")
        return True
    if kind == EV_CLOSE:
        daemon.on_term()
        return True
    return False


class WindowsDisplaySampler:
    """Same Sample shape as DisplaySampler so Daemon._tick is unchanged.

    pc_on comes from the console display state pushed in by the event loop.
    The monitor list is cosmetic (status/logging only) and is allowed to fail:
    an SSH session has no interactive desktop to query.
    """

    def __init__(self, override=""):
        self.override = override
        self.roku_name = None
        # Nothing can tell us the state before the first notification, but
        # registration delivers the current value within milliseconds, and a
        # daemon starting at logon is starting on a lit screen.
        self.display_on = True

    def set_display_on(self, on):
        if on != self.display_on:
            log.info("console display is now %s", "on" if on else "off")
        self.display_on = on

    def sample(self):
        connectors = []
        roku = "unknown"
        try:
            connectors = list_windows_targets()
        except Exception as e:  # no desktop (session 0), or an API that failed
            log.debug("display topology unavailable: %s", e)
        for c in connectors:
            if self.roku_name is None and c.is_roku:
                self.roku_name = c.name
            if c.name == self.roku_name:
                roku = "driven" if self.display_on else "idle"
        if self.roku_name and roku == "unknown" and connectors:
            roku = "absent"  # it was in the topology before and is not now
        return Sample(self.display_on, roku, self.roku_name, connectors)


class WinTarget:
    """One active display path, printed by `status`."""
    __slots__ = ("name", "is_roku")

    def __init__(self, name, is_roku):
        self.name, self.is_roku = name, is_roku

    def __repr__(self):
        return f"{self.name}{' (Roku)' if self.is_roku else ''}"


def list_windows_targets():
    """Active display targets via QueryDisplayConfig. Raises on failure."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    QDC_ONLY_ACTIVE_PATHS = 0x00000002
    ERROR_INSUFFICIENT_BUFFER = 122
    DEVICE_INFO_GET_TARGET_NAME = 2

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class PATH_SOURCE_INFO(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", wintypes.UINT),
                    ("modeInfoIdx", wintypes.UINT), ("statusFlags", wintypes.UINT)]

    class PATH_TARGET_INFO(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", wintypes.UINT), ("modeInfoIdx", wintypes.UINT),
                    ("outputTechnology", wintypes.UINT), ("rotation", wintypes.UINT),
                    ("scaling", wintypes.UINT), ("refreshNumerator", wintypes.UINT),
                    ("refreshDenominator", wintypes.UINT), ("scanLineOrdering", wintypes.UINT),
                    ("targetAvailable", wintypes.BOOL), ("statusFlags", wintypes.UINT)]

    class PATH_INFO(ctypes.Structure):
        _fields_ = [("sourceInfo", PATH_SOURCE_INFO), ("targetInfo", PATH_TARGET_INFO),
                    ("flags", wintypes.UINT)]

    class MODE_INFO(ctypes.Structure):  # opaque here; only its size matters
        _fields_ = [("infoType", wintypes.UINT), ("id", wintypes.UINT),
                    ("adapterId", LUID), ("blob", ctypes.c_byte * 48)]

    class DEVICE_INFO_HEADER(ctypes.Structure):
        _fields_ = [("type", wintypes.UINT), ("size", wintypes.UINT),
                    ("adapterId", LUID), ("id", wintypes.UINT)]

    class TARGET_DEVICE_NAME(ctypes.Structure):
        _fields_ = [("header", DEVICE_INFO_HEADER), ("flags", wintypes.UINT),
                    ("outputTechnology", wintypes.UINT),
                    ("edidManufactureId", wintypes.USHORT),
                    ("edidProductCodeId", wintypes.USHORT),
                    ("connectorInstance", wintypes.UINT),
                    ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
                    ("monitorDevicePath", wintypes.WCHAR * 128)]

    for _ in range(3):  # the topology can change between sizing and reading
        n_paths, n_modes = wintypes.UINT(), wintypes.UINT()
        rc = user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS,
                                                ctypes.byref(n_paths), ctypes.byref(n_modes))
        if rc:
            raise OSError(f"GetDisplayConfigBufferSizes failed: {rc}")
        paths = (PATH_INFO * n_paths.value)()
        modes = (MODE_INFO * n_modes.value)()
        rc = user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS,
                                       ctypes.byref(n_paths), paths,
                                       ctypes.byref(n_modes), modes, None)
        if rc == ERROR_INSUFFICIENT_BUFFER:
            continue
        if rc:
            raise OSError(f"QueryDisplayConfig failed: {rc}")
        out = []
        for i in range(n_paths.value):
            info = TARGET_DEVICE_NAME()
            info.header.type = DEVICE_INFO_GET_TARGET_NAME
            info.header.size = ctypes.sizeof(TARGET_DEVICE_NAME)
            info.header.adapterId = paths[i].targetInfo.adapterId
            info.header.id = paths[i].targetInfo.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(info)):
                continue
            name = info.monitorFriendlyDeviceName or f"target {info.header.id}"
            out.append(WinTarget(name, is_roku_target(info.edidManufactureId, name)))
        return out
    raise OSError("QueryDisplayConfig kept resizing")


def run_windows_loop(daemon):
    """Hidden-window message pump: display state, suspend/resume, session end.

    A message-only window would be simpler but does not receive broadcast
    messages (WM_ENDSESSION, PBT_APMSUSPEND), so this creates a normal
    top-level window and never shows it.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WM_DESTROY, WM_CLOSE, WM_TIMER = 0x0002, 0x0010, 0x0113
    WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016
    WM_POWERBROADCAST = 0x0218
    PBT_APMSUSPEND, PBT_APMRESUMEAUTOMATIC = 0x0004, 0x0012
    PBT_POWERSETTINGCHANGE = 0x8013
    ENDSESSION_LOGOFF = 0x80000000
    DEVICE_NOTIFY_WINDOW_HANDLE = 0
    ERROR_ALREADY_EXISTS = 183

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    # {6FE69556-704A-47A0-8F24-C28D936FDA47}
    GUID_CONSOLE_DISPLAY_STATE = GUID(0x6FE69556, 0x704A, 0x47A0,
                                      (ctypes.c_ubyte * 8)(0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))

    class POWERBROADCAST_SETTING(ctypes.Structure):
        _fields_ = [("PowerSetting", GUID), ("DataLength", wintypes.DWORD),
                    ("Data", ctypes.c_ubyte * 4)]

    LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
    WPARAM, LPARAM = ctypes.c_size_t, ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

    # Declare every signature we use. Without this ctypes assumes C int for
    # arguments, and a 64-bit lparam (a pointer, during WM_NCCREATE) overflows
    # — which fails window creation before the daemon ever starts.
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
    user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
    user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    try:
        user32.RegisterSuspendResumeNotification.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        user32.RegisterSuspendResumeNotification.restype = wintypes.HANDLE
    except AttributeError:
        pass

    # Only one daemon per user: systemd gave us this for free on Linux.
    mutex = kernel32.CreateMutexW(None, False, "Local\\" + APP)
    if mutex and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log.info("another %s is already running - exiting", APP)
        return 0

    def on_message(hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:
            daemon.tick()
            return 0
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_POWERSETTINGCHANGE:
                setting = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                if bytes(setting.PowerSetting) == bytes(GUID_CONSOLE_DISPLAY_STATE):
                    handle_win_event(daemon, EV_DISPLAY, setting.Data[0])
            elif wparam == PBT_APMSUSPEND:
                handle_win_event(daemon, EV_SUSPEND)
            elif wparam == PBT_APMRESUMEAUTOMATIC:
                handle_win_event(daemon, EV_RESUME)
            return 1
        if msg == WM_QUERYENDSESSION:
            return 1  # never block the shutdown; do the work in WM_ENDSESSION
        if msg == WM_ENDSESSION:
            if wparam:
                # The session can end as soon as we return, so this must be
                # synchronous. The block reason explains any brief delay.
                user32.ShutdownBlockReasonCreate(hwnd, "Turning the Roku TV off")
                try:
                    handle_win_event(daemon, EV_ENDSESSION, bool(lparam & ENDSESSION_LOGOFF))
                finally:
                    user32.ShutdownBlockReasonDestroy(hwnd)
            return 0
        if msg == WM_CLOSE:
            handle_win_event(daemon, EV_CLOSE)
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def safe_proc(hwnd, msg, wparam, lparam):
        try:
            return on_message(hwnd, msg, wparam, lparam)
        except Exception:  # a raising WndProc would take the process down
            log.exception("window message %s failed", msg)
            return 0

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    proc = WNDPROC(safe_proc)          # must outlive the window: keep a reference
    run_windows_loop._proc = proc
    wc = WNDCLASS()
    wc.lpfnWndProc = proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = APP + "-window"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")

    # WS_OVERLAPPEDWINDOW, never shown: a message-only window would not get
    # the broadcast messages (WM_ENDSESSION, PBT_APMSUSPEND) we depend on.
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, APP, 0x00CF0000,
                                  0, 0, 0, 0, None, None, wc.hInstance, None)
    if not hwnd:
        raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")

    if not user32.RegisterPowerSettingNotification(
            wintypes.HANDLE(hwnd), ctypes.byref(GUID_CONSOLE_DISPLAY_STATE),
            DEVICE_NOTIFY_WINDOW_HANDLE):
        log.warning("RegisterPowerSettingNotification failed (%s): display changes "
                    "will not be seen", ctypes.get_last_error())
    # Without this, Modern Standby machines never deliver suspend/resume.
    try:
        user32.RegisterSuspendResumeNotification(wintypes.HANDLE(hwnd), DEVICE_NOTIFY_WINDOW_HANDLE)
    except AttributeError:
        log.debug("RegisterSuspendResumeNotification unavailable")
    user32.SetTimer(hwnd, 1, int(daemon.cfg.poll_s * 1000), None)
    log.info("watching the console display state (hidden window)")

    def on_ctrl(_type):
        daemon.on_int()
        user32.PostMessageW(hwnd, WM_DESTROY, 0, 0)
        return True

    HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(on_ctrl)
    run_windows_loop._ctrl = HANDLER
    kernel32.SetConsoleCtrlHandler(HANDLER, True)

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
        if daemon.stop_event.is_set():
            break
    return daemon.exit_code


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


def parse_audio_device(xml_text):
    """(volume 0-100, muted) from /query/audio-device's <global> block.

    Undocumented but present on Roku OS 15 TVs; the only way to *read* the
    TV's volume (ECP can only press VolumeUp/VolumeDown/VolumeMute).
    """
    g = _xml(xml_text).find("global")
    if g is None or g.find("volume") is None:
        raise EcpError("audio-device: no <global><volume> - this Roku does not report its volume")
    try:
        vol = int((g.findtext("volume") or "").strip())
    except ValueError:
        raise EcpError("audio-device: volume is not a number") from None
    return max(0, min(100, vol)), (g.findtext("muted") or "").strip().lower() == "true"


class Roku:
    def __init__(self, ip, timeout=3.0, expected_serial=""):
        self.ip = ip
        self.timeout = timeout
        self.expected_serial = expected_serial
        self._lock = threading.Lock()  # the TV's tiny HTTP server dislikes overlap

    def _req(self, method, path, timeout=None, status=False):
        url = f"http://{self.ip}:{ECP_PORT}{path}"
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        with self._lock:
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    body = r.read().decode("utf-8", "replace")
                    return (body, r.status) if status else body
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

    def audio_device(self, timeout=None):
        return parse_audio_device(self._req("GET", "/query/audio-device", timeout))

    def keypress(self, key, timeout=None):
        """Returns the HTTP status: 200 = pressed; 202 = accepted but not acted on
        (observed for volume keys while the TV is in standby)."""
        return self._req("POST", "/keypress/" + urllib.parse.quote(key), timeout, status=True)[1]

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
        return ("TV is up but nothing answers on port 8060 - is 'Control by mobile apps' "
                "disabled, or is this not a Roku?")
    return str(exc)


def ensure_on(roku, cfg, stale=lambda: False):
    """One attempt. Returns True when the TV is on and showing cfg.input_app."""
    try:
        mode = roku.power_mode()
    except EcpUnreachable as e:
        log.info("TV unreachable (%s) - sending PowerOn%s", e, " + WoL" if cfg.mac else "")
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
            log.info("TV is on '%s' - switching to %s", name, cfg.input_name.upper())
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
        log.info("TV unreachable (%s) - treating as already off", err)
        return True
    if mode != "PowerOn":
        log.info("TV already off (%s)", mode)
        return True
    if cfg.only_off_when_on_input and not force:
        try:
            app_id, name = roku.active_app(timeout=t)
        except EcpError as e:
            log.warning("could not read active app (%s) - leaving the TV on", e)
            return True
        if app_id != cfg.input_app:
            log.info("TV is on '%s' (%s), not %s - leaving it on (ROKU_ONLY_OFF_WHEN_ON_INPUT)",
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
# Volume mirror (Linux, optional): the PC's volume control is the TV's volume
#
# The HDMI sink has no hardware volume, so a slider on it only scales samples
# in software. roku-monitor.pipewire.conf therefore adds a virtual "Roku TV"
# output (PW_SINK) whose monitor is pre-volume and pre-mute, relayed unchanged
# to the HDMI sink. Its volume and mute are just numbers: this section watches
# them through one `pw-dump -m` child and the reconciler thread mirrors them to
# the TV with VolumeUp/VolumeDown/VolumeMute, verified against the TV's own
# /query/audio-device. At sync points (start, TV on, the output (re)appearing)
# the TV's value is copied into the slider instead ("TV wins"). Nothing here is
# imported or touched on Windows; the tests swap the three subprocess seams.
# ---------------------------------------------------------------------------

def pw_percent(props):
    """(percent, muted) from a node's Props, the way GNOME shows it.

    PipeWire stores linear gain; GNOME and wpctl show its cube root (50% is
    0.125), so this number matches the on-screen slider and the TV's 0-100.
    """
    vols = props.get("channelVolumes") or []
    try:
        linear = max(float(v) for v in vols) if vols else float(props.get("volume", 1.0))
    except (TypeError, ValueError):
        linear = 1.0
    pct = int(round(100 * max(0.0, linear) ** (1.0 / 3.0)))
    return max(0, min(100, pct)), bool(props.get("mute", False))


def plan_volume_keys(tv_vol, tv_muted, pct, muted, step=1.0):
    """ECP keys that move the TV from (tv_vol, tv_muted) towards (pct, muted).

    Measured on a Roku TV (OS 15): one key is one step on 0-100, VolumeUp
    unmutes, VolumeDown leaves a muted TV muted, VolumeMute toggles, and the TV
    mutes itself at 0. So while the PC is muted only the mute is mirrored (the
    volume follows at unmute); an unmute that goes up just presses Up, one that
    goes down steps down first and unmutes last. `step` is how far one key was seen to move this TV; the caller re-reads and
    calls again, so a model that differs still settles within VOLUME_ROUNDS.
    """
    if muted:
        return [] if tv_muted else ["VolumeMute"]
    if pct == 0 and tv_vol == 0:
        return []  # silent either way
    delta = pct - tv_vol
    keys = []
    if delta:
        n = max(1, min(100, int(round(abs(delta) / max(step, 1.0)))))
        keys = ["VolumeUp" if delta > 0 else "VolumeDown"] * n
    if tv_muted and delta <= 0 and pct > 0:
        # Unmute last: VolumeDown keeps the mute, so the TV never comes on loud
        # and ramps down audibly. (pct == 0: reaching 0 leaves it silent anyway.)
        keys.append("VolumeMute")
    return keys


class PwNode:
    __slots__ = ("id", "props", "level")

    def __init__(self, node_id, props, level):
        self.id, self.props, self.level = node_id, props, level  # level: (pct, muted) or None

    @property
    def name(self):
        return str(self.props.get("node.name", ""))


def find_roku_alsa_sink(nodes):
    """The ALSA sink that feeds the Roku: the TV's EDID name reaches ALSA as the
    ELD monitor name, which PipeWire exposes as node.nick / alsa.name."""
    for node in nodes.values():
        p = node.props
        if p.get("media.class") == "Audio/Sink" and p.get("device.api") == "alsa":
            if "roku" in f"{p.get('node.nick', '')} {p.get('alsa.name', '')}".lower():
                return node
    return None


class PwGraph:
    """What `pw-dump` tells us about the audio graph, fed one JSON array at a
    time (the initial dump, then one array per change batch).

    apply() returns events: ("sink", present) for PW_SINK, ("volume", pct,
    muted) when its slider moved, ("hdmi", id, pct, muted) when the relay's
    target (the real HDMI sink) appeared or changed level, ("default", name)
    when the default output changed.
    """

    def __init__(self):
        self.nodes = {}          # id -> PwNode, audio nodes and streams only
        self.default = None      # node.name of default.audio.sink; "" if none; None if unknown
        self.relay_target = None  # node.name the relay plays into (from its target.object)

    def by_name(self, name):
        for node in self.nodes.values():
            if node.name == name:
                return node
        return None

    @property
    def sink(self):
        return self.by_name(PW_SINK)

    @property
    def tv_sink(self):
        if self.relay_target:
            return self.by_name(self.relay_target)
        return find_roku_alsa_sink(self.nodes)

    def apply(self, objs):
        events, changed = [], set()
        for o in objs:
            if not isinstance(o, dict):
                continue
            oid, info, otype = o.get("id"), o.get("info"), str(o.get("type", ""))
            if "info" in o and info is None:  # removal: {"id": N, "info": null}
                node = self.nodes.pop(oid, None)
                if node is not None and node.name == PW_SINK:
                    events.append(("sink", False))
                continue
            if otype == "PipeWire:Interface:Metadata":  # has top-level props/metadata, no info
                # Change batches carry only the changed entries: merge, never replace.
                if (o.get("props") or {}).get("metadata.name") == "default":
                    for entry in o.get("metadata") or []:
                        if entry.get("key") == "default.audio.sink":
                            value = entry.get("value")
                            name = str(value.get("name", "")) if isinstance(value, dict) else ""
                            if name != self.default:
                                self.default = name
                                events.append(("default", name))
                continue
            if otype != "PipeWire:Interface:Node" or not isinstance(info, dict):
                continue
            props = info.get("props") or {}
            if "Audio" not in str(props.get("media.class", "")):
                continue
            params = (info.get("params") or {}).get("Props") or []
            level = pw_percent(params[0]) if params and isinstance(params[0], dict) else None
            old = self.nodes.get(oid)
            if level is None and old is not None:
                level = old.level
            node = PwNode(oid, props, level)
            self.nodes[oid] = node
            if node.name == PW_RELAY_PLAYBACK and props.get("target.object"):
                self.relay_target = str(props["target.object"])
            if node.name == PW_SINK:
                if old is None:
                    events.append(("sink", True))
                elif level is not None and level != old.level:
                    events.append(("volume",) + level)
            elif level is not None and (old is None or level != old.level):
                changed.add(oid)
        tv = self.tv_sink
        if tv is not None and tv.id in changed and tv.level is not None:
            events.append(("hdmi", tv.id) + tv.level)
        return events


# Subprocess seams. The only non-Python tools the project uses; both ship with
# PipeWire/WirePlumber on every desktop distro. Tests replace these.

def _pw_dump_proc():
    """Long-lived `pw-dump -m`: the whole state as one JSON array, then one
    array per change. Line-buffered, '[' and ']' alone on a line."""
    return subprocess.Popen(["pw-dump", "-m", "-N"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")


def _pw_dump_once():
    """The graph right now (for `status` and a start-up check), or None."""
    try:
        out = subprocess.run(["pw-dump", "-N"], stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=5, check=True).stdout
        objs = json.loads(out)
        return objs if isinstance(objs, list) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


_warned = set()


def _warn_once(key, msg, *args):
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def _wpctl(*args, timeout=5):
    """wpctl set-volume / set-mute / set-default. False if it is missing or failed."""
    argv = ["wpctl", *map(str, args)]
    try:
        subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=True)
        return True
    except FileNotFoundError:
        _warn_once("wpctl", "wpctl not found (apt install wireplumber) - cannot move the PC's volume slider")
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("%s failed: %s", " ".join(argv), e)
    return False


class VolumeWatcher(threading.Thread):
    """Follows the "Roku TV" output in PipeWire: slider moves go to the
    reconciler (PC -> TV); set_pc() moves the slider (TV -> PC). Also keeps the
    graph in the shape the mirror needs: PW_SINK is the default output unless
    the user picked something else, and the HDMI sink stays at 100%.
    """

    def __init__(self, reconciler, stop_event):
        super().__init__(daemon=True, name="volume")
        self.reconciler = reconciler
        self.stop_event = stop_event
        self.graph = PwGraph()
        self.proc = None
        self.lock = threading.Lock()
        self._stop = threading.Event()  # stop() was called: no respawn, no more jobs
        self._sink_id = None    # PW_SINK's id and level, published for the reconciler thread
        self._level = None      # (pct, muted) as last seen on PW_SINK
        self._expect = None     # (pct, muted, old_pct, deadline): the echo of our own set_pc()
        self._seen_dump = False
        self._hdmi_fixed = None  # id of the HDMI sink we last reset; never fight twice
        self._default_asked = None  # id of PW_SINK we last made default; ask once per appearance

    # -- TV -> PC (called on the reconciler thread) ----------------------------

    def set_pc(self, pct, muted):
        sink_id, level = self._sink_id, self._level  # published by the watcher thread; never walk the graph here
        if sink_id is None:
            log.debug("no '%s' output in PipeWire - cannot set the PC volume", PW_SINK)
            return False
        if level == (pct, muted):
            return True  # nothing to write, so no echo to expect either
        with self.lock:
            self._expect = (pct, muted, level[0] if level else pct, time.monotonic() + 2.0)
        # Short timeouts: this runs on the reconciler thread, which the urgent
        # "TV off" path also needs; wpctl normally answers in milliseconds.
        ok = _wpctl("set-mute", sink_id, "1" if muted else "0", timeout=1.0)
        ok = _wpctl("set-volume", sink_id, f"{pct / 100:.2f}", timeout=1.0) and ok  # wpctl takes the cubic value
        if not ok:
            with self.lock:
                self._expect = None
        return ok

    def _is_echo(self, pct, muted):
        """Our own set_pc() comes back as events: first (old pct, new mute) after
        set-mute, then the final value. Anything else is the user."""
        with self.lock:
            exp = self._expect
            if exp is None:
                return False
            want_pct, want_muted, old_pct, deadline = exp
            if (pct, muted) == (want_pct, want_muted):
                self._expect = None
                return True
            if (pct, muted) == (old_pct, want_muted) and time.monotonic() < deadline:
                return True
            self._expect = None
            return False

    # -- PipeWire -> graph -> events -------------------------------------------

    def _stopping(self):
        return self._stop.is_set() or self.stop_event.is_set()

    def run(self):
        attempt = 0
        while not self._stopping():
            try:
                self.proc = _pw_dump_proc()
            except OSError as e:
                log.warning("pw-dump not available (%s): install pipewire-bin. Volume mirror off.", e)
                return
            started = time.monotonic()
            self.graph = PwGraph()
            self._sink_id = self._level = self._hdmi_fixed = self._default_asked = None
            self._seen_dump = False
            try:
                self._follow(self.proc)
            except Exception:
                log.exception("volume watcher failed - reconnecting")
            # systemd stops the whole cgroup at once, so at shutdown the child dies
            # a moment before our own SIGTERM handler runs: give it that moment.
            if self._stop.wait(1.0) or self._stopping():
                return
            if time.monotonic() - started > 60:
                attempt = 0  # it ran for a while: PipeWire restarted, nothing is broken
            if attempt == 0:
                log.warning("pw-dump exited (PipeWire restarting?) - reconnecting")
            delay = PW_DUMP_LADDER[min(attempt, len(PW_DUMP_LADDER) - 1)]
            log.debug("pw-dump retry %d in %ss", attempt + 1, delay)
            attempt += 1
            self._stop.wait(delay)

    def _follow(self, proc):
        lines = []
        try:
            for line in proc.stdout:
                if self._stopping():
                    break
                lines.append(line)
                if line.rstrip("\r\n") == "]":  # nested arrays are indented; this ends a batch
                    self._batch("".join(lines))
                    lines = []
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            proc.wait()

    def _batch(self, text):
        try:
            objs = json.loads(text)
        except ValueError as e:
            log.debug("unparseable pw-dump batch (%d bytes): %s", len(text), e)
            return
        if not isinstance(objs, list):
            return
        try:
            self._events(self.graph.apply(objs))
        except Exception:
            log.exception("pw-dump batch dropped")  # one odd object must not kill the mirror
            return
        # The first batch with real objects is the initial dump (a removal can
        # precede it); only then does "no roku-tv" mean the drop-in is missing.
        if not self._seen_dump and any(isinstance(o, dict) and "type" in o for o in objs):
            self._seen_dump = True
            if self.graph.sink is None:
                log.warning("ROKU_VOLUME=true but PipeWire has no '%s' output - run ./install.sh --volume "
                            "(the TV's volume will not follow the PC until then)", PW_SINK)

    def _events(self, events):
        for ev in events:
            kind = ev[0]
            if kind == "sink":
                if ev[1]:
                    sink = self.graph.sink
                    self._level, self._sink_id = sink.level, sink.id
                    log.info("following the '%s' output in PipeWire", PW_SINK)
                    self.reconciler.request_volume(SYNC)  # TV wins when the output (re)appears
                    self._ensure_default()
                else:
                    self._sink_id = self._level = None
                    log.info("the '%s' output is gone (PipeWire restarting?)", PW_SINK)
            elif kind == "volume":
                self._level = (ev[1], ev[2])
                if not self._is_echo(ev[1], ev[2]):
                    self.reconciler.request_volume((ev[1], ev[2]))
            elif kind == "default":
                self._ensure_default()
                tv = self.graph.tv_sink
                if self.graph.default == PW_SINK and tv is not None and tv.level is not None:
                    self._keep_unity(tv.id, *tv.level)  # we just became default: check the HDMI level now
            elif kind == "hdmi":
                self._keep_unity(ev[1], ev[2], ev[3])

    def _ensure_default(self):
        """Make PW_SINK the default output, but only to replace the raw HDMI sink
        (or no default at all): a headset the user picked stays picked."""
        g = self.graph
        sink = g.sink
        if sink is None or g.default is None or self._default_asked == sink.id:
            return
        if g.default == PW_SINK:
            self._default_asked = sink.id  # it is ours; from now on a change is the user's choice
            return
        tv = g.tv_sink
        if g.default == "" or (tv is not None and g.default == tv.name):
            self._default_asked = sink.id
            if _wpctl("set-default", sink.id):
                log.info("made '%s' the default output (was %s)", PW_SINK, g.default or "none")

    def _keep_unity(self, node_id, pct, muted):
        if (pct == 100 and not muted) or self.graph.default != PW_SINK or self._hdmi_fixed == node_id:
            return
        self._hdmi_fixed = node_id
        _wpctl("set-volume", node_id, "1.0")
        _wpctl("set-mute", node_id, "0")
        log.info("HDMI output was at %d%%%s - reset to 100%% (the TV does the attenuating)",
                 pct, " muted" if muted else "")

    def stop(self):
        self._stop.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Reconciler: one worker thread owns all TV I/O; bounded retries, never loops
# ---------------------------------------------------------------------------

ON_LADDER = (0, 3, 8, 15, 30, 60)  # seconds between attempts; covers Wi-Fi coming back after resume


class Reconciler(threading.Thread):
    def __init__(self, roku, cfg, pc_volume=None):
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
        # Volume mirror (Linux): a second latest-wins slot. Power always goes
        # first; a volume job checks `stale` between keys and yields to both a
        # newer slider value and a power edge.
        self.volume = None       # (pct, muted) or SYNC
        self.vgen = 0
        self.tv_on = False       # the last ensure_on() succeeded: on and on our input
        self.pc_volume = pc_volume  # callable(pct, muted) -> bool that sets the PC slider
        self._vol_skip = None    # why the last PC -> TV job was dropped (logged once per streak)

    def request(self, target, urgent=False):
        with self.cv:
            self.target, self.urgent = target, urgent
            self.gen += 1
            self.done.clear()
            self.cv.notify()

    def request_volume(self, want):
        """Key repeat collapses to the newest value; an in-flight burst notices
        it is stale. Never touches `done`: that belongs to the power path."""
        with self.cv:
            self.volume = want
            self.vgen += 1
            self.cv.notify()

    def wait_done(self, timeout):
        return self.done.wait(timeout)

    def run(self):
        while True:
            with self.cv:
                while self.target is None and self.volume is None:
                    self.cv.wait()
                if self.target is not None:
                    target, urgent, gen = self.target, self.urgent, self.gen
                    self.target = None
                    want = None
                else:
                    want, vgen, gen = self.volume, self.vgen, self.gen
                    self.volume = None
            if want is not None:
                try:
                    self._reconcile_volume(want, lambda: self.gen != gen or self.vgen != vgen)
                except Exception:
                    log.exception("volume job %s failed", want)
                continue
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
            self.tv_on = False
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
                self.tv_on = True
                if self.pc_volume is not None:
                    self.request_volume(SYNC)  # TV wins at power-on: the slider adopts the TV
                return
            log.info("retrying TV on in a moment")
        log.warning("giving up on TV on until the displays change again - "
                    "is the TV powered, on the network, with 'Fast TV start' enabled?")

    # -- volume mirror ----------------------------------------------------------

    def _skip(self, why):
        if why != self._vol_skip:
            log.info("%s", why)
            self._vol_skip = why

    def _reconcile_volume(self, want, stale):
        """One slider value -> the TV (or SYNC: the TV -> the slider). One attempt,
        short timeouts, bounded rounds; on any trouble log and wait for the next
        change or edge. Never loops at a TV that will not answer."""
        if self.pc_volume is None:
            return
        roku, t, cfg = self.roku, VOLUME_TIMEOUT_S, self.cfg
        if want == SYNC:
            try:
                tv_vol, tv_muted = roku.audio_device(timeout=t)
            except EcpError as e:
                log.info("cannot read the TV's volume (%s) - the PC slider keeps its value", e)
                return
            if stale():
                return  # the user moved the slider meanwhile (or a power edge): that wins
            if self.pc_volume(tv_vol, tv_muted):
                log.info("PC volume set to the TV's: %d%%%s", tv_vol, " (muted)" if tv_muted else "")
            return
        pct, muted = want
        if not self.tv_on:
            self._skip("TV is not on (as far as we know) - PC volume changes are not sent")
            return
        try:
            app_id, name = roku.active_app(timeout=t)
            if app_id != cfg.input_app:
                self._skip(f"TV is showing '{name}', not {cfg.input_name.upper()} - leaving its volume alone")
                return
            tv_vol, tv_muted = roku.audio_device(timeout=t)
            start, step = (tv_vol, tv_muted), 1.0
            for _ in range(VOLUME_ROUNDS):
                keys = plan_volume_keys(tv_vol, tv_muted, pct, muted, step)
                if not keys:
                    break
                before, sent = tv_vol, sum(k != "VolumeMute" for k in keys)
                for key in keys:
                    if stale():
                        return  # a newer value or a power edge: that job takes over
                    if roku.keypress(key, timeout=t) == 202:
                        self._skip("TV accepted but ignored a volume key (standby?) - PC volume changes are not sent")
                        return
                    if VOLUME_KEY_GAP_S:
                        time.sleep(VOLUME_KEY_GAP_S)
                    log.debug("sent %s", key)
                if not _sleep_unless(VOLUME_SETTLE_S, stale):
                    return
                tv_vol, tv_muted = roku.audio_device(timeout=t)
                if sent and abs(tv_vol - before) > sent:
                    step = abs(tv_vol - before) / sent  # a TV that moves more than one per key
        except EcpError as e:
            self._skip(f"TV volume not delivered ({_describe_power_mode_failure(e)}) - "
                       "will retry at the next change")
            return
        self._vol_skip = None
        if plan_volume_keys(tv_vol, tv_muted, pct, muted):
            log.warning("TV volume settled at %d%s, wanted %d%s - giving up until the next change",
                        tv_vol, " muted" if tv_muted else "", pct, " muted" if muted else "")
        elif (tv_vol, tv_muted) != start:
            log.info("TV volume %d%s -> %d%s", start[0], " muted" if start[1] else "",
                     tv_vol, " muted" if tv_muted else "")


# ---------------------------------------------------------------------------
# Daemon: sample, debounce, hand off; optional login1 hooks via Gio
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self, cfg, sampler, reconciler, volume=None):
        self.cfg, self.sampler, self.reconciler = cfg, sampler, reconciler
        self.volume = volume       # VolumeWatcher (Linux, ROKU_VOLUME=true) or None
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
                log.info("displays came back - TV stays on")
                self.off_deadline = None
            if self.committed != ON:
                self._commit(ON, "displays on")
        else:
            if self.committed == OFF:
                return
            self.off_samples += 1
            if self.off_samples >= 2 and self.off_deadline is None:
                self.off_deadline = now + self.cfg.off_delay_s
                log.info("displays off - TV off in %.0fs unless they come back", self.cfg.off_delay_s)
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
        if not self.reconciler.wait_done(URGENT_OFF_BUDGET):
            log.info("%s: TV off still in flight after %.1fs, continuing", why, URGENT_OFF_BUDGET)

    def on_term(self):
        if self.volume is not None:
            self.volume.stop()  # first: no volume job belongs in a shutdown
        if not self.stop_event.is_set():
            if self.shutdown_seen:
                log.info("SIGTERM during shutdown - already handled")
            elif self.cfg.off_on_stop:
                self._urgent_off("stopping")
            else:
                log.info("stopping - leaving the TV as it is (ROKU_OFF_ON_STOP=false)")
        self._quit()

    def on_int(self):
        log.info("interrupted - leaving the TV as it is")
        if self.volume is not None:
            self.volume.stop()
        self._quit()

    def _quit(self):
        self.stop_event.set()
        if self.volume is not None:
            self.volume.stop()
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
                    log.info("ROKU_OFF_ON_SLEEP=false - leaving the TV on")
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
        if self.volume is not None:
            self.volume.start()
        self._initial()
        if IS_WIN:
            return run_windows_loop(self)
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

def _make_output_lossy():
    """Never let an unencodable character take the process down.

    Windows consoles still run legacy code pages (cp437/cp850 cannot represent
    an em dash), and Python's default is to raise. A replacement character in
    one log line is always better than a dead daemon.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # pythonw (None), or a stream that cannot be reconfigured


def _setup_logging(level, to_file=False):
    handlers = []
    # Under pythonw.exe (how the Windows service runs) sys.stderr is None.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    if to_file:
        try:
            path = default_log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
        except OSError as e:
            if handlers:
                print(f"cannot open log file: {e}", file=sys.stderr)
    logging.basicConfig(handlers=handlers or [logging.NullHandler()],
                        format="%(asctime)s %(levelname)s %(message)s" if to_file else "%(levelname)s %(message)s",
                        level=getattr(logging, level, logging.INFO))


def describe_tv(ip, timeout=5.0):
    """One row of `discover` output for a known IP, or None if it isn't a Roku."""
    try:
        info = Roku(ip, timeout=timeout).device_info()
    except EcpError:
        return None
    name = info.get("user-device-name") or info.get("friendly-device-name") or "?"
    kind = "TV" if info.get("is-tv") == "true" else info.get("model-name", "player")
    return (ip, name, kind, info.get("serial-number", ""),
            info.get("wifi-mac") or info.get("ethernet-mac") or "", info.get("power-mode", ""))


def cmd_discover(args, cfg):
    ip = getattr(args, "ip", None)
    if ip:
        # Discovery is multicast, which some networks (and some access points)
        # never deliver to a given device. Naming the IP always works.
        row = describe_tv(ip)
        if not row:
            print(f"No Roku answered at {ip}. Check the IP, and that Settings > System > "
                  "Advanced system settings > Control by mobile apps is enabled.")
            return 1
        print(f"\n  {row[1]} ({row[2]}, {row[5]})")
        print(f"  ROKU_TV_IP={row[0]}\n  ROKU_TV_SERIAL={row[3]}\n  ROKU_TV_MAC={row[4]}")
        return 0
    print("Searching for Rokus (SSDP, ~4s)... TVs in deep sleep do not answer; turn the TV on first.")
    ips = ssdp_discover()
    if not ips:
        print("No Roku answered. Is the TV on and on the same network?\n"
              "Some networks do not deliver multicast to every device - if you know the TV's\n"
              f"address, use: {APP} discover --ip <tv-ip>")
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


def make_sampler(cfg):
    return WindowsDisplaySampler(cfg.connector) if IS_WIN else DisplaySampler(override=cfg.connector)


def cmd_status(args, cfg):
    s = make_sampler(cfg).sample()
    print(f"config: {cfg.source or '(defaults + environment)'}")
    if IS_WIN:
        # Windows has no per-output power state and the daemon learns the
        # console state from notifications, so a one-shot CLI run cannot know
        # it (and an SSH session has no desktop to query at all).
        print("Displays: live state is only known inside the running daemon "
              "(see the log); this listing is the active monitor topology.")
        for c in s.connectors:
            print(f"  {c!r}")
        if not s.connectors:
            print("  (no topology available - running without an interactive desktop?)")
        print(f"Roku monitor: {s.roku_name or 'not identified'}")
    else:
        print(f"PC displays driven: {'YES' if s.pc_on else 'no'}")
        for c in s.connectors:
            tag = "  <- Roku" if c.name == s.roku_name else ""
            print(f"  {c.name:<18} {c.status:<13} {c.enabled:<9} {c.dpms:<4} {'driven' if c.driven else ''}{tag}")
        print(f"Roku connector: {s.roku_name or 'not found (set ROKU_CONNECTOR)'} -> {s.roku}")
        _print_audio_status()
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
    if not IS_WIN:
        try:
            vol, muted = roku.audio_device()
            print(f"TV volume: {vol}{' (muted)' if muted else ''}; "
                  f"volume mirror (ROKU_VOLUME): {'on' if cfg.volume else 'off'}")
        except EcpError as e:
            print(f"TV volume: not readable ({e}) - the volume mirror needs /query/audio-device")
    return 0


def _level(node):
    if node.level is None:
        return "level unknown"
    return f"{node.level[0]}%{' muted' if node.level[1] else ''}"


def _print_audio_status():
    """Linux: the PipeWire side of the volume mirror. install.sh reads the first
    line to learn which sink feeds the TV, so its shape is fixed."""
    objs = _pw_dump_once()
    if objs is None:
        print("Audio: PipeWire not reachable (pw-dump missing or not in a desktop session)")
        return
    g = PwGraph()
    g.apply(objs)
    tv = g.tv_sink or find_roku_alsa_sink(g.nodes)  # by the relay's target, else by the Roku's name
    if tv is not None:
        print(f"Audio sink to TV: {tv.name} ({_level(tv)})")
        if g.relay_target and g.relay_target != tv.name:
            print(f"  but the relay plays into '{g.relay_target}', which is not present - "
                  "re-run ./install.sh --volume to point it at the sink above")
    else:
        print("Audio sink to TV: (not found - the HDMI sink appears with the TV's hot-plug; is the TV on?)")
    sink = g.sink
    if sink is not None:
        print(f"'Roku TV' output ({PW_SINK}): present, {_level(sink)}; default output: {g.default or 'none'}"
              f"{'' if g.default == PW_SINK else ' (not the Roku TV output - volume keys do not reach the TV)'}")
    else:
        print(f"'Roku TV' output ({PW_SINK}): absent - volume mirror not installed (./install.sh --volume)")


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
    reconciler = Reconciler(roku, cfg)
    daemon = Daemon(cfg, make_sampler(cfg), reconciler)
    if cfg.volume and IS_WIN:
        log.warning("ROKU_VOLUME=true is Linux-only (PipeWire) - ignored on Windows")
    elif cfg.volume:
        daemon.volume = VolumeWatcher(reconciler, daemon.stop_event)
        reconciler.pc_volume = daemon.volume.set_pc
    elif not IS_WIN:
        objs = _pw_dump_once()
        if objs is not None:
            g = PwGraph()
            g.apply(objs)
            if g.sink is not None:
                log.warning("PipeWire has a '%s' output but ROKU_VOLUME=false: its volume slider moves "
                            "nothing. Set ROKU_VOLUME=true, or remove "
                            "~/.config/pipewire/pipewire.conf.d/90-roku-monitor.conf and restart pipewire.",
                            PW_SINK)
    try:
        return daemon.run()
    finally:
        if daemon.volume is not None:
            daemon.volume.stop()


def main(argv=None):
    p = argparse.ArgumentParser(prog=APP, description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help=f"env file (default: {default_config_path()} or ./.env)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="follow the displays (what the service runs)")
    disc = sub.add_parser("discover", help="list Rokus on the LAN with IP/serial/MAC")
    disc.add_argument("--ip", help="skip the search and read this address directly "
                                   "(for networks that do not deliver multicast)")
    sub.add_parser("status", help="show connector state, TV state and what the daemon would do")
    sub.add_parser("on", help="turn the TV on and switch to the configured input")
    off = sub.add_parser("off", help="turn the TV off")
    off.add_argument("--force", action="store_true", help="ignore ROKU_ONLY_OFF_WHEN_ON_INPUT")
    args = p.parse_args(argv)
    _make_output_lossy()

    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        _setup_logging("INFO")
        log.error("%s", e)
        return EX_CONFIG
    # On Windows `run` is started by the logon task under pythonw.exe, where
    # there is no console to log to and no journal to log into.
    _setup_logging("DEBUG" if args.verbose else cfg.log_level,
                   to_file=IS_WIN and args.cmd == "run")
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
