"""Agent tools. Four tools, only one writes.

- run_scan   (read)  Run Trivy on the sandbox repo and return findings as JSON.
- read_file  (read)  Read a file from an allowlisted path.
- run_tests  (read)  Run the target repo's tests in the sandbox interpreter.
- edit_file  (write) Edit requirements.txt only; idempotent; requires approval. (Day 3)

Every call goes through registry.call_tool(), which validates the arguments
against the tool's strict JSON schema, turns every failure into a plain-text
error the model can act on (never a stack trace), and logs input, output and
duration to logs/<run_id>.jsonl.
"""
