"""PatchPilot: a vulnerability remediation agent.

Scans a Python repo with Trivy, proposes a dependency bump in requirements.txt,
runs the tests, and commits to a sandbox branch only after a human approves.
"""
