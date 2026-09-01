import { useEffect, useMemo, useState } from 'react';
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Icon } from '../../shared/components/Icons';
import { EmptyState, ErrorNotice, PageHeader, Panel, SkeletonCards } from '../../shared/components/Ui';
import './tools.css';

type Catalog = components['schemas']['SdkCatalogResponse'];
type Tool = components['schemas']['FunctionToolResponse'];
type WriteRequest = components['schemas']['FunctionToolWriteRequest'];

const GROUPS: Array<{ label: string; prefixes: string[] }> = [
  { label: 'Search the web', prefixes: ['web.', 'arxiv.', 'wikipedia.', 'webpage.'] },
  { label: 'Read papers', prefixes: ['documents.', 'research.', 'retrieval.'] },
  { label: 'Notes and files', prefixes: ['workspace.'] },
  { label: 'Run code and recall context', prefixes: ['python.', 'conversation.', 'artifacts.', 'extended.'] },
  { label: 'Build agents and tools', prefixes: ['agents.', 'function_tools.', 'sdk.', 'tools.', 'builder.'] },
];

type CatalogTool = Catalog['function_tools'][number];

function groupTools(items: CatalogTool[]) {
  const remaining = new Set(items);
  const groups = GROUPS.map(({ label, prefixes }) => {
    const tools = items.filter((tool) => prefixes.some((prefix) => tool.catalog_id.startsWith(prefix)));
    tools.forEach((tool) => remaining.delete(tool));
    return { label, tools };
  });
  if (remaining.size) groups.push({ label: 'Other', tools: [...remaining] });
  return groups.filter((group) => group.tools.length);
}

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
  const [query, setQuery] = useState('');
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

  const needle = query.trim().toLowerCase();
  const matches = (haystack: Array<string | null | undefined>) =>
    !needle || haystack.some((value) => value?.toLowerCase().includes(needle));
  const builtInGroups = useMemo(
    () =>
      groupTools(
        (catalog?.function_tools ?? []).filter((tool) =>
          matches([tool.label, tool.description, tool.catalog_id]),
        ),
      ),
    [catalog, needle],
  );
  const customTools = tools.filter((tool) =>
    matches([tool.name, tool.description, tool.latest_revision.catalog_id]),
  );
  const builtInCount = builtInGroups.reduce((total, group) => total + group.tools.length, 0);
  if (loading)
    return (
      <div className="page">
        <PageHeader title="Tools" description="Everything the agent can do, plus the Python tools you write yourself." />
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
        title="Tools"
        description="Everything the agent can do, plus the Python tools you write yourself."
        actions={
          <button className="button" type="button" onClick={() => setShowCreate((value) => !value)}>
            <Icon name={showCreate ? 'close' : 'plus'} size={16} />
            {showCreate ? 'Cancel' : 'New tool'}
          </button>
        }
      />
      {error ? <ErrorNotice error={error} /> : null}

      {showCreate ? (
        <Panel title="New tool" description="A sandboxed Python callback, stored as a new revision.">
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

      <div className="tool-search">
        <Icon name="search" size={15} />
        <input
          type="search"
          placeholder="Search tools…"
          aria-label="Search tools"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      {!builtInCount && !customTools.length ? (
        <EmptyState icon="search" title="No tools match" description={`Nothing found for “${query}”.`} />
      ) : null}

      {customTools.length ? (
        <Panel title="Your tools" description="Sandboxed Python callbacks you wrote.">
          <ul className="tool-list">
            {customTools.map((tool) => (
              <li className="tool-row" key={tool.id}>
                <div className="tool-row-head">
                  <strong>{tool.name}</strong>
                  <code>{tool.latest_revision.catalog_id}</code>
                  <span className="tool-rev">rev {tool.latest_revision.revision}</span>
                </div>
                <p>{tool.description}</p>
              </li>
            ))}
          </ul>
        </Panel>
      ) : !needle ? (
        <EmptyState
          icon="tools"
          title="No tools of your own yet"
          description="Write a sandboxed Python callback to give your agents a capability they don't have."
          action={
            <button className="button" type="button" onClick={() => setShowCreate(true)}>
              New tool
            </button>
          }
        />
      ) : null}

      {builtInGroups.map((group) => (
        <Panel key={group.label} title={group.label} description={`${group.tools.length} tools`}>
          <ul className="tool-list">
            {group.tools.map((tool) => (
              <li className="tool-row" key={tool.catalog_id}>
                <div className="tool-row-head">
                  <strong>{tool.label}</strong>
                  <code>{tool.catalog_id}</code>
                </div>
                <p>{tool.description}</p>
              </li>
            ))}
          </ul>
        </Panel>
      ))}

      <details className="tool-primitives">
        <summary>SDK building blocks</summary>
        <ul className="tool-list">
          {catalog?.primitives.map((primitive) => (
            <li className="tool-row" key={primitive.kind}>
              <div className="tool-row-head">
                <strong>{primitive.sdk_constructor}</strong>
              </div>
              <p>{primitive.description}</p>
            </li>
          ))}
        </ul>
      </details>

    </div>
  );
}
