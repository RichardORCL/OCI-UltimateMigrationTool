"""GCP post-migration cleanup of export prefix and Daisy scratch."""

from __future__ import annotations

from helper_app.gcp.client import parse_service_account_json
from helper_app.gcp.export import daisy_scratch_bucket, release_gcp_resources
from helper_app.models import GcpSourceInfo
from tests.fake_gcp import BUCKET, PROJECT, SA_JSON, ZONE, FakeGcp, _FakeGcpClient


def test_daisy_scratch_bucket_name():
    assert daisy_scratch_bucket("my-gcp-project", "europe-west3-b") == (
        "my-gcp-project-daisy-bkt-europe-west3"
    )


def test_release_deletes_job_prefix_including_daisy_scratch():
    fake = FakeGcp([])
    prefix = "oci-umt/job-1"
    fake.objects[f"{prefix}/0-disk.tar.gz"] = b"tar"
    fake.objects[f"{prefix}/daisy/logs/daisy.log"] = b"log"
    sa = parse_service_account_json(SA_JSON)
    client = _FakeGcpClient(sa["project_id"], sa["client_email"], sa["private_key"],
                            http=fake.http(), sleep=lambda s: None)
    info = GcpSourceInfo(
        project_id=PROJECT,
        zone=ZONE,
        machine_type="n2-standard-2",
        export_bucket=BUCKET,
        export_prefix=prefix,
        gcs_objects=[f"{prefix}/0-disk.tar.gz"],
        snapshot_names=[],
    )
    actions = release_gcp_resources(client, info)
    assert not fake.objects
    assert info.gcs_objects == []
    assert any("0-disk.tar.gz" in a for a in actions)
    assert any("daisy.log" in a for a in actions)
