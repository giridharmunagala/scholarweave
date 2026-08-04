import type {
  CustomConfigKind,
  CustomNodeConfigField,
  CustomNodePort,
  CustomNodeSpec,
  CustomPortKind,
} from '../types/api';

export const CUSTOM_PORT_KINDS: CustomPortKind[] = ['any', 'text', 'number', 'json', 'list'];
export const CUSTOM_CONFIG_KINDS: CustomConfigKind[] = ['text', 'number', 'integer', 'boolean', 'json', 'select'];
export const CUSTOM_NODE_NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_-]*$/;
export const CUSTOM_IDENTIFIER_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function blankCustomPort(kind: CustomPortKind = 'any'): CustomNodePort {
  return { name: '', kind, item_kind: null, description: '', required: true };
}

export function blankCustomConfigField(): CustomNodeConfigField {
  return { name: '', label: '', description: '', kind: 'text', required: false, default: undefined, options: [] };
}

export function blankCustomNodeSpec(): CustomNodeSpec {
  return {
    label: '',
    description: '',
    category: 'custom',
    tags: [],
    inputs: [],
    outputs: [{ name: 'result', kind: 'any', item_kind: null, description: '', required: true }],
    config_fields: [],
    code: 'def transform(inputs, config):\n    return {"result": inputs}\n',
  };
}

/** Mirrors backend.custom_nodes.compile_config_schema so authored previews match runtime validation. */
export function compileCustomConfigSchema(fields: CustomNodeConfigField[]): Record<string, unknown> {
  const properties: Record<string, Record<string, unknown>> = {};
  const required: string[] = [];
  const typeMap: Record<Exclude<CustomConfigKind, 'json' | 'select'>, string> = {
    text: 'string',
    number: 'number',
    integer: 'integer',
    boolean: 'boolean',
  };

  fields.forEach((field) => {
    const schema: Record<string, unknown> =
      field.kind === 'json'
        ? { type: ['object', 'array', 'string', 'number', 'integer', 'boolean', 'null'] }
        : field.kind === 'select'
          ? { type: 'string', enum: [...field.options] }
          : { type: typeMap[field.kind] };
    if (field.label) schema.title = field.label;
    if (field.description) schema.description = field.description;
    if (field.default !== undefined && field.default !== null) schema.default = field.default;
    properties[field.name] = schema;
    if (field.required && field.default == null) required.push(field.name);
  });

  return {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    type: 'object',
    properties,
    required,
    additionalProperties: false,
  };
}

export interface CustomSpecValidation {
  errors: Record<string, string>;
  valid: boolean;
}

export function validateCustomNodeSpec(name: string, spec: CustomNodeSpec): CustomSpecValidation {
  const errors: Record<string, string> = {};
  if (!CUSTOM_NODE_NAME_PATTERN.test(name.trim())) {
    errors.name = 'Use letters first, then letters, numbers, hyphens, or underscores.';
  }
  if (!spec.label.trim()) errors.label = 'A label is required.';
  if (!spec.category.trim()) errors.category = 'A category is required.';
  if (!spec.outputs.length) errors.outputs = 'At least one output port is required.';
  validatePorts(spec.inputs, 'inputs', errors);
  validatePorts(spec.outputs, 'outputs', errors);

  const fields = new Set<string>();
  spec.config_fields.forEach((field, index) => {
    const key = `config_fields.${index}.name`;
    if (!CUSTOM_IDENTIFIER_PATTERN.test(field.name.trim())) errors[key] = 'Use a valid identifier.';
    else if (fields.has(field.name)) errors[key] = 'Config field names must be unique.';
    fields.add(field.name);
    if (field.kind === 'select' && field.options.filter((option) => typeof option === 'string' && option.trim()).length === 0) {
      errors[`config_fields.${index}.options`] = 'Select fields need at least one option.';
    }
    if (field.kind !== 'select' && field.options.length) {
      errors[`config_fields.${index}.options`] = 'Only select fields can define options.';
    }
  });
  if (/\basync\s+def\s+transform\b/.test(spec.code)) {
    errors.code = 'transform(inputs, config) must be a regular (not async) function.';
  } else if (!/\bdef\s+transform\s*\(\s*inputs(?:\s*:[^,)]*)?\s*,\s*config(?:\s*:[^,)]*)?(?:\s*,[^)]*)?\s*\)/.test(spec.code)) {
    errors.code = 'Define transform(inputs, config) before saving.';
  }
  return { errors, valid: Object.keys(errors).length === 0 };
}

function validatePorts(ports: CustomNodePort[], role: 'inputs' | 'outputs', errors: Record<string, string>) {
  const seen = new Set<string>();
  ports.forEach((port, index) => {
    const key = `${role}.${index}.name`;
    if (!CUSTOM_IDENTIFIER_PATTERN.test(port.name.trim())) errors[key] = 'Use a valid identifier.';
    else if (seen.has(port.name)) errors[key] = `${role === 'inputs' ? 'Input' : 'Output'} names must be unique.`;
    else if (role === 'inputs' && port.name === 'workflow') errors[key] = '"workflow" is reserved for workflow inputs.';
    seen.add(port.name);
    if (port.item_kind && port.kind !== 'list') {
      errors[`${role}.${index}.item_kind`] = 'Item kind is only available for list ports.';
    }
  });
}

export function sanitizeCustomNodeName(value: string, fallback = 'custom-node'): string {
  const compact = value
    .trim()
    .replace(/[^A-Za-z0-9_-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-+|-+$/g, '');
  const prefixed = /^[A-Za-z]/.test(compact) ? compact : `node-${compact}`;
  return (prefixed || fallback).slice(0, 120);
}

export function tagsFromText(value: string): string[] {
  return [...new Set(value.split(',').map((tag) => tag.trim()).filter(Boolean))].slice(0, 50);
}
