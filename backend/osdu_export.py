from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote
from zipfile import ZIP_DEFLATED, ZipFile

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError
from linkml.generators.sqltablegen import RelationalModelTransformer, SQLTableGenerator
from linkml_runtime import SchemaView
from linkml_runtime.utils.formatutils import camelcase
from sqlalchemy import Column, Enum, ForeignKeyConstraint, MetaData, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
from sqlalchemy.schema import AddConstraint, CreateTable

RECORD_GROUPS = ("master-data", "reference-data", "work-product", "work-product-component", "dataset")
SYSTEM_FIELDS = {
    "id", "kind", "version", "acl", "legal", "tags", "createTime", "createUser",
    "modifyTime", "modifyUser", "ancestry", "meta",
}
EXPORT_NOTICE = (
    "Generated from the current LinkML model, not a lossless reconstruction of the OSDU "
    "Generated corpus. Metadata absent from the consolidated YAML (including indexing, "
    "virtual properties and some composition rules) cannot be recovered. Review the "
    "authority, source, versions and platform-specific constraints before registration."
)
TEMPLATE_NOTICE = (
    "Examples are templates, NOT ingestion-ready records. Replace every {{...}} placeholder, "
    "supply real identifiers, ACL groups and legal tags, resolve relationships, and validate "
    "against the registered schemas before ingestion. No records are submitted by this app. "
    "Each manifest contains one example record. Nested examples show required fields and "
    "one optional field; recursive references are left as explicit placeholders."
)


@dataclass(frozen=True)
class RecordType:
    class_name: str
    group: str
    entity: str
    version: str
    authority: str
    source: str

    @property
    def kind(self) -> str:
        return f"{self.authority}:{self.source}:{self.group}--{self.entity}:{self.version}"

    @property
    def path(self) -> str:
        return f"{self.group}/{self.entity}.{self.version}.json"


def annotations(element: Any) -> dict[str, Any]:
    values = element.annotations
    items = values.items() if isinstance(values, dict) else values._items()
    return {str(key): value.value for key, value in items}


def record_types(view: SchemaView, options: Any) -> list[RecordType]:
    classes = view.all_classes()
    selected = options.class_names
    if selected and len(set(selected)) != len(selected):
        raise ValueError("Choose each export class only once.")
    records = []
    for name in selected or sorted(view.schema.classes):
        if name not in classes:
            raise ValueError(f"Unknown export class: {name}.")
        cls = classes[name]
        location = str(cls.class_uri or name).rsplit(":", 1)[-1]
        group, _, entity = location.rpartition("/")
        group = group.rsplit("/", 1)[-1]
        if group not in RECORD_GROUPS:
            group, _, entity = name.partition("/")
        if group not in RECORD_GROUPS:
            if not selected:
                continue
            if cls.mixin or cls.abstract or cls.tree_root or "/" in name:
                raise ValueError(f"{name} is not an OSDU record class. Select an entity such as master-data/Well.")
            group, entity = options.group_type, name
        if cls.mixin or cls.abstract:
            if selected:
                raise ValueError(f"{name} is abstract and cannot be exported as an OSDU record.")
            continue
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", entity):
            raise ValueError(f"Invalid OSDU entity name for {name}: {entity!r}.")
        version = options.version
        if not version:
            for url in cls.see_also:
                match = re.search(r"/" + re.escape(entity) + r"\.(\d+\.\d+\.\d+)\.json$", str(url))
                if match:
                    version = match[1]
                    break
        if not version:
            kind = str(annotations(cls).get("kind", annotations(view.schema).get("kind", "")))
            if re.fullmatch(r"[\w.-]+:[\w.-]+:" + re.escape(f"{group}--{entity}") + r":\d+\.\d+\.\d+", kind):
                version = kind.rsplit(":", 1)[-1]
        if not version:
            raise ValueError(
                f"{name}: no OSDU version in see_also or kind annotations. "
                "Enter an explicit export version, for example 1.0.0."
            )
        records.append(RecordType(name, group, entity, version, options.authority, options.source))
    if not records:
        raise ValueError("No OSDU record classes found. Select a class and choose its group type, or use group-qualified class names.")
    kinds = [record.kind for record in records]
    if len(set(kinds)) != len(kinds):
        raise ValueError("Multiple classes map to the same OSDU kind. Export them separately or give them distinct identities.")
    return records


