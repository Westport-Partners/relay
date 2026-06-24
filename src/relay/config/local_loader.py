"""Local filesystem config loader for Relay.

A ConfigStore implementation that reads the config YAML files from a directory
on disk (bundled with the deployment artifact) instead of from GitLab.

This is the config source for teams that do NOT use GitLab as a config store
(e.g. GitHub shops), or for self-contained deployments where the config ships
inside the Lambda/container asset. It exposes the same get()/refresh()/load()
interface as GitLabConfigLoader so the handler can use either interchangeably.

Expected files in the config directory (escalation/routing are required;
the other three are optional):
    escalation.yaml  routing.yaml
    environments.yaml  hierarchy.yaml  catalog.yaml
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from relay.config.schema import RelayConfig

logger = logging.getLogger(__name__)

_ESCALATION_FILE = "escalation.yaml"
_ROUTING_FILE = "routing.yaml"
_ENVIRONMENTS_FILE = "environments.yaml"
_HIERARCHY_FILE = "hierarchy.yaml"
_CATALOG_FILE = "catalog.yaml"


class LocalConfigLoader:
    """Loads and caches Relay configuration from a local directory.

    Mirrors :class:`~relay.config.loader.GitLabConfigLoader`'s public surface
    (``get``, ``load``, ``refresh``) so the two are drop-in interchangeable.

    Args:
        config_dir: Directory containing the config YAML files. Defaults to the
            ``RELAY_CONFIG_DIR`` value the caller passes in.
    """

    def __init__(self, config_dir: str | Path) -> None:
        self._dir = Path(config_dir)
        self._config: RelayConfig | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read(self, name: str, *, required: bool) -> str | None:
        path = self._dir / name
        if not path.is_file():
            if required:
                raise FileNotFoundError(
                    f"Required config file '{name}' not found in {self._dir}"
                )
            return None
        return path.read_text(encoding="utf-8")

    def _load_fresh(self) -> RelayConfig:
        escalation_yaml = self._read(_ESCALATION_FILE, required=True)
        routing_yaml = self._read(_ROUTING_FILE, required=True)
        environments_yaml = self._read(_ENVIRONMENTS_FILE, required=False)
        hierarchy_yaml = self._read(_HIERARCHY_FILE, required=False)
        catalog_yaml = self._read(_CATALOG_FILE, required=False)

        logger.info("Loading Relay config from local dir %s", self._dir)
        return RelayConfig.from_yaml_files_extended(
            escalation_yaml=escalation_yaml,  # type: ignore[arg-type]
            routing_yaml=routing_yaml,  # type: ignore[arg-type]
            environments_yaml=environments_yaml,
            hierarchy_yaml=hierarchy_yaml,
            catalog_yaml=catalog_yaml,
        )

    # ------------------------------------------------------------------
    # Public interface (matches GitLabConfigLoader)
    # ------------------------------------------------------------------

    def load(self) -> RelayConfig:
        """Read + validate the config from disk and cache it."""
        new_config = self._load_fresh()
        with self._lock:
            self._config = new_config
        return new_config

    def refresh(self) -> RelayConfig:
        """Re-read the config from disk (e.g. after the asset is updated)."""
        with self._lock:
            self._config = None
        return self.load()

    def get(self) -> RelayConfig:
        """Return the cached config, loading on first call."""
        with self._lock:
            if self._config is not None:
                return self._config
        return self.load()
