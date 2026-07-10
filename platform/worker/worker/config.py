"""Worker settings (host service, §2.3). Reuses app.config for db/redis/orthanc; adds the
host paths + image names the worker needs to launch sibling podman jobs."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MELD7T_", env_file=".env", extra="ignore")

    repo_dir: str = "/var/home/bazzite/meld7t"
    meld_data: str = "/var/home/bazzite/meld7t/meld-data"
    dicom_staging: str = "/var/home/bazzite/meld7t/data/staging"
    fs_license: str = "/var/home/bazzite/meld7t/secrets/license.txt"
    meld_license: str = "/var/home/bazzite/meld7t/secrets/meld_license.txt"

    pkg_image: str = "localhost/meld7t/pkg:0.3.0"
    meld_image: str = "meldproject/meld_graph:v2.2.5_gpu"
    hippunfold_image: str = "docker.io/khanlab/hippunfold:latest"  # HS (§25.5)
    # Orthanc DICOMweb as seen from INSIDE meld-net (the pkg STOW container joins meld-net).
    orthanc_innet: str = "http://orthanc:8042/dicom-web"

    gpu_lock_key: str = "meld7t:gpu:inuse"
    queue_paused_key: str = "meld7t:queue:paused"


wsettings = WorkerSettings()
