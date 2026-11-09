# CKW portal: login and data API findings

Source: sanitized HAR recordings of my.ckw.ch (2026-11-08). No browser automation is needed; everything is plain HTTPS + JSON/CSV.

## Authentication (Keycloak, OIDC code flow + PKCE)
- IdP: `https://id.ckw.ch/realms/CKW-prod`, client `dtp-web` (public client, no secret).
- Authorize: `GET /protocol/openid-connect/auth?client_id=dtp-web&redirect_uri=https://my.ckw.ch/stromverbrauch&response_type=code&response_mode=fragment&scope=openid&state=..&nonce=..&code_challenge=..&code_challenge_method=S256`
- Login form (Keycloakify theme `dtp-theme`): the form action is in the page's `kcContext.url.loginAction` (`/login-actions/authenticate?session_code=..&execution=..&tab_id=..&client_data=..`).
- Step 1 (password): `POST loginAction` (urlencoded) with `username=<login>&password=<pw>&credentialId=` (empty). Response is **HTTP 200 HTML, not a redirect**; success and failure are told apart by `kcContext.pageId`:
  - success -> `pageId: "login-sms"` (template `login-sms.ftl`, execution id of the SMS authenticator, masked phone number shown). Parse the new `kcContext.url.loginAction` from this page for step 2.
  - wrong password -> `pageId: "login"` again with `kcContext.message = {summary: "Invalid username or password.", type: "error"}`.
- Step 2 failure (confirmed in HAR `id.ckw.ch_login_wrong_smscode`): `POST loginAction` with a wrong/old `code` returns **HTTP 200**, `pageId: "login-sms"`, `kcContext.message = {summary: "Invalid SMS code entered, please enter it again.", type: "error"}` and also the `global` field error. The page carries a **new `loginAction`** (fresh `session_code`, same `execution` id), so the same session can retry with the next code. Success is only a 302 with `code=` in the Location fragment. Treat the message text as a hint only (localized); the reliable signal is "not a 302 with code".
- Cookies: Keycloak auth-session cookies are required between steps (HAR hides Set-Cookie); use one cookie-jar session for the whole flow. Each step's `loginAction` URL contains a fresh `session_code`, so always re-parse it from the latest page.
- `kcContext` is a JS object literal embedded in the HTML (not strict JSON, contains functions), so extract needed values with targeted regexes (`"loginAction": "..."`, `"pageId": "..."`, `"summary": "..."`), not `json.loads`.
- Step 2 (SMS TAN): `POST loginAction` with form field `code=<TAN>` -> `302 Location: https://my.ckw.ch/stromverbrauch#state=..&session_state=..&iss=..&code=<auth code>`.
- Token: `POST /protocol/openid-connect/token`, form: `grant_type=authorization_code&client_id=dtp-web&redirect_uri=https://my.ckw.ch/stromverbrauch&code=..&code_verifier=..`
  - Response: `access_token` (`expires_in` 300 s), `refresh_token`, `id_token`, **`refresh_expires_in: 2627999` (~30.4 days)**, `scope: openid email profile`.
- Refresh: `POST /token` with `grant_type=refresh_token&client_id=dtp-web&refresh_token=..` (standard Keycloak; `use.refresh.tokens=true`).
- Realm "remember me" is disabled; 2FA is SMS only ("login and SMS MFA"). Google/Apple social login exists (not investigated whether it skips SMS).

### Consequence
Only one SMS TAN is needed per refresh-token lifetime. A daily refresh should keep the session alive.
OPEN: is the ~30.4 days an idle timeout (renewed on each refresh) or an absolute cap (SSO Session Max)? Test by logging `refresh_expires_in` after each daily refresh. If it counts down, a new TAN is needed roughly monthly, so the integration must raise a re-auth prompt.
OPEN: are refresh tokens rotated on use? Always persist the newest one.

## Data API (Azure API Management)
Base `https://dtp-apim.azure-api.net`. Headers: `Authorization: Bearer <access_token>` (confirmed via CORS preflight; HAR hides the value), `Content-Type: application/json`, and `x-ckw-gpnr: <account number>`. No subscription key seen.

| Purpose | Call |
|---|---|
| Account | `GET /account-api/api/v1/account` -> `body.accounts[].gpnr` (value for `x-ckw-gpnr`) |
| Contracts | `GET /account-api/api/v2/account/contracts?market=regulated` (needs `x-ckw-gpnr`, else HTTP 400) -> `body.activeContracts[]`: `locationId`, `meterId`, `activeMeterGroupType` |
| JSON data | `POST /measurement-api/api/v1/electricityconsumption/measurements/location` body `{"aggregation":"HOUR\|DAY\|...","startDate":"YYYY-MM-DD","endDate":"YYYY-MM-DD","locationId":"..","activeMeterGroupType":"BASIC"}` |
| CSV export | `POST /measurement-api/api/v1/file/location` same body with `"aggregation":"FIFTEEN_MINUTES"` (returns the 15-min CSV, full precision) |
| XLSX export | `POST /measurement-api/api/v1/file/excel/location` (not needed: the JSON call has the same columns) |

JSON response (`body`): `sumEnergy`, `amountTotalEnergy`, `amountTotalMaxPower`, `amountTotalSum`, `maxPowerPerMonth[{timestamp,maxPower,amount}]`, `measurements[{timestampStartOfPeriod, energy, maxPower, amount:{energy,total}}]`. Values are strings rounded to **2 decimals**. The CSV keeps full precision.
Observations: the daily `amount.total` equals the energy cost only. The monthly peak-power charge (`amountTotalMaxPower`, 2.73 CHF for 1.68 kW at 2026-11-07T20:15) appears only at month level, so it must be handled separately.
Aggregations seen in the app: `FIFTEEN_MINUTES, HOUR, DAY, MONTH, YEAR`. Untested: whether the JSON endpoint accepts `FIFTEEN_MINUTES`.

