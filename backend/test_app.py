import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import yaml
from fastapi.testclient import TestClient
from linkml_runtime.dumpers import json_dumper, yaml_dumper
from linkml_runtime.linkml_model import SchemaDefinition
from openai import OpenAI

from app import ChatRequest, Proposal, app, inspect_schema

client = TestClient(app)
FOUNDRY_ENV = {
    "FOUNDRY_ENDPOINT": "https://example.openai.azure.com/openai/v1",
    "FOUNDRY_API_KEY": "test-placeholder",
    "FOUNDRY_MODEL_DEPLOYMENT": "gpt-5.4-mini",
}
SCHEMA = """id: https://example.org/demo
name: demo
prefixes:
  demo: https://example.org/demo/
  linkml: https://w3id.org/linkml/
default_prefix: demo
default_range: string
imports:
  - linkml:types
classes:
  Person:
    attributes:
      id:
        identifier: true
      name:
        required: true
"""


def test_valid_schema_and_export():
    result = client.post("/api/validate", json={"yaml": SCHEMA})
    assert result.status_code == 200
    assert result.json()["valid"]
    exported = client.post("/api/export/json-schema", json={"yaml": SCHEMA})
    assert exported.status_code == 200
    assert "Person" in exported.json()["json_schema"]


def test_direct_normalization_preserves_yaml_normalization_behavior():
    definition = SchemaDefinition(**yaml.safe_load(SCHEMA))
    assert json_dumper.to_dict(definition) == yaml.safe_load(yaml_dumper.dumps(definition))


def test_schema_larger_than_four_megabytes_validates_and_exports():
    description = "schema details " * 300_000
    text = SCHEMA + f"description: {description}\n"
    assert len(text.encode("utf-8")) > 4 * 1024 * 1024
    result = client.post("/api/validate", json={"yaml": text})
    assert result.status_code == 200
    assert result.json()["valid"]
    assert result.json()["schema"]["description"] == description.rstrip()
    exported = client.post("/api/export/json-schema", json={"yaml": text})
    assert exported.status_code == 200
    assert "Person" in exported.json()["json_schema"]
    request = ChatRequest(yaml=text, messages=[{"role": "user", "content": "Help"}])
    assert request.yaml == text
    assert Proposal(message="Updated.", questions=[], proposed_yaml=text).proposed_yaml == text


def test_invalid_yaml_and_unknown_range():
    assert inspect_schema("classes: [").errors
    assert inspect_schema("hello").errors
    assert inspect_schema(SCHEMA.replace("required: true", "range: Missing")).errors


def test_external_imports_are_blocked():
    for value in ["https://example.org/schema", "../secret", "linkml:../secret"]:
        errors = inspect_schema(SCHEMA.replace("linkml:types", value)).errors
        assert len(errors) == 1
        assert "URLs and filesystem paths are not allowed" in errors[0]


def test_unknown_metamodel_field_and_parent():
    assert inspect_schema(SCHEMA + "surprise: true\n").errors
    assert inspect_schema(SCHEMA.replace("    attributes:", "    is_a: Missing\n    attributes:")).errors


def test_no_key_reports_unavailable():
    with patch.dict("os.environ", {**FOUNDRY_ENV, "FOUNDRY_API_KEY": "", "OPENAI_API_KEY": "unused-public-key"}):
        assert client.get("/api/health").json()["ai_configured"] is False
        assert "FOUNDRY_API_KEY" in client.get("/api/health").json()["ai_configuration_error"]
        assert client.post("/api/chat", json={"yaml": SCHEMA, "messages": [
            {"role": "user", "content": "Help me model a person"},
        ]}).status_code == 503


def test_foundry_requires_endpoint_and_deployment():
    for name in ("FOUNDRY_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT"):
        with patch.dict("os.environ", {**FOUNDRY_ENV, name: ""}):
            result = client.get("/api/health").json()
            assert result["ai_configured"] is False
            assert name in result["ai_configuration_error"]


