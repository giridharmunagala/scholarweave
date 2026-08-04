import { describe, expect, it } from 'vitest';
import {
  modelCapabilityForNode,
  resolveEffectiveModelReference,
  withModelReference,
} from './models';
import type { ProviderProfileResponse, SettingsResponse, WorkflowModelDefaults } from '../types/api';

const profile: ProviderProfileResponse = {
  id: 'provider-1',
  name: 'Shared Azure',
  kind: 'azure_openai',
  base_url: 'https://example.openai.azure.com',
  api_version: '2024-10-21',
  api_key_set: true,
  state: 'active',
  models: [
    { name: 'chat-model', capabilities: ['chat'] },
    { name: 'agent-model', capabilities: ['chat', 'tools'] },
    { name: 'vision-model', capabilities: ['vision'] },
  ],
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-01T00:00:00Z',
};

const settings = {
  default_model_references: {
    chat: { provider_profile_id: profile.id, model: 'chat-model' },
    tools: { provider_profile_id: profile.id, model: 'agent-model' },
  },
} as Pick<SettingsResponse, 'default_model_references'>;

describe('model inheritance', () => {
  it('uses node, workflow, then Settings references in order', () => {
    const workflowDefaults: WorkflowModelDefaults = {
      chat: { provider_profile_id: profile.id, model: 'chat-model' },
    };
    const inherited = resolveEffectiveModelReference('chat', null, workflowDefaults, settings, [profile]);
    const overridden = resolveEffectiveModelReference(
      'chat',
      { provider_profile_id: profile.id, model: 'agent-model' },
      workflowDefaults,
      settings,
      [profile],
    );

    expect(inherited.source).toBe('workflow');
    expect(overridden.source).toBe('node');
    expect(overridden.reference?.model).toBe('agent-model');
  });

  it('flags archived and capability-incompatible selections', () => {
    const archived = { ...profile, state: 'archived' as const };
    expect(
      resolveEffectiveModelReference(
        'chat',
        { provider_profile_id: archived.id, model: 'chat-model' },
        null,
        settings,
        [archived],
      ).issue,
    ).toBe('archived-profile');
    expect(
      resolveEffectiveModelReference(
        'vision',
        { provider_profile_id: profile.id, model: 'chat-model' },
        null,
        settings,
        [profile],
      ).issue,
    ).toBe('incompatible');
  });

  it('distinguishes simple agents from agents with tools and recognizes vision nodes', () => {
    expect(modelCapabilityForNode('agent')).toBe('chat');
    expect(modelCapabilityForNode('agent', {}, {}, { agentHasTools: true })).toBe('tools');
    expect(modelCapabilityForNode('pdf_ingest')).toBe('vision');
    expect(modelCapabilityForNode('keyword_retrieve', { properties: { model: { type: 'string' } } })).toBeNull();
  });

  it('replaces legacy document model fields with a provider reference', () => {
    expect(
      withModelReference(
        { llm_model: 'legacy-vision' },
        { provider_profile_id: profile.id, model: 'vision-model' },
      ),
    ).toEqual({
      model_reference: { provider_profile_id: profile.id, model: 'vision-model' },
      provider_profile_id: profile.id,
      model: 'vision-model',
    });
  });
});
