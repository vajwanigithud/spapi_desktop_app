import os

from dotenv import load_dotenv

load_dotenv()

APP_NAME = "Amazon SP-API Desktop App"
APP_VERSION = "1.0.0"

# Credentials (to be filled later)
LWA_CLIENT_ID = os.getenv("LWA_CLIENT_ID")
LWA_CLIENT_SECRET = os.getenv("LWA_CLIENT_SECRET")
LWA_REFRESH_TOKEN = os.getenv("LWA_REFRESH_TOKEN")

# Optional marketplace and region
MARKETPLACE_ID = os.getenv("MARKETPLACE_ID", "")
SPAPI_REGION = os.getenv("SPAPI_REGION", "eu-west-1")

# Tracking window
TRACKING_START = os.getenv("TRACKING_START", "2025-10-01T00:00:00Z")

# Optional marketplace and region
MARKETPLACE_ID = os.getenv("MARKETPLACE_ID", "")
SPAPI_REGION = os.getenv("SPAPI_REGION", "eu-west-1")
