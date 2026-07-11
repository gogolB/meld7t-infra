"""Deterministic, service-free defaults loaded before application modules are imported."""
from __future__ import annotations

import os


os.environ["MELD7T_DB_URL"] = "sqlite:///./test_meld.db"
os.environ["MELD7T_DEPLOYMENT_MODE"] = "test"
os.environ["MELD7T_AUTH_DEV_BYPASS"] = "true"
os.environ["MELD7T_AUDIT_REQUIRE_IMMUDB"] = "false"
os.environ["MELD7T_HARMONIZATION_REQUIRED"] = "false"
os.environ["MELD7T_IMMUDB_TIMEOUT_SECONDS"] = "0.05"
