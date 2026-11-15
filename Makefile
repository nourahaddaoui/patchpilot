.RECIPEPREFIX := >
# Thin wrappers; the real commands work without make on any OS:
#   python scripts/setup.py   |   python run.py   |   python run.py test
PY ?= python3

.PHONY: setup reset run test scan-template

setup:
> $(PY) scripts/setup.py

reset:
> bash sandbox/reset.sh

run:
> $(PY) run.py

test:
> $(PY) run.py test

scan-template:
> trivy fs --cache-dir .cache/trivy --skip-db-update --scanners vuln --format json sandbox/template
