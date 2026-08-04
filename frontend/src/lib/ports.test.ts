import { coercionFor, kindsCompatible } from './ports';

describe('port coercions', () => {
  it('passes identical and wildcard kinds through untouched', () => {
    expect(coercionFor('text', 'text')).toBeNull();
    expect(coercionFor('any', 'json')).toBeNull();
    expect(coercionFor('list', 'any')).toBeNull();
    expect(kindsCompatible('text', 'text')).toBe(true);
  });

  it('allows the conversions the engine performs', () => {
    expect(coercionFor('json', 'text')).toBe('serialise');
    expect(coercionFor('list', 'text')).toBe('serialise');
    expect(coercionFor('number', 'text')).toBe('serialise');
    expect(coercionFor('text', 'json')).toBe('parse_json');
    expect(coercionFor('text', 'number')).toBe('parse_number');
    expect(coercionFor('json', 'list')).toBe('narrow');
    expect(coercionFor('list', 'json')).toBe('widen');
  });

  it('still rejects pairs the engine cannot convert', () => {
    expect(kindsCompatible('number', 'list')).toBe(false);
    expect(kindsCompatible('list', 'number')).toBe(false);
    expect(coercionFor('number', 'json')).toBeNull();
  });
});
