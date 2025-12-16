import os

APP_NAME = "Amazon SP-API Desktop App"
APP_VERSION = "1.0.0"

# ----------------------------
# Helpers
# ----------------------------
def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v

def _csv_list(name: str, default: str = "") -> list[str]:
    raw = (os.getenv(name) or default).strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]

# ----------------------------
# Required credentials (env only)
# ----------------------------
LWA_CLIENT_ID = _req("LWA_CLIENT_ID")
LWA_CLIENT_SECRET = _req("LWA_CLIENT_SECRET")
LWA_REFRESH_TOKEN = _req("LWA_REFRESH_TOKEN")

# ----------------------------
# Marketplace / region
# ----------------------------
# Preferred: MARKETPLACE_IDS="A2VIGQ35RCS4UG" (comma-separated supported)
MARKETPLACE_IDS = _csv_list("MARKETPLACE_IDS")

# Back-compat: MARKETPLACE_ID="A2VIGQ35RCS4UG"
if not MARKETPLACE_IDS:
    single = (os.getenv("MARKETPLACE_ID") or "").strip()
    if single:
        MARKETPLACE_IDS = [single]

# Hard default (UAE) to avoid empty marketplaceIds breaking reports
if not MARKETPLACE_IDS:
    MARKETPLACE_IDS = ["A2VIGQ35RCS4UG"]

# Convenience single value
MARKETPLACE_ID = MARKETPLACE_IDS[0]

SPAPI_REGION = os.getenv("SPAPI_REGION", "eu-west-1")

# ----------------------------
# Tracking window
# ----------------------------
TRACKING_START = os.getenv("TRACKING_START", "2025-10-01T00:00:00Z")
