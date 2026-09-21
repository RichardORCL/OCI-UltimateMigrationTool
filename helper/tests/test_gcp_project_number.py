"""GCP project number / default Compute SA resolution."""

from __future__ import annotations

from helper_app.gcp.client import parse_service_account_json
from tests.fake_gcp import PROJECT, SA_JSON, FakeGcp, _FakeGcpClient


def test_default_compute_sa_uses_default_service_account_not_legacy_id():
    fake = FakeGcp([])
    sa = parse_service_account_json(SA_JSON)
    client = _FakeGcpClient(sa["project_id"], sa["client_email"], sa["private_key"],
                            http=fake.http(), sleep=lambda s: None)
    assert client.default_compute_service_email(PROJECT) == "123456789-compute@developer.gserviceaccount.com"
    assert client.project_number(PROJECT) == "123456789"
