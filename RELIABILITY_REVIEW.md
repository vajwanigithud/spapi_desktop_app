# Reliability & Error-Handling Review: Top 10 Weakest Points
## SP-API Desktop App - Complete Scan

---

## WEAKEST POINT #1: Missing Retry Logic on Auth Token Failures
**File:** `auth/spapi_auth.py` (lines 26-50)  
**Severity:** CRITICAL

### What Can Go Wrong
- Network timeout during token refresh → `requests.post()` fails silently or returns non-200
- Token endpoint rate-limited (429) → exception propagates to all API calls
- Transient DNS/network errors → all dependent operations fail immediately

### Current Code
```python
def get_lwa_access_token(self):
    if self._lwa_token and self._lwa_expiry > datetime.datetime.utcnow():
        return self._lwa_token
    
    url = "https://api.amazon.com/auth/o2/token"
    data = {...}
    
    resp = requests.post(url, data=data)  # ❌ NO RETRY, NO TIMEOUT
    if resp.status_code != 200:
        print("❌ LWA token request failed")  # ❌ JUST PRINTS, DOESN'T RETRY
        print("Status:", resp.status_code)
    resp.raise_for_status()  # Blocks entire app
    payload = resp.json()
    # ... proceeds assuming success
```

### How to Harden
```python
from tenacity import retry, stop_after_attempt, wait_exponential
import time

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True
)
def get_lwa_access_token(self):
    if self._lwa_token and self._lwa_expiry > datetime.datetime.utcnow():
        return self._lwa_token
    
    url = "https://api.amazon.com/auth/o2/token"
    data = {...}
    
    try:
        resp = requests.post(url, data=data, timeout=15)  # Add timeout
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("[Auth] Token request timed out after 15s")
        raise
    except requests.exceptions.RequestException as e:
        logger.warning(f"[Auth] Token request failed, will retry: {e}")
        raise  # Tenacity catches this
    
    payload = resp.json()
    self._lwa_token = payload["access_token"]
    self._lwa_expiry = datetime.datetime.utcnow() + datetime.timedelta(
        seconds=payload.get("expires_in", 3600) - 60
    )
    return self._lwa_token
```

---

## WEAKEST POINT #2: Unprotected Database Access Without Connection Pooling
**File:** `services/db.py` (lines 7-10)  
**Severity:** HIGH

### What Can Go Wrong
- **No connection pooling** → High concurrency creates hundreds of DB connections
- **No timeout** → Queries hang forever if DB is locked or fails
- **Database locked errors** → Multiple writers (forecast_sync + user endpoints) cause SQLITE_BUSY
- **Resource leak** → Connections not properly closed on exceptions

### Current Code
```python
def get_db_connection():
    conn = sqlite3.connect(CATALOG_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
```

### How to Harden
```python
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from threading import Lock
import logging

logger = logging.getLogger(__name__)
CATALOG_DB_PATH = Path(__file__).resolve().parent.parent / "catalog.db"
_db_lock = Lock()  # Serialize writes
_db_timeout = 10   # seconds

@contextmanager
def get_db_connection():
    """Context manager ensuring proper connection closure and timeout."""
    conn = None
    try:
        conn = sqlite3.connect(CATALOG_DB_PATH, timeout=_db_timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrency
        yield conn
    except sqlite3.DatabaseError as e:
        logger.error(f"[DB] Database error: {e}", exc_info=True)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception as e:
                logger.warning(f"[DB] Error closing connection: {e}")

def execute_write(sql: str, params: tuple = ()):
    """Serialize all writes to prevent SQLITE_BUSY."""
    with _db_lock:
        with get_db_connection() as conn:
            conn.execute(sql, params)
            conn.commit()
```

---

## WEAKEST POINT #3: Blocking I/O in FastAPI Endpoints (Sync Functions)
**File:** `main.py` (lines 839-880, 786-836, 611-643)  
**Severity:** HIGH (Production Impact)

### What Can Go Wrong
- **Long API call blocks entire thread** → All other requests hang
- `fetch_vendor_pos_from_api()` makes multiple `requests.get()` calls in a loop with no timeout
- Database operations (`spapi_catalog_status()` reads entire DB) in request handler
- Forecast sync runs in `sync_all_forecast_sources()` without async, blocking UI for 10+ minutes

