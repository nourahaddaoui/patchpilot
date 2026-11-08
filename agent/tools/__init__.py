"""Agent tools. Four tools, only one writes.

- run_scan   (read)  Run Trivy on the sandbox repo and return findings as JSON.
- read_file  (read)  Read a file from an allowlisted path.
- run_tests  (read)  Run the target repo's tests inside the container.
- edit_file  (write) Edit requirements.txt only; idempotent; requires approval.
"""
