import json
from io import BytesIO
from zipfile import ZipFile

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft7Validator
from pglast import parse_sql
from linkml_runtime import SchemaView
from linkml_runtime.linkml_model import SchemaDefinition
from linkml.transformers import relmodel_transformer

from app import app
from osdu_export import RECORD_GROUPS, postgres_ddl, postgres_name

client = TestClient(app)


def model():
    return {
        "id": "https://example.org/osdu", "name": "osdu_test", "default_range": "string",
        "prefixes": {"osdu": "https://schema.osdu.opengroup.org/ns/", "linkml": "https://w3id.org/linkml/"},
        "default_prefix": "osdu", "imports": ["linkml:types"],
        "enums": {"Category": {"permissible_values": {"A": {}, "B": {}}}},
        "classes": {
            "abstract/AbstractSystemProperties": {
                "mixin": True,
                "attributes": {"id": {"identifier": True}, "kind": {}, "tags": {
                    "multivalued": True, "annotations": {"map": True},
                }},
            },
            "Position": {"attributes": {"X": {"range": "float", "required": True}}},
            **{
                f"{group}/Example": {
                    "class_uri": f"osdu:{group}/Example",
                    "see_also": [f"https://schema.osdu.opengroup.org/json/{group}/Example.1.2.3.json"],
                    "mixins": ["abstract/AbstractSystemProperties"],
                    "attributes": {
                        "Name": {"required": True},
                        "Category": {"range": "Category"},
                        "TypeID": {
                            "range": "reference-data/Example",
                            "annotations": {"relationship_targets": "reference-data::Example"},
                        },
                        "Positions": {"range": "Position", "multivalued": True, "inlined_as_list": True},
                    },
                } for group in RECORD_GROUPS
            },
        },
    }


def body(document=None, **options):
    return {"yaml": yaml.safe_dump(document or model(), sort_keys=False), **options}


def unpack(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(response.content)) as archive:
        return {name: json.loads(archive.read(name)) for name in archive.namelist()}


def test_registration_contract_and_self_contained_draft7_schema():
    files = unpack(client.post("/api/export/osdu/schemas", json=body(class_names=["master-data/Example"])))
    schema = files["schemas/master-data/Example.1.2.3.json"]
    registration = files["registration/master-data/Example.1.2.3.json"]
    info = registration["schemaInfo"]
    assert info["schemaIdentity"] == {
        "id": "osdu:wks:master-data--Example:1.2.3",
        "authority": "osdu", "source": "wks", "entityType": "master-data--Example",
        "schemaVersionMajor": 1, "schemaVersionMinor": 2, "schemaVersionPatch": 3,
    }
    assert info["scope"] == "INTERNAL" and info["status"] == "DEVELOPMENT"
    assert registration["schema"] == schema
    Draft7Validator.check_schema(schema)
    data = schema["properties"]["data"]
    assert "id" not in data["properties"] and "kind" not in data["properties"]
    assert data["properties"]["TypeID"]["x-osdu-relationship"] == [
        {"GroupType": "reference-data", "EntityType": "Example"},
    ]
    assert schema["properties"]["tags"]["type"] == "object"
    record = {
        "kind": info["schemaIdentity"]["id"],
        "id": "partition:master-data--Example:one",
        "acl": {"owners": ["owners@example.org"], "viewers": ["viewers@example.org"]},
        "legal": {"legaltags": ["example-legal"], "otherRelevantDataCountries": ["US"]},
        "tags": {"a": "b"},
        "data": {"Name": "Example", "Category": "A", "Positions": [{"X": 1.5}]},
    }
    validator = Draft7Validator(schema)
    validator.validate(record)
    record["data"]["Positions"][0]["X"] = "not numeric"
    assert list(validator.iter_errors(record))
    assert "not a lossless reconstruction" in " ".join(files["export_report.json"]["warnings"])


@pytest.mark.parametrize("group,bucket,nested,singular", [
    ("master-data", "MasterData", False, False),
    ("reference-data", "ReferenceData", False, False),
    ("dataset", "Datasets", True, False),
    ("work-product-component", "WorkProductComponents", True, False),
    ("work-product", "WorkProduct", True, True),
])
def test_ingestion_manifest_group_shape(group, bucket, nested, singular):
    files = unpack(client.post("/api/export/osdu/manifests", json=body(class_names=[f"{group}/Example"])))
    manifest = files[f"manifests/{group}/Example.1.2.3.json"]
    assert manifest["kind"] == "osdu:wks:Manifest:1.0.0"
    container = manifest["Data"] if nested else manifest
    record = container[bucket] if singular else container[bucket][0]
    assert record["kind"] == f"osdu:wks:{group}--Example:1.2.3"
    assert record["id"].startswith("{{NAMESPACE}}:")
    assert record["acl"]["owners"] == ["{{DATA_OWNERS_GROUP}}"]
    assert record["legal"]["legaltags"] == ["{{LEGAL_TAG}}"]
    assert record["data"]["Positions"] == [{"X": 0}]
    assert set(manifest) == ({"kind", "Data"} if nested else {"kind", bucket})
    assert "NOT ingestion-ready" in " ".join(files["export_report.json"]["warnings"])


