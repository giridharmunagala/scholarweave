import type { Dispatch, SetStateAction } from 'react';
import type {
  AgentBlueprint,
  AgentSpec,
  AgentToolSpec,
  GuardrailSpec,
  HandoffSpec,
  ToolSpec,
} from '../types';
import { ModelSettingsEditor } from './ModelSettingsEditor';
import {
  AgentRelationships,
  AgentToolInspector,
  HandoffInspector,
} from './RelationshipInspectors';
import { StructuredOutputEditor } from './StructuredOutputEditor';
import { modelIsEnabled, type Provider } from '../../providers/api';
import { reasoningEffortsForModel } from '../../chat/ReasoningEffortSelect';

export function PrimitiveInspector({
  blueprint,
  setBlueprint,
  providers,
  selected,
  onRemove,
}: {
  blueprint: AgentBlueprint;
  setBlueprint: Dispatch<SetStateAction<AgentBlueprint>>;
  providers: Provider[];
  selected:
    | { kind: 'agent'; value: AgentSpec | undefined }
    | { kind: 'tool'; value: ToolSpec | undefined }
    | { kind: 'guardrail'; value: GuardrailSpec | undefined }
    | { kind: 'handoff'; value: HandoffSpec | undefined }
    | { kind: 'agent_tool'; value: AgentToolSpec | undefined }
    | null;
  onRemove: () => void;
}) {
  if (!selected?.value) return <RunSettings blueprint={blueprint} setBlueprint={setBlueprint} />;
  if (selected.kind === 'guardrail') {
    const guardrail = selected.value;
    const maxCharacters =
      typeof guardrail.config?.max_characters === 'number'
        ? guardrail.config.max_characters
        : 50000;
    return (
      <div className="inspector stack">
        <div className="inspector-heading">
          <div><span className="eyebrow">SDK guardrail</span><h2>{guardrail.id}</h2></div>
          <button className="button small danger" type="button" onClick={onRemove}>Remove</button>
        </div>
        <label className="field">Lifecycle<input value={guardrail.kind.split('_').join(' ')} disabled /></label>
        <label className="field">Catalog ID<input value={guardrail.catalog_id} disabled /></label>
        <label className="field">Maximum characters<input type="number" min={1} max={1000000} value={maxCharacters} onChange={(event) => setBlueprint((current) => ({ ...current, guardrails: (current.guardrails ?? []).map((item) => item.id === guardrail.id ? { ...item, config: { ...item.config, max_characters: Number(event.target.value) } } : item) }))} /></label>
        <p>Connect this card to an Agent or FunctionTool that supports its lifecycle kind.</p>
      </div>
    );
  }
  if (selected.kind === 'handoff') {
    return (
      <HandoffInspector
        blueprint={blueprint}
        setBlueprint={setBlueprint}
        relation={selected.value}
        onRemove={onRemove}
      />
    );
  }
  if (selected.kind === 'agent_tool') {
    return (
      <AgentToolInspector
        blueprint={blueprint}
        setBlueprint={setBlueprint}
        relation={selected.value}
        onRemove={onRemove}
      />
    );
  }
  if (selected.kind === 'agent') {
    const agent = selected.value;
    const selectedProvider = providers.find(
      (provider) => provider.id === agent.model?.provider_profile_id,
    );
    const availableModels = (selectedProvider?.models ?? []).filter(
      (model) =>
        modelIsEnabled(model)
        && (!model.capabilities?.length || model.capabilities.includes('chat')),
    );
    const supportedReasoningEfforts = reasoningEffortsForModel(
      providers,
      agent.model ?? {},
    );
    const update = (patch: Partial<AgentSpec>) =>
      setBlueprint((current) => ({
        ...current,
        agents: current.agents.map((item) => (item.id === agent.id ? { ...item, ...patch } : item)),
      }));
    const settings = agent.model_settings ?? {};
    const updateSettings = (patch: Partial<typeof settings>) =>
      update({ model_settings: { ...settings, ...patch } });
    return (
      <div className="inspector stack">
        <div className="inspector-heading">
          <div>
            <span className="eyebrow">Agent</span>
            <h2>{agent.name}</h2>
          </div>
          <button className="button small danger" type="button" onClick={onRemove}>Remove</button>
        </div>
        <label className="field">Name<input value={agent.name} onChange={(event) => update({ name: event.target.value })} /></label>
        <label className="field">Description<input value={agent.description ?? ''} onChange={(event) => update({ description: event.target.value })} /></label>
        <label className="field">Instructions<textarea value={agent.instructions} onChange={(event) => update({ instructions: event.target.value })} /></label>
        <div className="field-row">
          <label className="field">
            Provider
            <select
              value={agent.model?.provider_profile_id ?? ''}
              onChange={(event) =>
                update({
                  model: {
                    provider_profile_id: event.target.value || null,
                    model: null,
                  },
                  model_settings: { ...settings, reasoning: null },
                })
              }
            >
              <option value="">Default provider</option>
              {providers
                .filter((provider) => provider.models.some(modelIsEnabled))
                .map((provider) => (
                  <option value={provider.id} key={provider.id}>{provider.name}</option>
                ))}
            </select>
          </label>
          <label className="field">
            Model
            <select
              value={agent.model?.model ?? ''}
              disabled={!selectedProvider}
              onChange={(event) =>
                update({
                  model: { ...agent.model, model: event.target.value || null },
                  model_settings: { ...settings, reasoning: null },
                })
              }
            >
              <option value="">Default model</option>
              {agent.model?.model
                && !availableModels.some((model) => model.name === agent.model?.model) ? (
                  <option value={agent.model.model}>Unavailable: {agent.model.model}</option>
                ) : null}
              {availableModels.map((model) => (
                <option value={model.name} key={model.name}>{model.name}</option>
              ))}
            </select>
          </label>
        </div>
        <ModelSettingsEditor
          settings={settings}
          supportedReasoningEfforts={supportedReasoningEfforts}
          onChange={updateSettings}
        />
        <StructuredOutputEditor output={agent.output ?? null} onChange={(output) => update({ output })} />
        <label className="check-row">
          <input
            type="checkbox"
            checked={blueprint.entry_agent_id === agent.id}
            onChange={() => setBlueprint((current) => ({ ...current, entry_agent_id: agent.id }))}
          />
          Entry agent
        </label>
        <label className="field">
          Tool behavior
          <select value={agent.tool_use_behavior} onChange={(event) => update({ tool_use_behavior: event.target.value as AgentSpec['tool_use_behavior'] })}>
            <option value="run_llm_again">Run model after tools</option>
            <option value="stop_on_first_tool">Stop on first tool</option>
          </select>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={agent.reset_tool_choice}
            onChange={(event) => update({ reset_tool_choice: event.target.checked })}
          />
          Reset tool choice after each tool call
        </label>
        <AgentRelationships blueprint={blueprint} setBlueprint={setBlueprint} agent={agent} />
      </div>
    );
  }
  const tool = selected.value;
  const updateTool = (patch: Partial<ToolSpec>) =>
    setBlueprint((current) => ({
      ...current,
      tools: (current.tools ?? []).map((item) => (item.id === tool.id ? ({ ...item, ...patch } as ToolSpec) : item)),
    }));
  return (
    <div className="inspector stack">
      <div className="inspector-heading">
        <div><span className="eyebrow">SDK tool</span><h2>{tool.kind}</h2></div>
        <button className="button small danger" type="button" onClick={onRemove}>Remove</button>
      </div>
      {tool.kind === 'function' ? (
        <>
          <label className="field">Catalog ID<input value={tool.catalog_id} onChange={(event) => updateTool({ catalog_id: event.target.value })} /></label>
          <label className="field">Tool name override<input value={tool.name ?? ''} onChange={(event) => updateTool({ name: event.target.value || null })} /></label>
          <label className="field">Description override<textarea value={tool.description ?? ''} onChange={(event) => updateTool({ description: event.target.value || null })} /></label>
          <label className="check-row"><input type="checkbox" checked={tool.needs_approval} onChange={(event) => updateTool({ needs_approval: event.target.checked })} />Require approval</label>
        </>
      ) : tool.kind === 'web_search' ? (
        <label className="field">Search context<select value={tool.search_context_size} onChange={(event) => updateTool({ search_context_size: event.target.value as 'low' | 'medium' | 'high' })}><option>low</option><option>medium</option><option>high</option></select></label>
      ) : (
        <label className="field">Vector store IDs<textarea value={tool.vector_store_ids.join('\n')} onChange={(event) => updateTool({ vector_store_ids: event.target.value.split('\n').map((value) => value.trim()).filter(Boolean) })} /></label>
      )}
    </div>
  );
}

