#!/usr/bin/env bash
# Check that Trivy is installed, then download its vulnerability DB into a
# local cache so scans are reproducible (scan with --skip-db-update afterwards).
set -euo pipefail

CACHE_DIR="${1:-.cache/trivy}"

if ! command -v trivy >/dev/null 2>&1; then
  echo "Trivy is not installed. Install it, then re-run 'make setup':"
  echo "  macOS:   brew install trivy"
  echo "  Linux:   curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin"
  echo "  Windows: winget install AquaSecurity.Trivy"
  exit 1
fi

mkdir -p "$CACHE_DIR"
echo "Trivy $(trivy --version | head -n1)"
echo "Downloading vulnerability DB into $CACHE_DIR ..."
trivy fs --cache-dir "$CACHE_DIR" --download-db-only
echo "Trivy DB cached in $CACHE_DIR"