### Current Code
```python
# ❌ BLOCKING SYNC FUNCTION in FastAPI
@app.post("/api/catalog/fetch/{asin}")
def fetch_catalog_for_asin(asin: str):
    """Fetch catalog data for one ASIN from SP-API and persist locally."""
    fetched = spapi_catalog_status()  # ❌ Reads entire DB
    if asin in fetched:
        return {"asin": asin, "status": "cached"}
    result = fetch_spapi_catalog_item(asin)  # ❌ Network I/O blocking
    return {"asin": asin, "status": result.get("source", "spapi")}

def fetch_spapi_catalog_item(asin: str):
    # ...
    resp = requests.get(url, headers=headers, params=params)  # ❌ NO TIMEOUT
    # ...

def fetch_vendor_pos_from_api(created_after, created_before, max_pages=5):
    # ...
    while page < max_pages:
        resp = requests.get(url, headers=headers, params=params)  # ❌ NO TIMEOUT
        # ...
```

### How to Harden
```python
from fastapi import BackgroundTasks
import asyncio
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=4)

# Move long operations to background
@app.post("/api/catalog/fetch/{asin}")
async def fetch_catalog_for_asin(asin: str, background_tasks: BackgroundTasks):
    """Queue catalog fetch in background, return immediately."""
    try:
        fetched = spapi_catalog_status().get(asin)
        if fetched and fetched.get("title"):
            return {"asin": asin, "status": "cached"}
    except Exception as e:
        logger.error(f"[CatalogFetch] Error checking cache: {e}")
    
    # Queue in background, don't block
    background_tasks.add_task(fetch_spapi_catalog_item_safe, asin)
    return {"asin": asin, "status": "queued"}

def fetch_spapi_catalog_item_safe(asin: str):
    """Wrapped version with error handling and timeout."""
    try:
        fetch_spapi_catalog_item(asin)
    except Exception as e:
        logger.error(f"[CatalogFetch] Background fetch failed for {asin}: {e}")

# Add timeouts to ALL requests
def fetch_spapi_catalog_item(asin: str) -> Dict[str, Any]:
    # ...
    resp = requests.get(url, headers=headers, params=params, timeout=30)  # ✅ Timeout
    # ...

def fetch_vendor_pos_from_api(created_after: str, created_before: str, max_pages: int = 5):
    # ...
    resp = requests.get(url, headers=headers, params=params, timeout=20)  # ✅ Timeout
    # ...
```

---

## WEAKEST POINT #4: No Validation of External Config (Missing Credentials)
**File:** `config.py` (lines 10-12)  
**Severity:** CRITICAL

### What Can Go Wrong
- Missing `LWA_CLIENT_ID`, `LWA_CLIENT_SECRET`, `LWA_REFRESH_TOKEN` → Silent `None` values
- App starts successfully but crashes on first API call with cryptic error
- No early warning that credentials are invalid/missing

### Current Code
```python
LWA_CLIENT_ID = os.getenv("LWA_CLIENT_ID")         # ❌ Can be None
LWA_CLIENT_SECRET = os.getenv("LWA_CLIENT_SECRET") # ❌ Can be None
LWA_REFRESH_TOKEN = os.getenv("LWA_REFRESH_TOKEN") # ❌ Can be None
```

### How to Harden
```python
import os
import sys
from dotenv import load_dotenv

load_dotenv()

def _validate_config():
    """Validate required credentials at startup."""
    required = {
        "LWA_CLIENT_ID": "SP-API Client ID for OAuth",
        "LWA_CLIENT_SECRET": "SP-API Client Secret",
        "LWA_REFRESH_TOKEN": "SP-API Refresh Token",
    }
    missing = []
    for key, desc in required.items():
        val = os.getenv(key, "").strip()
        if not val:
            missing.append(f"  {key}: {desc}")
    
    if missing:
        print("\n❌ FATAL: Missing required environment variables:")
        for item in missing:
            print(item)
        print("\nPlease set these in .env or environment before starting.\n")
        sys.exit(1)

APP_NAME = "Amazon SP-API Desktop App"
APP_VERSION = "1.0.0"

_validate_config()  # ✅ Fail fast at import time

LWA_CLIENT_ID = os.getenv("LWA_CLIENT_ID")
LWA_CLIENT_SECRET = os.getenv("LWA_CLIENT_SECRET")
LWA_REFRESH_TOKEN = os.getenv("LWA_REFRESH_TOKEN")

MARKETPLACE_ID = os.getenv("MARKETPLACE_ID", "A1F83G8C2ARO7P")  # Default to UK
SPAPI_REGION = os.getenv("SPAPI_REGION", "eu-west-1")
TRACKING_START = os.getenv("TRACKING_START", "2025-10-01T00:00:00Z")
```

