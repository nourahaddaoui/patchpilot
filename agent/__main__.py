"""Entry point for `python -m agent`."""

from agent.stop_rule import Limits


def main() -> None:
    limits = Limits()
    print("PatchPilot: the agent loop arrives later (agent/loop.py).")
    print("Stop-rule limits:")
    print(f"  max_steps        = {limits.max_steps}")
    print(f"  max_cost_usd     = {limits.max_cost_usd:.2f}")
    print(f"  max_failed_fixes = {limits.max_failed_fixes}")


if __name__ == "__main__":
    main()
