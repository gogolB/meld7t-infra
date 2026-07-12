#!/usr/bin/env python3
"""Reject ambiguous planned Podman network topology without mutating host state."""
from __future__ import annotations

import ipaddress
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    name: str
    network: ipaddress.IPv4Network | ipaddress.IPv6Network | None
    origin: str


def _load(path: Path, *, planned: bool) -> list[Entry]:
    entries: list[Entry] = []
    fields = 3 if planned else 2
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parts = raw.split("\t")
        if len(parts) != fields or not all(parts):
            raise ValueError(f"{path}:{line_number}: malformed network inventory row")
        name, subnet = parts[:2]
        if not planned and subnet == "-":
            network = None
        else:
            try:
                network = ipaddress.ip_network(subnet, strict=True)
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid canonical subnet {subnet!r}"
                ) from exc
        entries.append(Entry(name, network, parts[2] if planned else f"existing:{name}"))
    return entries


def validate(planned: list[Entry], existing: list[Entry]) -> None:
    planned_by_name: dict[str, set[object]] = defaultdict(set)
    existing_by_name: dict[str, set[object]] = defaultdict(set)
    for entry in planned:
        assert entry.network is not None
        planned_by_name[entry.name].add(entry.network)
    for entry in existing:
        existing_by_name[entry.name]
        if entry.network is not None:
            existing_by_name[entry.name].add(entry.network)

    if len(planned_by_name) != len(planned):
        raise ValueError("each planned Quadlet network must have one unique NetworkName")

    for index, left in enumerate(planned):
        for right in planned[index + 1:]:
            assert left.network is not None and right.network is not None
            if left.network.version == right.network.version and left.network.overlaps(right.network):
                raise ValueError(
                    f"planned subnet {left.network} ({left.origin}) overlaps "
                    f"{right.network} ({right.origin})"
                )

    for name, actual in existing_by_name.items():
        expected = planned_by_name.get(name)
        if expected is not None and actual != expected:
            raise ValueError(
                f"existing Podman network {name!r} has subnets "
                f"{sorted(map(str, actual))}, expected {sorted(map(str, expected))}"
            )

    for candidate in planned:
        for current in existing:
            if current.name == candidate.name:
                continue
            if current.network is None:
                continue
            assert candidate.network is not None
            if (candidate.network.version == current.network.version
                    and candidate.network.overlaps(current.network)):
                raise ValueError(
                    f"planned subnet {candidate.network} ({candidate.origin}) overlaps "
                    f"existing Podman network {current.name!r} subnet {current.network}"
                )


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: validate-network-subnets.py PLANNED_TSV EXISTING_TSV", file=sys.stderr)
        return 64
    try:
        validate(_load(Path(sys.argv[1]), planned=True),
                 _load(Path(sys.argv[2]), planned=False))
    except (OSError, ValueError) as exc:
        print(f"validate-network-subnets: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
