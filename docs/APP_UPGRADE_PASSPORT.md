# APP Upgrade Passport

## Overview

This project is a local Amazon SP-API desktop application that bundles a FastAPI backend, rich UI assets served via `ui/`, and SQLite cache databases. It orchestrates Vendor Purchase Orders, real-time inventory checkpoints, real-time sales reporting, and printing/reconciliation workflows entirely on a workstation.

Core responsibilities:
- Vendor purchase order ingest, tracking, and reconciliation.
- Vendor real-time inventory sync (reports, checkpoints, UI snapshot).
- Vendor real-time sales auditing and coverage.
- Label/print management with local device integrations.
- Catalog cache maintenance and SQLite-backed analytics.

## Architecture Truth
- `routes/`: HTTP surface only. Routes may validate input, call services, and shape responses, but never perform business logic or contain SQL.
- `services/`: Business logic, orchestration, and DB access. All persistence, SP-API calls, and heavy calculations live here.
- Database access must remain centralized inside services/helpers; routes must never execute ad-hoc SQL.
- `ui/`: Presentation layer (HTML/JS/CSS) only. No direct business logic or DB calls.
- `tools/`: Audit, verification, and developer utilities. These must be side-effect free relative to production runtime.

## Non-Negotiable Contracts
- Endpoint response shapes must not drift silently. Additive or breaking changes require explicit coordination with UI/tools.
- UI column order, labels, and semantics are locked unless explicitly coordinated; the desktop operator workflow depends on stable layouts.
- CSV exports must mirror the UI tables exactly (header order, naming, field normalization).
- Inventory and Vendor PO semantics (windowing, filters, checkpoint rules) must change only with deliberate intent and accompanying documentation/tests.

## Known Fragile Zones
- **High risk (see docs/audit/static_findings.md):**
  - `main.py`: monolithic bootstrap; touching it impacts routing, scheduling, and desktop plumbing simultaneously.
  - `services/vendor_realtime_sales.py`: mixes polling, aggregation, and SQL; regressions break audit coverage.
  - `services/vendor_inventory_realtime.py`, `services/vendor_rt_inventory_state.py`, `services/vendor_inventory.py`, `services/catalog_service.py`: shared DTOs and DB state; any change can corrupt inventory or catalog data.
- **Medium risk:**
  - `routes/vendor_rt_inventory_routes.py`, `routes/printer_routes.py`, `routes/barcode_print_routes.py`: routes still reach into DB and hardware; require careful review to avoid side effects.
  - `scripts/apply_vendor_rt_inventory_incremental.py`: operational script orchestrates critical sync windows; CLI tweaks can degrade coverage.
  - `services/print_log.py`, legacy vendor PO modules (`legacy/modules/vendor_*`): legacy patterns, limited tests, heavy side effects.

These zones require tighter testing, code review, and incremental rollouts because small changes can affect production state, hardware devices, or operator workflows.

## Safe Upgrade Rules
- Refactor one surface at a time (routes OR services OR UI). No cross-layer rewrites in a single change.
- Use feature flags and configuration gates for behavioral changes so desktop operators can revert quickly.
- Record golden outputs (e.g., CSV snapshots, API payloads) before structural refactors to verify equivalence.
- Avoid mixed-responsibility edits—never blend UI tweaks with DB migrations in one PR.

## Decomposition Plan
- `app/bootstrap.py`: future home for app startup glue currently inside `main.py`.
- `app/http.py`: will host route-registration helpers and HTTP-only utilities.
- `app/cli.py`: planned destination for CLI argument parsing and entrypoints.
- `app/paths.py`: centralizes filesystem/Path handling shared across modules.
- `app/logging_setup.py`: consolidates logging formatter/handler setup.

## Verification Workflow
- Always run `python tools/verify.py` before committing or deploying upgrades. It enforces `ruff check .`, `python -m compileall -q .`, and an import sanity check for `main.py`.
- Treat `tools/verify.py` as mandatory preflight; no upgrade should proceed without a clean verify run.
