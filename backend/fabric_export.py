from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from linkml.generators.sqltablegen import METAMODEL_TYPE_TO_BASE, RelationalModelTransformer
from linkml_runtime import SchemaView
from linkml_runtime.dumpers import json_dumper

from osdu_export import annotations, json_text, run_relational_worker

SPARK_TYPES = {
    "str": "STRING", "string": "STRING", "String": "STRING", "NCName": "STRING",
    "URIorCURIE": "STRING", "URI": "STRING", "ElementIdentifier": "STRING",
    "NodeIdentifier": "STRING", "int": "BIGINT", "float": "FLOAT",
    "double": "DOUBLE", "Bool": "BOOLEAN", "XSDDate": "DATE",
    "XSDDateTime": "TIMESTAMP", "XSDTime": "STRING", "Decimal": "STRING",
}
FABRIC_NOTICES = [
    "This is a relational Delta projection, not an OSDU ingestion pipeline or a database migration. "
    "Primary/foreign keys, unique keys, enums, patterns and other domain rules are metadata only; "
    "only column NOT NULL is enforced by the generated DDL.",
    "Integer values must fit BIGINT (signed 64-bit). Unbounded decimals and time-of-day values use "
    "STRING to preserve precision and avoid inventing a date. Validate/cast incoming data explicitly.",
    "Attach a default schema-enabled Fabric Lakehouse and create the target schema before running. "
    "Generated/surrogate keys require values from your ingestion process; there is no auto-increment. "
    "The notebook retains compatible existing Delta tables and rejects schema/provider mismatches. "
    "DDL is not transactional; failures can leave some new tables created. No data is loaded.",
]
RESERVED_SCHEMAS = {
    "sys", "information_schema", "db_owner", "db_accessadmin", "db_securityadmin",
    "db_ddladmin", "db_backupoperator", "db_datareader", "db_datawriter",
    "db_denydatareader", "db_denydatawriter",
}


def validate_namespace(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) or name.lower() in RESERVED_SCHEMAS:
        raise ValueError("Use a non-system Lakehouse schema name of up to 128 ASCII letters, digits or underscores, starting with a letter or underscore.")


def fabric_names(values: list[str]) -> dict[str, str]:
    bases = {}
    for value in values:
        base = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_") or "item"
        if base[0].isdigit():
            base = "n_" + base
        bases[value] = base
    counts = Counter(bases.values())
    result = {}
    for value, base in bases.items():
        if len(base) > 128 or counts[base] > 1:
            base = base[:115] + "_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        result[value] = base
    if len(set(result.values())) != len(result):
        raise ValueError("Fabric identifier collision after normalization. Rename the conflicting model elements.")
    return result


def spark_type(range_name: str, view: SchemaView, identifiers: dict[str, Any]) -> tuple[str, list[str]]:
    seen = set()
    while range_name in identifiers:
        if range_name in seen:
            raise ValueError(f"Cyclic identifier type: {range_name}.")
        seen.add(range_name)
        identifier = identifiers[range_name]
        if identifier is None:
            raise ValueError(f"Cannot determine a scalar identifier type for {range_name}.")
        range_name = str(identifier.range)
    enums = view.all_enums()
    if range_name in enums:
        return "STRING", [str(value) for value in enums[range_name].permissible_values]
    if range_name in view.all_types():
        induced = view.induced_type(range_name)
        base = METAMODEL_TYPE_TO_BASE.get(range_name, str(induced.base))
        if str(induced.uri) in ("xsd:double", "http://www.w3.org/2001/XMLSchema#double"):
            base = "double"
    else:
        base = METAMODEL_TYPE_TO_BASE.get(range_name)
    if base not in SPARK_TYPES:
        raise ValueError(f"Unsupported Fabric scalar range {range_name!r} (base {base!r}).")
    return SPARK_TYPES[base], []


def column_sql(columns: list[dict[str, Any]]) -> str:
    return ",\n".join(
        f"  `{column['name']}` {column['type']}" + ("" if column["nullable"] else " NOT NULL")
        for column in columns
    )


def create_table_sql(table: dict[str, Any], namespace: str) -> str:
    return f"CREATE TABLE `{namespace}`.`{table['name']}` (\n{column_sql(table['columns'])}\n) USING DELTA"


