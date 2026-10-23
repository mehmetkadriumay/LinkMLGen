# LinkML Studio - OSDU Ready

A local-first AI-assisted LinkML schema builder: guided conversation, a draggable class diagram, property editing, controlled vocabularies, YAML import/export, undo/redo, and JSON Schema export.

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
