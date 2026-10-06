import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netops_ai import netbox, topology
from netops_ai.api import dashboard


class TestTopologyCache(unittest.TestCase):
    def setUp(self):
        topology._NETBOX_CACHE.update(key=None, at=0.0, data=None)

    def test_second_call_within_ttl_does_not_hit_netbox(self):
        calls = []

        def fake():
            calls.append(1)
            return {"A": object()}

        with mock.patch.object(netbox, "netbox_enabled", return_value=True), \
                mock.patch.object(netbox, "load_topology_from_netbox", fake):
            first = topology.load_topology()
            second = topology.load_topology()
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)

    def test_netbox_failure_falls_back_to_last_good_and_says_so(self):
        good = {"A": object()}
        state = {"fail": False}

        def fake():
            if state["fail"]:
                raise netbox.NetBoxError("timeout")
            return good

        with mock.patch.object(netbox, "netbox_enabled", return_value=True), \
                mock.patch.object(netbox, "load_topology_from_netbox", fake), \
                mock.patch.object(topology, "_cache_ttl", return_value=0.0), \
                mock.patch("time.sleep"):
            self.assertIs(topology.load_topology(), good)
            state["fail"] = True
            self.assertIs(topology.load_topology(), good)
        self.assertEqual(topology.LAST_SOURCE, "netbox")
        self.assertIn("先用", topology.LAST_NETBOX_ERROR)
        self.assertIn("timeout", topology.LAST_NETBOX_ERROR)

    def test_one_transient_failure_is_retried(self):
        seq = iter([netbox.NetBoxError("blip"), {"A": object()}])

        def fake():
            r = next(seq)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(netbox, "netbox_enabled", return_value=True), \
                mock.patch.object(netbox, "load_topology_from_netbox", fake), \
                mock.patch("time.sleep"):
            self.assertIn("A", topology.load_topology())
        self.assertEqual(topology.LAST_NETBOX_ERROR, "")


class TestLoadAlertsCache(unittest.TestCase):
    def test_reuses_parse_until_a_record_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "alert-1.json").write_text(json.dumps({"eventid": "1"}), encoding="utf-8")
            with mock.patch.object(dashboard, "RECORDS_DIR", d):
                dashboard._ALERTS_CACHE.update(sig=None, rows=[])
                a = dashboard.load_alerts()
                b = dashboard.load_alerts()
                self.assertIs(a, b)
                (d / "alert-2.json").write_text(json.dumps({"eventid": "2"}), encoding="utf-8")
                c = dashboard.load_alerts()
                self.assertEqual(len(c), 2)


if __name__ == "__main__":
    unittest.main()


class TestNetboxWebhook(unittest.TestCase):
    def test_signature_helper(self):
        import hashlib
        import hmac

        from netops_ai.api.app import netbox_signature_ok

        body = b'{"model":"dcim.device"}'
        sig = "sha512=" + hmac.new(b"s3", body, hashlib.sha512).hexdigest()
        self.assertTrue(netbox_signature_ok("s3", body, sig))
        self.assertFalse(netbox_signature_ok("s3", body, "sha512=deadbeef"))
        self.assertTrue(netbox_signature_ok("", body, ""))  # 没配 secret 不校验

    def test_webhook_invalidates_cache_and_refresh_param_too(self):
        from fastapi.testclient import TestClient

        from netops_ai.api import app as app_mod

        topology._NETBOX_CACHE.update(key=1, at=0.0, data={"A": 1})
        with mock.patch.object(app_mod, "_env", return_value={}):
            r = TestClient(app_mod.app).post("/webhooks/netbox", json={"model": "dcim.cable"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["model"], "dcim.cable")
        self.assertIsNone(topology.cache_age())

        topology._NETBOX_CACHE.update(key=1, at=0.0, data={"A": 1})
        with mock.patch.object(app_mod.dashboard, "build_topology_view", return_value={"ok": True}), \
                mock.patch.object(app_mod.dashboard, "load_alerts", return_value=[]), \
                mock.patch.object(app_mod.dashboard, "build_incidents", return_value=[]):
            TestClient(app_mod.app).get("/api/topology?refresh=1")
        self.assertIsNone(topology.cache_age())

    def test_bad_signature_is_rejected(self):
        from fastapi.testclient import TestClient

        from netops_ai.api import app as app_mod

        with mock.patch.object(app_mod, "_env", return_value={"NETBOX_WEBHOOK_SECRET": "s3"}):
            r = TestClient(app_mod.app).post("/webhooks/netbox", json={"model": "x"}, headers={"X-Hub-Signature": "sha512=00"})
        self.assertEqual(r.status_code, 403)