def notebook_for(tables: list[dict[str, Any]], namespace: str) -> dict[str, Any]:
    cells = []

    def cell(kind: str, source: str) -> None:
        value: dict[str, Any] = {
            "id": f"cell-{len(cells):04d}", "cell_type": kind, "metadata": {},
            "source": source.splitlines(keepends=True),
        }
        if kind == "code":
            value.update(execution_count=None, outputs=[])
        cells.append(value)

    cell("markdown", """# LinkML Studio - Fabric Lakehouse tables

Import this notebook as **PySpark** and attach your **default schema-enabled Lakehouse**.
Create the target schema in the Lakehouse explorer first (`dbo` already exists).
Review `export_report.json`, especially name mappings, data types and logical constraints.
The SQL file is an alternative for **Spark SQL**, not the read-only SQL analytics endpoint.

Run cells in order. Preflight checks all existing tables before creating any missing ones.
Only matching Delta schemas are retained; mismatches stop execution without overwriting.
Creation is not transactional and a later failure can leave some new tables created.
No rows are loaded. Assign surrogate keys and enforce relationships/enum/domain rules during ingestion.
Do not switch the default Lakehouse between preflight and creation.
""")
    cell("code", f"""import json
import re
import notebookutils

TARGET_SCHEMA = {namespace!r}
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{{0,127}}", TARGET_SCHEMA):
    raise ValueError("Invalid TARGET_SCHEMA.")
if TARGET_SCHEMA.lower() in {sorted(RESERVED_SCHEMAS)!r}:
    raise ValueError("System schemas cannot be used as TARGET_SCHEMA.")
LAKEHOUSE_ID = notebookutils.runtime.context.get("defaultLakehouseId")
if not LAKEHOUSE_ID:
    raise RuntimeError("Attach a default schema-enabled Fabric Lakehouse before running.")
TABLES = []
TO_CREATE = None
PREFLIGHT_SCHEMA = None
""")
    for index in range(0, len(tables), 100):
        payload = [{"name": table["name"], "columns": [
            {"name": column["name"], "type": column["type"], "nullable": column["nullable"]}
            for column in table["columns"]
        ]} for table in tables[index:index + 100]]
        cell("code", "TABLES.extend(json.loads(" + repr(json.dumps(payload, ensure_ascii=True, separators=(",", ":"))) + "))\n")
    cell("code", """def qualified_name(table):
    return f"`{TARGET_SCHEMA}`.`{table['name']}`"

def create_statement(table):
    columns = ",\\n".join(
        f"  `{column['name']}` {column['type']}" + ("" if column["nullable"] else " NOT NULL")
        for column in table["columns"]
    )
    return f"CREATE TABLE {qualified_name(table)} (\\n{columns}\\n) USING DELTA"

def assert_compatible(table):
    name = qualified_name(table)
    detail = spark.sql(f"DESCRIBE DETAIL {name}").first()
    if detail is None or detail["format"].lower() != "delta":
        raise RuntimeError(f"{name}: an existing non-Delta table cannot be reused.")
    actual = [(field.name, field.dataType.simpleString().upper(), field.nullable)
              for field in spark.table(name).schema.fields]
    expected = [(column["name"], column["type"], column["nullable"])
                for column in table["columns"]]
    if actual != expected:
        raise RuntimeError(f"{name}: existing column names/order, types or nullability differ. "
                           "No table will be overwritten. Review the schema and use a migration or another target.")

PREFLIGHT_SCHEMA = None
TO_CREATE = []
if notebookutils.runtime.context.get("defaultLakehouseId") != LAKEHOUSE_ID:
    raise RuntimeError("The default Lakehouse changed. Restart this notebook from the first cell.")
for table in TABLES:
    if spark.catalog.tableExists(qualified_name(table)):
        assert_compatible(table)
    else:
        TO_CREATE.append(table)
PREFLIGHT_SCHEMA = TARGET_SCHEMA
print(f"Preflight complete: {len(TO_CREATE)} new tables; {len(TABLES) - len(TO_CREATE)} compatible Delta tables retained.")
""")
    cell("code", """if PREFLIGHT_SCHEMA != TARGET_SCHEMA or TO_CREATE is None:
    raise RuntimeError("Run preflight again after changing the configuration.")
if notebookutils.runtime.context.get("defaultLakehouseId") != LAKEHOUSE_ID:
    raise RuntimeError("The default Lakehouse changed. Restart this notebook from the first cell.")
for table in TO_CREATE:
    spark.sql(create_statement(table))
print(f"Created {len(TO_CREATE)} Delta tables in schema {TARGET_SCHEMA}. Existing tables and rows were not modified.")
""")
    return {
        "nbformat": 4, "nbformat_minor": 5, "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Synapse PySpark", "language": "python", "name": "synapse_pyspark"},
            "language_info": {"name": "python"},
        },
    }