---

## WEAKEST POINT #5: Silent JSON Parsing Failures (No Default/Fallback)
**File:** `services/forecast_sync.py` (lines 45-73, 151-251)  
**Severity:** HIGH

### What Can Go Wrong
- `parse_report_tsv()` or `parse_report_json()` fails silently, returns empty list
- Caller doesn't know data was lost; silently proceeds with 0 rows
- Forecast reports appear "synced" but have 0 data, users see empty dashboard

### Current Code
```python
def parse_report_tsv(document_bytes: bytes):
    try:
        text = document_bytes.decode("utf-8-sig", errors="ignore")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        return [dict(row) for row in reader]
    except Exception as exc:
        logger.error(f"[Parser] TSV parsing failed: {exc}")
        return []  # ❌ Silently fails, returns empty

def parse_vendor_sales_json(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    # ... lots of fallback logic ...
    if skipped_missing_keys:
        logger.warning("[forecast_sync] Skipped %d sales entries with missing asin/startDate.",
                       skipped_missing_keys)  # ❌ Logs but doesn't tell caller it's bad
    return rows  # Even if skipped 90% due to missing keys
```

### How to Harden
```python
class ParseError(RuntimeError):
    """Raised when document parsing fails critically."""
    def __init__(self, doc_type: str, error_msg: str, sample: str = None):
        self.doc_type = doc_type
        self.error_msg = error_msg
        self.sample = sample
        super().__init__(f"[{doc_type}] Parse failed: {error_msg}")

def parse_report_tsv(document_bytes: bytes, strict: bool = False):
    """
    Parse TSV with optional strict mode.
    strict=True raises exception on parse error (instead of returning []).
    """
    try:
        if not document_bytes:
            if strict:
                raise ParseError("TSV", "Empty document")
            logger.warning("[Parser] Empty TSV document received")
            return []
        
        text = document_bytes.decode("utf-8-sig", errors="ignore")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        rows = [dict(row) for row in reader]
        
        if not rows and strict:
            raise ParseError("TSV", "No rows parsed from valid CSV", text[:500])
        
        return rows
    except ParseError:
        raise
    except Exception as exc:
        if strict:
            sample = document_bytes[:200] if isinstance(document_bytes, bytes) else str(document_bytes)[:200]
            raise ParseError("TSV", str(exc), sample) from exc
        logger.error(f"[Parser] TSV parsing failed (non-strict): {exc}")
        return []

# In sync functions, detect bad parse rates
def sync_vendor_sales_history(...):
    chunk_rows = parse_report_tsv(report_rows_raw, strict=False)
    
    # ✅ DETECT if parsing removed too much data
    if isinstance(report_rows_raw, (bytes, bytearray)):
        raw_text = report_rows_raw.decode("utf-8-sig", errors="ignore")
        line_count = len(raw_text.split('\n'))
        parse_rate = len(chunk_rows) / max(line_count, 1)
        if parse_rate < 0.5:
            logger.warning(f"[Parse] Only recovered {parse_rate*100:.1f}% of lines; "
                          f"expected {line_count}, got {len(chunk_rows)}")
```

---

## WEAKEST POINT #6: No Timeout on Long-Running Forecast Sync
**File:** `services/forecast_sync.py` (lines 940-1021, desktop.py lines 28-37)  
**Severity:** MEDIUM-HIGH

### What Can Go Wrong
- Forecast sync makes 3+ API calls, each with 600s polling timeout
- UI blocks for 10+ minutes waiting for response
- Network issue mid-sync → user clicks "Sync" again → duplicate requests
- No way to cancel long-running operation

