"""Unit tests for the pure helpers. No network, no gi, no real sysfs —
everything CI can actually prove about the logic."""

import io
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import roku_monitor as rm  # noqa: E402

# 128-byte EDID base block with PNP id RKU (R=18,K=11,U=21 -> 0x4975) and a
# 0xFC descriptor "Roku TV". Only the bytes the parser looks at are meaningful.
EDID_RKU = bytearray(128)
EDID_RKU[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
EDID_RKU[8:10] = b"\x49\x75"
EDID_RKU[54 + 18:54 + 18 + 18] = b"\x00\x00\x00\xfc\x00Roku TV\n      "

EDID_DELL = bytearray(128)
EDID_DELL[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
EDID_DELL[8:10] = b"\x10\xac"  # DEL
EDID_DELL[54:54 + 18] = b"\x00\x00\x00\xfc\x00DELL S2722DC\n"


class EnvFileTests(unittest.TestCase):
    def test_parse(self):
        text = """
        # comment
        ROKU_TV_IP=10.0.0.5
        export ROKU_TV_MAC="aa:bb:cc:dd:ee:ff"
        ROKU_INPUT='hdmi2'
        ROKU_OFF_DELAY_S=7   # trailing comment
        NOT_A_LINE
        """
        d = rm.parse_env_file(text)
        self.assertEqual(d["ROKU_TV_IP"], "10.0.0.5")
        self.assertEqual(d["ROKU_TV_MAC"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(d["ROKU_INPUT"], "hdmi2")
        self.assertEqual(d["ROKU_OFF_DELAY_S"], "7")
        self.assertNotIn("NOT_A_LINE", d)

    def test_placeholders_count_as_unset(self):
        cfg = rm.Config({"ROKU_TV_IP": "<tv-ip>", "ROKU_TV_MAC": "<aa:bb:cc:dd:ee:ff>"})
        self.assertEqual(cfg.tv_ip, "")
        self.assertEqual(cfg.mac, "")
        with self.assertRaises(rm.ConfigError):
            cfg.require_tv()

    def test_defaults_and_types(self):
        cfg = rm.Config({"ROKU_TV_IP": "10.0.0.5", "ROKU_ONLY_OFF_WHEN_ON_INPUT": "yes"})
        self.assertEqual(cfg.input_key, "InputHDMI3")
        self.assertEqual(cfg.input_app, "tvinput.hdmi3")
        self.assertTrue(cfg.only_off_when_on_input)
        self.assertTrue(cfg.off_on_sleep)
        self.assertEqual(cfg.off_delay_s, 10.0)
        self.assertEqual(cfg.poll_s, 1.0)

    def test_bad_input_name(self):
        with self.assertRaises(rm.ConfigError):
            rm.Config({"ROKU_INPUT": "hdmi9"})


class InputSpecTests(unittest.TestCase):
    def test_known(self):
        self.assertEqual(rm.input_spec("hdmi3"), ("InputHDMI3", "tvinput.hdmi3"))
        self.assertEqual(rm.input_spec("HDMI1"), ("InputHDMI1", "tvinput.hdmi1"))
        self.assertEqual(rm.input_spec("av1"), ("InputAV1", "tvinput.cvbs"))


class EcpParsingTests(unittest.TestCase):
    def test_device_info(self):
        xml = ('<?xml version="1.0"?><device-info><serial-number>X1</serial-number>'
               '<power-mode>DisplayOff</power-mode><is-tv>true</is-tv></device-info>')
        info = rm.parse_device_info(xml)
        self.assertEqual(info["power-mode"], "DisplayOff")
        self.assertEqual(info["is-tv"], "true")

    def test_active_app_home(self):
        self.assertEqual(rm.parse_active_app("<active-app><app>Roku</app></active-app>"), (None, "Roku"))

    def test_active_app_input(self):
        xml = '<active-app><app id="tvinput.hdmi3" type="tvin" version="1.0.0">HDMI 3</app></active-app>'
        self.assertEqual(rm.parse_active_app(xml), ("tvinput.hdmi3", "HDMI 3"))

    def test_active_app_screensaver_sibling(self):
        xml = ('<active-app><app>Roku</app>'
               '<screensaver id="55545" type="ssvr" version="7.3.102">Roku City</screensaver></active-app>')
        self.assertEqual(rm.parse_active_app(xml), (None, "Roku"))

    def test_apps(self):
        xml = ('<apps><app id="tvinput.hdmi3" type="tvin" version="1.0.0">HDMI 3</app>'
               '<app id="12" type="appl" version="1">Netflix</app></apps>')
        self.assertEqual(rm.parse_apps(xml), [("tvinput.hdmi3", "HDMI 3"), ("12", "Netflix")])


class WolTests(unittest.TestCase):
    def test_magic_packet(self):
        for mac in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabbccddeeff"):
            pkt = rm.magic_packet(mac)
            self.assertEqual(len(pkt), 102)
            self.assertEqual(pkt[:6], b"\xff" * 6)
            self.assertEqual(pkt[6:], bytes.fromhex("aabbccddeeff") * 16)

    def test_bad_mac(self):
        for mac in ("", "aa:bb", "zz:zz:zz:zz:zz:zz", None):
            with self.assertRaises(ValueError):
                rm.magic_packet(mac)

    def test_targets(self):
        t = rm.wol_targets("192.0.2.40", "255.255.255.255")
        self.assertIn(("255.255.255.255", 9), t)
        self.assertIn(("192.0.2.255", 9), t)
        self.assertIn(("192.0.2.40", 7), t)
        self.assertEqual(len(t), len(set(t)))


class SsdpTests(unittest.TestCase):
    def test_parse(self):
        resp = ("HTTP/1.1 200 OK\r\nCache-Control: max-age=3600\r\nST: roku:ecp\r\n"
                "USN: uuid:roku:ecp:X1\r\nLOCATION: http://192.0.2.40:8060/\r\n\r\n")
        self.assertEqual(rm.parse_ssdp_response(resp, "1.2.3.4"), "192.0.2.40")
        self.assertIsNone(rm.parse_ssdp_response("HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\n", "1.2.3.4"))
        self.assertEqual(rm.parse_ssdp_response("ST: roku:ecp\r\n", "1.2.3.4"), "1.2.3.4")


class EdidTests(unittest.TestCase):
    def test_vendor_and_name(self):
        self.assertEqual(rm.edid_vendor(bytes(EDID_RKU)), "RKU")
        self.assertEqual(rm.edid_name(bytes(EDID_RKU)), "Roku TV")
        self.assertTrue(rm.is_roku_edid(bytes(EDID_RKU)))
        self.assertEqual(rm.edid_vendor(bytes(EDID_DELL)), "DEL")
        self.assertFalse(rm.is_roku_edid(bytes(EDID_DELL)))
        self.assertFalse(rm.is_roku_edid(b""))


def fake_drm(root, name, status, enabled, dpms, edid=b""):
    d = os.path.join(root, name)
    os.makedirs(d)
    for fname, val in (("status", status), ("enabled", enabled), ("dpms", dpms)):
        with open(os.path.join(d, fname), "w") as f:
            f.write(val + "\n")
    with open(os.path.join(d, "edid"), "wb") as f:
        f.write(edid)


class DrmSamplerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_driven_predicate(self):
        self.assertTrue(rm.Connector("x", "connected", "enabled", "On").driven)
        # GNOME idle blank under atomic modesetting (verified on amdgpu/mutter 50)
        self.assertFalse(rm.Connector("x", "connected", "disabled", "Off").driven)
        # legacy DPMS path: still enabled, dpms Off
        self.assertFalse(rm.Connector("x", "connected", "enabled", "Off").driven)
        # never-modeset connector: dpms reads On (DRM_MODE_DPMS_ON == 0) while disabled
        self.assertFalse(rm.Connector("x", "unknown", "disabled", "On").driven)
        self.assertFalse(rm.Connector("x", "disconnected", "disabled", "Off").driven)

    def test_sample_on(self):
        fake_drm(self.root, "card1-DP-2", "connected", "enabled", "On", bytes(EDID_DELL))
        fake_drm(self.root, "card1-HDMI-A-1", "connected", "enabled", "On", bytes(EDID_RKU))
        fake_drm(self.root, "card1-DP-1", "disconnected", "disabled", "Off")
        fake_drm(self.root, "card1-Writeback-1", "unknown", "disabled", "On")
        s = rm.DisplaySampler(self.root).sample()
        self.assertTrue(s.pc_on)
        self.assertEqual(s.roku_name, "card1-HDMI-A-1")
        self.assertEqual(s.roku, "driven")
        self.assertEqual([c.name for c in s.connectors], ["card1-DP-1", "card1-DP-2", "card1-HDMI-A-1"])

    def test_sample_gnome_blank(self):
        fake_drm(self.root, "card1-DP-2", "connected", "disabled", "Off", bytes(EDID_DELL))
        fake_drm(self.root, "card1-HDMI-A-1", "connected", "disabled", "Off", bytes(EDID_RKU))
        s = rm.DisplaySampler(self.root).sample()
        self.assertFalse(s.pc_on)
        self.assertEqual(s.roku, "idle")

    def test_roku_absent_after_hpd_drop_keeps_cached_name(self):
        fake_drm(self.root, "card1-DP-2", "connected", "enabled", "On", bytes(EDID_DELL))
        fake_drm(self.root, "card1-HDMI-A-1", "connected", "enabled", "On", bytes(EDID_RKU))
        sampler = rm.DisplaySampler(self.root)
        sampler.sample()
        # TV powered off: HPD low -> disconnected, EDID gone
        for fname, val in (("status", "disconnected"), ("enabled", "disabled"), ("dpms", "Off")):
            with open(os.path.join(self.root, "card1-HDMI-A-1", fname), "w") as f:
                f.write(val)
        with open(os.path.join(self.root, "card1-HDMI-A-1", "edid"), "wb") as f:
            f.write(b"")
        s = sampler.sample()
        self.assertTrue(s.pc_on)  # the Dell is still driven
        self.assertEqual(s.roku_name, "card1-HDMI-A-1")
        self.assertEqual(s.roku, "absent")

    def test_override(self):
        fake_drm(self.root, "card0-HDMI-A-2", "connected", "enabled", "On")
        s = rm.DisplaySampler(self.root, override="HDMI-A-2").sample()
        self.assertEqual(s.roku_name, "card0-HDMI-A-2")
        self.assertEqual(s.roku, "driven")


class DaemonDebounceTests(unittest.TestCase):
    """Drive Daemon._tick with a scripted sampler and a recording reconciler."""

    class FakeSampler:
        def __init__(self, seq):
            self.seq = list(seq)

        def sample(self):
            pc_on = self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]
            return rm.Sample(pc_on, "driven" if pc_on else "idle", "card1-HDMI-A-1", [])

    class FakeReconciler:
        def __init__(self):
            self.requests = []
            self.last_off_at = None
            self.fatal = None

        def request(self, target, urgent=False):
            self.requests.append((target, urgent))

        def wait_done(self, timeout):
            return True

    def make(self, seq, off_delay=0.0):
        cfg = rm.Config({"ROKU_TV_IP": "192.0.2.40", "ROKU_OFF_DELAY_S": str(off_delay)})
        d = rm.Daemon(cfg, self.FakeSampler(seq), self.FakeReconciler())
        return d

    def test_startup_on_then_off_needs_two_samples(self):
        d = self.make([True, False, False, False])
        d._initial()
        self.assertEqual(d.reconciler.requests, [(rm.ON, False)])
        d._tick()  # first off sample: nothing yet
        self.assertEqual(len(d.reconciler.requests), 1)
        d._tick()  # second: grace 0s -> deadline already passed? deadline = now+0, checked same tick
        d._tick()
        self.assertEqual(d.reconciler.requests[-1], (rm.OFF, False))

    def test_blip_is_cancelled(self):
        d = self.make([True, False, False, True, True], off_delay=60)
        d._initial()
        d._tick(); d._tick()  # arms the grace timer
        self.assertIsNotNone(d.off_deadline)
        d._tick()  # displays back
        self.assertIsNone(d.off_deadline)
        self.assertEqual(d.reconciler.requests, [(rm.ON, False)])

    def test_sleeping_suppresses(self):
        d = self.make([True, False, False, False], off_delay=0)
        d._initial()
        d.sleeping = True
        for _ in range(4):
            d._tick()
        self.assertEqual(d.reconciler.requests, [(rm.ON, False)])

    def test_startup_while_blanked(self):
        d = self.make([False, False])
        d._initial()
        self.assertEqual(d.reconciler.requests, [(rm.OFF, False)])

    def test_urgent_off_not_suppressed_after_intervening_on(self):
        d = self.make([True, True])
        d._initial()
        d._commit(rm.OFF, "test")
        d.reconciler.last_off_at = __import__("time").monotonic()
        d._commit(rm.ON, "test")
        d._urgent_off("suspend")
        self.assertEqual(d.reconciler.requests[-1], (rm.OFF, True))
        self.assertEqual(d.committed, rm.OFF)

    def test_urgent_off_suppressed_right_after_off(self):
        d = self.make([False, False])
        d._initial()
        d.reconciler.last_off_at = __import__("time").monotonic()
        d._urgent_off("stop")
        self.assertEqual(d.reconciler.requests, [(rm.OFF, False)])

    def test_fatal_quits_loop(self):
        d = self.make([True, True])
        quits = []
        d.loop = type("L", (), {"quit": lambda self: quits.append(1)})()
        d.reconciler.fatal = rm.EX_CONFIG
        self.assertFalse(d.tick())
        self.assertEqual(d.exit_code, rm.EX_CONFIG)
        self.assertEqual(quits, [1])
        self.assertTrue(d.stop_event.is_set())


class WrongDeviceTests(unittest.TestCase):
    class FakeRoku:
        def power_mode(self, timeout=None):
            raise rm.WrongDevice("serial mismatch")

    def test_propagates_to_reconciler_fatal(self):
        cfg = rm.Config({"ROKU_TV_IP": "192.0.2.40", "ROKU_TV_SERIAL": "AAA"})
        with self.assertRaises(rm.WrongDevice):
            rm.ensure_on(self.FakeRoku(), cfg)
        with self.assertRaises(rm.WrongDevice):
            rm.ensure_off(self.FakeRoku(), cfg)
        rec = rm.Reconciler(self.FakeRoku(), cfg)
        rec.start()
        rec.request(rm.OFF)
        self.assertTrue(rec.wait_done(5))
        self.assertEqual(rec.fatal, rm.EX_CONFIG)

    def test_bad_mac_is_config_error(self):
        with self.assertRaises(rm.ConfigError):
            rm.Config({"ROKU_TV_IP": "192.0.2.40", "ROKU_TV_MAC": "nope"})

    def test_unparseable_xml_is_ecp_error(self):
        with self.assertRaises(rm.EcpError):
            rm.parse_active_app("<active-app><app>Roku")


class WindowsLogicTests(unittest.TestCase):
    """The Windows backend's decision logic, exercised without Windows."""

    def test_pnp_vendor_decode(self):
        self.assertEqual(rm.pnp_vendor(0x4975), "RKU")   # R=18, K=11, U=21
        self.assertEqual(rm.pnp_vendor(0x10AC), "DEL")

    def test_is_roku_target_handles_byte_order(self):
        self.assertTrue(rm.is_roku_target(0x4975, "Generic PnP Monitor"))
        self.assertTrue(rm.is_roku_target(0x7549, "Generic PnP Monitor"))  # byte-swapped
        self.assertTrue(rm.is_roku_target(0x10AC, "Roku TV"))              # by name
        self.assertFalse(rm.is_roku_target(0x10AC, "DELL S2722DC"))
        self.assertFalse(rm.is_roku_target(0, ""))

    def make_daemon(self, **cfg_over):
        values = {"ROKU_TV_IP": "192.0.2.40", "ROKU_OFF_DELAY_S": "0"}
        values.update(cfg_over)
        cfg = rm.Config(values)
        d = rm.Daemon(cfg, rm.WindowsDisplaySampler(), DaemonDebounceTests.FakeReconciler())
        d.sampler.display_on = True
        d.committed = rm.ON
        return d

    def test_display_off_then_on(self):
        d = self.make_daemon()
        rm.handle_win_event(d, rm.EV_DISPLAY, rm.DISPLAY_OFF)   # 1st off sample
        rm.handle_win_event(d, rm.EV_DISPLAY, rm.DISPLAY_OFF)   # 2nd -> commits
        self.assertEqual(d.reconciler.requests[-1], (rm.OFF, False))
        rm.handle_win_event(d, rm.EV_DISPLAY, rm.DISPLAY_ON)
        self.assertEqual(d.reconciler.requests[-1], (rm.ON, False))

    def test_dimmed_counts_as_on(self):
        d = self.make_daemon()
        rm.handle_win_event(d, rm.EV_DISPLAY, rm.DISPLAY_DIMMED)
        self.assertTrue(d.sampler.display_on)
        self.assertEqual(d.committed, rm.ON)

    def test_suspend_and_resume(self):
        d = self.make_daemon()
        rm.handle_win_event(d, rm.EV_SUSPEND)
        self.assertTrue(d.sleeping)
        self.assertEqual(d.reconciler.requests[-1], (rm.OFF, True))
        rm.handle_win_event(d, rm.EV_RESUME)
        self.assertFalse(d.sleeping)
        self.assertEqual(d.off_samples, 0)

    def test_suspend_respects_off_on_sleep_false(self):
        d = self.make_daemon(ROKU_OFF_ON_SLEEP="false")
        rm.handle_win_event(d, rm.EV_SUSPEND)
        self.assertTrue(d.sleeping)
        self.assertEqual(d.reconciler.requests, [])

    def test_display_events_ignored_while_sleeping(self):
        d = self.make_daemon()
        d.sleeping = True
        rm.handle_win_event(d, rm.EV_DISPLAY, rm.DISPLAY_OFF)
        rm.handle_win_event(d, rm.EV_DISPLAY, rm.DISPLAY_OFF)
        self.assertEqual(d.reconciler.requests, [])

    def test_endsession_logoff_and_shutdown(self):
        for logoff in (True, False):
            d = self.make_daemon()
            rm.handle_win_event(d, rm.EV_ENDSESSION, logoff)
            self.assertTrue(d.shutdown_seen)
            self.assertEqual(d.reconciler.requests[-1], (rm.OFF, True))

    def test_endsession_respects_off_on_stop_false(self):
        d = self.make_daemon(ROKU_OFF_ON_STOP="false")
        rm.handle_win_event(d, rm.EV_ENDSESSION, False)
        self.assertEqual(d.reconciler.requests, [])

    def test_unknown_event_is_ignored(self):
        self.assertFalse(rm.handle_win_event(self.make_daemon(), "nonsense"))

    def fake_targets(self, targets):
        """Swap the ctypes call out; never assume what displays the host has."""
        orig = rm.list_windows_targets
        rm.list_windows_targets = targets
        self.addCleanup(lambda: setattr(rm, "list_windows_targets", orig))

    def test_sampler_degrades_without_a_desktop(self):
        """An SSH session has no desktop to query; that must not break sampling."""
        def boom():
            raise OSError("no interactive desktop")
        self.fake_targets(boom)
        s = rm.WindowsDisplaySampler().sample()
        self.assertTrue(s.pc_on)          # still reports the cached display state
        self.assertEqual(s.connectors, [])
        self.assertIsNone(s.roku_name)

    def test_sampler_identifies_the_roku_and_tracks_display_state(self):
        self.fake_targets(lambda: [rm.WinTarget("DELL S2722DC", False),
                                   rm.WinTarget("Roku TV", True)])
        sampler = rm.WindowsDisplaySampler()
        s = sampler.sample()
        self.assertEqual(s.roku_name, "Roku TV")
        self.assertEqual(s.roku, "driven")
        sampler.set_display_on(False)
        s = sampler.sample()
        self.assertFalse(s.pc_on)
        self.assertEqual(s.roku, "idle")


class OutputEncodingTests(unittest.TestCase):
    def test_legacy_codepage_does_not_raise(self):
        """A cp437 console must not kill the process on an em dash."""
        import io
        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp437", errors="strict")
        orig = sys.stdout
        try:
            sys.stdout = buf
            rm._make_output_lossy()
            print("displays off — TV off…")  # em dash + ellipsis
        finally:
            sys.stdout = orig
        self.assertEqual(buf.errors, "replace")


class LogAsciiTests(unittest.TestCase):
    def test_runtime_messages_are_ascii(self):
        """Log lines must survive any code page and any log reader.

        Windows writes the log as UTF-8 but PowerShell 5.1's Get-Content reads
        it as ANSI, so a stray em dash turns into mojibake for the one person
        most likely to be reading it: someone debugging.
        """
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "roku_monitor.py"), encoding="utf-8").read()
        bad = [ln.strip() for ln in src.split("\n")
               if re.search(r"(log\.\w+\(|print\()", ln)
               and re.search(r"[^\x00-\x7F]", ln)
               and not ln.strip().startswith("#")]
        self.assertEqual(bad, [], "non-ASCII in runtime output")


class ConfigPathTests(unittest.TestCase):
    def test_win32_config_path(self):
        orig_win, orig_appdata = rm.IS_WIN, os.environ.get("APPDATA")
        try:
            rm.IS_WIN = True
            os.environ["APPDATA"] = os.path.join("C:", "Users", "x", "AppData", "Roaming")
            self.assertEqual(rm.default_config_path(),
                             os.path.join("C:", "Users", "x", "AppData", "Roaming", rm.APP, "env"))
        finally:
            rm.IS_WIN = orig_win
            if orig_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = orig_appdata

    def test_posix_config_path(self):
        self.assertTrue(rm.default_config_path().endswith(os.path.join(rm.APP, "env")))


# ---------------------------------------------------------------------------
# Volume mirror (Linux): pure helpers, the pw-dump parser, the reconciler's
# volume job and the watcher's handlers. No subprocess is ever spawned here.
# ---------------------------------------------------------------------------

AUDIO_DEVICE_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<audio-device>
  <capabilities><all-destinations>speakers,spdif</all-destinations></capabilities>
  <global>
    <muted>{muted}</muted>
    <volume>{volume}</volume>
    <destination-list>speakers,spdif,lineout</destination-list>
  </global>
  <destinations><destination name="speakers"><muted>false</muted><volume>15</volume></destination></destinations>
</audio-device>"""


def pw_node(node_id, name, media_class="Audio/Sink", volumes=(1.0, 1.0), mute=False, **props):
    p = {"node.name": name, "media.class": media_class}
    p.update(props)
    return {"id": node_id, "type": "PipeWire:Interface:Node",
            "info": {"props": p, "params": {"Props": [{"volume": 1.0, "mute": mute,
                                                     "channelVolumes": list(volumes)}]}}}


def pw_default(name):
    return {"id": 41, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
            "metadata": [{"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON",
                          "value": {"name": name}}]}


HDMI = "alsa_output.pci-0000_00_00.1.hdmi-stereo-extra1"


def pw_graph_objs(default=HDMI, with_sink=True, sink_volumes=(0.003375, 0.003375)):
    objs = [pw_node(80, HDMI, device_api="alsa", **{"node.nick": "Roku TV"}),
            pw_node(47, rm.PW_RELAY_PLAYBACK, "Stream/Output/Audio", **{"target.object": HDMI}),
            pw_node(79, rm.PW_RELAY_CAPTURE, "Stream/Input/Audio", **{"target.object": rm.PW_SINK}),
            pw_default(default)]
    if with_sink:
        objs.append(pw_node(85, rm.PW_SINK, volumes=sink_volumes))
    for o in objs:
        if "device_api" in (o.get("info") or {}).get("props", {}):
            o["info"]["props"]["device.api"] = o["info"]["props"].pop("device_api")
    return objs


class VolumeMappingTests(unittest.TestCase):
    def test_cubic_percent(self):
        self.assertEqual(rm.pw_percent({"channelVolumes": [1.0, 1.0]}), (100, False))
        self.assertEqual(rm.pw_percent({"channelVolumes": [0.125, 0.125]}), (50, False))
        self.assertEqual(rm.pw_percent({"channelVolumes": [0.064, 0.064], "mute": True}), (40, True))
        self.assertEqual(rm.pw_percent({"channelVolumes": [0.003375, 0.001]}), (15, False))  # max channel
        self.assertEqual(rm.pw_percent({"channelVolumes": [0.0, 0.0]}), (0, False))
        self.assertEqual(rm.pw_percent({"volume": 0.125}), (50, False))  # no channels: fall back
        self.assertEqual(rm.pw_percent({"channelVolumes": [2.0]}), (100, False))  # clamped

    def test_round_trip_every_percent(self):
        # wpctl takes the cubic value; what we write must read back as the same percent
        for pct in range(101):
            linear = (pct / 100) ** 3
            self.assertEqual(rm.pw_percent({"channelVolumes": [linear, linear]})[0], pct)


class AudioDeviceParseTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(rm.parse_audio_device(AUDIO_DEVICE_XML.format(muted="false", volume="15")), (15, False))
        self.assertEqual(rm.parse_audio_device(AUDIO_DEVICE_XML.format(muted="true", volume="0")), (0, True))
        self.assertEqual(rm.parse_audio_device(AUDIO_DEVICE_XML.format(muted="false", volume="140")), (100, False))

    def test_missing_is_ecp_error(self):
        with self.assertRaises(rm.EcpError):
            rm.parse_audio_device("<audio-device><capabilities/></audio-device>")
        with self.assertRaises(rm.EcpError):
            rm.parse_audio_device(AUDIO_DEVICE_XML.format(muted="false", volume="loud"))
        with self.assertRaises(rm.EcpError):
            rm.parse_audio_device("not xml")


class VolumePlanTests(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(rm.plan_volume_keys(15, False, 40, False), ["VolumeUp"] * 25)
        self.assertEqual(rm.plan_volume_keys(40, False, 15, False), ["VolumeDown"] * 25)
        self.assertEqual(rm.plan_volume_keys(15, False, 15, False), [])
        self.assertEqual(len(rm.plan_volume_keys(0, False, 100, False)), 100)

    def test_mute_rules(self):
        # PC muted: only the mute is mirrored, the volume follows at unmute
        self.assertEqual(rm.plan_volume_keys(15, False, 40, True), ["VolumeMute"])
        self.assertEqual(rm.plan_volume_keys(15, True, 40, True), [])
        # PC unmuted, TV muted: going up needs no toggle (VolumeUp unmutes) ...
        self.assertEqual(rm.plan_volume_keys(0, True, 6, False), ["VolumeUp"] * 6)
        self.assertEqual(rm.plan_volume_keys(15, True, 21, False), ["VolumeUp"] * 6)
        # ... staying needs the toggle, going down steps first (VolumeDown keeps a muted
        # TV muted) and unmutes last, so the TV never comes on loud and ramps down
        self.assertEqual(rm.plan_volume_keys(15, True, 15, False), ["VolumeMute"])
        self.assertEqual(rm.plan_volume_keys(15, True, 10, False), ["VolumeDown"] * 5 + ["VolumeMute"])
        self.assertEqual(rm.plan_volume_keys(15, True, 0, False), ["VolumeDown"] * 15)  # 0 is silent anyway
        # 0 is silent whatever the mute flag says (the TV mutes itself at 0)
        self.assertEqual(rm.plan_volume_keys(0, True, 0, False), [])
        self.assertEqual(rm.plan_volume_keys(0, False, 0, True), ["VolumeMute"])


class PwGraphTests(unittest.TestCase):
    def test_initial_dump(self):
        g = rm.PwGraph()
        events = g.apply(pw_graph_objs())
        self.assertIn(("sink", True), events)
        self.assertIn(("default", HDMI), events)
        self.assertIn(("hdmi", 80, 100, False), events)
        self.assertNotIn(("volume", 15, False), events)  # the initial level is state, not a change
        self.assertEqual(g.sink.level, (15, False))
        self.assertEqual(g.relay_target, HDMI)
        self.assertIs(g.tv_sink, g.nodes[80])
        self.assertEqual(g.default, HDMI)

    def test_volume_change_and_echo_of_other_params(self):
        g = rm.PwGraph()
        g.apply(pw_graph_objs())
        self.assertEqual(g.apply([pw_node(85, rm.PW_SINK, volumes=(0.064, 0.064))]), [("volume", 40, False)])
        self.assertEqual(g.apply([pw_node(85, rm.PW_SINK, volumes=(0.064, 0.064))]), [])  # same level: nothing
        self.assertEqual(g.apply([pw_node(85, rm.PW_SINK, volumes=(0.064, 0.064), mute=True)]),
                         [("volume", 40, True)])
        # a re-dump without Props (e.g. a state change) keeps the last level
        g.apply([{"id": 85, "type": "PipeWire:Interface:Node",
                  "info": {"props": {"node.name": rm.PW_SINK, "media.class": "Audio/Sink"}}}])
        self.assertEqual(g.sink.level, (40, True))

    def test_removal_and_return(self):
        g = rm.PwGraph()
        g.apply(pw_graph_objs())
        self.assertEqual(g.apply([{"id": 85, "info": None}]), [("sink", False)])
        self.assertIsNone(g.sink)
        self.assertEqual(g.apply([pw_node(85, rm.PW_SINK, volumes=(0.125, 0.125))]), [("sink", True)])
        # the HDMI sink vanishes at every blank and comes back with a new id
        g.apply([{"id": 80, "info": None}])
        self.assertIsNone(g.tv_sink)
        self.assertEqual(g.apply([pw_node(88, HDMI, volumes=(0.064, 0.064), **{"device.api": "alsa"})]),
                         [("hdmi", 88, 40, False)])

    def test_metadata_merge_and_default_change(self):
        g = rm.PwGraph()
        g.apply(pw_graph_objs())
        # a change batch carries only the changed entry
        self.assertEqual(g.apply([pw_default(rm.PW_SINK)]), [("default", rm.PW_SINK)])
        self.assertEqual(g.apply([{"id": 41, "type": "PipeWire:Interface:Metadata",
                                   "props": {"metadata.name": "default"},
                                   "metadata": [{"key": "default.audio.source", "value": {"name": "mic"}}]}]), [])
        self.assertEqual(g.default, rm.PW_SINK)

    def test_relay_target_learned_late_and_roku_fallback(self):
        g = rm.PwGraph()
        g.apply([pw_node(80, HDMI, **{"device.api": "alsa", "alsa.name": "Roku TV"})])
        self.assertEqual(g.tv_sink.id, 80)  # found by name before the relay is seen
        g.apply([pw_node(47, rm.PW_RELAY_PLAYBACK, "Stream/Output/Audio", **{"target.object": "other"})])
        self.assertIsNone(g.tv_sink)  # the relay names a sink that is not there
        g.apply([pw_node(90, "other", **{"device.api": "alsa"})])
        self.assertEqual(g.tv_sink.id, 90)

    def test_ignores_non_audio_and_garbage(self):
        g = rm.PwGraph()
        self.assertEqual(g.apply([{"id": 1, "type": "PipeWire:Interface:Port", "info": {}},
                                  pw_node(5, "cam", "Video/Source"), "junk", {"id": 9}]), [])
        self.assertEqual(g.nodes, {})


class FakeProc:
    def __init__(self, text):
        self.stdout = io.StringIO(text)
        self.waited = False

    def wait(self):
        self.waited = True

    def poll(self):
        return 0


class WatcherTests(unittest.TestCase):
    class FakeReconciler:
        def __init__(self):
            self.volumes = []

        def request_volume(self, want):
            self.volumes.append(want)

    def setUp(self):
        self.calls = []
        orig = rm._wpctl
        setattr(rm, "_wpctl", lambda *a, **k: self.calls.append(tuple(map(str, a))) or True)
        self.addCleanup(setattr, rm, "_wpctl", orig)
        self.rec = self.FakeReconciler()
        self.w = rm.VolumeWatcher(self.rec, rm.threading.Event())

    def feed(self, *batches):
        proc = FakeProc("".join(json.dumps(b, indent=2) + "\n" for b in batches))
        self.w._follow(proc)
        self.assertTrue(proc.waited)

    def test_initial_dump_syncs_and_sets_default(self):
        self.feed(pw_graph_objs())
        self.assertEqual(self.rec.volumes, [rm.SYNC])
        self.assertIn(("set-default", "85"), self.calls)  # default was the raw HDMI sink

    def test_slider_moves_become_jobs_and_echo_is_ignored(self):
        self.feed(pw_graph_objs(default=rm.PW_SINK))
        self.assertEqual(self.calls, [])  # nothing to fix
        self.feed(pw_graph_objs(default=rm.PW_SINK), [pw_node(85, rm.PW_SINK, volumes=(0.064, 0.064))])
        self.assertEqual(self.rec.volumes[-1], (40, False))
        # TV -> PC: our own write comes back as an event and must not bounce to the TV
        n = len(self.rec.volumes)
        self.assertTrue(self.w.set_pc(21, True))
        self.assertEqual(self.calls[-2:], [("set-mute", "85", "1"), ("set-volume", "85", "0.21")])
        self.w._events([("volume", 40, True)])   # intermediate: set-mute landed first
        self.w._events([("volume", 21, True)])   # the value we wrote
        self.assertEqual(len(self.rec.volumes), n)
        self.w._events([("volume", 27, True)])   # a real move afterwards
        self.assertEqual(self.rec.volumes[-1], (27, True))

    def test_noop_sync_does_not_swallow_the_next_key(self):
        self.feed(pw_graph_objs(default=rm.PW_SINK))  # slider at 15, unmuted
        n = len(self.calls)
        self.assertTrue(self.w.set_pc(15, False))   # TV says 15 too: nothing to write
        self.assertEqual(len(self.calls), n)
        self.w._events([("volume", 16, False)])    # Vol+ right after: must reach the TV
        self.assertEqual(self.rec.volumes[-1], (16, False))
        # an unrelated value during an armed echo is the user, not an intermediate
        self.w.set_pc(40, True)
        self.w._events([("volume", 33, False)])
        self.assertEqual(self.rec.volumes[-1], (33, False))
        # a failed wpctl arms nothing
        orig = rm._wpctl
        setattr(rm, "_wpctl", lambda *a, **k: False)
        self.addCleanup(setattr, rm, "_wpctl", orig)
        self.assertFalse(self.w.set_pc(50, False))
        self.w._events([("volume", 17, False)])
        self.assertEqual(self.rec.volumes[-1], (17, False))

    def test_default_rule_never_steals_a_headset(self):
        self.feed(pw_graph_objs(default="bluez_output.headset"))
        self.assertNotIn(("set-default", "85"), self.calls)
        self.w.graph.default = ""          # no default at all -> take it
        self.w._events([("default", "")])
        self.assertIn(("set-default", "85"), self.calls)

    def test_becoming_default_fixes_a_pre_attenuated_hdmi_sink(self):
        objs = pw_graph_objs()  # default is the raw HDMI sink, at 40% muted
        objs[0] = pw_node(80, HDMI, volumes=(0.064, 0.064), mute=True,
                          **{"device.api": "alsa", "node.nick": "Roku TV"})
        self.feed(objs)
        self.assertIn(("set-default", "85"), self.calls)
        self.assertNotIn(("set-volume", "80", "1.0"), self.calls)  # not ours yet
        self.feed(objs, [pw_default(rm.PW_SINK)])  # WirePlumber confirms: now it is
        self.assertEqual(self.calls[-2:], [("set-volume", "80", "1.0"), ("set-mute", "80", "0")])

    def test_user_choice_of_raw_hdmi_is_respected_once_we_were_default(self):
        self.feed(pw_graph_objs(default=rm.PW_SINK))
        self.feed(pw_graph_objs(default=rm.PW_SINK), [pw_default(HDMI)])  # user picked raw HDMI
        self.assertNotIn(("set-default", "85"), self.calls)

    def test_removal_before_the_initial_dump_does_not_warn(self):
        with self.assertNoLogs(rm.log, level="WARNING"):
            self.feed([{"id": 7, "info": None}], pw_graph_objs())

    def test_hdmi_unity_guard_only_when_we_are_default_and_once(self):
        self.feed(pw_graph_objs(default=rm.PW_SINK))
        self.w._events([("hdmi", 80, 40, True)])
        self.assertEqual(self.calls[-2:], [("set-volume", "80", "1.0"), ("set-mute", "80", "0")])
        self.w._events([("hdmi", 80, 40, False)])  # same node again: do not fight the user
        self.assertEqual(len(self.calls), 2)
        self.w._events([("hdmi", 88, 40, False)])  # the node came back (new id): fix again
        self.assertEqual(len(self.calls), 4)
        self.calls.clear()
        self.w.graph.default = HDMI  # user picked the raw HDMI output on purpose
        self.w._events([("hdmi", 90, 40, False)])
        self.assertEqual(self.calls, [])

    def test_missing_sink_warns_once_and_set_pc_fails_gracefully(self):
        with self.assertLogs(rm.log, level="WARNING") as cm:
            self.feed(pw_graph_objs(with_sink=False))
        self.assertTrue(any("install.sh --volume" in m for m in cm.output))
        self.assertEqual(self.rec.volumes, [])
        self.assertFalse(self.w.set_pc(10, False))

    def test_batches_survive_garbage_and_nested_brackets(self):
        text = "[\n  {\"id\": 1, \"info\": null}\n]\nnot json\n]\n" + json.dumps(pw_graph_objs(), indent=2) + "\n"
        self.w._follow(FakeProc(text))
        self.assertIsNotNone(self.w.graph.sink)


class ReconcilerVolumeTests(unittest.TestCase):
    class FakeRoku:
        """A TV: volume/mute state, keys act like a Roku TV, everything recorded."""

        def __init__(self, vol=15, muted=False, app="tvinput.hdmi3", step=1, status=200):
            self.vol, self.muted, self.app, self.step, self.status = vol, muted, app, step, status
            self.keys = []
            self.reads = 0
            self.fail = None  # EcpError to raise from every call

        def active_app(self, timeout=None):
            if self.fail:
                raise self.fail
            return self.app, "Some App"

        def audio_device(self, timeout=None):
            if self.fail:
                raise self.fail
            self.reads += 1
            return self.vol, self.muted

        def keypress(self, key, timeout=None):
            self.keys.append(key)
            if self.status != 200:
                return self.status
            if key == "VolumeUp":
                self.vol = min(100, self.vol + self.step)
                self.muted = False
            elif key == "VolumeDown":
                self.vol = max(0, self.vol - self.step)
            elif key == "VolumeMute":
                self.muted = not self.muted
            if self.vol == 0:
                self.muted = True
            return 200

    def setUp(self):
        orig = rm.VOLUME_SETTLE_S
        setattr(rm, "VOLUME_SETTLE_S", 0.0)
        self.addCleanup(setattr, rm, "VOLUME_SETTLE_S", orig)
        self.cfg = rm.Config({"ROKU_TV_IP": "192.0.2.40", "ROKU_VOLUME": "true"})
        self.pc = []

    def make(self, roku, tv_on=True):
        rec = rm.Reconciler(roku, self.cfg, pc_volume=lambda p, m: self.pc.append((p, m)) or True)
        rec.tv_on = tv_on
        return rec

    def test_pc_to_tv_converges_in_one_round(self):
        tv = self.FakeRoku(vol=15)
        rec = self.make(tv)
        with self.assertLogs(rm.log, level="INFO") as cm:
            rec._reconcile_volume((40, False), lambda: False)
        self.assertEqual((tv.vol, tv.muted), (40, False))
        self.assertEqual(tv.keys, ["VolumeUp"] * 25)
        self.assertEqual(tv.reads, 2)
        self.assertTrue(any("TV volume 15 -> 40" in m for m in cm.output))

    def test_two_per_press_tv_settles_instead_of_oscillating(self):
        tv = self.FakeRoku(vol=10, step=2)
        rec = self.make(tv)
        rec._reconcile_volume((16, False), lambda: False)
        self.assertEqual(tv.vol, 16)  # 10 -> 22 (learn: 2 per key) -> 16
        self.assertEqual(tv.keys, ["VolumeUp"] * 6 + ["VolumeDown"] * 3)
        tv = self.FakeRoku(vol=10, step=2)
        rec = self.make(tv)
        with self.assertLogs(rm.log, level="WARNING") as cm:
            rec._reconcile_volume((15, False), lambda: False)  # unreachable on such a TV
        self.assertLessEqual(abs(tv.vol - 15), 1)
        self.assertLessEqual(len(tv.keys), 8)
        self.assertTrue(any("settled" in m for m in cm.output))

    def test_dropped_keys_are_resent(self):
        class Droppy(self.FakeRoku):
            def keypress(self, key, timeout=None):
                if len(self.keys) in (2, 5, 9):  # a few keys lost on the wire
                    self.keys.append(key)
                    return 200
                return super().keypress(key, timeout)

        tv = Droppy(vol=0)
        rec = self.make(tv)
        rec._reconcile_volume((30, False), lambda: False)
        self.assertEqual(tv.vol, 30)

    def test_stuck_tv_gives_up_with_one_warning(self):
        tv = self.FakeRoku(vol=10, step=0)
        rec = self.make(tv)
        with self.assertLogs(rm.log, level="WARNING") as cm:
            rec._reconcile_volume((20, False), lambda: False)
        self.assertEqual(len(tv.keys), 10 * rm.VOLUME_ROUNDS)
        self.assertTrue(any("settled" in m for m in cm.output))

    def test_mute_then_unmute_keeps_the_slider_value(self):
        tv = self.FakeRoku(vol=15)
        rec = self.make(tv)
        rec._reconcile_volume((15, True), lambda: False)
        self.assertEqual((tv.vol, tv.muted), (15, True))
        rec._reconcile_volume((10, True), lambda: False)   # PC went down while muted: mute only
        self.assertEqual((tv.vol, tv.muted), (15, True))
        rec._reconcile_volume((10, False), lambda: False)  # unmute applies the pending value
        self.assertEqual((tv.vol, tv.muted), (10, False))
        self.assertEqual(tv.keys, ["VolumeMute"] + ["VolumeDown"] * 5 + ["VolumeMute"])

    def test_sync_sets_the_pc_and_never_presses_keys(self):
        tv = self.FakeRoku(vol=33, muted=True)
        rec = self.make(tv, tv_on=False)  # even while the TV is (believed) off: reading is harmless
        rec._reconcile_volume(rm.SYNC, lambda: False)
        self.assertEqual(self.pc, [(33, True)])
        self.assertEqual(tv.keys, [])

    def test_gates(self):
        tv = self.FakeRoku(vol=15)
        rec = self.make(tv, tv_on=False)
        rec._reconcile_volume((40, False), lambda: False)
        self.assertEqual(tv.keys, [])                   # TV believed off
        rec.tv_on = True
        tv.app = "tvinput.dtv"
        rec._reconcile_volume((40, False), lambda: False)
        self.assertEqual(tv.keys, [])                   # shared TV on another input
        tv.app = "tvinput.hdmi3"
        tv.status = 202
        rec._reconcile_volume((40, False), lambda: False)
        self.assertEqual(tv.keys, ["VolumeUp"])         # standby: first key is ignored, stop there
        tv.status = 200
        tv.fail = rm.EcpUnreachable("timed out")
        with self.assertLogs(rm.log, level="INFO") as cm:
            rec._reconcile_volume((40, False), lambda: False)
            rec._reconcile_volume((41, False), lambda: False)
        self.assertEqual(len([m for m in cm.output if "not delivered" in m]), 1)  # once per streak

    def test_stale_aborts_between_keys(self):
        tv = self.FakeRoku(vol=0)
        rec = self.make(tv)
        rec._reconcile_volume((50, False), lambda: len(tv.keys) >= 5)
        self.assertEqual(len(tv.keys), 5)

    def test_request_volume_leaves_the_power_path_alone(self):
        rec = self.make(self.FakeRoku())
        gen, done = rec.gen, rec.done.is_set()
        rec.request_volume((10, False))
        self.assertEqual(rec.gen, gen)
        self.assertEqual(rec.done.is_set(), done)
        self.assertEqual(rec.volume, (10, False))

    def test_volume_job_runs_on_the_thread(self):
        tv = self.FakeRoku(vol=15)
        rec = self.make(tv)
        rec.start()
        rec.request_volume((20, False))
        for _ in range(100):
            if tv.vol == 20:
                break
            rm.time.sleep(0.01)
        self.assertEqual(tv.vol, 20)

    def test_power_goes_first_and_its_sync_supersedes_a_stale_slider_value(self):
        order = []
        tv = self.FakeRoku(vol=15)
        rec = rm.Reconciler(tv, self.cfg, pc_volume=lambda p, m: order.append(("sync", p, m)) or True)
        orig_on = rm.ensure_on
        setattr(rm, "ensure_on", lambda roku, cfg, stale=None: order.append("power") or True)
        self.addCleanup(setattr, rm, "ensure_on", orig_on)
        with rec.cv:  # queue both before the thread can pick either
            rec.request_volume((20, False))  # moved while the TV was still coming on
            rec.request(rm.ON)
        rec.start()
        for _ in range(200):
            if len(order) >= 2:
                break
            rm.time.sleep(0.01)
        # power first; then the TV wins at power-on (the stale 20% is not sent)
        self.assertEqual(order, ["power", ("sync", 15, False)])
        self.assertEqual(tv.keys, [])

    def test_off_clears_tv_on_and_on_requests_sync(self):
        class Quiet(self.FakeRoku):
            def power_mode(self, timeout=None):
                return "Ready"

        rec = self.make(Quiet())
        rec._reconcile(rm.OFF, urgent=True, stale=lambda: False)
        self.assertFalse(rec.tv_on)
        orig_on = rm.ensure_on
        setattr(rm, "ensure_on", lambda roku, cfg, stale=None: True)
        self.addCleanup(setattr, rm, "ensure_on", orig_on)
        rec._reconcile(rm.ON, urgent=False, stale=lambda: False)
        self.assertTrue(rec.tv_on)
        self.assertEqual(rec.volume, rm.SYNC)


class VolumeConfigTests(unittest.TestCase):
    def test_knob(self):
        self.assertFalse(rm.Config({"ROKU_TV_IP": "192.0.2.40"}).volume)
        self.assertTrue(rm.Config({"ROKU_TV_IP": "192.0.2.40", "ROKU_VOLUME": "yes"}).volume)


if __name__ == "__main__":
    unittest.main()