function RunSettings({
  blueprint,
  setBlueprint,
}: {
  blueprint: AgentBlueprint;
  setBlueprint: Dispatch<SetStateAction<AgentBlueprint>>;
}) {
  const run = blueprint.run!;
  return (
    <div className="inspector stack">
      <span className="eyebrow">Runner and session</span>
      <label className="field">Agent name<input value={blueprint.name} onChange={(event) => setBlueprint((current) => ({ ...current, name: event.target.value }))} /></label>
      <label className="field">Description<textarea value={blueprint.description ?? ''} onChange={(event) => setBlueprint((current) => ({ ...current, description: event.target.value }))} /></label>
      <div className="field-row">
        <label className="field">Max turns<input type="number" min={1} max={100} value={run.max_turns} onChange={(event) => setBlueprint((current) => ({ ...current, run: { ...run, max_turns: Number(event.target.value) } }))} /></label>
        <label className="field">Tool concurrency<input type="number" min={1} value={run.max_tool_concurrency ?? ''} onChange={(event) => setBlueprint((current) => ({ ...current, run: { ...run, max_tool_concurrency: event.target.value ? Number(event.target.value) : null } }))} /></label>
      </div>
      <label className="check-row"><input type="checkbox" checked={run.tracing_enabled} onChange={(event) => setBlueprint((current) => ({ ...current, run: { ...run, tracing_enabled: event.target.checked } }))} />Enable SDK tracing</label>
    </div>
  );
}
