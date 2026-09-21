"""OCI Ultimate Migration Tool: web UI, NFC export from vCenter/ESXi and block copy onto OCI volumes."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vc-oci-helper")
except PackageNotFoundError:
    # Source checkout without an installed distribution.
    import tomllib
    from pathlib import Path

    __version__ = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["version"]
