import type { Edge } from '@xyflow/react';
import {
  BIDIRECTIONAL_EDGE_CLASS,
  canConnect,
  flowToWorkflowDefinition,
  suggestConnection,
  withDirectionMarkers,
  workflowToFlowEdges,
  workflowToFlowNodes,
  withRunInputValues,
  type FlowNode,
} from './workflowFlow';
import type { NodeDefinitionResponse, WorkflowDefinition } from '../types/api';

const textInput: NodeDefinitionResponse = {
  type: 'text_input',
  label: 'Text Input',
  description: '',
  category: 'inputs',
  tags: [],
  inputs: [],
  outputs: [{ name: 'text', kind: 'text', item_kind: null, description: '', required: true }],
  config_schema: {},
};

const promptNode: NodeDefinitionResponse = {
  type: 'prompt_template',
  label: 'Prompt',
  description: '',
  category: 'llm',
  tags: [],
  inputs: [{ name: 'prompt', kind: 'text', item_kind: null, description: '', required: true }],
  outputs: [{ name: 'text', kind: 'text', item_kind: null, description: '', required: true }],
  config_schema: {},
};

const retrieverNode: NodeDefinitionResponse = {
  type: 'vector_retrieve',
  label: 'Vector retrieve',
  description: '',
  category: 'retrieval',
  tags: [],
  inputs: [],
  outputs: [{ name: 'tool', kind: 'tool', item_kind: null, description: '', required: true }],
  config_schema: {},
};

const agentNode: NodeDefinitionResponse = {
  type: 'agent',
  label: 'Agent',
  description: '',
  category: 'agents',
  tags: [],
  inputs: [
    { name: 'input', kind: 'text', item_kind: null, description: '', required: false },
    { name: 'tools', kind: 'tool', item_kind: null, description: '', required: false, fan_in: true },
  ],
  outputs: [
    { name: 'text', kind: 'text', item_kind: null, description: '', required: true },
    { name: 'tool', kind: 'tool', item_kind: null, description: '', required: true },
  ],
  config_schema: {},
};

function agentFlow(): FlowNode[] {
  return [
    ...workflowToFlowNodes(
      {
        name: 'Agent graph',
        nodes: [
          { id: 'a', type: 'vector_retrieve', config: {}, static_inputs: {} },
          { id: 'b', type: 'keyword_retrieve', config: {}, static_inputs: {} },
          { id: 'agent', type: 'agent', config: {}, static_inputs: {} },
        ],
        edges: [],
      },
      new Map([
        ['vector_retrieve', retrieverNode],
        ['keyword_retrieve', { ...retrieverNode, type: 'keyword_retrieve' }],
        ['agent', agentNode],
      ]),
    ),
  ];
}

describe('fan-in ports', () => {
  it('accepts several tools into one agent', () => {
    const nodes = agentFlow();
    const existing: Edge[] = [{ id: 'e1', source: 'a', target: 'agent', sourceHandle: 'out:tool', targetHandle: 'in:tools' }];

    const allowed = canConnect(
      { source: 'b', target: 'agent', sourceHandle: 'out:tool', targetHandle: 'in:tools' },
      nodes,
      existing,
    );

    expect(allowed).toBe(true);
  });

  it('still refuses a second edge into a single-slot port', () => {
    const nodes = agentFlow();
    const existing: Edge[] = [{ id: 'e1', source: 'a', target: 'agent', sourceHandle: 'out:text', targetHandle: 'in:input' }];

    const allowed = canConnect(
      { source: 'b', target: 'agent', sourceHandle: 'out:text', targetHandle: 'in:input' },
      nodes,
      existing,
    );

    expect(allowed).toBe(false);
  });

  it('refuses to plug a tool into a text port', () => {
    const nodes = agentFlow();

    const allowed = canConnect(
      { source: 'a', target: 'agent', sourceHandle: 'out:tool', targetHandle: 'in:input' },
      nodes,
      [],
    );

    expect(allowed).toBe(false);
  });
});