### Current Code
```python
def sync_all_forecast_sources(...):
    # ...  
    sales_result = sync_vendor_sales_history(...)  # Could take 5+ min
    forecast_result = sync_vendor_forecast(...)     # Could take 5+ min
    inventory_result = sync_vendor_rt_inventory()   # Could take 5+ min
    # Total: 15+ minutes, all blocking

def poll_vendor_report(
    report_id: str, timeout_seconds: int = 600, poll_interval_seconds: int = 20
) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while True:
        resp = requests.get(url, headers=headers, timeout=30)  # ✅ Has timeout
        # ... 600 seconds polling ❌ UI blocked

# desktop.py: UI blocks on API call
def main():
    proc = run_api()
    try:
        open_window()  # ❌ Waits forever if API is broken
    finally:
        proc.terminate()
```

### How to Harden
```python
import asyncio
from concurrent.futures import TimeoutError as FuturesTimeoutError

# Move to background thread with timeout
def sync_all_forecast_sources_async(...) -> Dict[str, Any]:
    """
    Returns immediately, status is "queued" or "in-progress".
    Call with timeout wrapper.
    """
    global _sync_lock
    
    if not _sync_lock.acquire(blocking=False):
        return {"status": "warning", "error": "sync_already_running"}
    
    def _do_sync():
        try:
            # ... existing sync logic ...
            return {"status": "ok", "statuses": statuses, **results}
        except Exception as e:
            logger.error(f"[Sync] Failed: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            _sync_lock.release()
    
    # Run in executor with 30-minute timeout
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_do_sync)
    
    try:
        result = future.result(timeout=1800)  # 30 min max
    except FuturesTimeoutError:
        logger.error("[Sync] Timed out after 30 minutes")
        return {"status": "error", "error": "sync_timeout_30min"}
    
    return result

# In routes
@app.post("/api/forecast/sync-start")
async def start_forecast_sync(background_tasks: BackgroundTasks):
    """Queue sync in background, return immediately."""
    background_tasks.add_task(sync_all_forecast_sources_async)
    return {"status": "queued"}

@app.get("/api/forecast/sync-status")
async def get_sync_status():
    """Check if sync is running."""
    return {"running": _sync_lock.locked(), "last_sync": _last_full_sync_cache}

# desktop.py: Check API is ready before showing UI
def main():
    proc = run_api()
    try:
        # Wait up to 10s for API to be healthy
        for attempt in range(10):
            try:
                resp = requests.get(f"http://127.0.0.1:{API_PORT}/", timeout=2)
                if resp.status_code == 200:
                    break
            except requests.ConnectionError:
                time.sleep(1)
        else:
            print("❌ API failed to start after 10 seconds")
            return
        
        open_window()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
```

---

## WEAKEST POINT #7: No Request Timeout on Some API Calls
**File:** `main.py` (line 811, 815), `services/spapi_reports.py` (line 302, 312)  
**Severity:** MEDIUM

### What Can Go Wrong
- Network hangs → request waits forever (default `requests` timeout is none)
- System becomes unresponsive
- User kills and restarts app, loses progress

### Current Code
```python
# main.py line 811
resp = requests.get(url, headers=headers, params=params)  # ❌ NO TIMEOUT

# services/spapi_reports.py line 302
meta_resp = requests.get(meta_url, headers=headers, timeout=30)  # ✅ Has timeout
doc_resp = requests.get(download_url, timeout=60)  # ✅ Has timeout

# Some have timeout, some don't → inconsistent
```

### How to Harden
```python
# Create a utility wrapper
DEFAULT_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 60

class SafeRequest:
    @staticmethod
    def get(url: str, timeout: int = DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
        """GET with guaranteed timeout and retry on transient errors."""
        for attempt in range(3):
            try:
                return requests.get(url, timeout=timeout, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt < 2:
                    logger.warning(f"[Request] Attempt {attempt+1} failed, retrying: {e}")
                    time.sleep(2 ** attempt)  # Exponential backoff
                else:
                    logger.error(f"[Request] Failed after 3 attempts: {e}")
                    raise
    
    @staticmethod
    def post(url: str, timeout: int = DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
        """POST with guaranteed timeout and retry."""
        for attempt in range(3):
            try:
                return requests.post(url, timeout=timeout, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt < 2:
                    logger.warning(f"[Request] Attempt {attempt+1} failed, retrying: {e}")
                    time.sleep(2 ** attempt)
                else:
                    raise

# Replace all requests.get/post
# Before:
#   resp = requests.get(url, headers=headers, params=params)
# After:
#   resp = SafeRequest.get(url, headers=headers, params=params, timeout=20)
```