def fabric_bundle(view: SchemaView, namespace: str, warnings: list[str]) -> bytes:
    validate_namespace(namespace)
    relational = RelationalModelTransformer(view).transform().schema
    relational_view = SchemaView(relational)
    classes = relational_view.all_classes()
    if not classes:
        raise ValueError("Add at least one class before exporting Fabric Lakehouse tables.")
    table_names = fabric_names(list(classes))
    column_names = {name: fabric_names(list(cls.attributes)) for name, cls in classes.items()}
    identifiers = {
        name: RelationalModelTransformer.get_direct_identifier_attribute(relational_view, name)
        for name in classes
    }
    tables = []
    for name, cls in sorted(classes.items()):
        identifier = identifiers[name]
        primary = [str(identifier.name)] if identifier is not None else [
            str(key) for key, slot in cls.attributes.items()
            if str(annotations(slot).get("primary_key", "")).lower() == "true"
        ]
        columns = []
        for key, slot in cls.attributes.items():
            datatype, enum_values = spark_type(str(slot.range), relational_view, identifiers)
            raw = json_dumper.to_dict(slot)
            flags = annotations(slot)
            column = {
                "source_slot": str(key), "name": column_names[name][key],
                "source_range": str(slot.range), "type": datatype,
                "nullable": not (slot.required or key in primary),
                "requires_generated_values": str(flags.get("autoincrement", "")).lower() == "true",
                "logical_constraints": {tag: raw[tag] for tag in (
                    "minimum_value", "maximum_value", "pattern", "unit", "ifabsent",
                    "any_of", "all_of", "none_of", "exactly_one_of",
                ) if tag in raw},
            }
            if enum_values:
                column["enum_values"] = enum_values
            if slot.range in classes:
                target = identifiers[slot.range]
                if target is None:
                    raise ValueError(f"{name}.{key}: cannot resolve relationship target {slot.range}.")
                column["references"] = {
                    "table": table_names[slot.range], "column": column_names[slot.range][target.name],
                }
            columns.append(column)
        unique_keys = []
        for unique in cls.unique_keys.values():
            if any(key not in column_names[name] for key in unique.unique_key_slots):
                raise ValueError(f"{name}: a unique key refers to an unknown column.")
            unique_keys.append([column_names[name][key] for key in unique.unique_key_slots])
        tables.append({
            "source_class": str(name), "name": table_names[name],
            "derived_from": annotations(cls).get("linkml:derived_from"),
            "columns": columns, "primary_key": [column_names[name][key] for key in primary],
            "unique_keys": unique_keys,
        })
    notices = [*warnings, *FABRIC_NOTICES]
    report = {
        "format": "fabric-lakehouse", "layout": "relational", "target_schema": namespace,
        "source_schema": str(view.schema.id), "table_count": len(tables),
        "warnings": notices, "enforced_constraints": ["NOT NULL"],
        "logical_constraints_are_not_enforced": True, "tables": tables,
    }
    header = [
        "-- Microsoft Fabric Lakehouse: run in Spark SQL, NOT the SQL analytics endpoint.",
        "-- Attach the default Lakehouse and create the target schema before executing.",
        "-- CREATE TABLE intentionally fails on existing names. Use the notebook for safe re-runs.",
        "-- DDL is not transactional. No data is loaded; no keys or domain rules are enforced.",
        *["-- NOTICE: " + warning.replace("\r", " ").replace("\n", " ") for warning in notices],
    ]
    ddl = "\n".join(header) + "\n\n" + ";\n\n".join(create_table_sql(table, namespace) for table in tables) + ";\n"
    readme = (
        "Fabric Lakehouse relational export\n\n"
        "1. Import fabric_lakehouse.ipynb as a PySpark notebook in Fabric.\n"
        "2. Attach a DEFAULT schema-enabled Lakehouse. No workspace IDs or credentials are embedded.\n"
        f"3. Ensure schema {namespace!r} exists in the Lakehouse explorer (dbo exists by default).\n"
        "4. Review export_report.json for table/column mappings, scalar types and logical constraints.\n"
        "5. Run notebook cells in order. Preflight rejects incompatible existing tables before creation.\n\n"
        "Alternatively, execute lakehouse.spark.sql in a Spark SQL session for a NEW set of tables.\n"
        "Do not execute it in the read-only T-SQL analytics endpoint or a Fabric Warehouse.\n"
        "Existing compatible Delta tables are retained only by the notebook, not the standalone SQL.\n"
        "Spark DDL is not transactional; an execution failure can leave some newly created tables.\n"
        "Existing external tables retain their locations; SQL analytics visibility follows Fabric rules.\n\n"
        + "\n\n".join(notices) + "\n"
    )
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("fabric_lakehouse.ipynb", json_text(notebook_for(tables, namespace)))
        archive.writestr("lakehouse.spark.sql", ddl)
        archive.writestr("export_report.json", json_text(report))
        archive.writestr("README.txt", readme)
    return output.getvalue()


def export_fabric(schema: dict[str, Any], imports: dict[str, Any], warnings: list[str], namespace: str) -> bytes:
    validate_namespace(namespace)
    return run_relational_worker(schema, imports, warnings, "fabric", {"target_schema": namespace})
