import ast
import json
import re
import subprocess
from io import BytesIO
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

import nbformat
import pytest
import sqlglot
import yaml
from fastapi.testclient import TestClient
from linkml_runtime import SchemaView
from linkml_runtime.linkml_model import SchemaDefinition

from app import app
from fabric_export import export_fabric, fabric_bundle, fabric_names, notebook_for, spark_type
from test_osdu_export import body, model

client = TestClient(app)


@pytest.fixture(scope="module")
def artifacts():
    response = client.post("/api/export/fabric", json=body(target_schema="analytics"))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="fabric-lakehouse.zip"' in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.content)) as archive:
        files = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}
    report = json.loads(files["export_report.json"])
    assert len(report["warnings"]) == int(response.headers["X-Export-Warning-Count"])
    return files, report, json.loads(files["fabric_lakehouse.ipynb"])


class FakeSpark:
    def __init__(self, tables, namespace="analytics"):
        self.specs = {f"`{namespace}`.`{table['name']}`": table["columns"] for table in tables}
        self.existing = {}
        self.created = []
        self.catalog = SimpleNamespace(tableExists=lambda name: name in self.existing)

    def sql(self, statement):
        if statement.startswith("DESCRIBE DETAIL "):
            name = statement[len("DESCRIBE DETAIL "):]
            return SimpleNamespace(first=lambda: {"format": self.existing[name]["format"]})
        match = re.match(r"CREATE TABLE (`[^`]+`\.`[^`]+`) ", statement)
        assert match, f"Unexpected notebook statement: {statement}"
        name = match[1]
        assert name not in self.existing
        self.created.append(statement)
        self.existing[name] = {"format": "delta", "columns": self.specs[name], "rows": []}

    def table(self, name):
        fields = [
            SimpleNamespace(
                name=column["name"], nullable=column["nullable"],
                dataType=SimpleNamespace(simpleString=lambda value=column["type"]: value.lower()),
            ) for column in self.existing[name]["columns"]
        ]
        return SimpleNamespace(schema=SimpleNamespace(fields=fields))


def execute_cells(notebook, spark, context=None, stop_before_create=False):
    module = ModuleType("notebookutils")
    module.runtime = SimpleNamespace(context={"defaultLakehouseId": "attached-lakehouse"} if context is None else context)
    scope = {"spark": spark}
    code = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
    with patch.dict("sys.modules", {"notebookutils": module}):
        for source in code[:-1] if stop_before_create else code:
            exec(compile(source, "<exported-fabric-notebook>", "exec"), scope)
    return scope, code


def test_bundle_contains_valid_notebook_and_spark_delta_ddl(artifacts):
    files, report, notebook = artifacts
    assert set(files) == {"fabric_lakehouse.ipynb", "lakehouse.spark.sql", "export_report.json", "README.txt"}
    nbformat.validate(nbformat.from_dict(notebook))
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            ast.parse("".join(cell["source"]))
    statements = sqlglot.parse(files["lakehouse.spark.sql"], read="spark")
    assert len(statements) == report["table_count"]
    assert all(statement.key == "create" for statement in statements)
    ddl = files["lakehouse.spark.sql"]
    assert ddl.count("USING DELTA") == report["table_count"]
    assert "CREATE TABLE `analytics`.`master_data_example`" in ddl
    assert "SERIAL" not in ddl and "PRIMARY KEY" not in ddl and "FOREIGN KEY" not in ddl
    assert "IF NOT EXISTS" not in ddl and "OVERWRITE" not in ddl and "DROP TABLE" not in ddl
    assert "SQL analytics endpoint" in files["README.txt"]
    assert report["layout"] == "relational"
    assert report["enforced_constraints"] == ["NOT NULL"]


