"""Unit tests for the pure helpers. No network, no gi, no real sysfs —
everything CI can actually prove about the logic."""

import os
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


if __name__ == "__main__":
    unittest.main()