def pointer(name: str) -> str:
    return "#/definitions/" + quote(name.replace("~", "~0").replace("/", "~1"), safe="~")


def rewrite_refs(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                node[key] = pointer(value[len("#/$defs/"):])
            else:
                rewrite_refs(value)
    elif isinstance(node, list):
        for value in node:
            rewrite_refs(value)


def property_name(slot: Any, original_names: dict[str, str]) -> str:
    return original_names.get(str(slot.slot_uri), str(slot.alias or slot.name))


def osdu_definitions(view: SchemaView, generated: dict[str, Any]) -> dict[str, Any]:
    definitions = deepcopy(generated.get("$defs", {}))
    names = [camelcase(str(name)) for name in view.all_classes()]
    if len(set(names)) != len(names):
        raise ValueError("Class names collide in LinkML JSON Schema generation. Rename the conflicting classes before export.")
    original_names = {}
    for cls in view.all_classes().values():
        local = str(cls.class_uri or "").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        for slot in cls.attributes.values():
            uri = str(slot.slot_uri or "")
            suffix = uri.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            if local and suffix.startswith(local) and suffix != local:
                original_names[uri] = suffix[len(local):]
    for name in view.all_classes():
        definition = definitions.get(camelcase(str(name)))
        if not definition:
            continue
        properties = definition.get("properties", {})
        for slot in view.class_induced_slots(name):
            key = str(slot.alias or slot.name)
            if key not in properties:
                continue
            prop = properties[key]
            renamed = property_name(slot, original_names)
            if renamed != key:
                if renamed in properties:
                    raise ValueError(f"{name}: JSON property name collision between {key} and {renamed}.")
                properties[renamed] = properties.pop(key)
                definition["required"] = [renamed if value == key else value for value in definition.get("required", [])]
            anns = annotations(slot)
            if str(anns.get("map", "")).lower() == "true":
                prop.pop("items", None)
                prop["type"] = "object"
                prop["additionalProperties"] = {"type": "string"}
                if "patternProperties map" in str(anns.get("map_value_description", "")):
                    prop["additionalProperties"] = False
                    if anns.get("pattern_properties_json"):
                        patterns = json.loads(str(anns["pattern_properties_json"]))
                        if not isinstance(patterns, dict):
                            raise ValueError(f"{name}.{renamed}: pattern_properties_json must describe an object.")
                        prop["patternProperties"] = patterns
            targets = str(anns.get("relationship_targets", ""))
            relations = []
            for target in targets.split(","):
                group, separator, entity = target.strip().partition("::")
                if separator and group in RECORD_GROUPS:
                    relations.append({"GroupType": group, "EntityType": entity})
            if relations:
                prop["x-osdu-relationship"] = relations
            for key in ("attribution_authority", "attribution_publication", "attribution_revision"):
                if key in anns:
                    prop["x-osdu-" + key.replace("_", "-")] = anns[key]
            frame = anns.get("frame_of_reference")
            if frame:
                if frame == "UOM" and anns.get("quantity_kind"):
                    frame = f"UOM:{anns['quantity_kind']}"
                elif frame == "UOM_via_property" and anns.get("uom_property"):
                    frame = f"UOM_via_property:{anns['uom_property']}"
                prop["x-osdu-frame-of-reference"] = frame
            for key, value in anns.items():
                if key.startswith("x-osdu-"):
                    prop[key] = value
    rewrite_refs(definitions)
    return definitions


def schema_for(record: RecordType, definitions: dict[str, Any], view: SchemaView) -> dict[str, Any]:
    key = camelcase(record.class_name)
    if key not in definitions:
        raise ValueError(f"LinkML did not generate a definition for {record.class_name}.")
    data = deepcopy(definitions[key])
    props = data.setdefault("properties", {})
    envelope = {name: props.pop(name) for name in list(props) if name in SYSTEM_FIELDS}
    data["required"] = [name for name in data.get("required", []) if name not in SYSTEM_FIELDS]
    if not data["required"]:
        del data["required"]
    props.setdefault("ExtensionProperties", {"type": "object", "additionalProperties": True})
    data["additionalProperties"] = False
    envelope["id"] = {
        "type": "string",
        "pattern": r"^[\w\-.]+:" + re.escape(f"{record.group}--{record.entity}") + r":[\w\-.:%]+$",
    }
    envelope["kind"] = {"type": "string", "const": record.kind}
    envelope.setdefault("acl", {
        "type": "object", "additionalProperties": False, "required": ["owners", "viewers"],
        "properties": {key: {"type": "array", "minItems": 1, "items": {"type": "string"}} for key in ("owners", "viewers")},
    })
    envelope.setdefault("legal", {
        "type": "object", "required": ["legaltags", "otherRelevantDataCountries"],
        "properties": {
            "legaltags": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "otherRelevantDataCountries": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^[A-Z]{2}$"}},
            "status": {"type": "string"},
        },
    })
    envelope["data"] = data
    cls = view.get_class(record.class_name)
    result = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "urn:" + record.kind,
        "x-osdu-schema-source": record.kind,
        "title": record.entity, "description": str(cls.description or ""),
        "type": "object", "properties": envelope,
        "required": ["kind", "acl", "legal"] + (["data"] if data.get("required") else []),
        "additionalProperties": False,
    }
    license_text = annotations(cls).get("license", annotations(view.schema).get("license"))
    if license_text:
        result["x-osdu-license"] = license_text
    needed: dict[str, Any] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref", "")
            if ref:
                if not ref.startswith("#/definitions/"):
                    raise ValueError(f"Unsupported reference in OSDU export: {ref}.")
                name = unquote(ref[len("#/definitions/"):]).replace("~1", "/").replace("~0", "~")
                if name not in definitions:
                    raise ValueError(f"Unresolved JSON Schema reference: {ref}.")
                if name not in needed:
                    needed[name] = definitions[name]
                    visit(definitions[name])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    if needed:
        result["definitions"] = dict(sorted(needed.items()))
    try:
        Draft7Validator.check_schema(result)
    except SchemaError as exc:
        raise ValueError(f"{record.class_name}: invalid generated Draft-07 schema: {exc.message}") from exc
    return result