describe('workflowFlow', () => {
  it('round-trips workflow nodes, edges, and UI positions', () => {
    const definition: WorkflowDefinition = {
      name: 'Example',
      description: 'Demo',
      nodes: [
        {
          id: 'a',
          type: 'text_input',
          name: 'Question',
          config: { __ui: { position: { x: 150, y: 80 } }, input_key: 'question' },
          static_inputs: {},
        },
        {
          id: 'b',
          type: 'prompt_template',
          config: {},
          static_inputs: { prompt: 'hello' },
        },
      ],
      edges: [{ source_node_id: 'a', source_port: 'text', target_node_id: 'b', target_port: 'prompt' }],
    };

    const catalog = new Map([
      ['text_input', textInput],
      ['prompt_template', promptNode],
    ]);

    const nodes = workflowToFlowNodes(definition, catalog);
    const edges = workflowToFlowEdges(definition);
    const next = flowToWorkflowDefinition(definition.name, definition.description || '', nodes, edges);

    expect(nodes).toHaveLength(3);
    expect(nodes[1].position).toEqual({ x: 150, y: 80 });
    expect(edges[0].sourceHandle).toBe('out:text');
    expect(next.edges[0]).toEqual(definition.edges[0]);
    expect(next.nodes[0].config.input_key).toBe('question');
    expect((next.nodes[0].config.__ui as { position: { x: number; y: number } }).position).toEqual({ x: 150, y: 80 });
  });

  it('preserves conditional execution and workflow model defaults without changing legacy graphs', () => {
    const definition: WorkflowDefinition = {
      name: 'Conditional model graph',
      nodes: [
        {
          id: 'prompt',
          type: 'prompt_template',
          config: {},
          static_inputs: {},
          run_when: {
            type: 'group',
            operator: 'and',
            conditions: [{ type: 'predicate', source: 'workflow', path: 'enabled', operator: 'truthy' }],
          },
        },
      ],
      edges: [],
      model_defaults: {
        chat: { provider_profile_id: 'provider-a', model: 'chat-model' },
        embedding: { provider_profile_id: 'provider-a', model: 'embed-model' },
      },
    };
    const catalog = new Map([['prompt_template', promptNode]]);
    const nodes = workflowToFlowNodes(definition, catalog);
    const next = flowToWorkflowDefinition(definition.name, '', nodes, [], definition.model_defaults);

    expect(nodes[1].data.runWhen).toEqual(definition.nodes[0].run_when);
    expect(next.nodes[0].run_when).toEqual(definition.nodes[0].run_when);
    expect(next.model_defaults).toEqual(definition.model_defaults);
  });

  it('shows all public inputs on one agent start node and preserves their edges', () => {
    const workflowInput = {
      ...textInput,
      type: 'workflow_input',
      label: 'Workflow Input',
      outputs: [
        { name: 'value', kind: 'any', item_kind: null, description: '', required: true },
        { name: 'text', kind: 'text', item_kind: null, description: '', required: true },
      ],
      interface_role: 'input' as const,
    };
    const definition: WorkflowDefinition = {
      name: 'Agent',
      nodes: [
        { id: 'question', type: 'workflow_input', config: { key: 'question', kind: 'text' }, static_inputs: {} },
        { id: 'context', type: 'workflow_input', config: { key: 'context', kind: 'json' }, static_inputs: {} },
        { id: 'prompt', type: 'prompt_template', config: {}, static_inputs: {} },
      ],
      edges: [
        { source_node_id: 'question', source_port: 'text', target_node_id: 'prompt', target_port: 'prompt' },
      ],
    };
    const catalog = new Map([
      ['workflow_input', workflowInput],
      ['prompt_template', promptNode],
    ]);

    const nodes = workflowToFlowNodes(definition, catalog);
    const edges = workflowToFlowEdges(definition);
    const next = flowToWorkflowDefinition(definition.name, '', nodes, edges);

    expect(nodes).toHaveLength(2);
    expect(nodes[0].data.definition.type).toBe('__agent_start__');
    expect(nodes[0].data.definition.outputs).toHaveLength(4);
    expect(edges[0].source).toBe('__agent_start_ui__');
    expect(next.nodes.map((node) => node.id)).toEqual(['question', 'context', 'prompt']);
    expect(next.edges).toEqual(definition.edges);
  });

  it('allows only compatible connections and one source per target handle', () => {
    const nodes: FlowNode[] = [
      { id: 'a', type: 'workflowNode', position: { x: 0, y: 0 }, data: { definition: textInput, nodeName: '', config: {}, staticInputs: {} } },
      { id: 'b', type: 'workflowNode', position: { x: 0, y: 0 }, data: { definition: promptNode, nodeName: '', config: {}, staticInputs: {} } },
    ];
    const existingEdges: Edge[] = [];

    expect(
      canConnect({ source: 'a', sourceHandle: 'out:text', target: 'b', targetHandle: 'in:prompt' }, nodes, existingEdges),
    ).toBe(true);

    existingEdges.push({ id: 'edge-1', source: 'a', sourceHandle: 'out:text', target: 'b', targetHandle: 'in:prompt' });
    expect(
      canConnect({ source: 'x', sourceHandle: 'out:text', target: 'b', targetHandle: 'in:prompt' }, nodes, existingEdges),
    ).toBe(false);
  });

  it('allows coercible connections and suggests the first compatible pair', () => {
    const jsonSource: NodeDefinitionResponse = {
      ...textInput,
      type: 'json_source',
      inputs: [],
      outputs: [{ name: 'value', kind: 'json', item_kind: null, description: '', required: true }],
    };
    const nodes: FlowNode[] = [
      { id: 'a', type: 'workflowNode', position: { x: 0, y: 0 }, data: { definition: jsonSource, nodeName: '', config: {}, staticInputs: {} } },
      { id: 'b', type: 'workflowNode', position: { x: 0, y: 0 }, data: { definition: promptNode, nodeName: '', config: {}, staticInputs: {} } },
    ];

    // json -> text used to be rejected even though the engine serialises it.
    expect(
      canConnect({ source: 'a', sourceHandle: 'out:value', target: 'b', targetHandle: 'in:prompt' }, nodes, []),
    ).toBe(true);

    expect(suggestConnection(nodes[0], nodes[1], [])).toEqual({ sourcePort: 'value', targetPort: 'prompt' });
    expect(suggestConnection(nodes[1], nodes[0], [])).toBeNull();
  });

  it('does not suggest a port that is already wired', () => {
    const nodes: FlowNode[] = [
      { id: 'a', type: 'workflowNode', position: { x: 0, y: 0 }, data: { definition: textInput, nodeName: '', config: {}, staticInputs: {} } },
      { id: 'b', type: 'workflowNode', position: { x: 0, y: 0 }, data: { definition: promptNode, nodeName: '', config: {}, staticInputs: {} } },
    ];
    const taken: Edge[] = [{ id: 'e', source: 'x', sourceHandle: 'out:text', target: 'b', targetHandle: 'in:prompt' }];

    expect(suggestConnection(nodes[0], nodes[1], [])).toEqual({ sourcePort: 'text', targetPort: 'prompt' });
    expect(suggestConnection(nodes[0], nodes[1], taken)).toBeNull();
  });

  it('flags reverse connections so both ends get an arrow', () => {
    const forward: Edge = { id: 'forward', source: 'a', target: 'b' };
    const reverse: Edge = { id: 'reverse', source: 'b', target: 'a' };

    const oneWay = withDirectionMarkers([forward]);
    expect(oneWay[0].className).toBeUndefined();
    // Arrowheads are attached in CSS, so no marker data should be baked in.
    expect(oneWay[0].markerEnd).toBeUndefined();
    expect(oneWay[0].markerStart).toBeUndefined();
    expect(oneWay[0].zIndex).toBeUndefined();

    const twoWay = withDirectionMarkers([forward, reverse]);
    expect(twoWay.every((edge) => edge.className === BIDIRECTIONAL_EDGE_CLASS)).toBe(true);

    // Re-running must not accumulate duplicate classes or strand a stale flag.
    expect(withDirectionMarkers(twoWay).every((edge) => edge.className === BIDIRECTIONAL_EDGE_CLASS)).toBe(true);
    expect(withDirectionMarkers([twoWay[0]])[0].className).toBeUndefined();
  });

  it('shows run-provided values on matching text input nodes', () => {
    const definition: WorkflowDefinition = {
      name: 'Paper ingestion',
      nodes: [
        { id: 'document', type: 'text_input', config: { input_key: 'document_id' }, static_inputs: {} },
        { id: 'ingest', type: 'pdf_ingest', config: {}, static_inputs: {} },
      ],
      edges: [],
    };

    const configured = withRunInputValues(definition, { document_id: 'paper-123' });

    expect(configured.nodes[0].config).toEqual({ input_key: 'document_id', value: 'paper-123' });
    expect(configured.nodes[1]).toBe(definition.nodes[1]);
  });

  it('prefills declared workflow inputs through their default', () => {
    const definition: WorkflowDefinition = {
      name: 'Paper Q&A',
      nodes: [
        { id: 'document', type: 'workflow_input', config: { key: 'document_id', kind: 'text' }, static_inputs: {} },
        { id: 'question', type: 'workflow_input', config: { key: 'question', kind: 'text' }, static_inputs: {} },
      ],
      edges: [],
    };

    const configured = withRunInputValues(definition, { document_id: 'paper-123' });

    expect(configured.nodes[0].config).toEqual({ key: 'document_id', kind: 'text', default: 'paper-123' });
    expect(configured.nodes[1]).toBe(definition.nodes[1]);
  });
});
