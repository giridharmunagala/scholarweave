import type {
  ModelCapability,
  ModelReference,
  ProviderModelEntry,
  ProviderProfileResponse,
  SettingsResponse,
  WorkflowModelDefaults,
} from '../types/api';

export const MODEL_CAPABILITIES: Array<{ id: ModelCapability; label: string; description: string }> = [
  { id: 'chat', label: 'Chat', description: 'Text generation and chat completions' },
  { id: 'tools', label: 'Tools', description: 'Chat models that can call tools' },
  { id: 'embedding', label: 'Embedding', description: 'Vector embeddings for search and indexing' },
  { id: 'vision', label: 'Vision', description: 'Image and document understanding' },
];

export type ModelReferenceSource = 'node' | 'workflow' | 'settings' | 'none';

export interface EffectiveModelReference {
  reference: ModelReference | null;
  source: ModelReferenceSource;
  profile: ProviderProfileResponse | null;
  modelEntry: ProviderModelEntry | null;
  issue: 'missing-profile' | 'archived-profile' | 'missing-model' | 'incompatible' | null;
}

export function normalizeModelReference(value: ModelReference | null | undefined): ModelReference | null {
  if (!value || (!value.provider_profile_id && !value.model)) return null;
  return {
    provider_profile_id: value.provider_profile_id || null,
    model: value.model || null,
  };
}

export function modelReferenceLabel(
  value: ModelReference | null | undefined,
  profiles: ProviderProfileResponse[] = [],
  fallback = 'Not configured',
): string {
  const reference = normalizeModelReference(value);
  if (!reference) return fallback;
  const profile = reference.provider_profile_id
    ? profiles.find((candidate) => candidate.id === reference.provider_profile_id)
    : null;
  if (profile && reference.model) return `${profile.name} / ${reference.model}`;
  if (profile) return profile.name;
  if (reference.provider_profile_id && reference.model) return `${reference.provider_profile_id.slice(0, 8)} / ${reference.model}`;
  if (reference.provider_profile_id) return reference.provider_profile_id.slice(0, 8);
  return reference.model || fallback;
}

export function modelEntryForReference(
  value: ModelReference | null | undefined,
  profiles: ProviderProfileResponse[],
): ProviderModelEntry | null {
  const reference = normalizeModelReference(value);
  if (!reference?.provider_profile_id || !reference.model) return null;
  return (
    profiles
      .find((profile) => profile.id === reference.provider_profile_id)
      ?.models.find((model) => model.name === reference.model) ?? null
  );
}

/**
 * Untagged discovered models are intentionally treated as unknown rather than incompatible.
 * Authors can use them while they add capability metadata to a profile.
 */
export function isModelCompatible(
  capability: ModelCapability,
  reference: ModelReference | null | undefined,
  profiles: ProviderProfileResponse[],
): boolean {
  const entry = modelEntryForReference(reference, profiles);
  return !entry || entry.capabilities.length === 0 || entry.capabilities.includes(capability);
}

export function resolveEffectiveModelReference(
  capability: ModelCapability,
  nodeReference: ModelReference | null | undefined,
  workflowDefaults: WorkflowModelDefaults | null | undefined,
  settings: Pick<SettingsResponse, 'default_model_references'> | null | undefined,
  profiles: ProviderProfileResponse[],
): EffectiveModelReference {
  const candidates: Array<[ModelReferenceSource, ModelReference | null | undefined]> = [
    ['node', nodeReference],
    ['workflow', workflowDefaults?.[capability]],
    ['settings', settings?.default_model_references?.[capability]],
  ];
  const [source, rawReference] = candidates.find(([, candidate]) => normalizeModelReference(candidate)) || ['none', null];
  const reference = normalizeModelReference(rawReference);
  if (!reference) {
    return { reference: null, source: 'none', profile: null, modelEntry: null, issue: null };
  }

  const profile = reference.provider_profile_id
    ? profiles.find((candidate) => candidate.id === reference.provider_profile_id) ?? null
    : null;
  const modelEntry = modelEntryForReference(reference, profiles);
  let issue: EffectiveModelReference['issue'] = null;
  if (reference.provider_profile_id && !profile) issue = 'missing-profile';
  else if (profile?.state === 'archived') issue = 'archived-profile';
  else if (profile && reference.model && profile.models.length > 0 && !modelEntry) issue = 'missing-model';
  else if (!isModelCompatible(capability, reference, profiles)) issue = 'incompatible';

  return {
    reference,
    source: source as ModelReferenceSource,
    profile,
    modelEntry,
    issue,
  };
}

export function modelSourceLabel(source: ModelReferenceSource): string {
  return {
    node: 'Node override',
    workflow: 'Workflow default',
    settings: 'Settings default',
    none: 'Not configured',
  }[source];
}

/**
 * New picker changes write the modern reference while mirroring old fields so legacy
 * saved workflows and backend readers keep their expected shape.
 */
export function withModelReference(config: Record<string, unknown>, reference: ModelReference | null): Record<string, unknown> {
  const next = { ...config };
  delete next.llm_model;
  const normalized = normalizeModelReference(reference);
  if (!normalized) {
    delete next.model_reference;
    delete next.model;
    delete next.provider_profile_id;
    return next;
  }
  next.model_reference = normalized;
  next.model = normalized.model;
  next.provider_profile_id = normalized.provider_profile_id;
  return next;
}

export function modelReferenceFromConfig(config: Record<string, unknown>): ModelReference | null {
  const nested = config.model_reference;
  if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
    const value = nested as Record<string, unknown>;
    return normalizeModelReference({
      provider_profile_id: typeof value.provider_profile_id === 'string' ? value.provider_profile_id : null,
      model: typeof value.model === 'string' ? value.model : null,
    });
  }
  return normalizeModelReference({
    provider_profile_id: typeof config.provider_profile_id === 'string' ? config.provider_profile_id : null,
    model: typeof config.model === 'string' ? config.model : null,
  });
}

const NODE_CAPABILITIES: Record<string, ModelCapability> = {
  ollama_generate: 'chat',
  ollama_embed: 'embedding',
  index_chunks: 'embedding',
  vector_retrieve: 'embedding',
  pdf_ingest: 'vision',
  enhance_document_page: 'vision',
};

export function modelCapabilityForNode(
  type: string,
  _schema?: Record<string, unknown>,
  _config?: Record<string, unknown>,
  options: { agentHasTools?: boolean } = {},
): ModelCapability | null {
  if (type === 'agent') return options.agentHasTools ? 'tools' : 'chat';
  return NODE_CAPABILITIES[type] ?? null;
}

export function withoutModelFields(schema: Record<string, unknown>): Record<string, unknown> {
  const properties = schema.properties;
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return schema;
  const omitted = new Set(['model', 'llm_model', 'provider_profile_id', 'model_reference']);
  const nextProperties = Object.fromEntries(
    Object.entries(properties as Record<string, unknown>).filter(([key]) => !omitted.has(key)),
  );
  const required = Array.isArray(schema.required)
    ? schema.required.filter((item): item is string => typeof item === 'string' && !omitted.has(item))
    : undefined;
  return {
    ...schema,
    properties: nextProperties,
    ...(required ? { required } : {}),
  };
}
