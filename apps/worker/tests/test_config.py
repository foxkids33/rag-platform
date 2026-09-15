import pytest
from pydantic import ValidationError

from app.config import Settings


def test_job_lease_timing_accepts_three_heartbeat_windows() -> None:
    config = Settings(
        ingestion_heartbeat_seconds=2,
        ingestion_stale_after_seconds=10,
    )

    assert config.ingestion_stale_after_seconds == 10


def test_job_lease_timing_rejects_premature_stale_recovery() -> None:
    with pytest.raises(ValidationError, match="at least three times"):
        Settings(
            ingestion_heartbeat_seconds=5,
            ingestion_stale_after_seconds=10,
        )
