import { describe, expect, it } from 'vitest';
import { changeSlot, classReferences, effectiveSlots, initialYaml, readSchema, writeSchema } from './schema';

describe('schema editing', () => {
  it('preserves metadata through an edit and YAML roundtrip', () => {
    const schema = readSchema(initialYaml);
    const next = changeSlot(schema, 'Person', 'name', { multivalued: true });
    expect(readSchema(writeSchema(next)).prefixes).toEqual(schema.prefixes);
    expect(next.classes?.Person.attributes?.name.multivalued).toBe(true);
    expect(schema.classes?.Person.attributes?.name.multivalued).toBeUndefined();
  });
  it('overrides shared slots locally instead of mutating the global definition', () => {
    const schema = readSchema(initialYaml);
    schema.slots = { email: { range: 'string', required: false } };
    schema.classes!.Person.slots = ['email'];
    const next = changeSlot(schema, 'Person', 'email', { required: true });
    expect(next.slots?.email.required).toBe(false);
    expect(effectiveSlots(next, 'Person').email.required).toBe(true);
  });
  it('includes inherited properties and guards against cycles', () => {
    const schema = readSchema(initialYaml);
    schema.classes!.Employee = { is_a: 'Person' };
    expect(effectiveSlots(schema, 'Employee').id.identifier).toBe(true);
    schema.classes!.Person.is_a = 'Employee';
    expect(() => effectiveSlots(schema, 'Employee')).not.toThrow();
  });
  it('finds relationship references before deletion', () => {
    expect(classReferences(readSchema(initialYaml), 'Organization')).toContain('Person.organization');
  });
  it('applies class-local overrides to inherited properties', () => {
    const schema = readSchema(initialYaml);
    schema.classes!.Employee = { is_a: 'Person', slot_usage: { name: { multivalued: true } } };
    expect(effectiveSlots(schema, 'Employee').name.multivalued).toBe(true);
    const next = changeSlot(schema, 'Employee', 'name', { required: false });
    expect(effectiveSlots(next, 'Employee').name.required).toBe(false);
    expect(effectiveSlots(next, 'Person').name.required).toBe(true);
  });
  it('respects schema-level default ranges', () => {
    const schema = readSchema(initialYaml);
    schema.default_range = 'integer';
    expect(effectiveSlots(schema, 'Person').name.range).toBe('integer');
  });
});
