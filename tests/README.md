# Tests

Two layers:

- **`test_offline.py`** — the HA-independent core (dataset parsing, value
  typing, curated registry, binary-state decoding, login-field extraction).
  Pure Python, no Home Assistant import. Runs anywhere:

  ```bash
  python tests/test_offline.py
  ```

- **`test_coordinator.py` / `test_config_flow.py`** — exercise the Home
  Assistant integration (coordinator auth handling, multi-brand config/reauth
  flow). These need the HA test harness:

  ```bash
  pip install -r requirements_test.txt
  pytest tests/ -v
  ```

  The harness (`pytest-homeassistant-custom-component`) imports Unix-only
  modules, so the `pytest` suite runs on **Linux/macOS or in CI**, not on
  Windows. On Windows, run the offline suite above; the full suite runs in
  GitHub Actions (`.github/workflows/test.yml`).