def test_foundry_rejects_wrong_endpoint_format():
    for endpoint in (
        "http://example.openai.azure.com/openai/v1",
        "https://example.services.ai.azure.com/api/projects/demo",
        "https://example.openai.azure.com/openai/v1?api-version=preview",
        "https://user:password@example.openai.azure.com/openai/v1",
        "https://example.openai.azure.com:invalid/openai/v1",
    ):
        with patch.dict("os.environ", {**FOUNDRY_ENV, "FOUNDRY_ENDPOINT": endpoint}), patch("app.OpenAI") as sdk:
            assert client.get("/api/health").json()["ai_configured"] is False
            response = client.post("/api/chat", json={"yaml": SCHEMA, "messages": [
                {"role": "user", "content": "Help"},
            ]})
            assert response.status_code == 503
            sdk.assert_not_called()


def test_foundry_configuration_does_not_expose_key():
    with patch.dict("os.environ", FOUNDRY_ENV):
        response = client.get("/api/health")
        assert response.json()["ai_configured"] is True
        assert response.json()["ai_provider"] == "Microsoft Foundry"
        assert FOUNDRY_ENV["FOUNDRY_API_KEY"] not in response.text


def test_export_rejects_invalid_input():
    assert client.post("/api/export/json-schema", json={"yaml": "[]"}).status_code == 422


def test_empty_definitions_are_normalized_for_visual_editor():
    result = inspect_schema(SCHEMA + "  Empty:\n")
    assert not result.errors
    assert isinstance(result.schema["classes"]["Empty"], dict)
    result = inspect_schema(SCHEMA.replace("      name:\n        required: true", "      name:\n"))
    assert not result.errors
    assert isinstance(result.schema["classes"]["Person"]["attributes"]["name"], dict)


def test_inheritance_cycle_is_rejected():
    result = inspect_schema(SCHEMA.replace("    attributes:", "    is_a: Person\n    attributes:"))
    assert any("Cycle detected" in error for error in result.errors)


def test_local_imports_resolve_types_and_keep_annotation_metadata(tmp_path):
    dependency = """id: https://example.org/annotations
name: annotations
imports:
  - linkml:types
prefixes:
  linkml: https://w3id.org/linkml/
  xsd: http://www.w3.org/2001/XMLSchema#
types:
  osdu_id:
    typeof: string
    uri: xsd:string
slots:
  attribution_authority:
    range: string
"""
    (tmp_path / "osdu_annotations.yaml").write_text(dependency, encoding="utf-8")
    text = SCHEMA.replace("  - linkml:types", "  - linkml:types\n  - osdu_annotations").replace(
        "required: true", "range: osdu_id\n        annotations:\n          attribution_authority: Example"
    )
    with patch("app.IMPORTS_DIRECTORY", tmp_path):
        result = client.post("/api/validate", json={"yaml": text}).json()
        assert result["valid"]
        assert result["strict_valid"]
        assert "osdu_id" in result["ranges"]
        assert result["schema"]["imports"] == ["linkml:types", "osdu_annotations"]
        annotation = result["schema"]["classes"]["Person"]["attributes"]["name"]["annotations"]
        assert "Example" in str(annotation)
        assert client.post("/api/export/json-schema", json={"yaml": text}).status_code == 200


def test_nested_imports_are_checked_before_resolution(tmp_path):
    (tmp_path / "unsafe.yaml").write_text(
        "id: https://example.org/unsafe\nname: unsafe\nimports:\n  - https://example.org/remote\n",
        encoding="utf-8",
    )
    with patch("app.IMPORTS_DIRECTORY", tmp_path):
        result = inspect_schema(SCHEMA.replace("linkml:types", "unsafe"))
        assert any("URLs and filesystem paths are not allowed" in error for error in result.errors)
        result = inspect_schema(SCHEMA.replace("linkml:types", "missing"))
        assert any("Missing local import" in error for error in result.errors)


def test_quantity_kind_only_units_are_warned_and_preserved():
    text = SCHEMA.replace("required: true", "range: float\n        unit:\n          has_quantity_kind: https://example.org/length")
    result = client.post("/api/validate", json={"yaml": text}).json()
    assert result["valid"]
    assert not result["strict_valid"]
    assert len(result["warnings"]) == 1
    unit = result["schema"]["classes"]["Person"]["attributes"]["name"]["unit"]
    assert unit == {"has_quantity_kind": "https://example.org/length"}
    assert client.post("/api/export/json-schema", json={"yaml": text}).status_code == 200


