import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Connection, Edge } from '@xyflow/react';
import { agentsApi } from '../api';
import { providersApi, type Provider } from '../../providers/api';
import {
  blankAgent,
  blankBlueprint,
  normalizeBlueprint,
  presentationFrom,
  uniqueId,
  type AgentBlueprint,
  type AgentPresentation,
  type AgentResponse,
  type SdkCatalog,
} from '../types';
import {
  connectBlueprint,
  deleteBlueprintEdge,
  removeBlueprintSelection,
} from './blueprintMutations';

export function useAgentEditor(agentId: string | null) {
  const [blueprint, setBlueprint] = useState<AgentBlueprint>(blankBlueprint);
  const [presentation, setPresentation] = useState<AgentPresentation>({ positions: {} });
  const [record, setRecord] = useState<AgentResponse | null>(null);
  const [catalog, setCatalog] = useState<SdkCatalog | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>('agent:agent');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [issues, setIssues] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      agentsApi.catalog(),
      providersApi.list(),
      agentId ? agentsApi.get(agentId) : Promise.resolve(null),
    ])
      .then(([nextCatalog, nextProviders, nextRecord]) => {
        if (cancelled) return;
        setCatalog(nextCatalog);
        setProviders(nextProviders);
        if (nextRecord) {
          setRecord(nextRecord);
          setBlueprint(normalizeBlueprint(nextRecord.latest_revision.blueprint));
          setPresentation(presentationFrom(nextRecord.latest_revision.presentation));
          setSelectedId(`agent:${nextRecord.latest_revision.blueprint.entry_agent_id}`);
        } else {
          const stored = sessionStorage.getItem('scholarweave:new-blueprint');
          if (stored) {
            sessionStorage.removeItem('scholarweave:new-blueprint');
            try {
              const template = JSON.parse(stored) as AgentBlueprint;
              setBlueprint(template);
              setSelectedId(`agent:${template.entry_agent_id}`);
            } catch (parseError) {
              setError(
                parseError instanceof Error
                  ? new Error(`Could not load the selected template: ${parseError.message}`)
                  : new Error('Could not load the selected template.'),
              );
            }
          }
        }
      })
      .catch(setError)
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  const selected = useMemo(() => {
    if (!selectedId) return null;
    const [kind, id] = selectedId.split(':', 2);
    if (kind === 'agent') return { kind: 'agent' as const, value: blueprint.agents.find((item) => item.id === id) };
    if (kind.includes('guardrail')) return { kind: 'guardrail' as const, value: (blueprint.guardrails ?? []).find((item) => item.id === id) };
    if (kind === 'handoff') return { kind: 'handoff' as const, value: (blueprint.handoffs ?? []).find((item) => item.id === id) };
    if (kind === 'agent-tool') return { kind: 'agent_tool' as const, value: (blueprint.agent_tools ?? []).find((item) => item.id === id) };
    return { kind: 'tool' as const, value: (blueprint.tools ?? []).find((item) => item.id === id) };
  }, [blueprint, selectedId]);

  const addAgent = useCallback(() => {
    const id = uniqueId('agent', blueprint.agents.map((item) => item.id));
    setBlueprint((current) => ({
      ...current,
      agents: [...current.agents, blankAgent(id, `Agent ${current.agents.length + 1}`)],
    }));
    setSelectedId(`agent:${id}`);
  }, [blueprint.agents]);

  const addFunctionTool = useCallback(
    (catalogId: string) => {
      const id = uniqueId('tool', (blueprint.tools ?? []).map((item) => item.id));
      setBlueprint((current) => ({
        ...current,
        tools: [
          ...(current.tools ?? []),
          {
            id,
            kind: 'function',
            catalog_id: catalogId,
            config: {},
            needs_approval: false,
          },
        ],
      }));
      setSelectedId(`tool:${id}`);
    },
    [blueprint.tools],
  );

  const addGuardrail = useCallback(
    (kind: 'input' | 'output' | 'tool_input' | 'tool_output', catalogId: string) => {
      const id = uniqueId(
        `${kind}-guardrail`,
        (blueprint.guardrails ?? []).map((item) => item.id),
      );
      setBlueprint((current) => ({
        ...current,
        guardrails: [
          ...(current.guardrails ?? []),
          {
            id,
            kind,
            catalog_id: catalogId,
            config: { max_characters: 50000 },
          },
        ],
      }));
      setSelectedId(`guardrail:${id}`);
    },
    [blueprint.guardrails],
  );

  const connect = useCallback((connection: Connection) => {
    setBlueprint((current) => connectBlueprint(current, connection));
  }, []);

  const deleteEdge = useCallback((edge: Edge) => {
    setBlueprint((current) => deleteBlueprintEdge(current, edge));
  }, []);

  const removeSelected = useCallback(() => {
    if (!selectedId) return;
    setBlueprint((current) => removeBlueprintSelection(current, selectedId));
    setSelectedId(null);
  }, [selectedId]);

  const validate = useCallback(async () => {
    setError(null);
    const result = await agentsApi.validate(blueprint);
    setIssues(result.issues ?? []);
    return result.valid;
  }, [blueprint]);

  const save = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      const valid = await validate();
      if (!valid) return null;
      const saved = record
        ? await agentsApi.update(record.id, blueprint, presentation)
        : await agentsApi.create(blueprint, presentation);
      setRecord(saved);
      setBlueprint(normalizeBlueprint(saved.latest_revision.blueprint));
      return saved;
    } catch (nextError) {
      setError(nextError);
      return null;
    } finally {
      setSaving(false);
    }
  }, [blueprint, presentation, record, validate]);

  return {
    blueprint,
    setBlueprint,
    presentation,
    setPresentation,
    record,
    catalog,
    providers,
    selectedId,
    setSelectedId,
    selected,
    loading,
    saving,
    error,
    issues,
    addAgent,
    addFunctionTool,
    addGuardrail,
    connect,
    deleteEdge,
    removeSelected,
    validate,
    save,
  };
}
