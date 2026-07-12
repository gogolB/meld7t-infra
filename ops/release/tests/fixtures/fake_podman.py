#!/usr/bin/env python3
"""Minimal Podman stand-in for hermetic import-airgap integration tests."""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path


args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    print(args[2].rsplit("@", 1)[1])
elif args[:2] == ["volume", "exists"]:
    raise SystemExit(0)
elif args[:2] == ["volume", "export"]:
    with Path(os.environ["FAKE_PODMAN_CACHE_TAR"]).open("rb") as source:
        shutil.copyfileobj(source, sys.stdout.buffer)
elif args and args[0] == "load":
    raise SystemExit(0)
elif args and args[0] == "run":
    mount = next(
        value for value in args
        if ":/run/cache-files.sha256" in value
    )
    manifest = Path(mount.split(":/run/cache-files.sha256", 1)[0])
    print(hashlib.sha256(manifest.read_bytes()).hexdigest())
else:
    print(f"unsupported fake podman invocation: {args!r}", file=sys.stderr)
    raise SystemExit(2)
