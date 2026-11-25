"""Regression checks for connector polling and upload OpenAPI contracts."""

import pytest

from app.api.routes.connectors import ConnectorJobLease
from app.main import app


def test_connector_poll_documents_typed_lease_and_empty_no_job_response() -> None:
    specification = app.openapi()
    operation = specification["paths"]["/api/v1/connectors/{connector_id}/jobs/next"]["get"]

    lease_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert lease_schema == {"$ref": "#/components/schemas/ConnectorJobLease"}
    assert specification["components"]["schemas"]["ConnectorJobLease"] == (
        ConnectorJobLease.model_json_schema()
    )
    assert "content" not in operation["responses"]["204"]
    assert "machine credential" in operation["description"]
    assert operation["security"] == [{"ConnectorBearer": []}]
    assert specification["components"]["securitySchemes"]["ConnectorBearer"]["scheme"] == (
        "bearer"
    )
    assert "not an OIDC user token" in specification["components"]["securitySchemes"][
        "ConnectorBearer"
    ]["description"]


@pytest.mark.parametrize(
    ("path", "response_model", "error_model", "error_codes"),
    [
        (
            "/api/v1/workspaces/{workspace_id}/documents",
            "DocumentOut",
            "DocumentUploadError",
            {"400", "401", "404", "409", "413", "415", "503"},
        ),
        (
            "/api/v1/knowledge-bases/{knowledge_base_id}/versions/{version_id}/documents",
            "KnowledgeBaseDocumentOut",
            "KnowledgeBaseDocumentUploadError",
            {"400", "401", "403", "404", "409", "413", "415", "503"},
        ),
    ],
)
def test_upload_documents_multipart_created_and_actual_error_shapes(
    path: str, response_model: str, error_model: str, error_codes: set[str]
) -> None:
    specification = app.openapi()
    operation = specification["paths"][path]["post"]

    body_schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    body = specification["components"]["schemas"][body_schema["$ref"].rsplit("/", 1)[1]]
    assert body["required"] == ["file"]
    assert body["properties"]["file"]["type"] == "string"
    assert operation["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": f"#/components/schemas/{response_model}"
    }
    assert "422" in operation["responses"]
    for code in error_codes:
        assert operation["responses"][code]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{error_model}"
        }
    error = specification["components"]["schemas"][error_model]
    assert error["required"] == ["detail"]
    assert error["properties"]["detail"]["type"] == "string"