def example_value(node: dict[str, Any], schema: dict[str, Any], path: str, active: frozenset[str] = frozenset(), all_fields: bool = False) -> Any:
    if "$ref" in node:
        ref = node["$ref"]
        if ref in active:
            return "{{RECURSIVE_REFERENCE:" + path + "}}"
        name = unquote(ref[len("#/definitions/"):]).replace("~1", "/").replace("~0", "~")
        return example_value(schema["definitions"][name], schema, path, active | {ref})
    if "const" in node:
        return node["const"]
    if node.get("enum"):
        return node["enum"][0]
    for union in ("oneOf", "anyOf"):
        if node.get(union):
            choice = next((value for value in node[union] if value.get("type") != "null"), node[union][0])
            return example_value(choice, schema, path, active, all_fields)
    kind = node.get("type")
    if isinstance(kind, list):
        kind = next((value for value in kind if value != "null"), "null")
    if kind == "object" or "properties" in node:
        props = node.get("properties", {})
        required = set(node.get("required", []))
        optional = next((name for name in props if name not in required), None)
        return {
            name: example_value(value, schema, f"{path}.{name}", active)
            for name, value in props.items() if all_fields or name in required or name == optional
        }
    if kind == "array":
        return [example_value(node.get("items", {}), schema, path + "[]", active) for _ in range(max(1, node.get("minItems", 0)))] if node.get("maxItems") != 0 else []
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        return node.get("minimum", 0)
    if kind == "null":
        return None
    if node.get("format") in ("date", "date-time", "time"):
        return {"date": "2024-01-01", "date-time": "2024-01-01T00:00:00Z", "time": "00:00:00Z"}[node["format"]]
    return "{{" + path + "}}"


