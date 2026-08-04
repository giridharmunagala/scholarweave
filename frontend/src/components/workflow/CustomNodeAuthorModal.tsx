import { useEffect, useMemo, useState } from 'react';
import { ErrorNotice } from '../common/ErrorNotice';
import { Icon } from '../common/Icon';
import { Modal } from '../common/Modal';
import { toMessage, useToast } from '../common/Toast';
import { api } from '../../lib/api';
import {
  CUSTOM_CONFIG_KINDS,
  CUSTOM_PORT_KINDS,
  blankCustomConfigField,
  blankCustomNodeSpec,
  blankCustomPort,
  sanitizeCustomNodeName,
  tagsFromText,
  validateCustomNodeSpec,
} from '../../lib/customNodes';
import type {
  CustomConfigKind,
  CustomNodeConfigField,
  CustomNodePort,
  CustomNodeResponse,
  CustomNodeSpec,
  CustomPortKind,
} from '../../types/api';

interface Props {
  definition?: CustomNodeResponse | null;
  duplicate?: boolean;
  onClose: () => void;
  onSaved: (definition: CustomNodeResponse) => void;
}

function draftFromDefinition(definition?: CustomNodeResponse | null, duplicate = false) {
  const revision = definition?.latest_revision;
  return {
    name: definition ? `${definition.name}${duplicate ? '-copy' : ''}` : '',
    spec: revision
      ? {
          label: `${revision.label}${duplicate ? ' copy' : ''}`,
          description: revision.description,
          category: revision.category,
          tags: revision.tags,
          inputs: revision.inputs,
          outputs: revision.outputs,
          config_fields: revision.config_fields,
          code: revision.code,
        }
      : blankCustomNodeSpec(),
  };
}

function JsonValueInput({
  value,
  onChange,
  ariaLabel,
}: {
  value: unknown;
  onChange: (value: unknown) => void;
  ariaLabel: string;
}) {
  const [text, setText] = useState(value === undefined ? '' : JSON.stringify(value));
  const [error, setError] = useState('');
  useEffect(() => setText(value === undefined ? '' : JSON.stringify(value)), [value]);
  return (
    <div className="field-stack">
      <input
        className="input mono"
        aria-label={ariaLabel}
        placeholder="JSON default"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onBlur={() => {
          if (!text.trim()) {
            setError('');
            onChange(undefined);
            return;
          }
          try {
            onChange(JSON.parse(text));
            setError('');
          } catch {
            setError('Use valid JSON.');
          }
        }}
      />
      {error ? <span className="field-error">{error}</span> : null}
    </div>
  );
}

function parseObject(text: string, label: string): Record<string, unknown> {
  const parsed = JSON.parse(text || '{}') as unknown;
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(`${label} must be a JSON object.`);
  return parsed as Record<string, unknown>;
}

