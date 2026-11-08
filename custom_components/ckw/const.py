"""Constants for the CKW integration."""
from datetime import timedelta

DOMAIN = "ckw"
MANUFACTURER = "CKW"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_LOCATION_ID = "location_id"
CONF_METER_GROUP_TYPE = "meter_group_type"
CONF_METER_ID = "meter_id"
CONF_CODE = "code"
CONF_RESEND = "resend"

BACKFILL_DAYS = 365  # history imported on first setup
OVERLAP_DAYS = 3  # re-fetched on every run to pick up late corrections
REQUEST_PAUSE = 1.0  # seconds between requests during backfill (be polite)

UPDATE_HOUR, UPDATE_MINUTE = 2, 0  # daily fetch time (local); if yesterday is not published yet, the stale-data retry covers it
RETRY_ON_ERROR = timedelta(hours=1)
RETRY_ON_STALE = timedelta(hours=3)
MAX_RETRIES = 5

CONF_HISTORY_MONTHS = "history_months"  # how much history the first import loads; 0 = everything
HISTORY_CHOICES = (1, 3, 12, 36)  # plus "everything since the start of the supply contract"
SECONDS_PER_MONTH = 9  # measured: 13 months took about 2 minutes (two requests per month plus a pause)
