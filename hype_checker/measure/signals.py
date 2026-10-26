"""Star-count signals ported from Starguard thresholds (Apache-2.0, see docs/starguard-review.md)."""

from statistics import mean, median

DAY_S = 86400
WINDOW_DAYS = 28
MAD_MULTIPLIER = 3 * 1.48
MIN_WINDOW_STARS = 30
GROWTH_MULTIPLIER = 1 + 300 / 100
FORK_STAR_SUSPICIOUS = 0.5
FORK_STAR_CAUTION = 0.25


def daily_counts(history, until=None):
    days = []
    for week in sorted(history, key=lambda w: w["week"]):
        for offset, count in enumerate(week["days"]):
            day_ts = week["week"] + offset * DAY_S
            if until is None or day_ts <= until:
                days.append((day_ts, count))
    return days


def _spike(index, count, threshold, rule):
    return {"index": index, "count": count, "threshold": round(threshold, 2), "rule": rule}


def _threshold(window):
    if sum(window) < MIN_WINDOW_STARS:
        return max(1.0, mean(window)) * GROWTH_MULTIPLIER, "growth_percent"
    center = median(window)
    mad = median(abs(x - center) for x in window) or 1.0
    return center + MAD_MULTIPLIER * mad, "mad"


def mad_spikes(counts):
    if len(counts) <= WINDOW_DAYS:
        return None
    spikes = []
    for i in range(WINDOW_DAYS, len(counts)):
        threshold, rule = _threshold(counts[i - WINDOW_DAYS:i])
        if counts[i] > threshold:
            spikes.append(_spike(i, counts[i], threshold, rule))
    return spikes


def fork_star_ratio_flag(forks, stars):
    if stars <= 0:
        return None
    ratio = forks / stars
    if ratio >= FORK_STAR_SUSPICIOUS:
        return "suspicious"
    if ratio >= FORK_STAR_CAUTION:
        return "caution"
    return "normal"


def burst_summary(counts):
    spikes = mad_spikes(counts)
    if spikes is None:
        return None
    total = sum(counts)
    spike_stars = sum(s["count"] for s in spikes)
    return {
        "spike_days": len(spikes),
        "peak_count": max((s["count"] for s in spikes), default=0),
        "peak_ratio": max((s["count"] / s["threshold"] for s in spikes), default=0),
        "spike_star_share": spike_stars / total if total else 0.0,
    }
