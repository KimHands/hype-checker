import json
import stat
import tempfile
import unittest
from pathlib import Path

from hype_checker.v2.limits import Limits

DAY = "2026-11-01"


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "limits"

    def test_per_sender_limit(self):
        limits = Limits(self.root, per_sender=2)
        limits.record("a", DAY, 0.01)
        self.assertTrue(limits.allow("a", DAY))
        limits.record("a", DAY, None)
        self.assertFalse(limits.allow("a", DAY))
        self.assertTrue(limits.allow("b", DAY))

    def test_total_limit(self):
        limits = Limits(self.root, total=2)
        limits.record("a", DAY, 0)
        limits.record("b", DAY, 0)
        self.assertFalse(limits.allow("c", DAY))

    def test_usd_limit_and_none_counts_as_zero_dollars(self):
        limits = Limits(self.root, usd=1.0)
        limits.record("a", DAY, None)
        self.assertTrue(limits.allow("a", DAY))
        limits.record("a", DAY, 1.0)
        self.assertFalse(limits.allow("b", DAY))

    def test_new_day_starts_fresh(self):
        limits = Limits(self.root, per_sender=1)
        limits.record("a", DAY, 0)
        self.assertTrue(limits.allow("a", "2026-11-02"))

    def test_limits_survive_restart(self):
        Limits(self.root, per_sender=1).record("a", DAY, 0.5)
        self.assertFalse(Limits(self.root, per_sender=1).allow("a", DAY))
        path = self.root / f"{DAY}.json"
        self.assertEqual(json.loads(path.read_text())["senders"], {"a": 1})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_broken_file_fails_closed(self):
        limits = Limits(self.root)
        (self.root / f"{DAY}.json").write_text("{bad")
        self.assertFalse(limits.allow("a", DAY))

    def test_over_limit_notice_once_per_sender_per_day(self):
        limits = Limits(self.root, per_sender=0)
        self.assertTrue(limits.first_notice("a", DAY))
        self.assertFalse(limits.first_notice("a", DAY))
        self.assertFalse(Limits(self.root, per_sender=0).first_notice("a", DAY))
        self.assertTrue(limits.first_notice("b", DAY))
        self.assertTrue(limits.first_notice("a", "2026-11-02"))

    def test_record_keeps_notice_flags(self):
        limits = Limits(self.root)
        limits.first_notice("a", DAY)
        limits.record("b", DAY, 0)
        self.assertFalse(limits.first_notice("a", DAY))
        self.assertEqual(json.loads((self.root / f"{DAY}.json").read_text())["senders"], {"b": 1})

    def test_wrong_shape_fails_closed_without_raising(self):
        bad = [{"senders": {}}, {"total": "1", "usd": 0, "senders": {}}, {"total": 0, "usd": "0", "senders": {}},
               {"total": True, "usd": 0, "senders": {}}, {"total": 0, "usd": 0, "senders": {"a": "1"}},
               {"total": 0, "usd": 0, "senders": {}, "notified": "a"},
               {"total": 0, "usd": 0, "senders": {}, "notified": [1]}, [], "x"]
        for counts in bad:
            with self.subTest(counts=counts):
                limits = Limits(self.root)
                (self.root / f"{DAY}.json").write_text(json.dumps(counts))
                self.assertFalse(limits.allow("a", DAY))
                self.assertFalse(limits.first_notice("a", DAY))
                limits.record("a", DAY, 0.1)
                self.assertFalse(limits.allow("a", DAY))

    def test_file_without_notice_list_is_still_valid(self):
        limits = Limits(self.root)
        (self.root / f"{DAY}.json").write_text(json.dumps({"total": 1, "usd": 0.0, "senders": {"a": 1}}))
        self.assertTrue(limits.allow("a", DAY))
        self.assertTrue(limits.first_notice("a", DAY))


if __name__ == "__main__":
    unittest.main()
