# Notifications Component Architecture

## Current Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    SP-API Desktop App                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                    FRONTEND (ui/index.html)              │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │                                                          │  │
│  │  Tabs Navigation:                                       │  │
│  │  ├─ Vendor POs (with notification badges)              │  │
│  │  ├─ Catalog Fetcher                                    │  │
│  │  ├─ Out-of-Stock Items                                 │  │
│  │  ├─ Endpoint Tester                                    │  │
│  │  ├─ Vendor Real Time Sales                             │  │
│  │  └─ Notifications ← TO REMOVE                          │  │
│  │                                                          │  │
│  │  Notifications Tab Panel (TO REMOVE):                   │  │
│  │  ├─ loadNotifications() → fetches /api/vendor-notify... │  │
│  │  ├─ Displays recent notifications                       │  │
│  │  ├─ Links to POs                                        │  │
│  │  └─ recentNotifications variable                        │  │
│  │                                                          │  │
│  │  PO Tab Integration (KEEP):                             │  │
│  │  ├─ Notification badges on PO list                      │  │
│  │  ├─ "Update Ready" label                                │  │
│  │  ├─ Notification info in detail view                    │  │
│  │  └─ Uses: get_po_notification_flags()                   │  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│         ↓                                    ↓                   │
│         │ API Calls                         │ API Calls         │
│         │                                    │                   │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              BACKEND (FastAPI - main.py)                │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │                                                          │  │
│  │  Endpoints to REMOVE:                                   │  │
│  │  ├─ POST /api/vendor-notifications/test-ingest          │  │
│  │  │  └─ Calls: process_vendor_notification()             │  │
│  │  │                                                       │  │
│  │  └─ GET /api/vendor-notifications/recent                │  │
│  │     └─ Returns: get_recent_notifications()              │  │
│  │                                                          │  │
│  │  Endpoints to KEEP (use notification service):          │  │
│  │  ├─ GET /api/vendor-pos                                 │  │
│  │  │  └─ Calls: get_po_notification_flags()               │  │
│  │  │     └─ Returns: notificationFlags                    │  │
│  │  │                                                       │  │
│  │  └─ GET /api/vendor-pos/{poNumber}                      │  │
│  │     └─ Calls: get_po_notification_flags()               │  │
│  │        └─ Returns: notificationFlags                    │  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                ↓                                                │
│         Uses Notification Service                              │
│                ↓                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │    Notification Service (services/vendor_notify...)     │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │                                                          │  │
│  │  Core Functions (KEEP ALL):                             │  │
│  │  ├─ get_po_notification_flags()                          │  │
│  │  │  └─ Reads: logs/vendor_po_flags.json                 │  │
│  │  │  └─ Used by: PO list & detail endpoints              │  │
│  │  │                                                       │  │
│  │  ├─ mark_po_as_needing_refresh()                         │  │
│  │  │  └─ Writes: logs/vendor_po_flags.json                │  │
│  │  │  └─ Called by: process_vendor_notification()         │  │
│  │  │                                                       │  │
│  │  ├─ clear_po_refresh_flag()                              │  │
│  │  │  └─ Writes: logs/vendor_po_flags.json                │  │
│  │  │  └─ Called by: PO refresh logic                      │  │
│  │  │                                                       │  │
│  │  ├─ log_vendor_notification()                            │  │
│  │  │  └─ Appends to: logs/vendor_notifications.jsonl      │  │
│  │  │  └─ Called by: process_vendor_notification()         │  │
│  │  │                                                       │  │
│  │  ├─ process_vendor_notification() [REMOVE CALL FROM UI] │  │
│  │  │  └─ Not called from anywhere critical                │  │
│  │  │  └─ Only from test-ingest endpoint (being removed)   │  │
│  │  │                                                       │  │
│  │  └─ get_recent_notifications() [REMOVE CALL]            │  │
│  │     └─ Reads: logs/vendor_notifications.jsonl           │  │
│  │     └─ Used only by: /recent endpoint (being removed)   │  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                ↓                                                │
│         Uses Data Files                                        │
│                ↓                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │            Log/State Files (KEEP ALL)                    │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │                                                          │  │
│  │  ├─ logs/vendor_po_flags.json                            │  │
│  │  │  └─ Contains: PO notification flags, refresh states   │  │
│  │  │  └─ Used by: get_po_notification_flags()             │  │
│  │  │                                                       │  │
│  │  └─ logs/vendor_notifications.jsonl                      │  │
│  │     └─ Contains: Notification history                    │  │
│  │     └─ Used by: get_recent_notifications()              │  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## After Removal (What Changes)

