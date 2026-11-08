from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.ckw.ckw_client import CannotConnect, ReauthRequired
from custom_components.ckw.const import DOMAIN
from custom_components.ckw.statistics_import import COST, ENERGY, PEAK, statistic_id

from .conftest import LOC

UTC = timezone.utc


@pytest.fixture(autouse=True)
async def _setup(recorder_mock, hass, freezer):
    await hass.config.async_set_time_zone("Europe/Zurich")
    freezer.move_to("2026-11-08 07:00:00+01:00")
    with patch("custom_components.ckw.coordinator.REQUEST_PAUSE", 0):
        yield


async def rows(hass, kind):
    await async_wait_recording_done(hass)
    sid = statistic_id(kind, LOC)
    found = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2020, 1, 1, tzinfo=UTC), None, {sid}, "hour", None, {"sum", "max", "state"}
    )
    return found.get(sid, [])


async def setup(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def state_of(hass, key):
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("sensor", DOMAIN, f"{LOC}_{key}")
    assert eid, key
    return hass.states.get(eid)


async def test_backfill_imports_statistics_and_sensors(hass, entry, patched_client, quarters):
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.state is ConfigEntryState.LOADED

    energy = await rows(hass, ENERGY)
    assert len(energy) == 7 * 24
    assert energy[-1]["sum"] == pytest.approx(sum(q.kwh for q in quarters), abs=1e-3)
    sums = [r["sum"] for r in energy]
    assert sums == sorted(sums)  # running total never decreases

    cost = await rows(hass, COST)
    assert len(cost) == 24 and cost[-1]["sum"] == pytest.approx(1.28, abs=0.05)
    peak = await rows(hass, PEAK)
    assert len(peak) == 24 and max(r["max"] for r in peak) == 1.68

    assert float(state_of(hass, "day_energy").state) == pytest.approx(4.611, abs=1e-3)
    assert state_of(hass, "day_energy").attributes["date"] == "2026-11-07"
    assert float(state_of(hass, "day_peak").state) == 1.68
    assert float(state_of(hass, "day_cost").state) == pytest.approx(1.28, abs=0.05)
    assert float(state_of(hass, "month_peak").state) == 1.68
    assert float(state_of(hass, "month_power_charge").state) == 2.73
    assert state_of(hass, "last_data").state.startswith("2026-11-07T23:00")  # end of newest hour, UTC


async def test_refresh_is_idempotent_and_applies_corrections(hass, entry, patched_client, quarters):
    await setup(hass, entry)
    coordinator = entry.runtime_data
    before = [(r["start"], r["sum"]) for r in await rows(hass, ENERGY)]

    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert [(r["start"], r["sum"]) for r in await rows(hass, ENERGY)] == before  # no double counting

    # the portal corrects one quarter hour on 6 Nov by +1 kWh -> later totals shift by exactly 1
    idx = next(i for i, q in enumerate(patched_client.quarters) if q.start == datetime(2026, 11, 6, 12, 0, tzinfo=UTC))
    patched_client.quarters[idx] = replace(patched_client.quarters[idx], kwh=patched_client.quarters[idx].kwh + 1.0)
    await coordinator.async_refresh()
    after = await rows(hass, ENERGY)
    assert len(after) == len(before)
    assert after[-1]["sum"] == pytest.approx(before[-1][1] + 1.0, abs=1e-6)
    assert after[0]["sum"] == pytest.approx(before[0][1])  # untouched history


async def test_next_day_is_appended(hass, entry, patched_client, quarters, freezer):
    await setup(hass, entry)
    coordinator = entry.runtime_data
    first = await rows(hass, ENERGY)

    day7 = quarters[-96:]  # all 96 quarter hours of 7 Nov
    new = [replace(q, start=q.start + timedelta(days=1)) for q in day7]
    patched_client.quarters.extend(new)
    freezer.move_to("2026-11-09 07:00:00+01:00")
    await coordinator.async_refresh()

    after = await rows(hass, ENERGY)
    assert len(after) == len(first) + 24
    assert after[-1]["sum"] == pytest.approx(first[-1]["sum"] + sum(q.kwh for q in new), abs=1e-3)
    assert [r["sum"] for r in after[: len(first)]] == [r["sum"] for r in first]  # history unchanged
    # second run only asks for the overlap window, not the whole year again
    assert patched_client.calls[-1][1] >= datetime(2026, 11, 4).date()


async def test_backfill_is_chunked_by_month(hass, entry, patched_client):
    await setup(hass, entry)
    csv_calls = [c for c in patched_client.calls if c[0] == "csv"]
    assert len(csv_calls) == 13  # 2025-11-08 .. 2026-11-08
    assert csv_calls[0][1].isoformat() == "2025-11-08" and csv_calls[-1][2].isoformat() == "2026-11-08"
    assert all(c[1].month == c[2].month for c in csv_calls)


async def test_reauth_needed_during_first_import_starts_reauth_flow(hass, entry, patched_client):
    patched_client.error = ReauthRequired("invalid_grant")
    await setup(hass, entry)
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" and f["handler"] == DOMAIN for f in flows)
    assert state_of(hass, "day_energy").state == "unavailable"


async def test_reauth_needed_on_later_start_fails_setup(hass, entry, patched_client):
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    patched_client.error = ReauthRequired("invalid_grant")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_network_error_during_first_import_retries_later(hass, entry, patched_client):
    patched_client.error = CannotConnect("down")
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, "day_energy").state == "unavailable"
    assert entry.runtime_data._retry is not None  # tries again in an hour


