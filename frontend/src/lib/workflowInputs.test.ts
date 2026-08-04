import { declaredWorkflowInputs } from './workflowInputs';
import type { WorkflowNode } from '../types/api';

function input(id: string, config: Record<string, unknown>): WorkflowNode {
  return { id, type: 'workflow_input', config, static_inputs: {} };
}

describe('declaredWorkflowInputs', () => {
  it('merges repeated keys so a shared value is declared once', () => {
    const inputs = declaredWorkflowInputs([
      input('a', { key: 'document_id', kind: 'text', label: 'Paper' }),
      input('b', { key: 'document_id', kind: 'text', description: 'Which paper to read.' }),
      input('c', { key: 'question', kind: 'text' }),
      { id: 'd', type: 'pdf_ingest', config: {}, static_inputs: {} },
    ]);

    expect(inputs.map((entry) => entry.key)).toEqual(['document_id', 'question']);
    expect(inputs[0].nodeIds).toEqual(['a', 'b']);
    expect(inputs[0].label).toBe('Paper');
    expect(inputs[0].description).toBe('Which paper to read.');
    expect(inputs[0].conflicting).toBe(false);
  });

  it('flags keys whose declarations disagree about the type', () => {
    const inputs = declaredWorkflowInputs([
      input('a', { key: 'topic', kind: 'text' }),
      input('b', { key: 'topic', kind: 'json' }),
    ]);

    expect(inputs).toHaveLength(1);
    expect(inputs[0].conflicting).toBe(true);
  });

  it('treats a key as required when any declaration needs it, unless a default exists', () => {
    const [required] = declaredWorkflowInputs([
      input('a', { key: 'topic', kind: 'text', required: false }),
      input('b', { key: 'topic', kind: 'text', required: true }),
    ]);
    expect(required.required).toBe(true);

    const [defaulted] = declaredWorkflowInputs([
      input('a', { key: 'topic', kind: 'text', required: true }),
      input('b', { key: 'topic', kind: 'text', default: 'reactors' }),
    ]);
    expect(defaulted.required).toBe(false);
    expect(defaulted.default).toBe('reactors');
  });

  it('falls back to the key when no label is given', () => {
    const [entry] = declaredWorkflowInputs([input('a', { key: 'seed', kind: 'number' })]);
    expect(entry.label).toBe('seed');
    expect(entry.kind).toBe('number');
  });
});
