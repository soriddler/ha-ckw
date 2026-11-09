IDP_BASE = "https://id.ckw.ch/realms/CKW-prod/protocol/openid-connect"
AUTH_URL = f"{IDP_BASE}/auth"
TOKEN_URL = f"{IDP_BASE}/token"
CLIENT_ID = "dtp-web"
REDIRECT_URI = "https://my.ckw.ch/stromverbrauch"
# A normal (online) session: refresh token valid until a fixed deadline (~30 days after login) but it survives
# idle days. An offline_access session has no deadline, yet CKW deletes it after a few idle hours
# ("Offline user session not found"), see docs/auth-findings.md.
SCOPE = "openid"
API_BASE = "https://dtp-apim.azure-api.net"
REQUEST_TIMEOUT = 30
# The probe was run with this browser-like UA and passes Cloudflare; keep it unless blocked.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0 Safari/537.36"
)
