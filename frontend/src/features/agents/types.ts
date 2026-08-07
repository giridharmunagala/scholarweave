import type { components } from '../../api/schema.generated';

export type AgentBlueprint = components['schemas']['AgentBlueprint-Input'];
export type AgentBlueprintOutput = components['schemas']['AgentBlueprint-Output'];
export type AgentSpec = components['schemas']['AgentSpec'];
export type ToolSpec =
  | components['schemas']['FunctionToolSpec']
  | components['schemas']['WebSearchToolSpec']
  | components['schemas']['FileSearchToolSpec'];
export type HandoffSpec = components['schemas']['HandoffSpec'];
export type AgentToolSpec = components['schemas']['AgentToolSpec'];
export type GuardrailSpec = components['schemas']['GuardrailSpec'];
export type AgentResponse = components['schemas']['AgentResponse'];
export type AgentValidation = components['schemas']['AgentValidationResponse'];
export type SdkCatalog = components['schemas']['SdkCatalogResponse'];
export type ModelReference = components['schemas']['ModelReferenceSpec'];

export interface AgentPresentation extends Record<string, unknown> {
  positions: Record<string, { x: number; y: number }>;
}

export function blankBlueprint(): AgentBlueprint {
  return {
    schema_version: 1,
    sdk_version: '0.19.4',
    name: 'Untitled agent',
    description: '',
    entry_agent_id: 'agent',
    agents: [blankAgent('agent', 'Agent')],
    tools: [],
    handoffs: [],
    agent_tools: [],
    guardrails: [],
    run: { max_turns: 10, max_tool_concurrency: null, tracing_enabled: false },
    session: {},
  };
}

export function blankAgent(id: string, name: string): AgentSpec {
  return {
    id,
    name,
    description: '',
    instructions: 'Help the user complete the task.',
    model: { provider_profile_id: null, model: null },
    model_settings: {},
    output: null,
    tool_ids: [],
    input_guardrail_ids: [],
    output_guardrail_ids: [],
    tool_use_behavior: 'run_llm_again',
    reset_tool_choice: true,
  };
}

export function normalizeBlueprint(value: AgentBlueprintOutput): AgentBlueprint {
  return {
    ...value,
    schema_version: 1,
    sdk_version: '0.19.4',
    agents: value.agents.map((agent) => ({
      ...blankAgent(agent.id, agent.name),
      ...agent,
    })),
    tools: value.tools ?? [],
    handoffs: value.handoffs ?? [],
    agent_tools: value.agent_tools ?? [],
    guardrails: value.guardrails ?? [],
    run: value.run ?? { max_turns: 10, max_tool_concurrency: null, tracing_enabled: false },
    session: value.session ?? {},
  };
}

export function presentationFrom(value: Record<string, unknown>): AgentPresentation {
  const positions =
    value.positions && typeof value.positions === 'object'
      ? (value.positions as AgentPresentation['positions'])
      : {};
  return { positions };
}

export function uniqueId(prefix: string, used: Iterable<string>): string {
  const existing = new Set(used);
  let index = 1;
  let candidate = prefix;
  while (existing.has(candidate)) {
    index += 1;
    candidate = `${prefix}-${index}`;
  }
  return candidate;
}
