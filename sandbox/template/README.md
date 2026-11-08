# Target repo template

The repo PatchPilot is asked to fix. It is deliberately vulnerable:

- `requirements.txt` pins `requests==2.25.1`, affected by **CVE-2023-32681**
  (Proxy-Authorization header leaked on redirect to HTTPS). Fixed in **2.31.0**.

The expected fix is a single-line bump of that pin. The 3 tests in `tests/`
must still pass after the bump. `client.py` never makes a network call, so the
tests run offline inside the sandbox.

Every scenario in `eval/scenarios/` is generated from this template.
