#!/usr/bin/env python3
"""Fail-closed service-state gate for the create-only mediaflow installer.

Passes (exit 0) only when every named unit is absent (LoadState=not-found).
Any loaded unit, in any state, or any query problem refuses (exit 3).
"""
import os
import subprocess
import sys

PROPS = ("Id", "LoadState", "ActiveState", "UnitFileState")
TIMEOUT_SECONDS = 15


def systemctl_path():
    if os.environ.get("MEDIAFLOW_INSTALL_TEST") == "1":
        override = os.environ.get("MEDIAFLOW_SYSTEMCTL")
        if override:
            return override
    return "systemctl"


def query(unit):
    cmd = [systemctl_path(), "show"]
    for prop in PROPS:
        cmd += ["-p", prop]
    cmd.append(unit)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        return None, "query-error: systemctl not found"
    except subprocess.TimeoutExpired:
        return None, "query-error: systemctl timed out"
    except OSError as exc:
        return None, f"query-error: {exc.__class__.__name__}"
    if proc.returncode != 0:
        return None, f"query-error: systemctl exited {proc.returncode}"
    values = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value
    if "LoadState" not in values or "ActiveState" not in values:
        return None, "query-error: unparseable systemctl output"
    return values, ""


def classify(values):
    load = values.get("LoadState", "")
    if load == "not-found":
        return True, "absent"
    detail = "load={} active={} unitfile={}".format(
        load, values.get("ActiveState", "?"), values.get("UnitFileState", "") or "none"
    )
    return False, detail


def main(units):
    if not units:
        print("service_state: no units given", file=sys.stderr)
        return 3
    ok = True
    for unit in units:
        values, error = query(unit)
        if values is None:
            print(f"{unit}: REFUSE {error}")
            ok = False
            continue
        absent, detail = classify(values)
        print(f"{unit}: {'ok' if absent else 'REFUSE'} {detail}")
        ok = ok and absent
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
