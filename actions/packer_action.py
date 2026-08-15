#!/usr/bin/env python3
"""Shared stdin/JSON entry point for Packer actions."""

from __future__ import annotations

import json
import os
import sys

_PACK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACK_ROOT not in sys.path:
    sys.path.insert(0, _PACK_ROOT)

from lib.packer_runner import PackerCancelled, PackerError, execute  # noqa: E402


def main() -> int:
    try:
        raw = sys.stdin.read()
        params = json.loads(raw) if raw.strip() else {}
        if not isinstance(params, dict):
            raise PackerError("action parameters must be a JSON object")
        operation = os.environ.get("ATTUNE_ACTION", "").rsplit(".", 1)[-1]
        result = execute(operation, params)
        json.dump(result, sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0 if result["success"] else 1
    except json.JSONDecodeError:
        print("packer action failed: invalid JSON parameters", file=sys.stderr)
    except PackerError as exc:
        print(f"packer action failed: {exc}", file=sys.stderr)
    except (PackerCancelled, KeyboardInterrupt):
        print("packer action cancelled", file=sys.stderr)
        return 130
    except Exception:
        print("packer action failed: unexpected internal error", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