async def test_network_error_on_later_start_retries_setup(hass, entry, patched_client):
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    patched_client.error = CannotConnect("down")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_stale_data_schedules_a_retry(hass, entry, patched_client, freezer):
    await setup(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator._retry is None  # newest day (7 Nov) is yesterday -> fine
    freezer.move_to("2026-11-12 07:00:00+01:00")
    await coordinator.async_refresh()
    assert coordinator._retry is not None


async def test_multi_month_backfill_keeps_running_total_continuous(hass, entry, patched_client, quarters):
    """History spread over several monthly chunks must form ONE continuous running total."""
    base = list(quarters)
    extra = []
    for days in (35, 70, 105):  # same week shifted into Oct, Sep, Aug
        extra += [replace(q, start=q.start - timedelta(days=days)) for q in base]
    patched_client.quarters = sorted(base + extra, key=lambda q: q.start)

    await setup(hass, entry)
    energy = await rows(hass, ENERGY)
    assert len(energy) == 4 * 7 * 24
    sums = [r["sum"] for r in energy]
    assert sums == sorted(sums), "running total restarted somewhere"
    assert sums[-1] == pytest.approx(sum(q.kwh for q in patched_client.quarters), abs=1e-2)


async def test_first_setup_does_not_wait_for_the_import(hass, entry, patched_client):
    import asyncio

    gate = asyncio.Event()
    original = patched_client.quarter_hours

    async def slow(contract, start, end):
        await gate.wait()
        return await original(contract, start, end)

    patched_client.quarter_hours = slow
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()  # does NOT wait for background tasks
    assert entry.state is ConfigEntryState.LOADED  # the setup dialog would be released here
    assert state_of(hass, "day_energy").state == "unavailable"

    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(state_of(hass, "day_energy").state) == pytest.approx(4.611, abs=1e-3)
    assert len(await rows(hass, ENERGY)) == 7 * 24


async def test_second_start_is_quick_and_blocking(hass, entry, patched_client):
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    patched_client.calls.clear()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()  # no background wait needed: history exists
    assert float(state_of(hass, "day_energy").state) == pytest.approx(4.611, abs=1e-3)
    assert len([c for c in patched_client.calls if c[0] == "csv"]) == 1  # only the recent window


async def test_diagnostics_show_stored_statistics(hass, entry, patched_client):
    from custom_components.ckw.diagnostics import async_get_config_entry_diagnostics

    await setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    stored = diag["stored_statistics"]
    assert stored["energy_consumption"]["hours"] == 168 and stored["energy_cost"]["hours"] == 24
    assert stored["energy_consumption"]["first"].startswith("2026-10-31T23:00")
    assert "RT-0" not in str(diag) and "user@example.org" not in str(diag)
    assert LOC not in str(diag["entry"])  # location id / unique_id are redacted


async def test_import_never_waits_for_recorder_commit(hass, entry, patched_client, quarters):
    """Regression: awaiting the recorder's commit during startup deadlocked Home Assistant's boot
    (the recorder holds commits until startup is done, startup waited for this integration)."""
    from unittest.mock import AsyncMock

    from homeassistant.components.recorder.core import Recorder
    from homeassistant.core import CoreState

    base = list(quarters)
    patched_client.quarters = sorted(
        base + [replace(q, start=q.start - timedelta(days=35)) for q in base], key=lambda q: q.start
    )  # two months -> two chunks that must be chained without touching the database in between
    hass.set_state(CoreState.starting)  # Home Assistant is still booting: the recorder commits nothing yet
    with patch.object(Recorder, "async_block_till_done", AsyncMock(side_effect=AssertionError("must not wait"))):
        await setup(hass, entry)
    hass.set_state(CoreState.running)
    await entry.runtime_data.async_refresh()  # once running, a new run first reads the committed data
    assert entry.state is ConfigEntryState.LOADED
    energy = await rows(hass, ENERGY)
    sums = [r["sum"] for r in energy]
    assert len(energy) == 2 * 7 * 24 and sums == sorted(sums)
    assert sums[-1] == pytest.approx(sum(q.kwh for q in patched_client.quarters), abs=1e-2)


def _with_older_weeks(quarters, shifts=(35, 70)):
    """The recorded week repeated into Oct and Sep, as if the portal had that history."""
    extra = []
    for days in shifts:
        extra += [replace(q, start=q.start - timedelta(days=days)) for q in quarters]
    return sorted(list(quarters) + extra, key=lambda q: q.start)


async def test_first_import_starts_at_supply_start(hass, entry, patched_client):
    from datetime import date

    patched_client.supply_start = date(2026, 8, 15)
    await setup(hass, entry)
    csv_calls = [c for c in patched_client.calls if c[0] == "csv"]
    assert [(c[1].isoformat(), c[2].isoformat()) for c in csv_calls] == [
        ("2026-08-15", "2026-08-31"), ("2026-09-01", "2026-09-30"),
        ("2026-10-01", "2026-10-31"), ("2026-11-01", "2026-11-08"),
    ]


async def test_import_history_service_adds_older_data_and_rebases_totals(hass, entry, patched_client, quarters):
    await setup(hass, entry)  # only the recorded November week exists
    assert len(await rows(hass, ENERGY)) == 7 * 24

    patched_client.quarters = _with_older_weeks(quarters)  # the portal "now" also has Sep and Oct
    patched_client.calls.clear()
    await hass.services.async_call(DOMAIN, "import_history", {"start_date": "2026-08-01"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    energy = await rows(hass, ENERGY)
    assert len(energy) == 3 * 7 * 24
    sums = [r["sum"] for r in energy]
    assert sums == sorted(sums)
    first_hour = sum(q.kwh for q in patched_client.quarters[:4])
    assert sums[0] == pytest.approx(first_hour, abs=1e-3)  # totals were rebuilt from zero at the new start
    assert sums[-1] == pytest.approx(sum(q.kwh for q in patched_client.quarters), abs=1e-2)
    csv_calls = [c for c in patched_client.calls if c[0] == "csv"]
    assert csv_calls[0][1].isoformat() == "2026-08-01" and len(csv_calls) == 4


async def test_import_history_without_date_uses_supply_start_and_is_repeatable(hass, entry, patched_client, quarters):
    from datetime import date

    patched_client.supply_start = date(2026, 8, 1)
    patched_client.quarters = _with_older_weeks(quarters)
    await setup(hass, entry)
    first = [(r["start"], r["sum"]) for r in await rows(hass, ENERGY)]
    assert len(first) == 3 * 7 * 24

    patched_client.calls.clear()
    for _ in range(2):  # running it again must give exactly the same result
        await hass.services.async_call(DOMAIN, "import_history", {}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert [(r["start"], r["sum"]) for r in await rows(hass, ENERGY)] == first
    assert [c for c in patched_client.calls if c[0] == "csv"][0][1].isoformat() == "2026-08-01"


@pytest.mark.parametrize(("months", "first_day"), [(1, "2026-10-01"), (3, "2026-08-01"), (12, "2025-11-01")])
async def test_first_import_loads_the_chosen_amount(hass, entry, patched_client, months, first_day):
    hass.config_entries.async_update_entry(entry, data={**entry.data, "history_months": months})
    await setup(hass, entry)
    assert [c for c in patched_client.calls if c[0] == "csv"][0][1].isoformat() == first_day


async def test_chosen_amount_never_goes_before_supply_start(hass, entry, patched_client):
    from datetime import date

    patched_client.supply_start = date(2026, 9, 10)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "history_months": 36})
    await setup(hass, entry)
    assert [c for c in patched_client.calls if c[0] == "csv"][0][1].isoformat() == "2026-09-10"


async def test_history_service_ignores_the_setup_choice_and_loads_everything(hass, entry, patched_client):
    from datetime import date

    patched_client.supply_start = date(2026, 7, 1)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "history_months": 1})
    await setup(hass, entry)
    patched_client.calls.clear()
    await hass.services.async_call(DOMAIN, "import_history", {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [c for c in patched_client.calls if c[0] == "csv"][0][1].isoformat() == "2026-07-01"
