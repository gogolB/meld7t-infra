#!/usr/bin/env python3
"""Validate the signed release relationship between profiles and cohort bootstrap."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate_policy(inventory: object, profile_count: int, bootstrap_allowed: bool) -> None:
    if not isinstance(inventory, list):
        raise ValueError("expected harmonization inventory must be a JSON array")
    if bootstrap_allowed:
        if profile_count != 0:
            raise ValueError("empty-bootstrap release cannot contain harmonization profiles")
        if inventory != []:
            raise ValueError("empty-bootstrap release inventory must be exactly empty")
        return
    if profile_count < 1:
        raise ValueError("production release requires at least one harmonization profile")
    if not inventory:
        raise ValueError("production release requires a non-empty harmonization inventory")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--profile-count", required=True, type=int)
    parser.add_argument("--bootstrap-allowed", required=True, choices=("true", "false"))
    args = parser.parse_args()
    if args.profile_count < 0:
        parser.error("--profile-count cannot be negative")
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    validate_policy(inventory, args.profile_count, args.bootstrap_allowed == "true")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"harmonization-release-policy: {exc}") from exc