def test_relational_projection_preserves_key_relationship_and_enum_metadata(artifacts):
    _, report, _ = artifacts
    tables = {table["source_class"]: table for table in report["tables"]}
    master = tables["master-data/Example"]
    columns = {column["source_slot"]: column for column in master["columns"]}
    assert master["primary_key"] == ["id"]
    assert columns["Name"]["nullable"] is False
    assert columns["Category"]["enum_values"] == ["A", "B"]
    assert columns["TypeID"]["references"] == {"table": "reference_data_example", "column": "id"}
    assert "master-data/Example_tags" in tables
    position = tables["Position"]
    assert any(column["references"]["table"] == master["name"] for column in position["columns"] if "references" in column)
    assert any(column["requires_generated_values"] for column in position["columns"])


def test_notebook_creates_the_same_tables_as_standalone_sql_and_can_be_rerun(artifacts):
    files, report, notebook = artifacts
    spark = FakeSpark(report["tables"])
    execute_cells(notebook, spark)
    assert len(spark.created) == report["table_count"]
    expected = [statement.sql(dialect="spark", comments=False) for statement in sqlglot.parse(files["lakehouse.spark.sql"], read="spark")]
    actual = [sqlglot.parse_one(statement, read="spark").sql(dialect="spark", comments=False) for statement in spark.created]
    assert actual == expected
    one = next(iter(spark.existing.values()))
    one["rows"] = ["existing data"]
    execute_cells(notebook, spark)
    assert len(spark.created) == report["table_count"]
    assert one["rows"] == ["existing data"]


def test_notebook_requires_an_attached_default_lakehouse(artifacts):
    _, report, notebook = artifacts
    spark = FakeSpark(report["tables"])
    with pytest.raises(RuntimeError, match="Attach a default"):
        execute_cells(notebook, spark, context={})
    assert not spark.created


@pytest.mark.parametrize("mismatch", ["type", "nullable", "order", "provider"])
def test_notebook_preflights_all_existing_tables_before_creating_anything(artifacts, mismatch):
    _, report, notebook = artifacts
    spark = FakeSpark(report["tables"])
    name, columns = next(iter(spark.specs.items()))
    changed = [dict(column) for column in columns]
    provider = "delta"
    if mismatch == "type":
        changed[0]["type"] = "BINARY"
    elif mismatch == "nullable":
        changed[0]["nullable"] = not changed[0]["nullable"]
    elif mismatch == "order":
        changed.reverse()
    else:
        provider = "parquet"
    spark.existing[name] = {"format": provider, "columns": changed, "rows": ["keep"]}
    with pytest.raises(RuntimeError, match="existing"):
        execute_cells(notebook, spark)
    assert not spark.created
    assert spark.existing[name]["rows"] == ["keep"]


@pytest.mark.parametrize("change", ["lakehouse", "schema"])
def test_notebook_rejects_target_changes_after_preflight(artifacts, change):
    _, report, notebook = artifacts
    spark = FakeSpark(report["tables"])
    context = {"defaultLakehouseId": "attached"}
    scope, code = execute_cells(notebook, spark, context, stop_before_create=True)
    if change == "lakehouse":
        context["defaultLakehouseId"] = "different"
    else:
        scope["TARGET_SCHEMA"] = "elsewhere"
    with pytest.raises(RuntimeError, match="changed|preflight"):
        exec(code[-1], scope)
    assert not spark.created


def test_failed_preflight_invalidates_an_earlier_successful_plan(artifacts):
    _, report, notebook = artifacts
    spark = FakeSpark(report["tables"])
    scope, code = execute_cells(notebook, spark, stop_before_create=True)
    name, columns = next(iter(spark.specs.items()))
    spark.existing[name] = {"format": "parquet", "columns": columns}
    with pytest.raises(RuntimeError):
        exec(code[-2], scope)
    with pytest.raises(RuntimeError, match="preflight"):
        exec(code[-1], scope)
    assert not spark.created


