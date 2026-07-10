"""Settings — env-driven (12-factor). Defaults match the Quadlet services on meld-net."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MELD7T_", env_file=".env", extra="ignore")

    # Postgres (the `meld` DB created by the postgres.container init, §2.1)
    db_url: str = "postgresql+psycopg://meld:meld@postgres:5432/meld"

    # Redis (job broker + hot cache)
    redis_url: str = "redis://redis:6379/0"

    # Orthanc DICOMweb (same meld-net; QIDO/WADO/STOW)
    orthanc_dicomweb: str = "http://orthanc:8042/dicom-web"

    # immudb audit ledger (§26)
    immudb_host: str = "immudb"
    immudb_port: int = 3322
    immudb_user: str = "immudb"
    immudb_password: str = "immudb"
    immudb_db: str = "defaultdb"

    # Fallback audit mode when immudb is unreachable: still hash-chain into Postgres.
    audit_require_immudb: bool = False

    # meld-data root (mounted read-only) — where report PDFs + key-frame PNGs live (§9.1).
    meld_data: str = "/data"


settings = Settings()