---

## WEAKEST POINT #8: Race Condition in Forecast Sync Lock
**File:** `services/forecast_sync.py` (lines 31-32, 801, 945)  
**Severity:** MEDIUM

### What Can Go Wrong
- `_sync_lock.acquire(blocking=False)` returns False, but status shows "warning"
- Two threads check `_sync_lock.locked()` simultaneously → both think they can proceed
- Concurrent database writes → SQLITE_BUSY errors

### Current Code
```python
_sync_lock = threading.Lock()

def sync_vendor_rt_inventory() -> Dict[str, Any]:
    # ...
    if _inventory_lock.locked():  # ❌ Race condition
        logger.warning("[forecast_sync] Inventory sync already in progress; skipping this run")
        return {"inventory_rows": 0, "status": "warning", "error": "inventory sync already running"}
    
    with _inventory_lock:  # ✅ Proper lock usage
        # ... do work ...

def sync_all_forecast_sources(...):
    if not _sync_lock.acquire(blocking=False):  # ✅ Correct
        raise ForecastSyncError("sync already running")
    try:
        # ... do work ...
    finally:
        if _sync_lock.locked():  # ❌ Should just call release()
            _sync_lock.release()
```

### How to Harden
```python
from threading import Lock, RLock
from contextlib import contextmanager

_sync_lock = Lock()
_sync_in_progress = False
_sync_lock_guard = RLock()

@contextmanager
def acquire_sync_lock(timeout: float = 0.1):
    """Reentrant lock context manager with optional timeout."""
    acquired = _sync_lock.acquire(timeout=timeout)
    if not acquired:
        raise RuntimeError("Sync lock already held by another thread")
    try:
        yield
    finally:
        _sync_lock.release()

def sync_all_forecast_sources(...):
    try:
        with acquire_sync_lock(timeout=0.5):
            # ... do work ...
            pass
    except RuntimeError:
        logger.warning("[Sync] Already in progress, skipping")
        return {"status": "warning", "error": "sync_already_running"}
```

---

## WEAKEST POINT #9: Missing Error Context in JSON Deserialization
**File:** `main.py` (lines 564-576, 815-830), `routes/forecast_api.py` (line 138, 144)  
**Severity:** MEDIUM

### What Can Go Wrong
- `json.load(open(...))` fails → raw exception, no context on which file/why
- File corruption → app crashes trying to parse vendor_pos_cache.json
- User has no idea which operation failed

### Current Code
```python
# main.py line 738-748
try:
    data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
except Exception as exc:
    raise HTTPException(status_code=500, detail=f"Failed to read cache: {exc}")
    # ❌ Loses important context: which file? was it corrupted?

# routes/forecast_api.py line 138, 144
tracker = json.load(open(tracker_path, "r", encoding="utf-8"))  # ❌ No try/except
po_data = json.load(f)  # ❌ Not closed on exception
```

### How to Harden
```python
import json
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

def load_json_file(path: Path, default: Any = None, strict: bool = False) -> Any:
    """
    Safe JSON file loader with error context.
    
    Args:
        path: File path to load
        default: Value to return if file missing or parse fails (if not strict)
        strict: Raise exception instead of returning default
    """
    try:
        if not path.exists():
            msg = f"File not found: {path}"
            if strict:
                raise FileNotFoundError(msg)
            logger.warning(f"[JSON] {msg}, using default")
            return default
        
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            msg = f"Empty file: {path}"
            if strict:
                raise ValueError(msg)
            logger.warning(f"[JSON] {msg}, using default")
            return default
        
        data = json.loads(content)
        logger.debug(f"[JSON] Loaded {path}: {type(data).__name__}")
        return data
    
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in {path}: line {e.lineno}, col {e.colno}: {e.msg}"
        logger.error(f"[JSON] {msg}")
        if strict:
            raise ValueError(msg) from e
        return default
    except Exception as e:
        msg = f"Error loading {path}: {e}"
        logger.error(f"[JSON] {msg}", exc_info=True)
        if strict:
            raise
        return default

# Usage
def get_vendor_pos(...):
    try:
        data = load_json_file(VENDOR_POS_CACHE, default={}, strict=True)
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"[VendorPos] Cannot load cache: {e}")
        raise HTTPException(status_code=500, detail=f"Cache corrupted: {e}")
    
    normalized = normalize_pos_entries(data)
    # ...
```

