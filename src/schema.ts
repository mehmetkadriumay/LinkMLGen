import { parse, stringify } from 'yaml';

export interface Slot {
  range?: string;
  description?: string;
  required?: boolean;
  multivalued?: boolean;
  identifier?: boolean;
  [key: string]: unknown;
}

export interface ClassDefinition {
  description?: string;
  is_a?: string;
  mixins?: string[];
  slots?: string[];
  attributes?: Record<string, Slot>;
  slot_usage?: Record<string, Slot>;
  [key: string]: unknown;
}

export interface Schema {
  id: string;
  name: string;
  default_range?: string;
  classes?: Record<string, ClassDefinition>;
  slots?: Record<string, Slot>;
  enums?: Record<string, unknown>;
  types?: Record<string, unknown>;
  [key: string]: unknown;
}

export const initialYaml = `id: https://example.org/research
name: research
title: Research registry
description: A starting point for your next data model.
prefixes:
  research: https://example.org/research/
  linkml: https://w3id.org/linkml/
default_prefix: research
default_range: string
imports:
  - linkml:types
classes:
  Person:
    description: A person participating in research.
    attributes:
      id:
        identifier: true
        description: A unique identifier.
      name:
        required: true
      organization:
        range: Organization
  Organization:
    description: An organization involved in research.
    attributes:
      id:
        identifier: true
      name:
        required: true
`;

export function readSchema(text: string): Schema {
  const value: unknown = parse(text);
  if (!value || typeof value !== 'object' || !('id' in value) || !('name' in value)) {
    throw new Error('Schema must contain id and name. Use Validate for full LinkML diagnostics.');
  }
  // Full metamodel validation happens on the backend before this becomes editor state.
  return value as Schema;
}

export function writeSchema(schema: Schema): string {
  return stringify(schema, { lineWidth: 100 });
}

export function ownSlots(schema: Schema, name: string): Record<string, Slot> {
  const cls = schema.classes?.[name];
  if (!cls) return {};
  const slots: Record<string, Slot> = Object.fromEntries(
    (cls.slots ?? []).map(key => [key, { ...schema.slots?.[key], ...cls.slot_usage?.[key] }]),
  );
  return { ...slots, ...cls.attributes };
}

export function effectiveSlots(schema: Schema, name: string, seen = new Set<string>()): Record<string, Slot> {
  if (seen.has(name)) return {};
  seen.add(name);
  const cls = schema.classes?.[name];
  const inherited = Object.assign({}, ...[cls?.is_a, ...(cls?.mixins ?? [])]
    .filter((parent): parent is string => Boolean(parent))
    .map(parent => effectiveSlots(schema, parent, new Set(seen))));
  const slots: Record<string, Slot> = { ...inherited, ...ownSlots(schema, name) };
  for (const [slot, override] of Object.entries(cls?.slot_usage ?? {})) {
    if (slots[slot]) slots[slot] = { ...slots[slot], ...override };
  }
  return Object.fromEntries(Object.entries(slots).map(([slot, definition]) =>
    [slot, { ...definition, range: definition.range ?? schema.default_range ?? 'string' }]));
}

export function changeSlot(schema: Schema, className: string, name: string, patch: Slot): Schema {
  const next = structuredClone(schema);
  const cls = next.classes![className];
  if (cls.attributes?.[name]) {
    cls.attributes[name] = { ...cls.attributes[name], ...patch };
  } else {
    cls.slot_usage = { ...cls.slot_usage, [name]: { ...cls.slot_usage?.[name], ...patch } };
  }
  return next;
}

export function removeClass(schema: Schema, name: string): Schema {
  const next = structuredClone(schema);
  delete next.classes![name];
  return next;
}

export function classReferences(schema: Schema, name: string): string[] {
  const references: string[] = [];
  for (const [owner, cls] of Object.entries(schema.classes ?? {})) {
    if (owner === name) continue;
    if (cls.is_a === name || cls.mixins?.includes(name)) references.push(owner);
    for (const [slot, definition] of Object.entries(effectiveSlots(schema, owner))) {
      if (definition.range === name) references.push(`${owner}.${slot}`);
    }
  }
  for (const [slot, definition] of Object.entries(schema.slots ?? {})) {
    if (definition.range === name) references.push(`slots.${slot}`);
  }
  return references;
}