```
┌─────────────────────────────────────────────────────────────────┐
│                    SP-API Desktop App (After)                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                    FRONTEND (ui/index.html)              │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │                                                          │  │
│  │  Tabs Navigation:                                       │  │
│  │  ├─ Vendor POs (with notification badges) ✓ KEEP       │  │
│  │  ├─ Catalog Fetcher                       ✓ KEEP       │  │
│  │  ├─ Out-of-Stock Items                    ✓ KEEP       │  │
│  │  ├─ Endpoint Tester                       ✓ KEEP       │  │
│  │  ├─ Vendor Real Time Sales                ✓ KEEP       │  │
│  │  └─ Notifications ✗ DELETED                             │  │
│  │                                                          │  │
│  │  PO Tab Integration:                                    │  │
│  │  ├─ Notification badges on PO list        ✓ KEEP       │  │
│  │  ├─ "Update Ready" label                  ✓ KEEP       │  │
│  │  ├─ Notification info in detail view      ✓ KEEP       │  │
│  │  └─ Uses: get_po_notification_flags()     ✓ KEEP       │  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│         ↓                                    ↓                   │
│         │ API Calls                         │ API Calls         │
│         │                                    │                   │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              BACKEND (FastAPI - main.py)                │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │                                                          │  │
│  │  Endpoints:                                             │  │
│  │  (All removed notification-specific endpoints)          │  │
│  │                                                          │  │
│  │  Endpoints to KEEP (still use notification service):   │  │
│  │  ├─ GET /api/vendor-pos                                 │  │
│  │  │  └─ Calls: get_po_notification_flags()  ✓            │  │
│  │  │     └─ Returns: notificationFlags       ✓            │  │
│  │  │                                                       │  │
│  │  └─ GET /api/vendor-pos/{poNumber}                      │  │
│  │     └─ Calls: get_po_notification_flags()  ✓            │  │
│  │        └─ Returns: notificationFlags       ✓            │  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                ↓                                                │
│         Uses Notification Service                              │
│                ↓                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │    Notification Service (services/vendor_notify...)     │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │                                                          │  │
│  │  Core Functions (KEEP ALL):                             │  │
│  │  ├─ get_po_notification_flags()            ✓ KEEP       │  │
│  │  ├─ mark_po_as_needing_refresh()           ✓ KEEP       │  │
│  │  ├─ clear_po_refresh_flag()                ✓ KEEP       │  │
│  │  ├─ log_vendor_notification()              ✓ KEEP       │  │
│  │  ├─ process_vendor_notification()          ✓ KEEP       │  │
│  │  │                                                       │  │
│  │  └─ get_recent_notifications()     ✗ REMOVED (no calls)│  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                ↓                                                │
│         Uses Data Files                                        │
│                ↓                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │            Log/State Files (KEEP ALL)                    │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │                                                          │  │
│  │  ├─ logs/vendor_po_flags.json              ✓ KEEP       │  │
│  │  │  └─ Still used by: get_po_notification_flags()      │  │
│  │  │                                                       │  │
│  │  └─ logs/vendor_notifications.jsonl                      │  │
│  │     └─ Still created by: log_vendor_notification()     │  │
│  │     └─ No longer read (UI tab deleted)                 │  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Impact Summary

### ✅ Will Continue to Work

```
PO List Endpoint
├─ Still calls: get_po_notification_flags()
├─ Still includes: notificationFlags in response
├─ Still shows: Notification badges on POs
└─ Result: Users still see "Updated" or "Update Ready" labels

PO Detail Endpoint  
├─ Still calls: get_po_notification_flags()
├─ Still includes: notificationFlags in response
├─ Still shows: Last notification info
└─ Result: Users still see notification history in PO detail

Notification Service
├─ Still logs: All vendor notifications to file
├─ Still tracks: PO refresh needs
├─ Still marks: POs that need updating
└─ Result: System still knows when POs are out of date

Notification Files
├─ Still created: logs/vendor_po_flags.json
├─ Still created: logs/vendor_notifications.jsonl
├─ Still updated: On every notification event
└─ Result: Complete notification history preserved
```

### ❌ Will Stop Working

```
Notifications Tab
├─ No longer accessible from navigation
├─ No longer loads notification history
└─ Result: Users can't view notifications in UI

Recent Notifications API
├─ /api/vendor-notifications/recent endpoint gone
└─ Result: No way to get recent notifications via API

Test Ingest Endpoint
├─ /api/vendor-notifications/test-ingest endpoint gone
└─ Result: Can't test notification ingestion
```

---

## Conclusion

**The notification service continues to power PO features** even after the tab is removed.

The UI tab is just a "window" into the notification data. Removing the window doesn't break the system that creates and uses the data.

