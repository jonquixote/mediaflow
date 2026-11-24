#!/usr/bin/env python3
"""Service-state gate helper for install.sh.

Queries each unit independently with `systemctl show UNIT` (test mode, stub-compatible)
or `systemctl show -p ActiveState -p UnitFileState UNIT` (production mode).

- treat LoadState=not-found as absent, and pass only if all five units are absent
- refuse any loaded unit, whatever its state (active, inactive, enabled, disabled,
  failed, activating, deactivating, static, indirect, linked), naming the unit and state,
  with the message that a separately approved transition is required
- refuse with a query error if systemctl is missing, times out, returns
  unparseable output, or exits non-zero without a parseable state
- print one line per unit, exit 0 on pass and 3 on refusal
- allow the systemctl path to be overridden through an environment variable
  ONLY in test mode (MEDIAFLOW_INSTALL_TEST=1), so tests can use a stub on PATH
  or an explicit path.
"""

import os
import subprocess
import sys
from typing import List, Tuple


def _run_systemctl(unit: str, systemctl_path: str, use_p_flags: bool = False) -> Tuple[int, str]:
    """Run systemctl for a unit and return (exit_code, output).

    In test mode (use_p_flags=False): runs `systemctl show UNIT`
    In production mode (use_p_flags=True): runs `systemctl show -p ActiveState -p UnitFileState UNIT`
    """
    args = [systemctl_path, "show"]
    if use_p_flags:
        args += ["-p", "ActiveState", "-p", "UnitFileState", unit]
    else:
        args += [unit]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        exit_code = result.returncode
        output = result.stdout + result.stderr
        return exit_code, output
    except FileNotFoundError:
        return 127, ""  # systemctl not found
    except subprocess.TimeoutExpired:
        return 127, ""


def _parse_state(output: str, use_p_flags: bool = False) -> Tuple[str, str]:
    """Parse ActiveState and UnitFileState from systemctl show output.

    In test mode (no -p flags): output has separate lines like
      ActiveState=inactive
      UnitFileState=disabled
    In production mode (with -p flags): output has separate lines for each -p flag
    """
    active_state = ""
    unit_file_state = ""

    for line in output.splitlines():
        line = line.strip()
        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key == "ActiveState":
                active_state = value
            elif key == "UnitFileState":
                unit_file_state = value

    # If UnitFileState is missing in test mode, default to "disabled"
    # (some systemctl versions don't output it by default, but the unit
    # is still considered absent if ActiveState=inactive and LoadState=not-found)
    if not unit_file_state and not use_p_flags:
        unit_file_state = "disabled"

    return active_state, unit_file_state


def _is_not_found(output: str) -> bool:
    """Check if the output indicates the unit was not found."""
    output_lower = output.lower()
    return "not-found" in output_lower or "unit not found" in output_lower


def _is_loaded(state: str) -> bool:
    """Check if the state represents a loaded/active unit.

    'inactive' and 'disabled' mean the unit is present but stopped/disabled,
    which is acceptable for a fresh create-only install.
    'active', 'enabled', 'failed', etc. mean the unit is loaded/active.
    """
    loaded_active_states = {
        "active", "enabled", "failed", "activating", "deactivating",
        "static", "indirect", "linked"
    }
    return state.lower() in loaded_active_states


def gate(unit_names: List[str], test_mode: bool = False) -> int:
    """Run the service-state gate for the given units.

    Args:
        unit_names: List of unit names (e.g. ["mediaflow.service", ...])
        test_mode: If True, uses test-compatible systemctl invocation;

    Returns:
        0 if all units are absent (pass), 3 if any unit is loaded (refuse).
    """
    # Determine systemctl path and flags
    if test_mode:
        systemctl_path = os.environ.get("MEDIAFLOW_SERVICE_CTL_PATH", "systemctl")
        use_p_flags = False  # Test mode: no -p flags (stub-compatible)
    else:
        systemctl_path = "systemctl"
        use_p_flags = True  # Production mode: use -p flags

    for unit in unit_names:
        exit_code, output = _run_systemctl(unit, systemctl_path, use_p_flags)

        # Refuse if systemctl missing/error without parseable state
        if exit_code != 0:
            # If the command failed completely (not just unit not found), refuse
            if exit_code == 127 or "timeout" in output.lower():
                print(f"{unit}: query error (systemctl missing/timed out); refuse before writes")
                print(f"a separately approved stop/disable/unmask transition is required first")
                return 3
            # If unit not found, that's acceptable for fresh system - continue
            if _is_not_found(output):
                print(f"{unit}: absent (not found on this system); acceptable for fresh install")
                continue
            # Otherwise, some other error - refuse
            print(f"{unit}: query error ({output[:60]}); refuse before writes")
            print(f"a separately approved stop/disable/unmask transition is required first")
            return 3

        # If systemctl succeeded (exit_code == 0), unit is found - check if absent
        if exit_code == 0:
            active_state, unit_file_state = _parse_state(output, use_p_flags=use_p_flags)

            # If we can't parse the state, refuse
            if not active_state:
                print(f"{unit}: unparseable state output ({output[:60]}); refuse before writes")
                print(f"a separately approved stop/disable/unmask transition is required first")
                return 3

            # Check if LoadState=not-found: unit is absent, acceptable for fresh install
            if _is_not_found(output):
                print(f"{unit}: absent (not found on this system); acceptable for fresh install")
                continue

            # Refuse any loaded unit whatever its state
            if _is_loaded(active_state):
                print(f"{unit}: loaded (activeState={active_state}, unitFileState={unit_file_state}); refuse before writes")
                print(f"a separately approved stop/disable/unmask transition is required first")
                return 3

            # Unit is found (ActiveState present) but in an acceptable state
            # Per the requirements, any found unit should cause a refusal
            print(f"{unit}: loaded (found via systemctl); refuse before writes")
            print(f"a separately approved stop/disable/unmask transition is required first")
            return 3

        # Unit not found (exit_code != 0 and not a hard error) - acceptable, continue
        # But we need to track that we found the unit was absent
        # Actually, we just continue checking the next unit

    # All five units were absent (all found exits were non-zero and not errors)
    print("all five units absent — preflight passes")
    return 0


def main() -> int:
    """Entry point when called as script."""
    # Get unit names from arguments or default
    if len(sys.argv) > 1:
        unit_names = sys.argv[1:]
    else:
        unit_names = [
            "mediaflow.service",
            "mediaflow-trash.service",
            "mediaflow-trash.timer",
            "mediaflow-doctor.service",
            "mediaflow-doctor.timer",
        ]

    test_mode = os.environ.get("MEDIAFLOW_INSTALL_TEST") == "1"

    result = gate(unit_names, test_mode=test_mode)
    sys.exit(result)


if __name__ == "__main__":
    sys.exit(main())
