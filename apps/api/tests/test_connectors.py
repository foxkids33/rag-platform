import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from fastapi import HTTPException

from app.api.routes.connectors import (
    ConnectorHeartbeat,
    ConnectorEnrollRequest,
    EnrollmentCreate,
    SpacePagesOperationRequest,
    ImportOperationRequest,
    PageNode,
    _validate_lease,
    _progress_deadline,
)
from app.core.config import settings
from app.services.connector_queue import issue_bound_token, token_connector_id, token_matches


def test_enrollment_can_include_all_146_visible_account_spaces():
    keys = [f"SPACE_{index}" for index in range(146)]
    assert EnrollmentCreate(allowed_spaces=keys).allowed_spaces == keys
    with pytest.raises(ValidationError):
        EnrollmentCreate(allowed_spaces=[f"SPACE_{index}" for index in range(1001)])
    with pytest.raises(ValidationError):
        EnrollmentCreate(allowed_spaces=["*"])


def test_personal_confluence_space_is_a_valid_identity_not_a_path():
    assert EnrollmentCreate(allowed_spaces=["KBR", "~demo.user"]).allowed_spaces == [
        "KBR", "~demo.user",
    ]
    assert PageNode(id="42", title="Personal", space_key="~demo.user").space_key == "~demo.user"
    assert SpacePagesOperationRequest(space_key="~demo.user").space_key == "~demo.user"
    for value in ("~", "~/HR", "../HR", "*", "https://example.test"):
        with pytest.raises(ValidationError):
            EnrollmentCreate(allowed_spaces=[value])


def test_space_pagination_request_and_capability_refresh():
    request = SpacePagesOperationRequest(space_key=" KBR ", start=100)
    assert request.space_key == "KBR"
    with pytest.raises(ValidationError):
        SpacePagesOperationRequest(space_key="KBR", start=-1)
    with pytest.raises(ValidationError):
        SpacePagesOperationRequest(space_key="../HR")
    assert ConnectorHeartbeat().capabilities is None  # Old connectors remain compatible.
    assert ConnectorHeartbeat(capabilities=["list_space_pages"]).capabilities == ["LIST_SPACE_PAGES"]
    with pytest.raises(ValidationError):
        ConnectorHeartbeat(capabilities=["FETCH_URL"])


def test_connector_tokens_are_bound_hashed_and_not_interchangeable() -> None:
    connector_id = uuid.uuid4()
    other_id = uuid.uuid4()
    token, digest = issue_bound_token(connector_id)

    assert token_connector_id(token) == connector_id
    assert token_matches(token, connector_id, digest)
    assert not token_matches(token, other_id, digest)
    assert not token_matches(f"{connector_id}.wrong-secret", connector_id, digest)
    assert token not in digest


def test_connector_enrollment_requires_known_capabilities() -> None:
    with pytest.raises(ValidationError, match="capabilities"):
        ConnectorEnrollRequest(
            enrollment_token=f"{uuid.uuid4()}.a-long-random-enrollment-secret",
            capabilities=["GENERIC_HTTP_PROXY"],
        )


def test_import_operation_requires_a_space_for_every_page() -> None:
    with pytest.raises(ValidationError, match="page_spaces"):
        ImportOperationRequest(page_ids=["202211666"], page_spaces={})

    request = ImportOperationRequest(
        page_ids=["202211666"],
        page_spaces={"202211666": "KBR"},
    )
    assert request.page_spaces == {"202211666": "KBR"}


def test_page_node_rejects_invalid_space_or_page_identifiers() -> None:
    with pytest.raises(ValidationError):
        PageNode(
            id="https://internal.example/page",
            title="Bad",
            space_key="KBR",
        )
    with pytest.raises(ValidationError):
        PageNode(id="42", title="Bad", space_key="../../HR")


@pytest.mark.parametrize("state", ["CANCELLED", "FAILED", "QUEUED", "expired", "wrong_lease"])
def test_stale_or_cancelled_result_cannot_complete_a_job(state):
    now = datetime.now(UTC)
    lease_id = uuid.uuid4()
    job = SimpleNamespace(
        status=state if state in {"CANCELLED", "FAILED", "QUEUED"} else "LEASED",
        lease_id=uuid.uuid4() if state == "wrong_lease" else lease_id,
        deadline_at=now + timedelta(minutes=5),
        leased_until=now + timedelta(seconds=-1 if state == "expired" else 30),
    )
    with pytest.raises(HTTPException) as error:
        _validate_lease(job, lease_id)
    assert error.value.status_code == 409


def test_successful_result_can_be_acknowledged_again():
    _validate_lease(SimpleNamespace(status="SUCCEEDED"), uuid.uuid4())


def test_heartbeat_extends_deadline_but_cannot_keep_job_alive_forever():
    now = datetime.now(UTC)
    job = SimpleNamespace(created_at=now - timedelta(seconds=290))
    assert _progress_deadline(job, now) == now + timedelta(
        seconds=settings.confluence_connector_job_timeout_seconds,
    )
    job.created_at = now - timedelta(seconds=settings.confluence_connector_max_job_seconds - 10)
    assert _progress_deadline(job, now) == now + timedelta(seconds=10)
