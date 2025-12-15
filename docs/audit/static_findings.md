# High-risk modules

- `main.py` (~4,200 LOC) – monolithic bootstrap that mixes FastAPI routes, desktop glue, scheduling, and CLI flags; any edit can ripple across the entire app.
- `services/vendor_realtime_sales.py` (~2,700 LOC) – blends SP-API polling, aggregation, and SQL, so subtle changes can break sales coverage or quotas.
- `services/vendor_inventory_realtime.py` (~550 LOC) – combines inventory math, report parsing, and persistence, leading to tight coupling with vendor tables.
- `services/catalog_service.py` (~650 LOC) – acts as both DAO and business façade; schema or transaction adjustments here affect most catalog writes.
- `services/vendor_rt_inventory_state.py` (~420 LOC) – owns schema creation, checkpoints, parsing, and incremental apply logic; mistakes risk corrupting real-time state.
- `services/vendor_inventory.py` (~380 LOC) – handles Vendor PO calculations while issuing direct DB writes, so logic bugs can mis-state PO readiness.

# Medium-risk modules

- `routes/vendor_rt_inventory_routes.py` – HTTP routes directly query SQLite, enrich catalog metadata, and trigger syncs; a route tweak can change persistence semantics.
- `scripts/apply_vendor_rt_inventory_incremental.py` – CLI script still orchestrates sync windows/checkpoints; flag changes can shift incremental coverage silently.
- `routes/printer_routes.py` & `routes/barcode_print_routes.py` – endpoints talk to printer drivers/filesystem directly; hardware differences make upgrades fragile.
- `services/print_log.py` – mixes logging, status tracking, and formatting for print jobs; errors can wedge the label pipeline.
- Inventory glue across `services/vendor_inventory_realtime.py`, `services/vendor_inventory.py`, `routes/vendor_rt_inventory_routes.py` – shared DTOs/table knowledge mean schema tweaks ripple through UI and scripts.
- Vendor PO helpers in `legacy/modules/vendor_shipments.py` and `legacy/modules/vendor_transactions.py` – legacy modules still manipulate live PO state with limited tests.

# Low-risk modules

- Support tooling (`tools/verify.py`, `tools/debug/*`) – standalone utilities whose failures do not affect runtime behavior.
- Tests/fixtures (`tests/`, `tests/golden/`) – provide coverage scaffolding; edits are isolated from production.

# Notes

- Printing stack spans `routes/printer_routes.py`, `routes/barcode_print_routes.py`, `services/print_log.py`, and device-specific helpers; confirm driver assumptions during audits.
- Inventory + Vendor PO logic is spread across services, routes, and scripts, reflecting mixed responsibilities between HTTP, business rules, and DB access.
- Large modules listed above should be decomposed only after additional safety tooling and regression coverage are in place.
