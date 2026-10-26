import unittest

from hype_checker.measure.signals import daily_counts, fork_star_ratio_flag, mad_spikes


class DailyCountsTest(unittest.TestCase):
    def test_flattens_weeks_oldest_first(self):
        history = [  # API order: newest week first
            {"week": 1792281600, "total": 3, "days": [1, 2, 0, 0, 0, 0, 0]},
            {"week": 1791676800, "total": 7, "days": [1, 1, 1, 1, 1, 1, 1]},
        ]
        days = daily_counts(history)
        self.assertEqual(len(days), 14)
        self.assertEqual(days[0], (1791676800, 1))
        self.assertEqual(days[7], (1792281600, 1))
        self.assertEqual(days[8], (1792281600 + 86400, 2))


class MadSpikesTest(unittest.TestCase):
    def test_flags_burst_over_flat_baseline(self):
        counts = [10, 12, 9, 11, 10, 13, 9] * 4 + [400]
        spikes = mad_spikes(counts)
        self.assertEqual([s["index"] for s in spikes], [28])

    def test_no_spike_in_steady_growth(self):
        counts = [10 + i % 3 for i in range(60)]
        self.assertEqual(mad_spikes(counts), [])

    def test_small_repo_uses_growth_percent(self):
        counts = [0, 1, 0, 1, 0, 0, 1, 0] * 4 + [9]
        spikes = mad_spikes(counts)
        self.assertEqual([s["index"] for s in spikes], [32])
        self.assertEqual(spikes[0]["rule"], "growth_percent")

    def test_too_short_history_is_unmeasurable(self):
        self.assertIsNone(mad_spikes([5, 6, 7]))


class ForkStarRatioTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(fork_star_ratio_flag(forks=60, stars=100), "suspicious")
        self.assertEqual(fork_star_ratio_flag(forks=30, stars=100), "caution")
        self.assertEqual(fork_star_ratio_flag(forks=10, stars=100), "normal")
        self.assertIsNone(fork_star_ratio_flag(forks=0, stars=0))


if __name__ == "__main__":
    unittest.main()


class BurstSummaryTest(unittest.TestCase):
    def test_summarizes_peak_and_share(self):
        from hype_checker.measure.signals import burst_summary
        counts = [10] * 28 + [400, 10]
        summary = burst_summary(counts)
        self.assertEqual(summary["spike_days"], 1)
        self.assertEqual(summary["peak_count"], 400)
        self.assertAlmostEqual(summary["peak_ratio"], 400 / 14.44, places=1)
        self.assertAlmostEqual(summary["spike_star_share"], 400 / 690, places=3)

    def test_no_spikes(self):
        from hype_checker.measure.signals import burst_summary
        summary = burst_summary([10] * 40)
        self.assertEqual(summary["spike_days"], 0)
        self.assertEqual(summary["peak_ratio"], 0)

    def test_unmeasurable(self):
        from hype_checker.measure.signals import burst_summary
        self.assertIsNone(burst_summary([1, 2]))