## Decisions
- Use CSV (15 min, precise kWh) for energy; JSON `HOUR` for peak kW and cost. No xlsx parsing, no openpyxl.
- Dependencies: `aiohttp` only.

## SMS resend (HAR `id.ckw.ch_login_wrong_smscode_resendcode`)
- A wrong code does NOT trigger a new SMS; the user must click "send new code" on the SMS page.
- "Send new code" is a plain navigation: `GET https://id.ckw.ch/realms/CKW-prod/login-actions/authenticate?execution=<sms-execution-id>&client_id=dtp-web&tab_id=..&client_data=..` (the `loginAction` URL **without `session_code`**), same-origin, same cookies. Response: HTTP 200, `pageId: login-sms`, no message, masked phone, and a fresh `loginAction` with new `session_code`.
- Keycloak re-runs the SMS authenticator on each such GET, so every GET of the SMS step presumably sends an SMS (cost + rate limit risk): the client must GET it only on explicit user request, never automatically or in retry loops.
- Confirmed by user: after a resend the OLD code is invalid; only the most recent code works. So the HA code step must tell the user to enter the newest SMS, and a resend invalidates any code they already have.
- Unverified (probe `--resend`): any cooldown/rate limit on resending.

## Live probe, run 1 (2026-11-08)
- Plain stdlib client (no browser UA tricks beyond a Chrome UA string) is NOT blocked by Cloudflare. Cookies set on the auth page: `AUTH_SESSION_ID`, `KC_AUTH_SESSION_HASH`, `KC_RESTART`, `__cf_bm` (keep them all in the cookie jar).
- Password step OK (200, `login-sms`). Wrong code: 200, `login-sms`, new loginAction, 0.1 s. **The error text is localized**: this client (no Accept-Language) got German "Ungültiger SMS Code, bitte erneut eingeben.". Never match on the text.
- Correct code -> HTTP 302 (success). Header names must be handled case-insensitively (server sends lowercase `location`); the probe's first run misread this as a failure and consumed the code.

## Live probe, refresh behaviour (2026-11-08)
- Refresh works without SMS; refresh tokens ROTATE on every use (persist the new one atomically).
- The refresh token expiry is an **absolute cap fixed at login**: two refreshes 139 s and 62 s apart both showed exp = 2026-12-08 22:07:32 (+0 s), `refresh_expires_in` just counted down. Daily refresh does not extend it. A new SMS login is needed about every 30.4 days (SSO Session Max ~ 2628000 s).
- Next experiment: `--offline` (scope `openid offline_access`) to see whether an offline token without the cap is allowed for client `dtp-web`.

## Live probe: offline_access (2026-11-08)
- Auth request with `scope=openid offline_access` is ACCEPTED for client `dtp-web`. Token response: `scope='openid email profile offline_access'`, `refresh_expires_in: 0` (no fixed expiry), refresh token has no `exp` claim.
- Offline tokens normally expire only after an idle period (Keycloak default "Offline Session Idle" 30 days, reset on each use) and at an optional "Offline Session Max". To be verified by daily refreshes (probe `--refresh`); watch for `invalid_grant`.
- Therefore the integration requests `offline_access` and refreshes daily; SMS re-login only when `invalid_grant` is returned.
- Probe bug in run 1 (crash on missing `exp`) lost that token; fixed.

## Live probe: offline token confirmed (2026-11-08)
- Full login with `--offline` works: token type `Offline`, no `exp`, `refresh_expires_in=0`; two further refreshes (immediate, +32 s) succeeded, tokens rotate each time, data API returns 200.
- Still to observe: survival across days (offline idle timeout, typically 30 days since last use, reset by each refresh). Run `--refresh ~/.ckw_probe_offline.json` daily for a few days; a failure shows as `invalid_grant`.
- Design decision: request `scope=openid offline_access`; refresh once per day; persist the rotated refresh token before using the new access token; on `invalid_grant` raise HA re-auth (password stored, only the SMS code is asked).

## Offline sessions are deleted by CKW after a short idle time (2026-11-09)
- Log: `invalid_grant: Offline user session not found` (session removed server-side, not a lost token).
- Observed: session created 8 Nov ~13:00, last contact ~13:07, dead at 02:00 next day. A second session (09:05 9 Nov) was dead by the next contact 3 h later. Probe session (8 Nov) also dead after 24 h.
- So the idle timeout is somewhere between minutes and ~3 h (earlier test: refreshes seconds apart worked). To be measured with `tools/probe_idle.py` (`--every 10` and `--escalate`).
- Integration bug fixed: the stale-data retry used the JSON's last day (lags the CSV by a day) and scheduled a 3 h retry, which was the only contact all morning.

## Decision (2026-11-09): use the normal (online) session, not offline_access
- Offline sessions: deleted by CKW after a few idle hours (see above).
- Online session (scope `openid`, what the browser portal uses): a probe session survived 24.7 h idle and refreshed fine; refresh token is a JWT with a fixed `exp` (~30.4 days after login, refreshing never extends it).
- Therefore: scope `openid`, daily refresh, `TokenManager.session_expires_at` reads the deadline from the token, the integration starts a re-login (one SMS) 3 days before it and shows the sensor "Login valid until".
- Still unmeasured: the online idle timeout beyond ~25 h (daily refresh uses 24 h gaps, so it needs to be > 24 h; verify over the next days with `probe_idle.py` or the real integration).
