# CKW for Home Assistant

**This is not an official CKW integration. Use at your own risk. This code was generated with Claude Sonnet 5.5**

Imports smart-meter data from the CKW customer portal (my.ckw.ch) once a day:

* **Energy consumption** (15-minute values, summed to hours) as long-term statistics, ready for the **Energy dashboard**
* **Energy cost** (CHF, hourly) as a statistic for the Energy dashboard
* **Peak power** (kW, hourly maximum) as a statistic
* Sensors: latest day's consumption / peak / cost, this month's peak and power charge, time of last data

CKW has no public API. The integration logs in like the web portal does (password + SMS code) and then refreshes the
session daily without any SMS. CKW ends every login about **30 days** after the SMS code (a fixed deadline that refreshing
cannot extend), so you need to enter a new SMS code roughly **once a month**. Home Assistant asks for it 3 days before the
deadline (a *Repair* notification), so the data never stops; the sensor *Login valid until* shows the date.

## Install (Home Assistant OS)

1. Copy the folder `custom_components/ckw` to `/config/custom_components/ckw` on your Home Assistant
   (VS Code remote / Samba / SSH add-on).
2. Restart Home Assistant.
3. *Settings → Devices & services → Add integration → CKW*.
4. Enter your my.ckw.ch user name and password. CKW sends an SMS; enter the code from the **most recent** SMS.
   (*Send new code* sends a new SMS and invalidates earlier codes.)
5. Choose how much history to load first (1, 3, 12, 36 months or everything since your contract start). The setup shows the
   expected duration of each choice (about 9 seconds per month, e.g. 12 months ≈ 2 minutes, 5 years ≈ 10 minutes).
   The import runs **in the background**; the sensors show *unavailable* until it is done. Progress is logged.
   More can be loaded later with the action *CKW: Import history*. The sensors show *unavailable* until it
   is done. Progress is logged: *Settings → System → Logs*, search for `ckw`.

## Energy dashboard

*Settings → Dashboards → Energy → Add consumption* (Electricity grid):

* Grid consumption: **CKW energy consumption**
* Costs: *Use an entity tracking the total costs* → **CKW energy cost**

The monthly peak-power charge (Leistungspreis) is not part of the hourly cost; see the sensor *Power charge (this month)*.

## Import all history again / older data

*Developer tools → Actions → **CKW: Import history*** (`ckw.import_history`), optionally with a start date
(default: start of your supply contract). It **deletes** the three CKW statistics and imports everything again, in the background;
progress is logged. Needed to add older data than what is already imported, because the stored running total starts at
the first imported hour. The Energy dashboard setup keeps working (same statistic ids).

## Where is the history?

The imported history is stored as **long-term statistics**, not as sensor history:

* The sensors only show the *latest day* (that is by design); their history graph starts when you installed the integration.
* The Energy dashboard shows the history: switch the period at the top (day / week / month / year) and use the arrows.
* *Developer tools → Statistics* lists `CKW energy consumption`, `CKW energy cost`, `CKW peak power`.
* A *Statistics graph* card can chart them, e.g. `ckw:energy_consumption_<your location id>`.
* *Settings → Devices & services → CKW → ⋮ → Download diagnostics* shows, per statistic, how many hours are stored and the first/last hour.

## When data arrives

CKW publishes data with about one day delay. Home Assistant fetches at 02:00 every day and re-reads the last 3 days to pick up
corrections. If the newest data is older than yesterday it retries every 3 hours (max 5 times); on network errors every hour.

## Monthly re-login

3 days before the login ends, Home Assistant shows a re-authentication notification (*Settings → Devices & services → CKW*).
Confirm your password and enter the new SMS code (*Send new code* if it did not arrive). History is kept.
If you miss the deadline, the same notification appears and the missing days are fetched afterwards (CKW keeps the data).

## Privacy

User name, password and refresh token are stored in Home Assistant's config entry (`.storage`, not encrypted). Diagnostics redact them.

## Development

```
tools/test.sh                 # library tests (Python 3.10 venv) + Home Assistant tests (3.14 venv, HA 2026.10)
docs/auth-findings.md         # how the portal login and API work (from browser recordings)
tools/probe_login.py          # live probe of the login flow
tools/live_check.py           # smoke test of the library against the real portal
```

### Limitation
The login session expires after 30.4 days. The user needs to reauthenticat with an SMS code. Home Assistant will show a message informing about the expiring credentials.