function PortRows({
  label,
  ports,
  onChange,
  errors,
}: {
  label: string;
  ports: CustomNodePort[];
  onChange: (next: CustomNodePort[]) => void;
  errors: Record<string, string>;
}) {
  const key = label.toLowerCase();
  const patch = (index: number, value: Partial<CustomNodePort>) =>
    onChange(ports.map((port, portIndex) => (portIndex === index ? { ...port, ...value } : port)));
  return (
    <section className="custom-author-section">
      <div className="custom-author-section-head">
        <div>
          <h3>{label}</h3>
          <p>{label === 'Inputs' ? 'Values passed from connected ports.' : 'Values transform() must return.'}</p>
        </div>
        <button type="button" className="button subtle sm" onClick={() => onChange([...ports, blankCustomPort()])}>
          <Icon name="plus" size={12} />
          Add {label.slice(0, -1).toLowerCase()}
        </button>
      </div>
      <div className="custom-author-rows">
        {ports.map((port, index) => (
          <div className="custom-author-row port" key={`${key}-${index}`}>
            <label className="field-stack">
              <span className="sr-only">{label} name</span>
              <input className="input mono" value={port.name} placeholder="name" onChange={(event) => patch(index, { name: event.target.value })} />
              {errors[`${key}.${index}.name`] ? <span className="field-error">{errors[`${key}.${index}.name`]}</span> : null}
            </label>
            <label className="field-stack">
              <span className="sr-only">{label} type</span>
              <select className="input" value={port.kind} onChange={(event) => patch(index, { kind: event.target.value as CustomPortKind, item_kind: null })}>
                {CUSTOM_PORT_KINDS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
              </select>
            </label>
            {port.kind === 'list' ? (
              <label className="field-stack">
                <span className="sr-only">List item type</span>
                <select className="input" value={port.item_kind || ''} onChange={(event) => patch(index, { item_kind: (event.target.value || null) as CustomPortKind | null })}>
                  <option value="">Any item</option>
                  {CUSTOM_PORT_KINDS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
                </select>
              </label>
            ) : <span className="custom-row-spacer" />}
            <label className="field-stack custom-port-description">
              <span className="sr-only">{label} description</span>
              <input className="input" value={port.description} placeholder="Description" onChange={(event) => patch(index, { description: event.target.value })} />
            </label>
            <label className="checkbox-row custom-row-check">
              <input type="checkbox" checked={port.required} onChange={(event) => patch(index, { required: event.target.checked })} />
              <span>Required</span>
            </label>
            <button type="button" className="button subtle icon-only sm" onClick={() => onChange(ports.filter((_, portIndex) => portIndex !== index))} aria-label={`Remove ${label.slice(0, -1).toLowerCase()}`}>
              <Icon name="trash" size={13} />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

function ConfigRows({
  fields,
  onChange,
  errors,
}: {
  fields: CustomNodeConfigField[];
  onChange: (next: CustomNodeConfigField[]) => void;
  errors: Record<string, string>;
}) {
  const patch = (index: number, value: Partial<CustomNodeConfigField>) =>
    onChange(fields.map((field, fieldIndex) => (fieldIndex === index ? { ...field, ...value } : field)));
  return (
    <section className="custom-author-section">
      <div className="custom-author-section-head">
        <div>
          <h3>Configuration fields</h3>
          <p>Build the typed config passed to transform(inputs, config).</p>
        </div>
        <button type="button" className="button subtle sm" onClick={() => onChange([...fields, blankCustomConfigField()])}>
          <Icon name="plus" size={12} />
          Add field
        </button>
      </div>
      <div className="custom-author-rows">
        {fields.map((field, index) => (
          <div className="custom-author-row config" key={`field-${index}`}>
            <label className="field-stack">
              <span className="sr-only">Field name</span>
              <input className="input mono" value={field.name} placeholder="name" onChange={(event) => patch(index, { name: event.target.value })} />
              {errors[`config_fields.${index}.name`] ? <span className="field-error">{errors[`config_fields.${index}.name`]}</span> : null}
            </label>
            <label className="field-stack">
              <span className="sr-only">Field label</span>
              <input className="input" value={field.label} placeholder="Label" onChange={(event) => patch(index, { label: event.target.value })} />
            </label>
            <label className="field-stack">
              <span className="sr-only">Field description</span>
              <input className="input" value={field.description} placeholder="Description" onChange={(event) => patch(index, { description: event.target.value })} />
            </label>
            <label className="field-stack">
              <span className="sr-only">Field type</span>
              <select className="input" value={field.kind} onChange={(event) => patch(index, { kind: event.target.value as CustomConfigKind, options: event.target.value === 'select' ? field.options : [] })}>
                {CUSTOM_CONFIG_KINDS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
              </select>
            </label>
            <JsonValueInput value={field.default} ariaLabel="Field default JSON" onChange={(value) => patch(index, { default: value })} />
            {field.kind === 'select' ? (
              <label className="field-stack">
                <span className="sr-only">Select options</span>
                <input
                  className="input"
                  value={field.options.map(String).join(', ')}
                  placeholder="option one, option two"
                  onChange={(event) => patch(index, { options: event.target.value.split(',').map((option) => option.trim()).filter(Boolean) })}
                />
                {errors[`config_fields.${index}.options`] ? <span className="field-error">{errors[`config_fields.${index}.options`]}</span> : null}
              </label>
            ) : <span className="custom-row-spacer" />}
            <label className="checkbox-row custom-row-check">
              <input type="checkbox" checked={field.required} onChange={(event) => patch(index, { required: event.target.checked })} />
              <span>Required</span>
            </label>
            <button type="button" className="button subtle icon-only sm" onClick={() => onChange(fields.filter((_, fieldIndex) => fieldIndex !== index))} aria-label="Remove configuration field">
              <Icon name="trash" size={13} />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

export function CustomNodeAuthorModal({ definition, duplicate = false, onClose, onSaved }: Props) {
  const toast = useToast();
  const [draft, setDraft] = useState(() => draftFromDefinition(definition, duplicate));
  const [sampleInputs, setSampleInputs] = useState('{}');
  const [sampleConfig, setSampleConfig] = useState('{}');
  const [sampleResult, setSampleResult] = useState('');
  const [sampleStdout, setSampleStdout] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<'save' | 'sample' | ''>('');

  useEffect(() => {
    setDraft(draftFromDefinition(definition, duplicate));
    setSampleInputs('{}');
    setSampleConfig('{}');
    setSampleResult('');
    setSampleStdout('');
    setError('');
  }, [definition?.id, definition?.latest_revision.id, duplicate]);

  const validation = useMemo(() => validateCustomNodeSpec(draft.name, draft.spec), [draft]);
  const patchSpec = (patch: Partial<CustomNodeSpec>) => setDraft((current) => ({ ...current, spec: { ...current.spec, ...patch } }));

  const runSample = async () => {
    if (!validation.valid) {
      setError('Fix the inline definition errors before running a sample.');
      return;
    }
    setBusy('sample');
    setError('');
    try {
      const result = await api.sampleCustomNode({
        spec: draft.spec,
        inputs: parseObject(sampleInputs, 'Sample inputs'),
        config: parseObject(sampleConfig, 'Sample config'),
        workflow_inputs: {},
      });
      setSampleResult(JSON.stringify(result.output, null, 2));
      setSampleStdout(result.stdout);
      toast.success('Sample completed', 'The transform ran through the same sandbox contract used by workflows.');
    } catch (err) {
      const message = toMessage(err, 'Sample run failed');
      setError(message);
      setSampleResult('');
      setSampleStdout('');
    } finally {
      setBusy('');
    }
  };

  const save = async () => {
    if (!validation.valid) {
      setError('Fix the inline definition errors before saving.');
      return;
    }
    setBusy('save');
    setError('');
    try {
      const payload = { name: draft.name.trim(), ...draft.spec };
      const saved = definition && !duplicate
        ? await api.updateCustomNode(definition.id, payload)
        : await api.createCustomNode(payload);
      toast.success(definition && !duplicate ? 'New revision saved' : 'Custom node created', `${saved.name} is ready in the node palette.`);
      onSaved(saved);
    } catch (err) {
      setError(toMessage(err, 'Could not save custom node'));
    } finally {
      setBusy('');
    }
  };

  const title = definition && !duplicate ? `Edit ${definition.name}` : 'Create custom node';
  return (
    <Modal title={title} description="Author a typed, revision-pinned Python transform." onClose={onClose} className="custom-node-author-modal">
      <div className="custom-node-author-scroll">
        {error ? <ErrorNotice message={error} /> : null}
        <section className="custom-author-section">
          <div className="form-grid two-col">
            <div className="field-stack">
              <label className="field-label" htmlFor="custom-node-name">Definition name</label>
              <input id="custom-node-name" className="input mono" value={draft.name} placeholder="citation-cleaner" onChange={(event) => setDraft((current) => ({ ...current, name: sanitizeCustomNodeName(event.target.value, '') }))} />
              {validation.errors.name ? <p className="field-error">{validation.errors.name}</p> : <p className="field-hint">Stable identifier used by the custom-node library.</p>}
            </div>
            <div className="field-stack">
              <label className="field-label" htmlFor="custom-node-label">Label</label>
              <input id="custom-node-label" className="input" value={draft.spec.label} placeholder="Citation cleaner" onChange={(event) => patchSpec({ label: event.target.value })} />
              {validation.errors.label ? <p className="field-error">{validation.errors.label}</p> : null}
            </div>
            <div className="field-stack">
              <label className="field-label" htmlFor="custom-node-category">Category</label>
              <input id="custom-node-category" className="input" value={draft.spec.category} placeholder="custom" onChange={(event) => patchSpec({ category: event.target.value })} />
              {validation.errors.category ? <p className="field-error">{validation.errors.category}</p> : null}
            </div>
            <div className="field-stack">
              <label className="field-label" htmlFor="custom-node-tags">Tags</label>
              <input id="custom-node-tags" className="input" value={draft.spec.tags.join(', ')} placeholder="text, cleanup" onChange={(event) => patchSpec({ tags: tagsFromText(event.target.value) })} />
            </div>
            <div className="field-stack span-2">
              <label className="field-label" htmlFor="custom-node-description">Description</label>
              <textarea id="custom-node-description" className="input" rows={2} value={draft.spec.description} onChange={(event) => patchSpec({ description: event.target.value })} />
            </div>
          </div>
        </section>

        <PortRows label="Inputs" ports={draft.spec.inputs} onChange={(inputs) => patchSpec({ inputs })} errors={validation.errors} />
        <PortRows label="Outputs" ports={draft.spec.outputs} onChange={(outputs) => patchSpec({ outputs })} errors={validation.errors} />
        {validation.errors.outputs ? <p className="field-error">{validation.errors.outputs}</p> : null}
        <ConfigRows fields={draft.spec.config_fields} onChange={(config_fields) => patchSpec({ config_fields })} errors={validation.errors} />

        <section className="custom-author-section">
          <div className="custom-author-section-head">
            <div>
              <h3>Python transform</h3>
              <p>Define transform(inputs, config). It runs in the configured sandbox with no network access.</p>
            </div>
          </div>
          <textarea className="input mono code-editor" rows={14} spellCheck={false} value={draft.spec.code} onChange={(event) => patchSpec({ code: event.target.value })} />
          {validation.errors.code ? <p className="field-error">{validation.errors.code}</p> : null}
        </section>

        <section className="custom-author-section custom-sample-section">
          <div className="custom-author-section-head">
            <div>
              <h3>Sample test</h3>
              <p>Run this unsaved definition against representative inputs.</p>
            </div>
            <button type="button" className="button subtle" disabled={busy === 'sample'} onClick={() => void runSample()}>
              <Icon name="play" size={13} />
              {busy === 'sample' ? 'Running…' : 'Run sample'}
            </button>
          </div>
          <div className="form-grid two-col">
            <div className="field-stack">
              <label className="field-label">Sample inputs</label>
              <textarea className="json-editor" value={sampleInputs} onChange={(event) => setSampleInputs(event.target.value)} />
            </div>
            <div className="field-stack">
              <label className="field-label">Sample config</label>
              <textarea className="json-editor" value={sampleConfig} onChange={(event) => setSampleConfig(event.target.value)} />
            </div>
          </div>
          {sampleResult ? <div className="field-stack"><span className="field-label">Result</span><pre className="preview-block">{sampleResult}</pre></div> : null}
          {sampleStdout ? <div className="field-stack"><span className="field-label">stdout</span><pre className="preview-block">{sampleStdout}</pre></div> : null}
        </section>
      </div>
      <footer className="button-row end custom-author-footer">
        <button type="button" className="button subtle" onClick={onClose}>Cancel</button>
        <button type="button" className="button primary" disabled={busy === 'save'} onClick={() => void save()}>
          <Icon name="save" size={13} />
          {busy === 'save' ? 'Saving…' : definition && !duplicate ? 'Save new revision' : 'Create custom node'}
        </button>
      </footer>
    </Modal>
  );
}
