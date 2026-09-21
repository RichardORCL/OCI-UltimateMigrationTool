# Repository review — 21 September 2026

Originally reviewed checkout: `695975d`. The findings below describe that snapshot.

## Remediation status

Implemented in version 0.3.18: all nine numbered findings, regression coverage, lint cleanup,
shared inventory/transfer/source-cleanup helpers, shared cloud inventory UI, deprecated legacy OVA
staging API, contributor documentation, complete generated configuration defaults, dependency
constraints used by deployment/update, and Linux/Windows CI with ZIP and documentation checks.
The screenshot was removed from the current tree; historical copies were not rewritten.
Service names, state paths and the `moid` API field remain compatible and are documented.
Large provider workflows can be split further incrementally; this change extracts cleanup and
shared copy mechanics without replacing the orchestration model.

The original verification figures below apply to the review snapshot, not the remediation.

Remediation validation: **303 tests passed, 3 skipped** on Windows/Python 3.12. Ruff,
JavaScript syntax, dependency consistency, generated defaults, local documentation links and stack
ZIP checks passed. Python 3.11 dependency resolution was also checked. Browser smoke checks against
fake AWS and GCP backends confirmed inventory rendering, filtering and refresh behavior, with no
browser errors. The Linux/Windows CI matrix is configured; live cloud migrations were not run.

## Findings, ordered by priority

### 1. P1 — Azure disk access signatures are written to normal logs

Locations: `helper/helper_app/logging_config.py:66`, `helper/helper_app/azure/client.py:374` and `:383`.

The logging configuration enables the `httpx` logger at INFO. Azure export requests pass complete SAS URLs to httpx, which logs the request URL, including the signature. This happens with the default INFO configuration; enabling OCI request debugging is not required. Anyone who can read the service journal can obtain the disk access URL while it remains valid.

Confirmed offline using MockTransport and a synthetic signature: the signature appeared in captured INFO output. Suppress these request logs or sanitize URLs before emission, and apply the same redaction policy to exception messages and diagnostics. Add a regression test asserting that synthetic credentials never appear in captured logs.

### 2. P1 — Password file read failures disable authentication

Location: `helper/helper_app/ui_password.py:85`.

`reload()` treats every `OSError` as if no password were configured. On a permission or filesystem error, `_record` becomes `None`; `required` becomes false, and the anonymous-session endpoint permits access. A protected deployment can therefore become open after a restart with a damaged or unreadable password file.

Confirmed by injecting `PermissionError`. Only an intentionally absent file should mean an unconfigured password. Other failures should fail startup or keep access locked. Password writes should also use an atomic replacement: the current direct write can leave an empty file after interruption, with the same outcome on restart.

### 3. P1 — A short GCP disk export is accepted as successful

Locations: `helper/helper_app/disk/gcs_range_copy.py:118`, `helper/helper_app/jobs/runner.py:817`.

The tarball copy merely logs a warning when `disk.raw` is smaller than the expected disk, then returns normally. The raw-object branch similarly takes the minimum of expected capacity and object size. Neither path establishes that omitted bytes really are zeros. A wrong or incomplete export can therefore become a successful migration with missing data.

Confirmed with a valid tarball containing a 512-byte disk and an expected capacity of 1,024 bytes: the copy returned success. Require the expected logical size, or explicitly verify a format contract that establishes omitted regions as zeros. Sparse allocation alone is not evidence that a shorter logical file is complete.

### 4. P2 — AWS cleanup loses the caller's credentials

Locations: `helper/helper_app/api/routes_jobs.py:580`, `helper/helper_app/jobs/runner.py:318`.

Cancellation passes the session to cleanup only if it contains Azure or GCP credentials. An AWS-authenticated user cancelling a failed/restarted AWS job therefore reaches `_release_aws()` without an AWS session. Snapshots remain and continue to incur charges. Include AWS in the condition, preferably through a shared provider/session helper, and test the API-to-runner cleanup path.

Related issue: `Provisioner.cleanup()` marks the job CANCELLED before cloud cleanup, and the cancellation endpoint rejects CANCELLED jobs. The message telling users to log in and cancel again cannot be followed after this transition. Preserve a retryable cleanup state or provide an idempotent cleanup endpoint, including when cloud deletion fails.

