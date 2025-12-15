# Golden Tests

Golden tests capture canonical outputs so future refactors can change internals without breaking observable behavior.

Usage expectations:
- Keep expected payloads in this directory to lock API response shapes, CSV exports, or printable artifacts.
- When refactoring, update implementation details but avoid touching golden outputs unless business behavior intentionally changes.
- Regenerate golden files only with explicit approval and documentation.
