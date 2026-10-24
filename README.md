# LinkML Studio - OSDU Ready

A local-first AI-assisted LinkML schema builder: guided conversation, a draggable class diagram, property editing, controlled vocabularies, YAML import/export, undo/redo, JSON Schema, OSDU registration/manifest exports, PostgreSQL DDL, and Microsoft Fabric Lakehouse exports.

Built on [LinkML](https://github.com/linkml/linkml), not a fork. The backend uses the official LinkML metamodel validator, `SchemaView`, and `JsonSchemaGenerator`. React Flow provides the interactive diagram. AI proposals are validated and require explicit approval before being applied.

## Run locally (Windows PowerShell)

Requires Python 3.10+ and Node.js 20.19+ (or 22.12+).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if (-not (Test-Path backend\.env)) { Copy-Item backend\.env.example backend\.env }
npm install
```

Configure **Microsoft Foundry** in `backend\.env`:

```dotenv
FOUNDRY_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/openai/v1/
FOUNDRY_API_KEY=your-foundry-resource-key
FOUNDRY_MODEL_DEPLOYMENT=gpt-5.4-mini
```

Use the HTTPS **model endpoint**, not a Foundry project/agent endpoint, and the exact **deployment name** from Foundry. Resource endpoints ending in `.services.ai.azure.com/openai/v1/` are also supported. A trailing slash is normalized automatically; no `api-version` parameter is needed for this v1 API. The deployment must support Chat Completions and JSON Schema structured outputs.

The app uses the OpenAI Python SDK as the client for Foundry's OpenAI-compatible API, but requests go only to the explicitly configured Foundry endpoint. It does not use the public OpenAI API or fall back to `OPENAI_API_KEY`/`OPENAI_MODEL`. All three `FOUNDRY_*` variables are required; manual and visual editing work without them. Never put API keys in frontend code or paste them into chat.

Start the backend in one terminal:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --app-dir backend --env-file backend\.env --host 127.0.0.1 --port 8000
```

Start the frontend in another terminal:

```powershell
npm run dev
```

Open **http://127.0.0.1:5173**. Vite proxies `/api` to the local backend. Restart the backend after changing `.env`.

The AI panel shows any missing or invalid Foundry configuration. `http://127.0.0.1:8000/api/health` reports `ai_configured: true` when the three settings are present and the endpoint format is valid; this is a configuration check, not proof that authentication or the deployment works. After restarting, refresh the page, wait for schema validation to finish, apply/discard pending YAML changes, and type a message to enable **Send message**.

## Build a schema

1. Describe your domain to the guide. It asks focused questions about entities, identifiers, relationships, cardinality, and vocabularies.
2. Review the current and proposed YAML before applying AI changes. Invalid proposals cannot be applied.
3. Select classes on the canvas to edit properties. Drag from a class's right handle to another class's left handle to add a relationship. Change the property's range and multiple-values flag in the inspector.
4. Add controlled vocabularies in the inspector, then select their names as property ranges. Use YAML for advanced features such as inheritance, prefixes, global slots, `slot_usage`, patterns, and numeric bounds.
5. Validate and apply YAML changes to update the visual model. Export YAML or JSON Schema to save a portable copy.

The YAML document is the source of truth. All applied edits pass backend validation before replacing the current schema. Browser storage retains the last YAML draft; it is restored and validated on reload. Invalid drafts remain in the YAML editor without replacing the valid visual model. Undo history and chat are session-only. Canvas positions are presentation-only and are not exported.

During validation or another request, a progress banner shows the current operation and elapsed time. **Cancel operation** stops waiting and unlocks the editor without applying a late response; unapplied YAML is preserved. **Discard edits** remains available during processing and cancels the pending operation before restoring the last applied schema. Requests time out after five minutes rather than leaving controls locked indefinitely. Cancellation does not terminate validation already running on the backend.

## Privacy and limits

- Sending a chat message sends the current schema, its registered local imports, and that conversation to your configured **Microsoft Foundry** endpoint. Other actions do not send schema content to an AI provider. Foundry usage requires your own resource key and deployment and may incur Azure charges.
- Schema YAML is saved in this browser's local storage, not a backend database. Do not use a shared browser profile for confidential schemas. API keys stay server-side.
- This is a local-development MVP, not a multi-user hosted service. Bind to loopback only. Authentication, collaboration, rate limiting, deployment hardening, and durable server-side storage are not implemented.
- Imports support `linkml:types` and named YAML dependencies explicitly placed in `backend\imports`. Dependencies are validated recursively and resolved in memory. URL imports, filesystem paths, and files outside that directory are rejected. Local dependency files are ignored by Git.
- The diagram renders class inheritance, mixins, and class-valued properties. Advanced LinkML constructs remain editable and preserved in YAML, but are not all represented visually. Visual deletion can be rejected when other LinkML constructs still reference the deleted element.
- There is no application-imposed schema size limit for import, validation, export, or AI proposals. Large schemas take longer to process and can exceed browser storage quotas; if a storage warning appears, export YAML to save your work. AI requests are still subject to the selected model's context and output limits. Conversations are capped at 40 messages; start a new chat to continue.
- Validation checks the metamodel and supported semantic references and exercises official JSON Schema generation. It is not a proof of domain correctness or equivalence across every LinkML export format.
- Quantity-kind-only `unit` definitions (a `has_quantity_kind` without a unit code, symbol, or mapping) load with visible metamodel warnings. Their metadata remains unchanged; no concrete unit is guessed. Other metamodel errors still block applying a schema. API responses distinguish accepted documents (`valid`) from strict metamodel conformance (`strict_valid`).

## OSDU and local dependencies

For a schema importing `osdu_annotations`, register the actual annotation schema before importing the main YAML:

```powershell
New-Item -ItemType Directory -Path backend\imports -Force
Copy-Item C:\path\to\osdu_annotations.yaml backend\imports\osdu_annotations.yaml
```

Then import `osdu_latest.yaml` in the app. The main file and annotation values are retained, including OSDU class names such as `master-data/Well`. Imported type names are available in the property range selector. Dependencies remain separate: when sharing exported YAML, include its dependency files as well. Place additional named dependencies in the same folder; no backend restart is needed when registering or updating a dependency.

If the app reports quantity-kind-only unit warnings, expand the warnings panel to see affected properties. The schema can be edited and exported with these warnings, but it is not labeled strictly valid.

## OSDU JSON and PostgreSQL exports

In the property inspector, scroll to **OSDU exports**. Choose **Selected class** (for example `master-data/Well`) or **All OSDU record classes**. Abstract/mixin classes and the logical `OsduManifest` container are not ingestion records.

Review **Schema authority** and **Schema source** before exporting: defaults are `osdu` / `wks` for the supplied OSDU models. Use your organization's namespace for custom schemas. The version is taken from each class's versioned OSDU `see_also` URL or `kind` annotation. An explicit **Export version override** replaces that version; it is required for new classes without OSDU version metadata. For unqualified new classes such as `Person`, choose a group type and use selected-class scope. Whole-model scope includes only group-qualified OSDU record classes.

| Button | Download |
| --- | --- |
| Export OSDU schemas + registrations | ZIP with `schemas/<group>/<entity>.<version>.json` (self-contained Draft-07 JSON Schema) and `registration/...` (Schema Service request bodies with `schemaInfo.schemaIdentity` and `schema`). Registrations are deliberately `INTERNAL` / `DEVELOPMENT`, not published shared standards. |
| Export OSDU manifest templates | ZIP with one ingestion manifest example per record type, using the `osdu:wks:Manifest:1.0.0` envelope. Master/reference records use `MasterData` / `ReferenceData`; datasets and components use `Data.Datasets` / `Data.WorkProductComponents`; work products use the singular `Data.WorkProduct`. |
| Export PostgreSQL DDL (whole model) | Transactional `.sql` file covering the entire model, including abstract/nested classes and relational tables for multivalued properties. This button does not use the JSON export scope or identity settings. |

Every ZIP contains `export_report.json` with class-to-kind mappings, notices and validation warnings. Examples contain explicit `{{NAMESPACE}}`, ACL, legal-tag, country-code and domain-value placeholders. They are **not ingestion-ready**. Replace all placeholders, supply real relationships, check dependency/load order, and validate completed records against your platform's registered schemas before submitting. Nested objects show required fields and one illustrative optional field; cycles are marked `{{RECURSIVE_REFERENCE:...}}` rather than expanded forever. Separate example manifests do not automatically assemble a related work-product/dataset/component ensemble. No export registers schemas, executes SQL or submits data.

The OSDU exporter adapts the official LinkML JSON Schema generator: it places domain properties under `data`, preserves the system envelope, emits kind identity, enum/pattern/cardinality constraints and available relationship/attribution annotations, restores renamed property names from slot URIs, and embeds reachable definitions with escaped local references. Record-schema `$id` values use `urn:<kind>` rather than claiming a published schema URL. This is **not a lossless reconstruction** of upstream OSDU schemas. The consolidated model lacks some original indexing, virtual-property and composition metadata; these cannot be recovered automatically. Review generated schemas for platform and domain requirements, especially advanced union/composition annotations, before registration. The generic **Export JSON Schema** remains available separately.

DDL uses LinkML's official `RelationalModelTransformer` and SQL type mappings with PostgreSQL-specific SQLAlchemy compilation. Repeated primitive/reference properties use junction tables; inlined child objects can instead carry parent back-reference columns. Identifiers are quoted to retain case and shortened deterministically to at most **63 UTF-8 bytes** using hash suffixes; the SQL header records table-name mappings. Foreign keys are added after all tables, including cyclic relationships. Enum values and unique keys become SQL constraints. JSON Schema patterns, units, complex LinkML rules and OSDU ingestion/governance rules are not automatically SQL constraints. This is a **relational projection**, not the OSDU Storage Service's internal database schema or a database migration. Review the SQL before executing it in a new database.

Whole-model SQL generation runs in an isolated local Python worker. A target-view adapter avoids repeated metadata injection while LinkML mutates its flattened, import-free relational model; the API process's LinkML globals are not modified. The worker stops after four minutes and returns an explicit error if it cannot finish, within the UI's five-minute request timeout. Large exports may take several minutes and have no schema-size cap.

The implementation was informed by the OSDU schema repository's [883-logical-data-models-linkml branch](https://gitlab.opengroup.org/osdu/subcommittees/data-def/work-products/schema/-/tree/883-logical-data-models-linkml), inspected at commit `6ad3d88367b535fb23b29cb4f3bef744e33be9be`: `Generated/manifest/Manifest.1.0.0.json`, `Scripts/SchemaInfo.py`, `Scripts/SchemaServiceBootstrapper.py`, `Scripts/LinkMLHybridReconstructor.py`, and `Realisations/Scripts/generate_ddl.py`. Upstream's hybrid round-trip tool also needs per-entity YAML and original Generated JSON; this app does not bundle that corpus or claim its round-trip guarantees. Runtime exports need no upstream checkout or network connection.

## Microsoft Fabric Lakehouse exports

Under **Microsoft Fabric Lakehouse** in the inspector, set the **Fabric Lakehouse schema** (default `dbo`) and choose **Export Fabric Lakehouse (whole model)**. This works with ordinary LinkML and OSDU models; it does not use the OSDU JSON export scope, authority or version fields. The download is `fabric-lakehouse.zip` containing:

| File | Purpose |
| --- | --- |
| `fabric_lakehouse.ipynb` | Importable PySpark notebook that preflights existing tables and creates missing managed Delta tables in the attached default Lakehouse. |
| `lakehouse.spark.sql` | Equivalent `CREATE TABLE ... USING DELTA` statements for a new set of tables, to execute through **Spark SQL**, not the SQL analytics endpoint or a Fabric Warehouse. |
| `export_report.json` | Original-to-Fabric table/column mappings, scalar types, logical keys, relationships, enum values, other available constraints, and warnings. |
| `README.txt` | Setup instructions and limitations included with the download. |

Import the notebook into Fabric, select **PySpark**, attach a **default schema-enabled Lakehouse**, and create the named schema in the Lakehouse explorer first. `dbo` already exists in schema-enabled Lakehouses. Review the export report and run cells in order. No credentials, workspace IDs or Lakehouse IDs are embedded; creation occurs only when you run the downloaded artifacts in your own Fabric session. The app does not connect to Fabric or upload data.

The exporter shares the official LinkML relational transformer with PostgreSQL: classes become tables, repeated scalars/references use junction tables, and inlined children can carry parent back-references. Names are normalized to lowercase ASCII letters/digits/underscores, with deterministic hash suffixes for collisions or names longer than 128 characters. Use the report to map names and relationships when loading data. Columns use `STRING`, `BIGINT`, `FLOAT`, `DOUBLE`, `BOOLEAN`, `DATE`, or `TIMESTAMP`. Unbounded LinkML decimals use `STRING` rather than guessing precision/scale, and time-of-day uses `STRING` rather than inventing a date. Derived scalar types inherit their base type. Integer values must fit signed 64-bit `BIGINT`; review ingestion casts and timestamp/time-zone handling.

**Primary keys, foreign keys, unique keys, enums and domain rules are not enforced** by the Delta DDL; they are recorded in the report. Required/key columns use `NOT NULL`. Allocate synthetic/surrogate keys during ingestion: there is no auto-increment or sample-data load.

The notebook checks every existing table's Delta provider, column names/order, types and nullability before creating missing tables. Compatible tables and their rows are retained; mismatches fail explicitly and nothing is overwritten or migrated. It also refuses to run without a default Lakehouse or after the target changes between preflight and creation. Spark DDL is not transactional: execution failures can leave some new tables created. The standalone SQL intentionally fails on existing table names; use the notebook for safe re-runs. Existing external tables retain their locations and remain subject to Fabric's SQL analytics discovery limitations.

Both relational exporters run in the same isolated local worker with a four-minute generation timeout. Notebook schema definitions are split into multiple cells for large models without truncating the schema.

References: [Lakehouse schemas](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-schemas), [read-only SQL analytics endpoint](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-sql-analytics-endpoint), and [NotebookUtils runtime context](https://learn.microsoft.com/en-us/fabric/data-engineering/notebookutils/notebookutils-runtime).

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest backend -q
npm test
npm run build
```

Browser integration checks (no real Microsoft Foundry requests):

```powershell
npx playwright install chromium
npm run test:e2e
```

These checks start and stop their own local backend and frontend. Leave ports 8000 and 5173 free.

The frontend build is written to `dist`. `npm run preview` does not proxy API requests; production hosting must serve the frontend and route `/api` to the backend.