### 5. P2 — VMDK failures leave the target file descriptor open

Location: `helper/helper_app/disk/object_vmdk_copy.py:41`.

`writer.close()` runs only after the complete decode succeeds. Invalid input, download failures, decoder failures, or progress callback errors bypass it. Repeated failed imports leak descriptors and skip the writer's explicit flush/close. Object response streams also lack explicit lifetime management in the iterator helpers.

Confirmed by injecting an invalid VMDK header and observing that the writer was not closed. Use the writer's context manager and close response streams in `finally`/context-manager blocks.

### 6. P2 — Package and application versions disagree

Locations: `helper/pyproject.toml:7`, `helper/helper_app/__init__.py:3`.

The distribution version is `0.3.17`, while `__version__` is `0.3.8`. Health, OpenAPI, Setup and diagnostic output use the latter. This makes deployment verification and bug reports misleading. Derive both from one source, such as installed package metadata, with an explicit development fallback.

### 7. P2 — OVF boot order is read but discarded

Location: `helper/helper_app/ova/ovf.py:130`.

The parser reads the Boot `order` attribute into an unused variable and appends references in XML order. `boot_disk_index()` then picks the first reference. An input listing order 2 before order 1 selects the wrong boot disk. Confirmed with a two-disk fixture. Sort validated boot references by their numeric order; define behavior for absent or invalid priorities.

### 8. P2 — OVA imports cannot cancel during disk transfer

Locations: `helper/helper_app/oci/ova_import.py:143` and `:181`, `helper/helper_app/disk/object_vmdk_copy.py:59`.

The cancellation callback is checked between disks, but not passed to the VMDK stream decoder. Once a large disk begins copying, cancellation waits for that entire copy to finish or fail. The runner checks cancellation after the import returns, so this is a cancellation-delay issue rather than a claim that every cancellation is ignored. Pass a callback through the copy helpers and check it per chunk, with safe writer/stream cleanup.

### 9. P3 — The published screenshot includes personal and internal details

Location: `docs/Screenshot.png`.

The browser chrome exposes a private network IP and a browser profile photo. These are not authentication secrets, but are unnecessary in public product documentation. Replace it with a cropped application-only image or a screenshot from a neutral demo environment.

## Unused code and naming

- Ruff reports **41 findings**: 17 import-order issues, 9 long lines, 6 unused imports, 4 unused locals, 4 closure-binding warnings and 1 unused loop variable.
- Production unused imports include `GcpClient` in `gcp/inventory.py:9` and `io` in `ova/package.py:5`. The unused OVF `order` variable is a functional issue, not simply something to delete.
- `disk/object_vmdk_copy.py:29` (`vmdk_capacity_bytes`) and `vsphere/inventory.py:12` (`PreflightError`) have no other Python references in the repository. They are removal candidates, subject to any external callers. The unused capacity helper also checks for only 12 bytes before reading an eight-byte field at offset 16.
- `branding.TAG_NAMESPACE` and the `PAGE_BYTES` / `VHD_FOOTER_BYTES` constants in `disk/vhd_range_copy.py` have no other Python references.
- `ova.package.parse_and_stage()` and its staging helpers remain exported and tested, but the active importer uses `parse_ova_layout()`. Decide whether this older API is supported; deprecate it or remove it and its obsolete tests together. Do not classify it as wholly unreachable because it is publicly exported.
- The four Ruff closure warnings in `oci/ova_import.py` are not confirmed runtime bugs: the callback is consumed synchronously during each iteration. Bind the disk explicitly or extract a callback factory to make the lifetime clear.
- Branding spans `OCI Ultimate Migration Tool`, `OCI Migration Tool`, `oci-umt`, `vc-oci-helper`, `vcoci_session` and `/opt/vc-oci`. Document stable internal compatibility names before renaming services, state paths or cookies. Centralize new display names and prefixes rather than doing a blind replacement.
- `copy_sequential()` actually uses parallel range requests. Rename it to describe that behavior. Shared VM models still call all provider identifiers `moid`; document this compatibility field or migrate through an alias to a provider-neutral name.
- Some comments still describe only VMware/Azure despite supporting AWS/GCP, notably sessions/auth, the cleanup runner docstring and contradictory comments above the Setup router.