def test_unit_warnings_do_not_hide_other_metamodel_errors():
    text = SCHEMA.replace("required: true", "unit:\n          has_quantity_kind: https://example.org/length\n        unknown_field: true")
    result = inspect_schema(text)
    assert result.errors
    assert result.warnings


def test_malformed_definition_collections_are_validation_errors():
    assert inspect_schema(SCHEMA.replace("classes:\n", "classes: []\nunused:\n")).errors


def test_ai_proposals_are_validated_without_applying_them():
    proposal = Proposal(message="Add a study.", questions=["What identifies a study?"], proposed_yaml=SCHEMA + "  Study:\n")
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=proposal))])
    model = Mock()
    model.beta.chat.completions.parse.return_value = completion
    with patch.dict("os.environ", FOUNDRY_ENV), patch("app.OpenAI", return_value=model) as sdk:
        response = client.post("/api/chat", json={"yaml": SCHEMA, "messages": [
            {"role": "user", "content": "Add a study"},
        ]})
        assert response.status_code == 200
        sdk.assert_called_once_with(
            api_key=FOUNDRY_ENV["FOUNDRY_API_KEY"],
            base_url=FOUNDRY_ENV["FOUNDRY_ENDPOINT"] + "/",
            timeout=60, max_retries=1,
        )
        assert model.beta.chat.completions.parse.call_args.kwargs["model"] == "gpt-5.4-mini"
        assert response.json()["validation_errors"] == []
        assert response.json()["proposed_yaml"] == proposal.proposed_yaml
        proposal.proposed_yaml = SCHEMA.replace("required: true", "range: Missing")
        assert client.post("/api/chat", json={"yaml": SCHEMA, "messages": [
            {"role": "user", "content": "Add a study"},
        ]}).json()["validation_errors"]


def test_ai_refusal_is_reported_explicitly():
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(refusal="Refused", parsed=None))])
    model = Mock()
    model.beta.chat.completions.parse.return_value = completion
    with patch.dict("os.environ", FOUNDRY_ENV), patch("app.OpenAI", return_value=model):
        response = client.post("/api/chat", json={"yaml": SCHEMA, "messages": [
            {"role": "user", "content": "Help"},
        ]})
        assert response.status_code == 502
        assert "usable proposal" in response.json()["detail"]


def test_foundry_v1_sdk_transport_uses_endpoint_deployment_and_key():
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == "https://example.openai.azure.com/openai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-placeholder"
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.4-mini"
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "gpt-5.4-mini",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({
                    "message": "Let's clarify the domain.", "questions": ["What are you modeling?"], "proposed_yaml": None,
                }),
            }}],
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        with patch.dict("os.environ", FOUNDRY_ENV), patch(
            "app.OpenAI", side_effect=lambda **kwargs: OpenAI(**kwargs, http_client=transport),
        ):
            response = client.post("/api/chat", json={"yaml": SCHEMA, "messages": [
                {"role": "user", "content": "Help me build a model"},
            ]})
            assert response.status_code == 200
            assert response.json()["questions"] == ["What are you modeling?"]
            assert len(requests) == 1


def test_foundry_authentication_error_is_reported_without_secrets():
    def handle(request):
        return httpx.Response(401, json={"error": {"message": "Unauthorized", "code": "401"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        with patch.dict("os.environ", FOUNDRY_ENV), patch(
            "app.OpenAI", side_effect=lambda **kwargs: OpenAI(**kwargs, http_client=transport),
        ):
            response = client.post("/api/chat", json={"yaml": SCHEMA, "messages": [
                {"role": "user", "content": "Help"},
            ]})
            assert response.status_code == 502
            assert "Microsoft Foundry" in response.json()["detail"]
            assert "HTTP 401" in response.json()["detail"]
            assert FOUNDRY_ENV["FOUNDRY_API_KEY"] not in response.text