---

## WEAKEST POINT #10: No Circuit Breaker on SP-API Rate Limits
**File:** `services/spapi_reports.py` (lines 43-53), `services/forecast_sync.py` (lines 687-711)  
**Severity:** MEDIUM

### What Can Go Wrong
- Hit 429 rate limit → code sets cooldown (30 min)
- But only if exception is caught correctly
- If 429 response is not properly detected → continues hammering API
- Multiple endpoints hitting same API concurrently → cascade failures

### Current Code
```python
def request_vendor_report(...) -> str:
    # ...
    resp = requests.post(url, json=body, headers=headers, timeout=30)
    if resp.status_code == 429:
        try:
            payload = resp.json()
        except Exception:
            payload = resp.text
        logger.error("[spapi_reports] createReport failed 429 QuotaExceeded...")
        raise SpApiQuotaError(f"QuotaExceeded creating report: {payload}")
    # ✅ Raises exception

def sync_vendor_forecast(...):
    try:
        report_id = request_vendor_report(...)
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        # ❌ Complex nested getattr for status code
        # ❌ SpApiQuotaError doesn't have .response attribute
        if status_code == 429:
            cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            # ...
        raise
```

### How to Harden
```python
from enum import Enum
from datetime import datetime, timedelta, timezone

class ApiRateLimitState(Enum):
    READY = "ready"
    BACKOFF_10MIN = "backoff_10min"
    BACKOFF_30MIN = "backoff_30min"
    BACKOFF_CRITICAL = "backoff_critical"

class SpApiCircuitBreaker:
    """Global rate limit circuit breaker for SP-API."""
    def __init__(self):
        self.state = ApiRateLimitState.READY
        self.backoff_until: datetime | None = None
        self._lock = threading.Lock()
        self.hit_count = 0
    
    def check_can_proceed(self) -> bool:
        """Check if API is available, else raise with backoff info."""
        with self._lock:
            if self.state == ApiRateLimitState.READY:
                return True
            
            if self.backoff_until and datetime.now(timezone.utc) > self.backoff_until:
                logger.info("[CircuitBreaker] Backoff expired, resetting to READY")
                self.state = ApiRateLimitState.READY
                self.backoff_until = None
                self.hit_count = 0
                return True
            
            wait_min = int((self.backoff_until - datetime.now(timezone.utc)).total_seconds() // 60)
            raise SpApiQuotaError(f"Circuit breaker: backoff for {wait_min} more minutes")
    
    def record_rate_limit(self):
        """Record a 429, escalate backoff."""
        with self._lock:
            self.hit_count += 1
            now = datetime.now(timezone.utc)
            
            if self.hit_count == 1:
                self.state = ApiRateLimitState.BACKOFF_10MIN
                self.backoff_until = now + timedelta(minutes=10)
                logger.warning(f"[CircuitBreaker] First 429, backoff 10 min until {self.backoff_until}")
            elif self.hit_count == 2:
                self.state = ApiRateLimitState.BACKOFF_30MIN
                self.backoff_until = now + timedelta(minutes=30)
                logger.warning(f"[CircuitBreaker] Second 429, escalate backoff 30 min until {self.backoff_until}")
            else:
                self.state = ApiRateLimitState.BACKOFF_CRITICAL
                self.backoff_until = now + timedelta(hours=2)
                logger.error(f"[CircuitBreaker] Multiple 429s, critical backoff 2 hours until {self.backoff_until}")

_circuit_breaker = SpApiCircuitBreaker()

def request_vendor_report(...) -> str:
    _circuit_breaker.check_can_proceed()  # ✅ Fail fast before making request
    
    # ...
    resp = requests.post(url, json=body, headers=headers, timeout=30)
    
    if resp.status_code == 429:
        _circuit_breaker.record_rate_limit()  # ✅ Record and escalate
        payload = resp.json() if resp.headers.get('content-type') == 'application/json' else resp.text
        raise SpApiQuotaError(f"Quota exceeded: {payload}")
    # ...

# In all forecast sync functions
def sync_vendor_forecast(...):
    try:
        report_id = request_vendor_report(...)  # ✅ Will check circuit breaker first
        # ...
    except SpApiQuotaError as e:
        logger.warning(f"[Sync] Quota hit: {e}")
        return {"status": "warning", "error": str(e)}
```