def test_identifiers_are_safe_bounded_case_insensitively_unique_and_deterministic():
    values = ["Kind", "kind", "master-data/Well", "master_data_Well", "9name", "\u6e2c" * 50, "Long" * 80, "a`; DROP TABLE x;--"]
    mapped = fabric_names(values)
    assert len(set(mapped.values())) == len(values)
    assert mapped == fabric_names(list(reversed(values)))
    assert all(re.fullmatch("[a-z_][a-z0-9_]{0,127}", value) for value in mapped.values())
    assert mapped["Kind"] != mapped["kind"]
    assert mapped["master-data/Well"] != mapped["master_data_Well"]


def test_scalar_mapping_supports_custom_types_and_preserves_unbounded_decimals_as_strings():
    document = model()
    document["types"] = {
        "Money": {"typeof": "decimal"}, "Count": {"typeof": "integer"},
        "DoubleAlias": {"typeof": "double"},
    }
    view = SchemaView(SchemaDefinition(**document))
    for source, expected in {
        "string": "STRING", "integer": "BIGINT", "boolean": "BOOLEAN", "float": "FLOAT",
        "double": "DOUBLE", "date": "DATE", "datetime": "TIMESTAMP", "time": "STRING",
        "decimal": "STRING", "Money": "STRING", "Count": "BIGINT", "DoubleAlias": "DOUBLE",
        "objectidentifier": "STRING", "nodeidentifier": "STRING",
    }.items():
        assert spark_type(source, view, {}) == (expected, [])
    assert spark_type("Category", view, {}) == ("STRING", ["A", "B"])
    with pytest.raises(ValueError, match="Unsupported"):
        spark_type("UnknownType", view, {})


def test_large_notebook_splits_cells_without_dropping_tables():
    tables = [{"name": f"table_{index}", "columns": [{"name": "id", "type": "BIGINT", "nullable": False}]} for index in range(305)]
    notebook = notebook_for(tables, "dbo")
    nbformat.validate(nbformat.from_dict(notebook))
    spark = FakeSpark(tables, namespace="dbo")
    scope, _ = execute_cells(notebook, spark)
    assert len(scope["TABLES"]) == 305 and len(spark.created) == 305
    assert sum("TABLES.extend" in "".join(cell["source"]) for cell in notebook["cells"]) == 4


@pytest.mark.parametrize("namespace", ["../schema", "dbo;DROP TABLE x", "", "a" * 129, "sys", "INFORMATION_SCHEMA", "db_owner"])
def test_invalid_or_reserved_target_schemas_are_reported(namespace):
    response = client.post("/api/export/fabric", json=body(target_schema=namespace))
    assert response.status_code == 422


def test_invalid_yaml_is_rejected():
    response = client.post("/api/export/fabric", json={"yaml": "classes: ["})
    assert response.status_code == 422


def test_export_worker_timeout_and_error_are_explicit():
    with patch("osdu_export.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 240)):
        with pytest.raises(ValueError, match="Fabric Lakehouse generation exceeded"):
            export_fabric({}, {}, [], "dbo")
    with patch("osdu_export.subprocess.run", return_value=SimpleNamespace(returncode=1, stderr=b"Unsupported type", stdout=b"")):
        with pytest.raises(ValueError, match="Fabric Lakehouse generator failed: Unsupported type"):
            export_fabric({}, {}, [], "dbo")


def test_whole_model_does_not_require_osdu_metadata_and_preserves_validation_warnings():
    document = model()
    document["classes"] = {"Person": {"attributes": {
        "Length": {"range": "float", "unit": {"has_quantity_kind": "https://example.org/Length"}},
    }}}
    response = client.post("/api/export/fabric", json={"yaml": yaml.safe_dump(document)})
    assert response.status_code == 200, response.text
    with ZipFile(BytesIO(response.content)) as archive:
        report = json.loads(archive.read("export_report.json"))
    assert any("quantity kind" in warning for warning in report["warnings"])
    assert report["tables"][0]["source_class"] == "Person"
    assert report["table_count"] == 1


def test_empty_model_has_an_explicit_export_error():
    document = model()
    document["classes"] = {}
    with pytest.raises(ValueError, match="at least one class"):
        fabric_bundle(SchemaView(SchemaDefinition(**document)), "dbo", [])
