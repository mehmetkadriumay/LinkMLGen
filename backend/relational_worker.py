import json
import sys

import linkml.transformers.relmodel_transformer as relational
from linkml_runtime import SchemaView
from linkml_runtime.linkml_model import SchemaDefinition

from osdu_export import postgres_ddl


class RelationalSchemaView(SchemaView):
    def imports_closure(self, imports=True, traverse=None, inject_metadata=True):
        # The transformer repeatedly mutates its flattened, import-free target.
        # Re-injecting metadata into every slot after each mutation is quadratic.
        return super().imports_closure(
            imports=imports, traverse=traverse,
            inject_metadata=inject_metadata if self.schema.imports else False,
        )


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")
    payload = json.load(sys.stdin)
    source = SchemaView(SchemaDefinition(**payload["schema"]), importmap=payload["imports"])
    # Keep the LinkML target-view adapter in this worker, never API process globals.
    relational.SchemaView = RelationalSchemaView
    if payload["format"] == "postgresql":
        sys.stdout.write(postgres_ddl(source, payload["warnings"]))
    elif payload["format"] == "fabric":
        from fabric_export import fabric_bundle

        sys.stdout.buffer.write(fabric_bundle(source, payload["options"]["target_schema"], payload["warnings"]))
    else:
        raise ValueError("Unsupported relational export format.")


if __name__ == "__main__":
    main()
