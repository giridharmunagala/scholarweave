import { describe, expect, it } from 'vitest';
import {
  blankCustomNodeSpec,
  compileCustomConfigSchema,
  sanitizeCustomNodeName,
  validateCustomNodeSpec,
} from './customNodes';

describe('custom node helpers', () => {
  it('compiles the authoring field vocabulary into backend-compatible schema', () => {
    expect(
      compileCustomConfigSchema([
        { name: 'style', label: 'Style', description: '', kind: 'select', required: true, options: ['short', 'long'] },
        { name: 'limit', label: '', description: '', kind: 'integer', required: false, default: 3, options: [] },
      ]),
    ).toEqual({
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      type: 'object',
      properties: {
        style: { type: 'string', enum: ['short', 'long'], title: 'Style' },
        limit: { type: 'integer', default: 3 },
      },
      required: ['style'],
      additionalProperties: false,
    });
  });

  it('finds duplicate and invalid authoring identifiers before save', () => {
    const spec = blankCustomNodeSpec();
    spec.inputs = [
      { name: 'text', kind: 'text', item_kind: null, description: '', required: true },
      { name: 'text', kind: 'text', item_kind: null, description: '', required: false },
    ];
    spec.outputs = [];
    spec.config_fields = [
      { name: 'mode', label: '', description: '', kind: 'select', required: false, options: [] },
    ];
    spec.code = 'def transform(inputs):\n    return {}';

    const validation = validateCustomNodeSpec('bad name!', spec);
    expect(validation.valid).toBe(false);
    expect(validation.errors.name).toBeTruthy();
    expect(validation.errors['inputs.1.name']).toMatch(/unique/);
    expect(validation.errors.outputs).toBeTruthy();
    expect(validation.errors['config_fields.0.options']).toBeTruthy();
    expect(validation.errors.code).toBeTruthy();
  });

  it('makes a safe reusable definition slug', () => {
    expect(sanitizeCustomNodeName('  Citation cleaner v2!  ')).toBe('Citation-cleaner-v2');
    expect(sanitizeCustomNodeName('9 lives')).toBe('node-9-lives');
  });
});
