.RECIPEPREFIX := >
PYTHON := .venv/bin/python
SANDBOX_PYTHON := sandbox/.venv/bin/python
TRIVY_CACHE := .cache/trivy

.PHONY: setup reset run test scan-template

setup:
> python3 -m venv .venv
> $(PYTHON) -m pip install --upgrade pip
> $(PYTHON) -m pip install -r requirements.txt
> python3 -m venv sandbox/.venv
> $(SANDBOX_PYTHON) -m pip install pytest==8.3.3
> bash scripts/setup_trivy.sh $(TRIVY_CACHE)

reset:
> bash sandbox/reset.sh

run:
> $(PYTHON) -m agent

test:
> $(PYTHON) -m pytest -q

scan-template:
> trivy fs --cache-dir $(TRIVY_CACHE) --skip-db-update --scanners vuln --format json sandbox/template