def manifest_for(record: RecordType, schema: dict[str, Any]) -> dict[str, Any]:
    example = {
        "id": "{{NAMESPACE}}:" + f"{record.group}--{record.entity}:example",
        "kind": record.kind,
        "acl": {"owners": ["{{DATA_OWNERS_GROUP}}"], "viewers": ["{{DATA_VIEWERS_GROUP}}"]},
        "legal": {"legaltags": ["{{LEGAL_TAG}}"], "otherRelevantDataCountries": ["{{ISO_3166_ALPHA_2_CODE}}"]},
        "data": example_value(schema["properties"]["data"], schema, "data", all_fields=True),
    }
    manifest: dict[str, Any] = {"kind": "osdu:wks:Manifest:1.0.0"}
    if record.group in ("master-data", "reference-data"):
        manifest["MasterData" if record.group == "master-data" else "ReferenceData"] = [example]
    elif record.group == "work-product":
        manifest["Data"] = {"WorkProduct": example}
    else:
        manifest["Data"] = {"Datasets" if record.group == "dataset" else "WorkProductComponents": [example]}
    return manifest


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def export_bundle(view: SchemaView, generated: dict[str, Any], options: Any, mode: str, warnings: list[str]) -> bytes:
    records = record_types(view, options)
    definitions = osdu_definitions(view, generated)
    report = {
        "format": mode, "record_count": len(records),
        "warnings": [*warnings, EXPORT_NOTICE] + ([TEMPLATE_NOTICE] if mode == "manifests" else []),
        "reference_branch": "883-logical-data-models-linkml",
        "reference_commit": "6ad3d88367b535fb23b29cb4f3bef744e33be9be",
        "records": [{"class": record.class_name, "kind": record.kind, "path": record.path} for record in records],
    }
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("export_report.json", json_text(report))
        for record in records:
            schema = schema_for(record, definitions, view)
            if mode == "manifests":
                archive.writestr("manifests/" + record.path, json_text(manifest_for(record, schema)))
                continue
            archive.writestr("schemas/" + record.path, json_text(schema))
            major, minor, patch = map(int, record.version.split("."))
            registration = {
                "schemaInfo": {
                    "schemaIdentity": {
                        "authority": record.authority, "source": record.source,
                        "entityType": f"{record.group}--{record.entity}",
                        "schemaVersionMajor": major, "schemaVersionMinor": minor,
                        "schemaVersionPatch": patch, "id": record.kind,
                    },
                    "scope": "INTERNAL", "status": "DEVELOPMENT",
                },
                "schema": schema,
            }
            archive.writestr("registration/" + record.path, json_text(registration))
    return output.getvalue()


def postgres_name(value: str) -> str:
    if "\x00" in value:
        raise ValueError("PostgreSQL identifiers cannot contain null characters.")
    if len(value.encode("utf-8")) <= 63:
        return value
    prefix = value
    while len(prefix.encode("utf-8")) > 50:
        prefix = prefix[:-1]
    return prefix + "_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def unique_names(values: list[str]) -> dict[str, str]:
    result = {value: postgres_name(value) for value in values}
    if len(set(result.values())) != len(result):
        raise ValueError("PostgreSQL identifier collision after shortening. Rename the conflicting elements.")
    return result


def run_relational_worker(
    schema: dict[str, Any], imports: dict[str, Any], warnings: list[str],
    format: Literal["postgresql", "fabric"], options: dict[str, Any] | None = None,
) -> bytes:
    label = "PostgreSQL" if format == "postgresql" else "Fabric Lakehouse"
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("relational_worker.py"))],
            input=json_text({
                "schema": schema, "imports": imports, "warnings": warnings, "format": format,
                "options": options or {},
            }).encode("utf-8"),
            capture_output=True, timeout=240,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{label} generation exceeded four minutes. The schema is unchanged.") from exc
    if result.returncode or result.stderr.strip():
        raise ValueError(f"{label} generator failed: " + (result.stderr.decode("utf-8").strip() or f"exit code {result.returncode}"))
    if not result.stdout.strip():
        raise ValueError(f"{label} generator returned an empty file.")
    return result.stdout


def export_postgres(schema: dict[str, Any], imports: dict[str, Any], warnings: list[str]) -> str:
    return run_relational_worker(schema, imports, warnings, "postgresql").decode("utf-8")