def test_whole_model_exports_only_record_classes_and_honors_identity_override():
    files = unpack(client.post("/api/export/osdu/schemas", json=body(authority="custom", source="test", version="2.0.1")))
    assert files["export_report.json"]["record_count"] == len(RECORD_GROUPS)
    schemas = [value for name, value in files.items() if name.startswith("schemas/")]
    assert len(schemas) == len(RECORD_GROUPS)
    assert all(schema["x-osdu-schema-source"].startswith("custom:test:") for schema in schemas)
    assert all(schema["x-osdu-schema-source"].endswith(":2.0.1") for schema in schemas)


def test_unqualified_new_class_needs_an_explicit_version():
    document = model()
    document["classes"] = {"Person": {"attributes": {"Name": {"required": True}}}}
    response = client.post("/api/export/osdu/schemas", json=body(document, class_names=["Person"]))
    assert response.status_code == 422 and "explicit export version" in response.text
    files = unpack(client.post("/api/export/osdu/schemas", json=body(
        document, class_names=["Person"], group_type="reference-data", authority="custom", version="1.0.0",
    )))
    assert files["registration/reference-data/Person.1.0.0.json"]["schemaInfo"]["schemaIdentity"]["id"] == "custom:wks:reference-data--Person:1.0.0"


def test_original_slot_name_is_recovered_from_its_uri():
    document = model()
    document["classes"]["Position"]["class_uri"] = "osdu:Position"
    document["classes"]["Position"]["attributes"]["CostValue"] = {"slot_uri": "osdu:PositionCost", "range": "float"}
    files = unpack(client.post("/api/export/osdu/schemas", json=body(document, class_names=["master-data/Example"])))
    schema = files["schemas/master-data/Example.1.2.3.json"]
    position = schema["definitions"]["Position"]["properties"]
    assert "Cost" in position and "CostValue" not in position


def test_recursive_template_is_finite_and_marked():
    document = model()
    document["classes"]["Position"]["attributes"]["Next"] = {"range": "Position", "inlined": True, "required": True}
    files = unpack(client.post("/api/export/osdu/manifests", json=body(document, class_names=["master-data/Example"])))
    template = files["manifests/master-data/Example.1.2.3.json"]
    assert "{{RECURSIVE_REFERENCE:" in json.dumps(template)


@pytest.mark.parametrize("options", [
    {"class_names": ["Unknown"]}, {"class_names": ["abstract/AbstractSystemProperties"]},
    {"class_names": ["master-data/Example", "master-data/Example"]},
    {"version": "1.0"}, {"version": "01.0.0"}, {"authority": "../bad"},
    {"source": "name:with:colons"}, {"group_type": "content"},
])
def test_invalid_export_options_are_reported(options):
    response = client.post("/api/export/osdu/schemas", json=body(**options))
    assert response.status_code == 422


def test_postgresql_has_junction_tables_valid_foreign_keys_enums_and_stable_output():
    response = client.post("/api/export/postgresql", json=body())
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/sql")
    ddl = response.text
    assert 'CREATE TABLE "master-data/Example"' in ddl
    assert '"master-data/Example_tags"' in ddl
    assert "FOREIGN KEY" in ddl and "CHECK" in ddl
    assert " IN ('A', 'B')" in ddl
    assert ddl.rindex("CREATE TABLE") < ddl.index("ALTER TABLE")
    assert parse_sql(ddl)
    assert client.post("/api/export/postgresql", json=body()).text == ddl


def test_postgresql_preserves_case_distinctions_and_shortens_utf8_identifiers():
    document = model()
    name = "Entity" + "Long" * 20
    column = "Column" + "測" * 30
    document["classes"] = {name: {"attributes": {
        "kind": {}, "Kind": {}, column: {"multivalued": True},
    }}}
    response = client.post("/api/export/postgresql", json=body(document))
    assert response.status_code == 200, response.text
    assert '"kind"' in response.text and '"Kind"' in response.text
    assert len(postgres_name(column).encode("utf-8")) <= 63
    assert postgres_name(column) in response.text
    statements = parse_sql(response.text)
    for statement in statements:
        node = statement.stmt
        if hasattr(node, "relation") and node.relation:
            assert len(node.relation.relname.encode("utf-8")) <= 63


def test_worker_matches_official_transformer_without_changing_api_process_globals():
    document = model()
    before = relmodel_transformer.SchemaView
    expected = postgres_ddl(SchemaView(SchemaDefinition(**document)), [])
    response = client.post("/api/export/postgresql", json=body(document))
    assert response.status_code == 200, response.text
    assert response.text == expected
    assert relmodel_transformer.SchemaView is before


def test_foreign_key_uses_the_actual_direct_primary_key():
    document = model()
    document["classes"] = {
        "Target": {"attributes": {"z": {"identifier": True}, "a": {"identifier": True}}},
        "Source": {"attributes": {"TargetID": {"range": "Target"}}},
    }
    response = client.post("/api/export/postgresql", json=body(document))
    assert response.status_code == 200, response.text
    assert 'PRIMARY KEY ("z")' in response.text
    assert 'REFERENCES "Target" ("z")' in response.text
    assert 'PRIMARY KEY ("z", "a")' not in response.text


@pytest.mark.parametrize("endpoint", ["osdu/schemas", "osdu/manifests", "postgresql"])
def test_export_rejects_invalid_yaml(endpoint):
    response = client.post(f"/api/export/{endpoint}", json={"yaml": "classes: ["})
    assert response.status_code == 422
