# Development

Maintainer and contributor: [RichardORCL](https://github.com/RichardORCL).
Use that identity for project contributions. Preserve existing license and third-party notices.

## Local setup

Python 3.11 or 3.12 is supported by CI. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
# PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -c helper/constraints.txt -e './helper[dev]'
python -m pytest helper/tests -q
python -m ruff check helper
node --check helper/helper_app/ui/app.js
python helper/tools/check_repository.py
```

Tests use fake cloud clients and do not need live credentials. The application factory accepts injected
clients, sessions, command runners and guest fixers; the `mock` setting alone does not create those
clients. Do not launch the normal service expecting `HELPER_OCI_AUTH=mock` to emulate a cloud.

The deployed service targets Oracle Linux 9. Real block-device, systemd, guest-mount/chroot and OCI
instance-principal behavior needs a Linux test VM. Windows tests cover portable logic and mocks;
they cannot establish that a real migration will succeed.

## Dependencies and releases

`helper/constraints.txt` pins the tested application and development dependency versions. Both the
cloud-init installer and self-updater use it. Constraints control versions of selected dependencies;
they are not a wheel hash lock or an offline package mirror. Linux-only uvloop is pinned separately.
To update, resolve dependencies in a clean environment, update the constraints, and run the Windows
and Linux CI matrix. Do not generate constraints from a personal environment containing unrelated packages.

The distribution version in `helper/pyproject.toml` is authoritative. Runtime version reporting uses
installed distribution metadata, falling back to that file in an uninstalled source checkout.
Reinstall the editable package after changing its version. Releases can deploy a reviewed tag or commit
using `source_git_ref`; `main` remains the default for compatibility with the existing self-updater.

Terraform provider lock files are no longer ignored. When changing provider constraints, run
`terraform init` and review/commit the generated `.terraform.lock.hcl` from your Terraform environment.
The current Resource Manager package retains its five-file layout.

## Generated files

After changing deployment templates or configuration defaults:

```bash
python helper/tools/check_repository.py --write
python helper/tools/check_repository.py
```

This regenerates `docs/configuration-defaults.md` and the committed Resource Manager ZIP. CI checks
that the ZIP matches the source templates and local documentation file links resolve.

## Naming and compatibility

Use **OCI Ultimate Migration Tool** in product copy and `oci-umt` for new OCI labels. The distribution,
CLI and systemd service remain `vc-oci-helper`; `/opt/vc-oci`, `/var/lib/vc-oci-helper`, `HELPER_`
environment variables and the `vcoci_session` cookie are compatibility identifiers. Changing them
requires an explicit upgrade path. The shared `moid` API field holds provider-native VM identifiers.

`ova.package.parse_and_stage` is deprecated but retained for external callers. New imports use
`parse_ova_layout`; remove the deprecated API only with a documented compatibility change.
Provider-specific cloud behavior stays in provider modules. Reuse the common inventory, transfer
worker/statistics and source cleanup helpers rather than copying provider conditionals.

## Security and diagnostics

Never commit credentials, customer identifiers or browser screenshots containing personal details.
Tests should use clearly fake credentials. URL queries, common credential fields and key blocks are
redacted from logging and diagnostics; raw HTTP wire dumps are disabled. Resource names, addresses
and identifiers remain, so inspect diagnostics before sharing. Secret scanning is defense in depth,
not a guarantee that arbitrary guest messages cannot contain sensitive information.
