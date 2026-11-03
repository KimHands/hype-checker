import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from hype_checker.v2.config import load_config
from hype_checker.v2.inbox import Inbox
from hype_checker.v2.limits import Limits
from hype_checker.v2.webhook import is_allowed, is_public, parse_shares
from hype_checker.v2.worker import INTERVAL_S, LIMIT_REACHED, POLL_S, SHARE_GUIDE, Runtime, process_pending, run_loop
from tests.test_pipeline import RUFLO_GH, RUFLO_HTTP, make_context
from tests.test_v2 import ACCOUNT, CONFIG, ME, NOW_S, text_share

STRANGER, FRIEND = "900000000000002", "900000000000003"
PUBLIC = replace(CONFIG, allowed_senders=frozenset({"*"}), unlimited_senders=frozenset({FRIEND}))
DAY = time.strftime("%Y-%m-%d", time.gmtime(NOW_S))


class AllowedTest(unittest.TestCase):
    def test_public_mode_takes_anyone_but_blank_and_my_account(self):
        self.assertTrue(is_public(PUBLIC))
        self.assertTrue(is_allowed(STRANGER, PUBLIC))
        self.assertFalse(is_allowed("", PUBLIC))
        self.assertFalse(is_allowed(None, PUBLIC))
        self.assertFalse(is_allowed(ACCOUNT, PUBLIC))

    def test_private_mode_still_drops_strangers_but_takes_unlimited(self):
        private = replace(CONFIG, unlimited_senders=frozenset({FRIEND}))
        self.assertFalse(is_public(private))
        self.assertTrue(is_allowed(ME, private))
        self.assertTrue(is_allowed(FRIEND, private))
        self.assertFalse(is_allowed(STRANGER, private))

    def test_webhook_queues_a_stranger_only_in_public_mode(self):
        self.assertEqual(text_share("m", "hi", sender=STRANGER), [])
        self.assertEqual(len(text_share("m", "hi", sender=STRANGER, config=PUBLIC)), 1)

    def test_config_reads_unlimited_senders(self):
        path = Path(tempfile.mkdtemp()) / "v2.env"
        path.write_text("IG_VERIFY_TOKEN=t\nIG_APP_SECRETS=a\nIG_ALLOWED_SENDERS=*\nIG_UNLIMITED_SENDERS=1, 2\n")
        os.chmod(path, 0o600)
        config, _ = load_config(path)
        self.assertTrue(is_public(config))
        self.assertEqual(config.unlimited_senders, frozenset({"1", "2"}))


class DefaultLimitsTest(unittest.TestCase):
    def test_two_per_account_sixty_overall(self):
        limits = Limits(Path(tempfile.mkdtemp()) / "limits")
        self.assertEqual((limits.per_sender, limits.total), (2, 60))


class PublicWorkerTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context(RUFLO_GH, RUFLO_HTTP)
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.limits = Limits(self.inbox.root / "limits")
        self.sent = []
        self.runtime = Runtime(limits=self.limits, sleep=lambda _: None)

    def send(self, recipient, text):
        self.sent.append((recipient, text))

    def run_with(self, *shares, check=None):
        for mid, text, sender in shares:
            self.inbox.enqueue(text_share(mid, text, sender=sender, config=PUBLIC)[0])
        with mock.patch("hype_checker.v2.worker.check", check or mock.DEFAULT) as checked:
            process_pending(self.inbox, self.ctx, PUBLIC, send=self.send, now_s=NOW_S, runtime=self.runtime)
        return checked

    def counts(self):
        path = self.inbox.root / "limits" / f"{DAY}.json"
        return json.loads(path.read_text()) if path.exists() else {"total": 0, "senders": {}}

    def test_small_talk_gets_one_guide_and_spends_nothing(self):
        checked = self.run_with(("a", "안녕하세요", STRANGER), ("b", "뭐 하는 계정이에요?", STRANGER))
        self.assertEqual(checked.call_count, 0)
        self.assertEqual(self.sent, [(STRANGER, SHARE_GUIDE)])
        self.assertEqual(self.counts()["total"], 0)
        self.assertTrue(self.limits.allow(STRANGER, DAY))

    def test_guide_does_not_use_up_the_limit_notice(self):
        self.runtime = replace(self.runtime, limits=Limits(self.inbox.root / "limits", per_sender=0))
        self.run_with(("a", "안녕", STRANGER), ("b", "https://github.com/ruvnet/ruflo", STRANGER))
        self.assertEqual([text for _, text in self.sent], [SHARE_GUIDE, LIMIT_REACHED])

    def test_registry_link_is_a_check_not_small_talk(self):
        self.run_with(("a", "https://github.com/ruvnet/ruflo", STRANGER))
        self.assertEqual(self.counts()["senders"], {STRANGER: 1})

    def test_third_share_of_the_day_is_refused(self):
        link = "https://github.com/ruvnet/ruflo"
        self.run_with(("a", link, STRANGER), ("b", link, STRANGER), ("c", link, STRANGER))
        self.assertEqual(self.counts()["senders"], {STRANGER: 2})
        self.assertEqual([t for _, t in self.sent].count(LIMIT_REACHED), 1)

    def test_unlimited_sender_is_never_limited_nor_counted(self):
        self.runtime = replace(self.runtime, limits=Limits(self.inbox.root / "limits", per_sender=0, total=0))
        self.run_with(("a", "https://github.com/ruvnet/ruflo", FRIEND), ("b", "자유 형식 소개글", FRIEND))
        self.assertNotIn(LIMIT_REACHED, [t for _, t in self.sent])
        self.assertEqual(self.counts()["total"], 0)

    def test_daily_cost_includes_extraction_tokens(self):
        chat = mock.Mock()
        chat.usage = [{"input_tokens": 999, "output_tokens": 999}]   # an earlier item: not this one's cost

        def spends(*_args, **_kwargs):
            chat.usage.append({"input_tokens": 1_000_000, "output_tokens": 0})
            raise RuntimeError("stop after extraction")
        self.ctx = replace(self.ctx, chat=chat)
        self.runtime = replace(self.runtime, prices=(0.5, 1.0))
        self.run_with(("a", "https://github.com/ruvnet/ruflo", STRANGER), check=spends)
        self.assertEqual(self.counts()["usd"], 0.5)


class PollingTest(unittest.TestCase):
    def setUp(self):
        self.inbox = Inbox(Path(tempfile.mkdtemp()))
        self.now, self.upkeep, self.sent = NOW_S, [], []

    def loop(self, on_tick, ticks):
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            self.now += seconds
            on_tick(len(slept))
        runtime = Runtime(limits=Limits(self.inbox.root / "limits"), sleep=sleep,
                          before_pass=lambda: self.upkeep.append(self.now))
        run_loop(self.inbox, make_context(RUFLO_GH, RUFLO_HTTP), PUBLIC, lambda r, t: self.sent.append((self.now, t)),
                 runtime=runtime, clock=lambda: self.now, stop=lambda: len(slept) >= ticks)
        return slept

    def test_new_share_starts_within_a_second_while_idle(self):
        def arrive(tick):
            if tick == 5:
                self.inbox.enqueue(text_share("a", "안녕", sender=STRANGER, config=PUBLIC)[0])
        self.loop(arrive, 8)
        self.assertEqual(self.sent, [(NOW_S + 5, SHARE_GUIDE)])

    def test_upkeep_stays_at_the_interval_while_idle(self):
        slept = self.loop(lambda tick: None, 61)
        self.assertEqual(set(slept), {POLL_S})
        self.assertEqual(self.upkeep, [NOW_S, NOW_S + INTERVAL_S, NOW_S + 2 * INTERVAL_S])


class PruneTest(unittest.TestCase):
    def test_sender_ids_in_limits_and_disputes_expire(self):
        inbox = Inbox(Path(tempfile.mkdtemp()))
        for name in ("limits", "disputes"):
            folder = inbox.root / name
            folder.mkdir()
            old = folder / "old.json"
            old.write_text("{}")
            os.utime(old, (1000, 1000))
            (folder / "new.json").write_text("{}")
        inbox.prune(days=7)
        for name in ("limits", "disputes"):
            self.assertEqual([p.name for p in (inbox.root / name).iterdir()], ["new.json"])
