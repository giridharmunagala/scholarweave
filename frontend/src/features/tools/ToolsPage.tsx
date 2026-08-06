import { useEffect, useState } from 'react';
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Icon } from '../../shared/components/Icons';
import { EmptyState, ErrorNotice, PageHeader, Panel, SkeletonCards } from '../../shared/components/Ui';
import './tools.css';

type Catalog = components['schemas']['SdkCatalogResponse'];
type Tool = components['schemas']['FunctionToolResponse'];
type WriteRequest = components['schemas']['FunctionToolWriteRequest'];

const initial: WriteRequest = {
  name: 'echo_message',
  description: 'Return the supplied message.',
  parameters_schema: {
    type: 'object',
    properties: { message: { type: 'string' } },
    required: ['message'],
    additionalProperties: false,
  },
  output_schema: {
    type: 'object',
    properties: { message: { type: 'string' } },
    required: ['message'],
    additionalProperties: false,
  },
  code: 'def invoke(arguments):\n    return {"message": arguments["message"]}\n',
  requires_approval: false,
};

export default function ToolsPage() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [tools, setTools] = useState<Tool[]>([]);
  const [draft, setDraft] = useState<WriteRequest>(initial);
  const [parametersText, setParametersText] = useState(JSON.stringify(initial.parameters_schema, null, 2));
  const [outputText, setOutputText] = useState(JSON.stringify(initial.output_schema, null, 2));
  const [showCreate, setShowCreate] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  const load = () =>
    Promise.all([request<Catalog>('/sdk/catalog'), request<Tool[]>('/tools')]).then(([nextCatalog, nextTools]) => {
      setCatalog(nextCatalog);
      setTools(nextTools);
    });
  useEffect(() => {
    load().catch(setError).finally(() => setLoading(false));
  }, []);
  if (loading)
    return (
      <div className="page">
        <PageHeader eyebrow="SDK capabilities" title="Tools" description="Built-in capabilities and revisioned custom callbacks compile into real FunctionTool instances." />
        <SkeletonCards count={8} />
      </div>
    );

  const create = () => {
    setError(null);
    let parametersSchema: Record<string, unknown>;
    let outputSchema: Record<string, unknown> | null;
    try {
      parametersSchema = JSON.parse(parametersText) as Record<string, unknown>;
      outputSchema = outputText.trim() ? (JSON.parse(outputText) as Record<string, unknown>) : null;
    } catch (parseError) {
      setError(
        parseError instanceof Error
          ? new Error(`Invalid JSON Schema: ${parseError.message}`)
          : new Error('Invalid JSON Schema.'),
      );
      return Promise.resolve();
    }
    return request<Tool>(
      '/tools',
      json('POST', { ...draft, parameters_schema: parametersSchema, output_schema: outputSchema }),
    )
      .then(() => {
        setShowCreate(false);
        return load();
      })
      .catch(setError);
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="SDK capabilities"
        title="Tools"
        description="Built-in capabilities and revisioned custom callbacks compile into real FunctionTool instances."
        actions={
          <button className="button" type="button" onClick={() => setShowCreate((value) => !value)}>
            <Icon name={showCreate ? 'close' : 'plus'} size={16} />
            {showCreate ? 'Cancel' : 'New FunctionTool'}
          </button>
        }
      />
      {error ? <ErrorNotice error={error} /> : null}

      {showCreate ? (
        <Panel title="Custom FunctionTool" description="Sandboxed Python callback stored as a new revision.">
          <div className="stack">
            <div className="field-row">
              <label className="field">
                Name
                <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
              </label>
              <label className="field">
                Description
                <input value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
              </label>
            </div>
            <div className="field-row">
              <label className="field">
                Parameters JSON Schema
                <textarea className="mono schema-editor" value={parametersText} onChange={(event) => setParametersText(event.target.value)} />
              </label>
              <label className="field">
                Output JSON Schema
                <textarea className="mono schema-editor" value={outputText} onChange={(event) => setOutputText(event.target.value)} />
              </label>
            </div>
            <label className="field">
              Python callback
              <textarea className="mono schema-editor" value={draft.code} onChange={(event) => setDraft({ ...draft, code: event.target.value })} />
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={draft.requires_approval}
                onChange={(event) => setDraft({ ...draft, requires_approval: event.target.checked })}
              />
              Require approval before execution
            </label>
            <div className="button-row">
              <button className="button" type="button" onClick={() => void create()}>
                <Icon name="save" size={16} />
                Create revision
              </button>
              <button className="button ghost" type="button" onClick={() => setShowCreate(false)}>
                Cancel
              </button>
            </div>
          </div>
        </Panel>
      ) : null}

      <Panel title="Application FunctionTools" description="Shipped with ScholarWeave and always available.">
        <div className="card-grid">
          {catalog?.function_tools.map((tool) => (
            <article className="card tool-card" key={tool.catalog_id}>
              <span className="tool-badge kind-function_tool">
                <Icon name="tools" size={14} />
              </span>
              <h2 className="truncate">{tool.label}</h2>
              <p>{tool.description}</p>
              <code className="tag truncate">{tool.catalog_id}</code>
            </article>
          ))}
        </div>
      </Panel>

      <Panel title="Custom FunctionTools" description="Your revisioned Python callbacks.">
        {tools.length ? (
          <div className="card-grid">
            {tools.map((tool) => (
              <article className="card tool-card" key={tool.id}>
                <span className="tool-badge kind-custom">
                  <Icon name="sparkle" size={14} />
                </span>
                <div className="row-between">
                  <h2 className="truncate">{tool.name}</h2>
                  <span className="eyebrow" style={{ margin: 0 }}>
                    rev {tool.latest_revision.revision}
                  </span>
                </div>
                <p>{tool.description}</p>
                <code className="tag truncate">{tool.latest_revision.catalog_id}</code>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState
            icon="tools"
            title="No custom tools yet"
            description="Author a sandboxed Python callback to give your agents a new capability."
            action={
              <button className="button" type="button" onClick={() => setShowCreate(true)}>
                New FunctionTool
              </button>
            }
          />
        )}
      </Panel>

      <Panel title="SDK primitive catalog" description="The building blocks the canvas can place.">
        <div className="card-grid">
          {catalog?.primitives.map((primitive) => (
            <article className="card tool-card" key={primitive.kind}>
              <span className="eyebrow">{primitive.kind.split('_').join(' ')}</span>
              <h2 className="truncate">{primitive.sdk_constructor}</h2>
              <p>{primitive.description}</p>
            </article>
          ))}
        </div>
      </Panel>
    </div>
  );
}
