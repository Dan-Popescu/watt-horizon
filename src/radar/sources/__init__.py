"""Connectors. Convention per module: pure `parse_*` functions (HTML/JSON/XML -> records, unit-tested with
fixtures) + one `collect_*` function that fetches through `PoliteHttp` and ingests. Add regions in `regions.py`."""
