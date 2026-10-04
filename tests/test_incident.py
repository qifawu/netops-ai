from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from netops_ai.incident import (
    Incident,
    compute_fingerprint,
    get_incident,
    make_incident_id,
    recent_incidents,
    same_fingerprint_count,
    save_incident,
)


def _incident(
    incident_id: str,
    fingerprint: str,
    hostid: str = "10683",
    first_clock: int = 100,
    last_clock: int = 120,
    event_ids: list[str] | None = None,
    root_event_id: str | None = None,
    analysis: dict | None = None,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        fingerprint=fingerprint,
        hostid=hostid,
        first_clock=first_clock,
        last_clock=last_clock,
        event_ids=event_ids or [incident_id],
        root_event_id=root_event_id,
        analysis=analysis,
        created_at="2026-09-19T12:00:00+00:00",
    )


class TestFingerprint(unittest.TestCase):
    def test_同样输入同样输出(self):
        fp1 = compute_fingerprint("10683", ["38369", "38370"], ["Gi0/1"])
        fp2 = compute_fingerprint("10683", ["38369", "38370"], ["Gi0/1"])
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_trigger和interface顺序不影响结果(self):
        fp1 = compute_fingerprint("10683", ["38370", "38369"], ["Gi0/1", "Gi0/0"])
        fp2 = compute_fingerprint("10683", ["38369", "38370"], ["Gi0/0", "Gi0/1"])
        self.assertEqual(fp1, fp2)

    def test_hostid仍然参与fingerprint(self):
        fp1 = compute_fingerprint("10683", ["38369"], ["Gi0/1"])
        fp2 = compute_fingerprint("99999", ["38369"], ["Gi0/1"])
        self.assertNotEqual(fp1, fp2)


class TestIncidentStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "incidents.db"

    def test_save和get_round_trip(self):
        fp = compute_fingerprint("10683", ["38369"], ["Gi0/1"])
        incident = _incident(
            make_incident_id(fp, 100),
            fp,
            event_ids=["52152", "52229"],
            root_event_id="52152",
            analysis={"root_cause": "Gi0/1 shutdown", "confidence": "high"},
        )
        save_incident(incident, interfaces=["Gi0/1"], db_path=self.db_path)

        loaded = get_incident(incident.incident_id, db_path=self.db_path)

        self.assertEqual(loaded, incident)

    def test_save是upsert并替换接口索引(self):
        fp = compute_fingerprint("10683", ["38369"], ["Gi0/1"])
        incident = _incident("incident-1", fp, first_clock=100, last_clock=120)
        save_incident(incident, interfaces=["Gi0/1"], db_path=self.db_path)

        updated = _incident("incident-1", fp, first_clock=100, last_clock=180, event_ids=["52152", "52229"])
        save_incident(updated, interfaces=["Gi0/2"], db_path=self.db_path)

        self.assertEqual(get_incident("incident-1", db_path=self.db_path), updated)
        self.assertEqual(recent_incidents("10683", "Gi0/1", 0, db_path=self.db_path), [])
        self.assertEqual([i.incident_id for i in recent_incidents("10683", "Gi0/2", 0, db_path=self.db_path)], ["incident-1"])

    def test_recent_incidents按host_interface_since过滤并倒序(self):
        fp_gi01 = compute_fingerprint("10683", ["38369"], ["Gi0/1"])
        fp_gi02 = compute_fingerprint("10683", ["38369"], ["Gi0/2"])
        save_incident(_incident("old", fp_gi01, last_clock=90), interfaces=["Gi0/1"], db_path=self.db_path)
        save_incident(_incident("match-older", fp_gi01, last_clock=150), interfaces=["Gi0/1"], db_path=self.db_path)
        save_incident(_incident("match-newer", fp_gi01, last_clock=200), interfaces=["Gi0/1"], db_path=self.db_path)
        save_incident(_incident("wrong-interface", fp_gi02, last_clock=210), interfaces=["Gi0/2"], db_path=self.db_path)
        save_incident(_incident("wrong-host", fp_gi01, hostid="99999", last_clock=220), interfaces=["Gi0/1"], db_path=self.db_path)

        found = recent_incidents("10683", "Gi0/1", 100, db_path=self.db_path)

        self.assertEqual([incident.incident_id for incident in found], ["match-newer", "match-older"])

    def test_same_fingerprint_count按fingerprint和since过滤(self):
        fp = compute_fingerprint("10683", ["38369"], ["Gi0/1"])
        other_fp = compute_fingerprint("10683", ["38105"], [])
        save_incident(_incident("old", fp, last_clock=90), interfaces=["Gi0/1"], db_path=self.db_path)
        save_incident(_incident("recent-1", fp, last_clock=150), interfaces=["Gi0/1"], db_path=self.db_path)
        save_incident(_incident("recent-2", fp, last_clock=200), interfaces=["Gi0/1"], db_path=self.db_path)
        save_incident(_incident("other", other_fp, last_clock=220), interfaces=[], db_path=self.db_path)

        self.assertEqual(same_fingerprint_count(fp, 100, db_path=self.db_path), 2)



if __name__ == "__main__":
    unittest.main()
