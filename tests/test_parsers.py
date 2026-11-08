import json
from datetime import datetime, timedelta, timezone

import pytest

from ckw_client import (
    ParseError,
    aggregate_hourly,
    parse_measurements,
    parse_monthly_peaks,
    parse_quarter_hour_csv,
)

UTC = timezone.utc


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# ------------------------------------------------------------------ CSV ----------
def test_csv_skips_missing_and_reads_valid_rows(fx):
    rows = parse_quarter_hour_csv(fx("quarter_hour_2026-11.csv"))
    assert len(rows) == 7 * 96  # 1-7 Nov are complete, 8 Nov onwards is '-'
    assert rows[0].start == utc(2026, 10, 31, 23, 0)  # 01.11. 00:00 CET
    assert rows[0].kwh == 0.033
    assert rows[-1].start == utc(2026, 11, 7, 22, 45)  # 07.11. 23:45 CET
    assert rows[-1].kwh == 0.034
    assert all(r.kwh >= 0 for r in rows)


def test_csv_missing_value_is_not_zero():
    text = "Zeitraum;Energieverbrauch (kWh)\n01.11.2026 00:00;0.1\n01.11.2026 00:15;-\n01.11.2026 00:30;0.2\nTotal;0.3\n"
    rows = parse_quarter_hour_csv(text)
    assert [r.kwh for r in rows] == [0.1, 0.2]
    assert rows[1].start - rows[0].start == timedelta(minutes=30)


def test_csv_decimal_comma_and_float_noise():
    text = "Zeitraum;Energieverbrauch (kWh)\n01.11.2026 00:00;0,25\n01.11.2026 00:15;0.032999999821186066\n"
    assert [r.kwh for r in parse_quarter_hour_csv(text)] == [0.25, 0.033]


@pytest.mark.parametrize(
    "text",
    ["", "garbage", "Zeitraum;Leistung (kW)\n01.11.2026 00:00;1\n", "Zeitraum;Energieverbrauch (kWh)\nfoo;1\n"],
)
def test_csv_rejects_unexpected_structure(text):
    with pytest.raises(ParseError):
        parse_quarter_hour_csv(text)


def test_csv_autumn_dst_repeated_hour_is_monotonic():
    # 25 Oct 2026: 02:00-02:59 happens twice (CEST then CET)
    rows = ["01:45", "02:00", "02:15", "02:30", "02:45", "02:00", "02:15", "02:30", "02:45", "03:00"]
    text = "Zeitraum;Energieverbrauch (kWh)\n" + "".join(f"25.10.2026 {t};0.1\n" for t in rows)
    got = parse_quarter_hour_csv(text)
    starts = [r.start for r in got]
    assert starts[0] == utc(2026, 10, 24, 23, 45)
    assert starts[1] == utc(2026, 10, 25, 0, 0)  # first 02:00 = CEST
    assert starts[5] == utc(2026, 10, 25, 1, 0)  # second 02:00 = CET
    assert starts[-1] == utc(2026, 10, 25, 2, 0)  # 03:00 CET
    assert all(b - a == timedelta(minutes=15) for a, b in zip(starts, starts[1:]))


def test_aggregate_hourly_sums_complete_hours_only(fx):
    rows = parse_quarter_hour_csv(fx("quarter_hour_2026-11.csv"))
    hours = aggregate_hourly(rows)
    assert len(hours) == 7 * 24
    assert hours[0].start == utc(2026, 10, 31, 23, 0)
    assert hours[0].kwh == round(0.033 + 0.031 + 0.05 + 0.046, 4)
    # drop an incomplete hour
    assert len(aggregate_hourly(rows[:-1])) == 7 * 24 - 1


# ------------------------------------------------------------------ JSON ---------
def test_hourly_json(fx):
    data = json.loads(fx("api_hourly_2026-11-07.json"))
    ms = parse_measurements(data)
    assert len(ms) == 24
    first = ms[0]
    assert first.start == utc(2026, 11, 6, 23, 0)
    assert (first.kwh, first.peak_kw, first.energy_cost_chf) == (0.11, 0.16, 0.03)
    charged = [m for m in ms if m.power_charge_chf is not None]
    assert len(charged) == 1 and charged[0].power_charge_chf == 2.73
    assert charged[0].start == utc(2026, 11, 7, 19, 0)  # 20:00 local, hour of the 20:15 peak


def test_daily_json_nodata_is_none(fx):
    ms = parse_measurements(json.loads(fx("api_daily_2026-11.json")))
    assert len(ms) == 30
    by_day = {m.start.date().isoformat(): m for m in ms}  # UTC date of local midnight = previous day
    d7 = by_day["2026-11-06"]  # 07.11. 00:00 local = 06.11. 23:00Z
    assert (d7.kwh, d7.peak_kw, d7.energy_cost_chf, d7.power_charge_chf) == (4.61, 1.68, 1.28, 2.73)
    d8 = by_day["2026-11-07"]
    assert (d8.kwh, d8.peak_kw, d8.energy_cost_chf) == (None, None, None)


def test_monthly_peak(fx):
    peaks = parse_monthly_peaks(json.loads(fx("api_daily_2026-11.json")))
    assert len(peaks) == 1
    assert peaks[0].at == utc(2026, 11, 7, 19, 15)
    assert (peaks[0].kw, peaks[0].charge_chf) == (1.68, 2.73)


def test_json_error_status_and_structure():
    with pytest.raises(ParseError):
        parse_measurements({"status": {"type": "ERROR", "message": "boom"}, "body": {}})
    with pytest.raises(ParseError):
        parse_measurements({"nope": 1})
    with pytest.raises(ParseError):
        parse_measurements({"status": {"type": "SUCCESS"}, "body": {}})


def test_csv_and_json_agree_for_a_day(fx):
    csv_hours = aggregate_hourly(parse_quarter_hour_csv(fx("quarter_hour_2026-11.csv")))
    day = [h for h in csv_hours if utc(2026, 11, 6, 23) <= h.start < utc(2026, 11, 7, 23)]
    json_ms = parse_measurements(json.loads(fx("api_hourly_2026-11-07.json")))
    assert len(day) == len(json_ms) == 24
    assert abs(sum(h.kwh for h in day) - sum(m.kwh for m in json_ms)) < 0.05  # JSON is rounded to 2 decimals
    for h, m in zip(day, json_ms):
        assert h.start == m.start
        assert abs(h.kwh - m.kwh) <= 0.005 + 1e-9