## Duplication and maintainability

- The Azure, GCP and AWS inventory routes repeat the 30-second cache algorithm and much of inspection-result construction. Extract a small typed cache helper and OS-mapping builder while keeping provider-specific errors and preflight rules separate.
- Source selection for cleanup is duplicated across API and runner code; the missed AWS branch above demonstrates the cost. Define the provider selection once and test every supported provider through the same contract.
- Provider copy code repeats statistics, worker scheduling, retry and cancellation mechanics. Share these mechanics selectively; SAS refresh, EBS checksums and GCS archive handling remain provider-specific.
- `ova/package.py` has two OVA-to-OVF reading paths (`read_ovf_from_object` and `_ovf_from_ova_stream`) plus older staging code. Consolidate the stream traversal before adding new formats.
- `ui/app.js` is one large file with repeated provider pages and import form logic. Extract shared form controls and provider configuration; keep browser escaping and validation centralized.
- `jobs/runner.py` and `oci/provision.py` each exceed 1,100 lines. Extract provider workflows and resource lifecycle operations gradually, retaining explicit orchestration and injectable dependencies.
- Dependencies have minimum versions only, deployment follows `main` by default, and `.terraform.lock.hcl` is ignored. There is no checked-in CI workflow. Add a tested dependency lock/constraints policy, a Linux CI test/lint job, and a check that the distributed Terraform ZIP matches its sources. This is a reproducibility gap, not a claim that an installed dependency is vulnerable.

## Documentation review

- **README contradicts implemented features:** `README.md:25` excludes guest-side network/driver reconfiguration, while the implementation and limitations describe default Linux network and initramfs fixes plus provider cleanup. State exactly which Linux changes are supported and which Windows/manual steps remain necessary.
- **README overpromises:** the opening guarantee that every migration produces a ready-to-run instance is incompatible with the documented guest-driver, Secure Boot and unsupported-guest limitations. Describe the intended result and link prerequisites prominently.
- **Configuration reference is incomplete:** `docs/install-helper.md:140` lists Azure settings but omits AWS and GCP stop/snapshot/export timeouts and range worker settings present in `Settings`. Generate or validate the table from configuration metadata.
- **No contributor workflow:** add a short development section covering Python version, editable installation with dev dependencies, test and lint commands, mock-mode requirements, Linux-only operations and stack packaging.
- **Security guidance should match the access model:** explain near Quick Start that an unprotected anonymous UI session has OCI management capabilities, and that all unlocked sessions share broad authority; source login is not per-job authorization. The limitations document describes this, but it is easy to miss. Include redaction guidance for diagnostics and request dumps.
- **Cleanup instructions need correction:** retrying cancellation after login currently fails once a job is CANCELLED, as described in finding 4. Fix behavior and document the actual recovery operation together.
- All checked local Markdown file targets exist. This check did not validate remote URLs or every anchor.
- The committed stack ZIP's five files exactly match the Terraform/cloud-init source files.

## Secret review and verification limits

No convincing live credential was found by the tracked-text pattern scan and manual inspection. Matches for AWS credentials and a private-key marker are explicitly fake test fixtures. The GitHub owner/repository references are expected public deployment links, not secrets. The screenshot and runtime SAS logging findings still require attention.

The scan covered tracked current-checkout text, with third-party vendor notices excluded from personal-data findings, and included visual inspection of the screenshot and comparison of the deployment ZIP. It was not an exhaustive entropy scan, full Git-history secret audit, dependency vulnerability audit or live cloud penetration test. No cloud resources were changed and no real credentials were exercised.

Validation: **285 tests passed, 3 skipped, 2 dependency deprecation warnings** on Windows/Python 3.12 with freshly installed declared dependencies. Ruff found the 41 issues above. Five additional offline probes reproduced SAS logging, password fail-open, short GCP export acceptance, unclosed VMDK writer and ignored OVF boot order. Linux block devices, systemd, guest chroot operations and live provider APIs were not exercised. Local review tooling and outputs are under the ignored `.venv` directory.