---

---

# PRIORITIZED FIX PLAN: Top 3 for "Bullet-Proof" Production

## PRIORITY #1: Fix Auth Token Retry + Config Validation (Weakest #1 + #4)
**Impact:** Prevents complete app failure  
**Effort:** 2-3 hours  
**Files:** `auth/spapi_auth.py`, `config.py`

### Why First?
- Without valid credentials or retry logic, app is **non-functional**
- Config validation catches errors **before startup** (fail-fast)
- Token retries prevent transient network issues from cascading

### What to Do
1. Add `tenacity` retry decorator to `get_lwa_access_token()` (3 attempts, exponential backoff)
2. Add timeout to auth requests (15 seconds)
3. Add `_validate_config()` in config.py that runs at import time
4. Log clear error messages (what's missing, where to set it)

### Test
```bash
# Test 1: Missing credentials
unset LWA_CLIENT_ID
python -c "import config"  # Should print error and exit with code 1

# Test 2: Transient network error
# (Simulate with mock or network partition)
# Should retry 3 times, then give up
```

---

## PRIORITY #2: Add Database Timeout + Write Serialization (Weakest #2)
**Impact:** Prevents SQLITE_BUSY crashes and connection leaks  
**Effort:** 3-4 hours  
**Files:** `services/db.py`, `main.py` (update all `with sqlite3.connect()` calls)

### Why Second?
- SQLite is single-writer, shared reader
- Multiple sync threads + user endpoints = deadlock
- Connection pooling and WAL mode prevent most issues
- Current code has no protection → data loss or crash under load

### What to Do
1. Rewrite `get_db_connection()` as a context manager with timeout
2. Enable WAL (Write-Ahead Logging) mode for better concurrency
3. Add a write lock that serializes all `INSERT/UPDATE/DELETE`
4. Wrap all existing bare `sqlite3.connect()` calls with new context manager

### Test
```bash
# Simulate concurrent writes
# Run 5 concurrent forecast syncs
# Should serialize, not error
```

---

## PRIORITY #3: Add Timeout + Background Tasks for Long Operations (Weakest #3 + #6)
**Impact:** Prevents UI freeze and improves responsiveness  
**Effort:** 3-4 hours  
**Files:** `main.py` (catalog fetch endpoints), `desktop.py`, `routes/forecast_api.py`

### Why Third?
- Currently, **any long network call blocks entire FastAPI thread pool**
- Forecast sync can take 15+ minutes → UI unresponsive for that duration
- Users see hanging app, restart, lose progress

### What to Do
1. Convert long-running endpoints (`/api/catalog/fetch/*`, `/api/forecast/sync`) to **background tasks**
2. Return immediately with `{"status": "queued"}`
3. Add polling endpoint `/api/forecast/sync-status` to check progress
4. Add request timeouts: 30s for catalog, 60s for downloads, 20s for regular API calls
5. Update desktop.py to check API health before showing UI

### Test
```bash
# Test 1: UI responsiveness during forecast sync
# Start sync, make other requests → should not block

# Test 2: Long-running request timeout
# Simulate slow network, request should timeout after 30s

# Test 3: API startup check
# Kill API, restart, check desktop.py waits 10s for health
```

---

## Implementation Order
1. **Monday:** Config validation + Auth retry (4 hours)
2. **Tuesday:** Database layer hardening (4 hours)
3. **Wednesday:** Async/timeout improvements (4 hours)

Total: ~12 hours for production-hardened version.

---

## Quick Wins (Can Do in Parallel)
- Add timeout to `requests.get()` in `fetch_vendor_pos_from_api()` (15 min)
- Add `load_json_file()` wrapper with error context (30 min)
- Consistent error logging format across modules (1 hour)

