.RECIPEPREFIX := >
PYTHON := .venv/bin/python
TRIVY_CACHE := .cache/trivy

.PHONY: setup run test scan-template

setup:
> python3 -m venv .venv
> $(PYTHON) -m pip install --upgrade pip
> $(PYTHON) -m pip install -r requirements.txt
> bash scripts/setup_trivy.sh $(TRIVY_CACHE)

run:
> $(PYTHON) -m agent

test:
> $(PYTHON) -m pytest -q

scan-template:
> trivy fs --cache-dir $(TRIVY_CACHE) --skip-db-update --scanners vuln --format json sandbox/template