def postgres_ddl(view: SchemaView, warnings: list[str]) -> str:
    relational = RelationalModelTransformer(view).transform().schema
    relational_view = SchemaView(relational)
    classes = relational_view.all_classes()
    table_names = unique_names(list(classes))
    generator = SQLTableGenerator(view.schema, importmap=view.importmap, dialect="postgresql")
    metadata = MetaData()
    tables = {}
    column_maps = {}
    for name, cls in sorted(classes.items()):
        column_names = unique_names(list(cls.attributes))
        column_maps[name] = column_names
        identifier = RelationalModelTransformer.get_direct_identifier_attribute(relational_view, name)
        columns = []
        for key, slot in cls.attributes.items():
            flags = annotations(slot)
            primary = key == identifier.name if identifier is not None else str(flags.get("primary_key", "")).lower() == "true"
            datatype = generator.get_sql_range(slot, relational, sv=relational_view)
            if isinstance(datatype, Enum):
                datatype = Enum(
                    *datatype.enums, native_enum=False, create_constraint=True,
                    name=postgres_name(f"ck_{table_names[name]}_{column_names[key]}_enum"),
                )
            columns.append(Column(column_names[key], datatype, primary_key=primary, nullable=not (primary or slot.required), quote=True))
        tables[name] = Table(table_names[name], metadata, *columns, quote=True)
        for unique in cls.unique_keys.values():
            keys = list(unique.unique_key_slots)
            if not all(key in column_names for key in keys):
                raise ValueError(f"{name}: unique key refers to a missing column.")
            tables[name].append_constraint(UniqueConstraint(*(tables[name].c[column_names[key]] for key in keys)))
    foreign_keys = []
    for name, cls in sorted(classes.items()):
        for key, slot in sorted(cls.attributes.items()):
            if slot.range not in tables:
                continue
            target = slot.range
            identifier = RelationalModelTransformer.get_direct_identifier_attribute(relational_view, target)
            if identifier is None:
                identifier = relational_view.get_identifier_slot(target)
            if identifier is None or identifier.name not in column_maps[target]:
                raise ValueError(f"{name}.{key}: cannot resolve the foreign key for {target}.")
            target_column = tables[target].c[column_maps[target][identifier.name]]
            unique_targets = [tables[target].primary_key, *[
                item for item in tables[target].constraints if isinstance(item, UniqueConstraint)
            ]]
            if not any(len(item.columns) == 1 and next(iter(item.columns)) is target_column for item in unique_targets):
                raise ValueError(f"{name}.{key}: foreign key target {target}.{identifier.name} is not uniquely constrained.")
            constraint = ForeignKeyConstraint(
                [tables[name].c[column_maps[name][key]]], [target_column],
                name=postgres_name(f"fk_{table_names[name]}_{column_maps[name][key]}"), use_alter=True,
            )
            tables[name].append_constraint(constraint)
            foreign_keys.append(constraint)
    dialect = postgresql_dialect()
    header = [
        "-- PostgreSQL DDL generated from LinkML's official relational model transformer.",
        "-- Quoted identifiers preserve case. Long names use stable hash suffixes (63 UTF-8 bytes maximum).",
        "-- Repeated properties use junction tables. Foreign keys follow all CREATE TABLE statements.",
        "-- This is a relational projection, not the OSDU Storage Service database schema or a migration.",
        "-- JSON Schema patterns, units and OSDU ingestion rules are not SQL constraints.",
        *["-- WARNING: " + warning.replace("\r", " ").replace("\n", " ") for warning in warnings],
        "-- Table mapping: " + json.dumps(table_names, ensure_ascii=True, sort_keys=True),
    ]
    statements = [
        str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=dialect)).strip() + ";"
        for table in sorted(tables.values(), key=lambda item: item.name)
    ]
    statements += [
        str(AddConstraint(constraint).compile(dialect=dialect)).strip() + ";"
        for constraint in sorted(foreign_keys, key=lambda item: (item.table.name, item.name))
    ]
    return "\n".join(header) + "\n\nBEGIN;\n\n" + "\n\n".join(statements) + "\n\nCOMMIT;\n"
