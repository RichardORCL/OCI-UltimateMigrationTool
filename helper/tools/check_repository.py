"""Validate generated defaults, local documentation links, and the Resource Manager ZIP.

Run with --write to regenerate the defaults and ZIP after a reviewed source change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "helper"))

from helper_app.config import Settings  # noqa: E402

STACK_FILES = ("main.tf", "variables.tf", "outputs.tf", "schema.yaml", "cloud-init.yaml")


def configuration_table() -> str:
    lines = ["# Configuration defaults", "",
             "Generated from `helper/helper_app/config.py`; regenerate with",
             "`python helper/tools/check_repository.py --write`. Environment variables use the `HELPER_` prefix.",
             "See [install-helper.md](install-helper.md#configuration-reference) for usage and runtime overrides.", "",
             "| Variable | Default |", "| --- | --- |"]
    for name, field in Settings.model_fields.items():
        value = "<package>/ui" if name == "ui_dir" else field.default
        display = json.dumps(value, ensure_ascii=True).replace("|", "&#124;")
        lines.append(f"| `HELPER_{name.upper()}` | `{display}` |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    config_path = ROOT / "docs/configuration-defaults.md"
    zip_path = ROOT / "oci-ultimate-migration-tool-stack.zip"
    source = ROOT / "helper/deploy/terraform"
    if args.write:
        config_path.write_text(configuration_table(), encoding="utf-8", newline="\n")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in STACK_FILES:
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, (source / name).read_bytes())
    errors = []
    if not config_path.exists() or config_path.read_text(encoding="utf-8") != configuration_table():
        errors.append("Configuration defaults are missing or stale")
    with zipfile.ZipFile(zip_path) as archive:
        if set(archive.namelist()) != set(STACK_FILES):
            errors.append("Unexpected files in stack ZIP")
        for name in STACK_FILES:
            if name not in archive.namelist() or archive.read(name) != (source / name).read_bytes():
                errors.append(f"Stack ZIP differs from source: {name}")
    for doc in [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *(ROOT / "docs").glob("*.md")]:
        for target in re.findall(r"\]\(([^)]+)\)", doc.read_text(encoding="utf-8")):
            if re.match(r"https?://|#", target):
                continue
            if not (doc.parent / target.split("#", 1)[0]).exists():
                errors.append(f"Missing local link in {doc.name}: {target}")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Configuration defaults, local documentation links and stack ZIP match their sources.")


if __name__ == "__main__":
    main()
