from datetime import date, datetime, timedelta, timezone

from custom_components.ckw.stats import chunks, cumulative

UTC = timezone.utc


def test_cumulative_running_total_and_base():
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    rows = cumulative([(t0 + timedelta(hours=i), 0.5) for i in range(3)], base=10.0)
    assert [r.sum for r in rows] == [10.5, 11.0, 11.5]


def test_cumulative_duplicates_last_wins_and_sorted():
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    rows = cumulative([(t0 + timedelta(hours=1), 1.0), (t0, 2.0), (t0, 3.0)])
    assert [(r.start, r.sum) for r in rows] == [(t0, 3.0), (t0 + timedelta(hours=1), 4.0)]


def test_chunks_split_at_month_boundaries():
    assert chunks(date(2026, 10, 30), date(2026, 12, 2)) == [
        (date(2026, 10, 30), date(2026, 10, 31)),
        (date(2026, 11, 1), date(2026, 11, 30)),
        (date(2026, 12, 1), date(2026, 12, 2)),
    ]
    assert chunks(date(2026, 11, 8), date(2026, 11, 8)) == [(date(2026, 11, 8), date(2026, 11, 8))]
    assert chunks(date(2026, 11, 9), date(2026, 11, 8)) == []
