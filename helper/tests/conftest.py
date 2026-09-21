import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Like on a real helper VM, the identity (instance/compartment/AD/region) is *not* configured through
# the environment; the code must take it from clients.identity_info (FakeOci provides it).
for var in ("HELPER_INSTANCE_ID", "HELPER_COMPARTMENT_ID", "HELPER_AVAILABILITY_DOMAIN", "HELPER_REGION",
            "HELPER_TENANCY_ID"):
    os.environ.pop(var, None)
os.environ.setdefault("HELPER_SEED_BUCKET", "oci-umt-seed")
os.environ.setdefault("HELPER_VCENTER_HOST", "vc.test")


@pytest.fixture
def fast_retries():
    """Shorten the runner's back-off sleeps so retry scenarios finish quickly."""
    import helper_app.jobs.runner as runner_mod

    orig_sleep = runner_mod.time.sleep
    runner_mod.time.sleep = lambda s: orig_sleep(min(s, 0.05))
    try:
        yield
    finally:
        runner_mod.time.sleep = orig_sleep
