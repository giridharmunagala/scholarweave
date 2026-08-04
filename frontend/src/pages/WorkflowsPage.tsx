import {
  addEdge,
  applyEdgeChanges,
  Background,
  ConnectionLineType,
  Controls,
  MiniMap,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
} from '@xyflow/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from '../lib/router';
import { useConfirm } from '../components/common/ConfirmDialog';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorNotice } from '../components/common/ErrorNotice';
import { Icon } from '../components/common/Icon';
import { StatusBadge } from '../components/common/StatusBadge';
import { toMessage, useToast } from '../components/common/Toast';
import { NodePalette } from '../components/workflow/NodePalette';
import EdgeMarkers from '../components/workflow/EdgeMarkers';
import { isQuickAddShortcut, QuickAddPalette } from '../components/workflow/QuickAddPalette';
import { RunInputsForm } from '../components/workflow/RunInputsForm';
import { WorkflowPicker, type WorkflowChoice } from '../components/workflow/WorkflowPicker';
import {
  WorkflowInputsPanel,
  type NewWorkflowInput,
  type WorkflowInputPatch,
} from '../components/workflow/WorkflowInputsPanel';
import { WorkflowNodeCard } from '../components/workflow/WorkflowNodeCard';
import { NodeEditorModal } from '../components/workflow/NodeEditorModal';
import { ProviderModelPicker } from '../components/workflow/ProviderModelPicker';
import { CustomNodeAuthorModal } from '../components/workflow/CustomNodeAuthorModal';
import { CustomNodeLibraryModal } from '../components/workflow/CustomNodeLibraryModal';
import { api } from '../lib/api';
import { formatDateTime } from '../lib/format';
import { parseJsonObject } from '../lib/json';
import { createConditionGroup } from '../lib/conditions';
import { categoryMeta } from '../lib/nodeCatalog';
import {
  modelCapabilityForNode,
  modelReferenceFromConfig,
  modelReferenceLabel,
  modelSourceLabel,
  resolveEffectiveModelReference,
} from '../lib/models';
import {
  AGENT_START_NODE_ID,
  AGENT_START_TYPE,
  WORKFLOW_INPUT_TYPE,
  agentInputNodes,
  declaredWorkflowInputs,
} from '../lib/workflowInputs';
import { workflowEditorPath } from '../lib/workflowRoutes';
import {
  canConnect,
  coercionForEdge,
  flowToWorkflowDefinition,
  inputHandleId,
  outputHandleId,
  parseHandleId,
  suggestConnection,
  withDirectionMarkers,
  withAgentInputNodes,
  withRunInputValues,
  workflowToFlowEdges,
  workflowToFlowNodes,
  type FlowNode,
} from '../lib/workflowFlow';
import { COERCION_LABELS } from '../lib/ports';
import type {
  DocumentResponse,
  CustomNodeResponse,
  NodeDefinitionResponse,
  ProviderProfileResponse,
  SettingsResponse,
  WorkflowDefinition,
  WorkflowModelDefaults,
  WorkflowNode,
  WorkflowResponse,
  WorkflowSignature,
  WorkflowValidationResult,
  WorkflowVersionResponse,
} from '../types/api';

const nodeTypes = {
  workflowNode: WorkflowNodeCard,
};

function blankWorkflow(): WorkflowDefinition {
  return { name: 'Untitled agent', description: '', nodes: [], edges: [] };
}

/** The server palette exposes current revisions only; this map also resolves revisions pinned by saved workflows. */
function revisionCatalogEntries(definitions: CustomNodeResponse[]): NodeDefinitionResponse[] {
  return definitions.flatMap((definition) =>
    definition.revisions.map((revision) => ({
      type: revision.node_type,
      label: revision.label,
      description: revision.description,
      category: revision.category,
      tags: [...revision.tags, `custom-revision:${revision.revision}`],
      inputs: revision.inputs.map((port) => ({ ...port, item_kind: port.item_kind ?? null })),
      outputs: revision.outputs.map((port) => ({ ...port, item_kind: port.item_kind ?? null })),
      config_schema: revision.config_schema,
    })),
  );
}

function catalogMapWithPinnedRevisions(nodes: NodeDefinitionResponse[], definitions: CustomNodeResponse[]) {
  return new Map([...nodes, ...revisionCatalogEntries(definitions)].map((entry) => [entry.type, entry]));
}

const EDGE_ROUTING_KEY = 'workbench.edgeRouting';

const EDGE_ROUTINGS = [
  { id: 'smoothstep', label: 'Stepped', type: 'smoothstep', pathOptions: { borderRadius: 14 }, connectionLine: ConnectionLineType.SmoothStep },
  { id: 'bezier', label: 'Curved', type: 'default', pathOptions: { curvature: 0.32 }, connectionLine: ConnectionLineType.Bezier },
  { id: 'straight', label: 'Straight', type: 'straight', pathOptions: undefined, connectionLine: ConnectionLineType.Straight },
] as const;

type EdgeRouting = (typeof EDGE_ROUTINGS)[number]['id'];

function readEdgeRouting(): EdgeRouting {
  try {
    const stored = window.localStorage.getItem(EDGE_ROUTING_KEY);
    if (EDGE_ROUTINGS.some((option) => option.id === stored)) return stored as EdgeRouting;
  } catch {
    // Private browsing or a blocked storage partition — fall back to the default.
  }
  return 'smoothstep';
}

function useDraftWorkflow(initialNodes: FlowNode[], initialEdges: Edge[]) {
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(initialNodes);
  const [edges, setEdges] = useEdgesState(initialEdges);
  const onEdgesChange = useCallback(
    (changes: EdgeChange<Edge>[]) => setEdges((current) => withDirectionMarkers(applyEdgeChanges(changes, current))),
    [setEdges],
  );
  return { nodes, setNodes, onNodesChange, edges, setEdges, onEdgesChange };
}

function WorkflowEditorInner() {
  const navigate = useNavigate();
  const toast = useToast();
  const confirm = useConfirm();
  const { workflowId } = useParams();
  const [searchParams] = useSearchParams();
  const documentId = searchParams.get('documentId') || '';
  const templateName = searchParams.get('template') || '';
  const reactFlow = useReactFlow<FlowNode, Edge>();
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  const [catalog, setCatalog] = useState<NodeDefinitionResponse[]>([]);
  const [customDefinitions, setCustomDefinitions] = useState<CustomNodeResponse[]>([]);
  const catalogMap = useMemo(() => catalogMapWithPinnedRevisions(catalog, customDefinitions), [catalog, customDefinitions]);
  const [templates, setTemplates] = useState<WorkflowDefinition[]>([]);
  const [versions, setVersions] = useState<WorkflowVersionResponse[]>([]);
  const [paletteSearch, setPaletteSearch] = useState('');
  const [quickAddOpen, setQuickAddOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<'agent' | 'run'>('agent');
  const [workflowMeta, setWorkflowMeta] = useState({ name: 'Untitled agent', description: '', isTemplate: false });
  const [selectedNodeId, setSelectedNodeId] = useState('');
  const [nodeEditorId, setNodeEditorId] = useState('');
  const [validation, setValidation] = useState<WorkflowValidationResult | null>(null);
  const [inputsText, setInputsText] = useState(documentId ? JSON.stringify({ document_id: documentId }, null, 2) : '{}');
  const [pageError, setPageError] = useState('');
  const [busy, setBusy] = useState('');
  const [selectedWorkflow, setSelectedWorkflow] = useState<WorkflowResponse | null>(null);
  const [sourceDocument, setSourceDocument] = useState<DocumentResponse | null>(null);
  const [signature, setSignature] = useState<WorkflowSignature | null>(null);
  const [inputsMode, setInputsMode] = useState<'form' | 'json'>('form');
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [providerProfiles, setProviderProfiles] = useState<ProviderProfileResponse[]>([]);
  const [workflowModelDefaults, setWorkflowModelDefaults] = useState<WorkflowModelDefaults | null>(null);
  const [customLibraryOpen, setCustomLibraryOpen] = useState(false);
  const [customAuthor, setCustomAuthor] = useState<{ definition: CustomNodeResponse | null; duplicate: boolean } | null>(null);

  useEffect(() => {
    // Only used to describe the Python node's sandbox, so a failure here is not fatal.
    api.getSettings().then(setSettings).catch(() => undefined);
  }, []);

  const { nodes, setNodes, onNodesChange, edges, setEdges, onEdgesChange } = useDraftWorkflow([], []);
  const buildableCatalog = useMemo(
    () => catalog.filter((entry) => entry.type !== WORKFLOW_INPUT_TYPE),
    [catalog],
  );

  const [edgeRouting, setEdgeRouting] = useState<EdgeRouting>(readEdgeRouting);

  const changeEdgeRouting = useCallback((routing: EdgeRouting) => {
    setEdgeRouting(routing);
    try {
      window.localStorage.setItem(EDGE_ROUTING_KEY, routing);
    } catch {
      // Preference is cosmetic; ignore storage failures.
    }
  }, []);

  // Routing is a view preference, so it is layered on at render time and never
  // written back into the saved workflow definition.
  const routing = useMemo(
    () => EDGE_ROUTINGS.find((entry) => entry.id === edgeRouting) ?? EDGE_ROUTINGS[0],
    [edgeRouting],
  );

  const displayEdges = useMemo(
    () =>
      edges.map((edge) => {
        const coercion = coercionForEdge(edge, nodes);
        return {
          ...edge,
          type: routing.type,
          pathOptions: routing.pathOptions,
          className: coercion ? 'edge-coerced' : undefined,
          label: coercion ? COERCION_LABELS[coercion] : undefined,
        };
      }),
    [edges, nodes, routing],
  );
  const displayNodes = useMemo(
    () =>
      nodes.map((node) => {
        if (node.data.definition.type === AGENT_START_TYPE) return node;
        const agentHasTools =
          node.data.definition.type === 'agent' &&
          edges.some(
            (edge) =>
              edge.target === node.id &&
              ['tools', 'handoffs'].includes(parseHandleId(edge.targetHandle) || ''),
          );
        const capability = modelCapabilityForNode(
          node.data.definition.type,
          node.data.definition.config_schema,
          node.data.config,
          { agentHasTools },
        );
        if (!capability) return node;
        const effective = resolveEffectiveModelReference(
          capability,
          modelReferenceFromConfig(node.data.config),
          workflowModelDefaults,
          settings,
          providerProfiles,
        );
        return {
          ...node,
          data: {
            ...node.data,
            effectiveModel: effective.reference
              ? {
                  label: modelReferenceLabel(effective.reference, providerProfiles),
                  source: modelSourceLabel(effective.source),
                  issue: effective.issue,
                }
              : undefined,
          },
        };
      }),
    [edges, nodes, providerProfiles, settings, workflowModelDefaults],
  );

  const loadDefinition = useCallback(
    (definition: WorkflowDefinition, options?: { isTemplate?: boolean; workflow?: WorkflowResponse | null; versions?: WorkflowVersionResponse[] }) => {
      const preparedDefinition = withRunInputValues(
        definition,
        documentId ? { document_id: documentId } : {},
      );
      setWorkflowMeta({
        name: preparedDefinition.name,
        description: preparedDefinition.description || '',
        isTemplate: options?.isTemplate ?? options?.workflow?.is_template ?? false,
      });
      setNodes(workflowToFlowNodes(preparedDefinition, catalogMap));
      setEdges(workflowToFlowEdges(preparedDefinition));
      setSelectedNodeId('');
      setNodeEditorId('');
      setWorkflowModelDefaults(preparedDefinition.model_defaults || null);
      setValidation(null);
      setSelectedWorkflow(options?.workflow || null);
      setVersions(options?.versions || []);
      setPageError('');
      toast.info(`Loaded “${preparedDefinition.name}”`, `${preparedDefinition.nodes.length} steps on the canvas.`);
      requestAnimationFrame(() => reactFlow.fitView({ padding: 0.2 }));
    },
    [catalogMap, documentId, reactFlow, setEdges, setNodes, toast],
  );

  const refreshLibrary = useCallback(async () => {
    const [nodeCatalog, starter, definitions, profiles] = await Promise.all([
      api.listNodes(),
      api.listWorkflowTemplates(),
      api.listCustomNodes(true),
      api.listProviderProfiles(true),
    ]);
    setCatalog(nodeCatalog);
    setTemplates(starter);
    setCustomDefinitions(definitions);
    setProviderProfiles(profiles);
    return { nodeCatalog, starter, definitions, profiles };
  }, []);

  useEffect(() => {
    let active = true;
    setBusy('bootstrap');
    Promise.all([
      refreshLibrary(),
      documentId ? api.getDocument(documentId) : Promise.resolve(null),
    ])
      .then(async ([{ nodeCatalog, starter, definitions }, document]) => {
        if (!active) return;
        const nextCatalogMap = catalogMapWithPinnedRevisions(nodeCatalog, definitions);
        const runInputs = documentId ? { document_id: documentId } : {};
        const applyLoaded = (definition: WorkflowDefinition, workflow?: WorkflowResponse | null, loadedVersions?: WorkflowVersionResponse[]) => {
          const preparedDefinition = withRunInputValues(definition, runInputs);
          setWorkflowMeta({ name: preparedDefinition.name, description: preparedDefinition.description || '', isTemplate: workflow?.is_template ?? false });
          setNodes(workflowToFlowNodes(preparedDefinition, nextCatalogMap));
          setEdges(workflowToFlowEdges(preparedDefinition));
          setWorkflowModelDefaults(preparedDefinition.model_defaults || null);
          setSelectedWorkflow(workflow || null);
          setVersions(loadedVersions || []);
        };
        setSourceDocument(document);
        setInputsText(JSON.stringify(runInputs, null, 2));
        if (workflowId) {
          const workflow = await api.getWorkflow(workflowId);
          const nextVersions = await api.listWorkflowVersions(workflowId);
          applyLoaded(workflow.latest_version?.definition || blankWorkflow(), workflow, nextVersions);
          if (documentId) setInspectorTab('run');
        } else if (templateName) {
          applyLoaded(starter.find((workflow) => workflow.name === templateName) || blankWorkflow());
          if (documentId) setInspectorTab('run');
        } else {
          applyLoaded(blankWorkflow());
          // Arriving from a paper without a chosen workflow: offer the catalogue
          // instead of silently loading one particular template.
          if (documentId) setPickerOpen(true);
        }
      })
      .catch((err) => active && setPageError(err instanceof Error ? err.message : 'Failed to load agent builder'))
      .finally(() => active && setBusy(''));
    return () => {
      active = false;
    };
  }, [documentId, refreshLibrary, setEdges, setNodes, templateName, workflowId]);

  const definition = useMemo(
    () => flowToWorkflowDefinition(workflowMeta.name, workflowMeta.description, nodes, edges, workflowModelDefaults),
    [workflowMeta.name, workflowMeta.description, nodes, edges, workflowModelDefaults],
  );

  // Serialised so dragging a node around does not look like an interface change.
  const interfaceNodesJson = useMemo(
    () =>
      JSON.stringify(
        definition.nodes.filter((node) => catalogMap.get(node.type)?.interface_role),
      ),
    [catalogMap, definition.nodes],
  );

  useEffect(() => {
    const interfaceNodes = JSON.parse(interfaceNodesJson) as WorkflowNode[];
    if (interfaceNodes.length === 0) {
      setSignature(null);
      return;
    }
    let active = true;
    // Only the interface nodes are sent: the rest of the graph cannot change the signature.
    const timer = window.setTimeout(() => {
      api
        .validateWorkflow({ name: 'signature', description: '', nodes: interfaceNodes, edges: [] })
        .then((result) => active && setSignature(result.signature))
        .catch(() => active && setSignature(null));
    }, 200);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [interfaceNodesJson]);

  const parsedInputs = useMemo(() => {
    try {
      return parseJsonObject(inputsText || '{}');
    } catch {
      return null;
    }
  }, [inputsText]);

  const editorNode = useMemo(() => nodes.find((node) => node.id === nodeEditorId) || null, [nodes, nodeEditorId]);

  const updateWorkflowModelDefault = useCallback(
    (capability: keyof WorkflowModelDefaults, reference: WorkflowModelDefaults[keyof WorkflowModelDefaults]) => {
      setWorkflowModelDefaults((current) => {
        const next = { ...(current || {}) };
        if (reference?.provider_profile_id || reference?.model) next[capability] = reference;
        else delete next[capability];
        return Object.keys(next).length ? next : null;
      });
    },
    [],
  );

  /** Names the nodes feeding a fan-in port, so an agent inspector can list its tools. */
  const wiredPortLabels = useCallback(
    (nodeId: string, port: string) =>
      edges
        .filter((edge) => edge.target === nodeId && parseHandleId(edge.targetHandle ?? null) === port)
        .map((edge) => nodes.find((node) => node.id === edge.source)?.data.nodeName || edge.source),
    [edges, nodes],
  );

  /** Source port names feeding a node, matching how the engine labels its inputs. */
  const incomingPortNames = useCallback(
    (nodeId: string) =>
      edges
        .filter((edge) => edge.target === nodeId)
        .map((edge) => parseHandleId(edge.sourceHandle ?? null))
        .filter((name): name is string => Boolean(name)),
    [edges],
  );

  const updateNode = (nodeId: string, updater: (node: FlowNode) => FlowNode) => {
    setNodes((prev) => prev.map((node) => (node.id === nodeId ? updater(node) : node)));
  };

  const createNode = useCallback(
    (type: string, position?: { x: number; y: number }, config?: Record<string, unknown>) => {
      const definitionEntry = catalogMap.get(type);
      if (!definitionEntry) return;
      const nodeIdBase = type.replace(/[^a-z0-9]+/gi, '-').toLowerCase();
      let counter = nodes.length + 1;
      let nodeId = `${nodeIdBase}-${counter}`;
      while (nodes.some((node) => node.id === nodeId)) {
        counter += 1;
        nodeId = `${nodeIdBase}-${counter}`;
      }
      const newNode: FlowNode = {
        id: nodeId,
        type: 'workflowNode',
        position: position || { x: 80 + (nodes.length % 3) * 220, y: 80 + Math.floor(nodes.length / 3) * 160 },
        selected: true,
        data: {
          definition: definitionEntry,
          nodeName: definitionEntry.label,
          config: config || (type === 'if_else' ? { condition: createConditionGroup() } : {}),
          staticInputs: {},
          runWhen: null,
        },
      };
      // Deselect everything else so keyboard deletion and the modal target stay unambiguous.
      setNodes((prev) => [...prev.map((node) => (node.selected ? { ...node, selected: false } : node)), newNode]);

      // Wire the new node to whatever was selected, so adding a step to a chain does not
      // mean hunting for the right pair of handles.
      const previous = nodes.find((node) => node.id === selectedNodeId) || null;
      if (previous) {
        const forward = suggestConnection(previous, newNode, edges);
        const backward = forward ? null : suggestConnection(newNode, previous, edges);
        const link = forward
          ? { source: previous.id, sourcePort: forward.sourcePort, target: nodeId, targetPort: forward.targetPort }
          : backward
            ? { source: nodeId, sourcePort: backward.sourcePort, target: previous.id, targetPort: backward.targetPort }
            : null;
        if (link) {
          setEdges((current) =>
            withDirectionMarkers(
              addEdge(
                {
                  source: link.source,
                  sourceHandle: outputHandleId(link.sourcePort),
                  target: link.target,
                  targetHandle: inputHandleId(link.targetPort),
                  animated: false,
                },
                current,
              ),
            ),
          );
        }
      }

      setSelectedNodeId(nodeId);
      setNodeEditorId(nodeId);
      return nodeId;
    },
    [catalogMap, edges, nodes, selectedNodeId, setEdges, setNodes],
  );

  const agentStartNode = useMemo(
    () => nodes.find((node) => node.data.definition.type === AGENT_START_TYPE) || null,
    [nodes],
  );

  // The agent's public inputs are stored together on the visual start node while
  // serialising back to the existing backend-compatible interface nodes.
  const declaredInputs = useMemo(
    () => declaredWorkflowInputs(agentInputNodes(agentStartNode?.data.config)),
    [agentStartNode],
  );

  const patchWorkflowInput = useCallback(
    (key: string, patch: WorkflowInputPatch) => {
      setNodes((prev) =>
        prev.map((node) => {
          if (node.data.definition.type !== AGENT_START_TYPE) return node;
          const nextInputs = agentInputNodes(node.data.config).map((input) => {
            if ((input.config.key as string | undefined) !== key) return input;
            const config = { ...input.config, ...patch };
            if ('default' in patch && patch.default === undefined) delete config.default;
            return { ...input, config };
          });
          return withAgentInputNodes(node, nextInputs, catalogMap.get(WORKFLOW_INPUT_TYPE));
        }),
      );
    },
    [catalogMap, setNodes],
  );

  const removeWorkflowInput = useCallback(
    (key: string) => {
      const currentInputs = agentInputNodes(agentStartNode?.data.config);
      const doomed = new Set(currentInputs.filter((node) => node.config.key === key).map((node) => node.id));
      if (doomed.size === 0) return;
      setNodes((prev) =>
        prev.map((node) =>
          node.data.definition.type === AGENT_START_TYPE
            ? withAgentInputNodes(
                node,
                agentInputNodes(node.data.config).filter((input) => !doomed.has(input.id)),
                catalogMap.get(WORKFLOW_INPUT_TYPE),
              )
            : node,
        ),
      );
      setEdges((prev) =>
        prev.filter((edge) => {
          if (edge.source !== AGENT_START_NODE_ID) return true;
          const sourcePort = parseHandleId(edge.sourceHandle);
          return !sourcePort || ![...doomed].some((id) => sourcePort.startsWith(`${encodeURIComponent(id)}:`));
        }),
      );
    },
    [agentStartNode, catalogMap, setEdges, setNodes],
  );

  const addWorkflowInput = useCallback(
    (input: NewWorkflowInput) => {
      setNodes((prev) =>
        prev.map((node) => {
          if (node.data.definition.type !== AGENT_START_TYPE) return node;
          const existingIds = new Set([
            ...prev.map((entry) => entry.id),
            ...agentInputNodes(node.data.config).map((entry) => entry.id),
          ]);
          let counter = agentInputNodes(node.data.config).length + 1;
          let nodeId = `input-${counter}`;
          while (existingIds.has(nodeId)) {
            counter += 1;
            nodeId = `input-${counter}`;
          }
          return withAgentInputNodes(
            node,
            [
              ...agentInputNodes(node.data.config),
              {
                id: nodeId,
                type: WORKFLOW_INPUT_TYPE,
                config: {
                  key: input.key,
                  kind: input.kind,
                  label: input.label,
                  required: true,
                },
                static_inputs: {},
              },
            ],
            catalogMap.get(WORKFLOW_INPUT_TYPE),
          );
        }),
      );
      setSelectedNodeId(AGENT_START_NODE_ID);
      setInspectorTab('agent');
    },
    [catalogMap, setNodes],
  );

  const focusNode = useCallback(
    (nodeId: string) => {
      setSelectedNodeId(nodeId);
      setNodes((prev) => prev.map((node) => (node.selected === (node.id === nodeId) ? node : { ...node, selected: node.id === nodeId })));
      const target = nodes.find((node) => node.id === nodeId);
      if (target) reactFlow.setCenter(target.position.x + 110, target.position.y + 60, { zoom: 1, duration: 300 });
    },
    [nodes, reactFlow, setNodes],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!canConnect(connection, nodes, edges)) {
        setPageError('That connection is not type-compatible or the target port is already connected.');
        return;
      }
      setEdges((current) => withDirectionMarkers(addEdge({ ...connection, animated: false }, current)));
      setPageError('');
    },
    [edges, nodes, setEdges],
  );

  const exportPython = async () => {
    setBusy('export');
    try {
      const result = await api.exportWorkflowPython(definition);
      const blob = new Blob([result.source], { type: 'text/x-python' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = result.filename;
      link.click();
      URL.revokeObjectURL(url);
      toast.success('Exported as Python', `Saved ${result.filename}. Tool bodies that read this app's library are left as stubs.`);
    } catch (err) {
      const message = toMessage(err, 'Export failed');
      setPageError(message);
      toast.failure('Export failed', message);
    } finally {
      setBusy('');
    }
  };

  const validate = async () => {
    setBusy('validate');
    try {
      const result = await api.validateWorkflow(definition);
      setValidation(result);
      setPageError(result.valid ? '' : result.errors.join('\n'));
      if (result.valid) {
        toast.success('Agent is valid', result.order.length ? `Order: ${result.order.join(' → ')}` : undefined);
      } else {
        setInspectorTab('agent');
        toast.failure(`${result.errors.length} validation ${result.errors.length === 1 ? 'error' : 'errors'}`, result.errors[0]);
      }
      return result;
    } catch (err) {
      const message = toMessage(err, 'Validation failed');
      setPageError(message);
      toast.failure('Validation failed', message);
      setValidation(null);
      return null;
    } finally {
      setBusy('');
    }
  };

  const saveWorkflow = async () => {
    const result = await validate();
    if (!result?.valid) return;
    setBusy('save');
    try {
      const saved = await api.createWorkflow({
        name: workflowMeta.name,
        description: workflowMeta.description || undefined,
        definition,
        is_template: workflowMeta.isTemplate,
      });
      const nextVersions = await api.listWorkflowVersions(saved.id);
      setSelectedWorkflow(saved);
      setVersions(nextVersions);
      navigate(`/agents/${saved.id}`);
      setPageError('');
      toast.success(`Saved ${saved.name}`, `Stored as version ${saved.latest_version?.version ?? nextVersions[0]?.version ?? 1}.`);
    } catch (err) {
      const message = toMessage(err, 'Failed to save agent');
      setPageError(message);
      toast.failure('Could not save agent', message);
    } finally {
      setBusy('');
    }
  };

  const runWorkflow = async (mode: 'draft' | 'saved') => {
    let inputs: Record<string, unknown>;
    try {
      inputs = parseJsonObject(inputsText || '{}');
    } catch (err) {
      const message = toMessage(err, 'Inputs must be a JSON object');
      setPageError(message);
      setInspectorTab('run');
      toast.failure('Run inputs are not valid JSON', message);
      return;
    }

    if (mode === 'draft') {
      const result = await validate();
      if (!result?.valid) return;
    }

    setBusy('run');
    try {
      const run = await api.createRun(
        mode === 'saved' && versions[0]
          ? { workflow_version_id: versions[0].id, inputs }
          : { workflow: definition, inputs },
      );
      setPageError('');
      toast.success('Run started', 'Streaming live output on the Runs page.');
      navigate(`/runs/${run.id}`);
    } catch (err) {
      const message = toMessage(err, 'Failed to start run');
      setPageError(message);
      toast.failure('Could not start the run', message);
    } finally {
      setBusy('');
    }
  };

  const deleteSelectedWorkflow = async () => {
    if (!selectedWorkflow) return;
    const confirmed = await confirm({
      title: `Delete “${selectedWorkflow.name}”?`,
      description: 'Every saved version of this agent will be removed. Existing run history is kept.',
      confirmLabel: 'Delete agent',
    });
    if (!confirmed) return;
    setBusy('delete');
    setPageError('');
    try {
      await api.deleteWorkflow(selectedWorkflow.id);
      toast.success(`Deleted ${selectedWorkflow.name}`);
      navigate('/agents');
    } catch (err) {
      const message = toMessage(err, 'Failed to delete agent');
      setPageError(message);
      toast.failure('Delete failed', message);
    } finally {
      setBusy('');
    }
  };

  const tidyLayout = useCallback(() => {
    setNodes((current) => {
      if (current.length === 0) return current;
      const depth = new Map<string, number>();
      current.forEach((node) => depth.set(node.id, 0));
      // Longest-path layering: repeat until stable, capped by node count.
      for (let pass = 0; pass < current.length; pass += 1) {
        let changed = false;
        edges.forEach((edge) => {
          const from = depth.get(edge.source);
          const to = depth.get(edge.target);
          if (from === undefined || to === undefined) return;
          if (to < from + 1) {
            depth.set(edge.target, from + 1);
            changed = true;
          }
        });
        if (!changed) break;
      }
      const columns = new Map<number, string[]>();
      current.forEach((node) => {
        const column = depth.get(node.id) ?? 0;
        const bucket = columns.get(column) || [];
        bucket.push(node.id);
        columns.set(column, bucket);
      });
      const positions = new Map<string, { x: number; y: number }>();
      columns.forEach((ids, column) => {
        ids.forEach((id, row) => {
          positions.set(id, { x: column * 290, y: row * 165 });
        });
      });
      return current.map((node) => ({ ...node, position: positions.get(node.id) || node.position }));
    });
    requestAnimationFrame(() => reactFlow.fitView({ padding: 0.2, duration: 300 }));
  }, [edges, reactFlow, setNodes]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isQuickAddShortcut(event)) {
        event.preventDefault();
        setQuickAddOpen((open) => !open);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  /** Drops a node in the middle of the current viewport, so ⌘K additions are always visible. */
  const addNodeAtViewportCentre = useCallback(
    (type: string) => {
      const bounds = wrapperRef.current?.getBoundingClientRect();
      const position = bounds
        ? reactFlow.screenToFlowPosition({ x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 })
        : undefined;
      createNode(type, position);
    },
    [createNode, reactFlow],
  );

  const stepCount = definition.nodes.length;

  return (
    <div className="workflow-page">
      <header className="panel workflow-toolbar">
        <div className="workflow-toolbar-heading">
          <Link className="button subtle sm" to="/agents" title="Back to all agents">
            <Icon name="arrowLeft" size={13} />
            Agents
          </Link>
          <div className="min-width-0">
            <h2 className="truncate">{workflowMeta.name}</h2>
            <p className="workflow-meta-line">
              <span>{stepCount} steps</span>
              <span className="dot">·</span>
              <span>{edges.length} connections</span>
              <span className="dot">·</span>
              {selectedWorkflow ? <span>v{versions[0]?.version ?? 1} saved</span> : <span>Unsaved draft</span>}
              {validation ? (
                <span className={validation.valid ? 'success-text' : 'danger-text'}>
                  {validation.valid ? '· Valid' : `· ${validation.errors.length} errors`}
                </span>
              ) : null}
            </p>
          </div>
        </div>
        <div className="button-row wrap">
          <button
            type="button"
            className="button subtle"
            title="Quick-add a node (Ctrl/⌘ K)"
            onClick={() => setQuickAddOpen(true)}
          >
            <Icon name="command" size={13} />
            Add node
            <kbd className="kbd">K</kbd>
          </button>
          <button
            type="button"
            className="button subtle"
            title="Open a different saved agent or starter template"
            onClick={() => setPickerOpen(true)}
          >
            <Icon name="layers" size={14} />
            Switch
          </button>
          <button type="button" className="button subtle" title="Start a new agent" onClick={() => loadDefinition(blankWorkflow())}>
            <Icon name="plus" size={14} />
            New
          </button>
          <button
            type="button"
            className="button subtle icon-only"
            title="Tidy the layout — arrange nodes left to right by execution order"
            aria-label="Tidy layout"
            disabled={nodes.length === 0}
            onClick={tidyLayout}
          >
            <Icon name="grid" size={14} />
          </button>
          {selectedWorkflow ? (
            <button
              type="button"
              className="button subtle icon-only danger-text"
              title="Delete this agent and all its versions"
              aria-label="Delete agent"
              disabled={busy === 'delete'}
              onClick={() => void deleteSelectedWorkflow()}
            >
              <Icon name="trash" size={14} />
            </button>
          ) : null}
          <button
            type="button"
            className="button subtle icon-only"
            title="Download this agent as a runnable OpenAI Agents SDK script"
            aria-label="Export as Python"
            disabled={busy === 'export' || stepCount === 0}
            onClick={() => void exportPython()}
          >
            <Icon name="braces" size={14} />
          </button>
          <button type="button" className="button" title="Check the graph for type and wiring errors" disabled={busy === 'validate'} onClick={() => void validate()}>
            <Icon name="check" size={14} />
            {busy === 'validate' ? 'Validating…' : 'Validate'}
          </button>
          <button type="button" className="button" disabled={busy === 'save'} onClick={() => void saveWorkflow()}>
            <Icon name="save" size={14} />
            {busy === 'save' ? 'Saving…' : selectedWorkflow ? 'Save version' : 'Save'}
          </button>
          <button
            type="button"
            className="button primary"
            title="Validate and execute this agent"
            disabled={busy === 'run' || stepCount === 0}
            onClick={() => void runWorkflow(versions[0] ? 'saved' : 'draft')}
          >
            <Icon name="play" size={13} />
            {busy === 'run' ? 'Starting…' : 'Run'}
          </button>
        </div>
      </header>

      {pageError ? <ErrorNotice message={pageError} /> : null}
      {sourceDocument ? (
        <div className="workflow-source-banner">
          <div>
            <strong>Using paper: {sourceDocument.title}</strong>
            <span>Its document ID is already filled into Agent inputs and the run form.</span>
          </div>
          <code>{sourceDocument.id}</code>
        </div>
      ) : null}

      <div className="workflow-grid">
        <aside className="panel workflow-side-panel">
          <div className="palette-head">
            <label className="search-field">
              <Icon name="search" size={13} />
              <input value={paletteSearch} placeholder="Search nodes" onChange={(event) => setPaletteSearch(event.target.value)} />
              {paletteSearch ? (
                <button type="button" aria-label="Clear node search" onClick={() => setPaletteSearch('')}>
                  <Icon name="close" size={12} />
                </button>
              ) : null}
            </label>
            <button type="button" className="button subtle sm block quick-add-trigger" onClick={() => setQuickAddOpen(true)}>
              <Icon name="command" size={12} />
              Quick add
              <kbd className="kbd">Ctrl K</kbd>
            </button>
          </div>
          <NodePalette
            nodes={buildableCatalog}
            search={paletteSearch}
            onAdd={(type) => createNode(type)}
            onCreateCustom={() => setCustomAuthor({ definition: null, duplicate: false })}
            onManageCustom={() => setCustomLibraryOpen(true)}
          />
        </aside>

        <section className="panel workflow-canvas-panel">
          <EdgeMarkers />
          <div
            ref={wrapperRef}
            className="workflow-canvas"
            onDragOver={(event) => {
              event.preventDefault();
              event.dataTransfer.dropEffect = 'copy';
            }}
            onDrop={(event) => {
              event.preventDefault();
              const type = event.dataTransfer.getData('application/x-scholarweave-node');
              if (!type || !wrapperRef.current) return;
              const position = reactFlow.screenToFlowPosition({ x: event.clientX, y: event.clientY });
              createNode(type, position);
            }}
          >
            <div className="canvas-status">
              <Icon name={stepCount ? 'workflow' : 'sparkle'} size={13} />
              <span>
                {stepCount
                 ? 'Customer inputs start on the left · Drag between ports to connect the agent'
                  : 'Press Ctrl/⌘ K, or drag a node from the palette to begin'}
              </span>
            </div>
            {nodes.length === 0 ? (
              <div className="canvas-blank">
                <Icon name="workflow" size={26} />
                <strong>Add the first agent step</strong>
                <p>Define customer inputs on the right, then add an agent or tool from the palette.</p>
                <button type="button" className="button primary" onClick={() => setQuickAddOpen(true)}>
                  <Icon name="command" size={13} />
                  Add your first node
                </button>
              </div>
            ) : null}
            <ReactFlow
              nodes={displayNodes}
              edges={displayEdges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              isValidConnection={(connection) => canConnect(connection, nodes, edges)}
              onNodeClick={(_, node) => {
                setSelectedNodeId(node.id);
                if (node.data.definition.type === AGENT_START_TYPE) {
                  setInspectorTab('agent');
                } else {
                  setNodeEditorId(node.id);
                }
              }}
              onPaneClick={() => setSelectedNodeId('')}
              onSelectionChange={({ nodes: selectedNodes }) => {
                // Keeps the inspector in step with marquee and keyboard selection.
                setSelectedNodeId(selectedNodes.length === 1 ? selectedNodes[0].id : '');
              }}
              onNodesDelete={(deleted) => {
                if (deleted.some((node) => node.id === selectedNodeId)) {
                  setSelectedNodeId('');
                }
                if (deleted.some((node) => node.id === nodeEditorId)) {
                  setNodeEditorId('');
                }
              }}
              deleteKeyCode={['Backspace', 'Delete']}
              fitView
              fitViewOptions={{ padding: 0.25 }}
              defaultEdgeOptions={{ type: routing.type }}
              connectionRadius={28}
              connectionLineType={routing.connectionLine}
              elevateNodesOnSelect
              minZoom={0.35}
              maxZoom={1.6}
              snapToGrid
              snapGrid={[16, 16]}
            >
              <Panel position="top-right" className="canvas-panel">
                <span className="canvas-panel-label">Links</span>
                <div className="segmented-control inline" role="group" aria-label="Connection style">
                  {EDGE_ROUTINGS.map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      className={edgeRouting === option.id ? 'active' : ''}
                      aria-pressed={edgeRouting === option.id}
                      onClick={() => changeEdgeRouting(option.id)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </Panel>
              <MiniMap
                pannable
                zoomable
                nodeClassName={(node) => `cat-${categoryMeta((node as FlowNode).data.definition.category).id}`}
                maskColor="var(--minimap-mask)"
                style={{ width: 132, height: 88, backgroundColor: 'var(--node-bg)' }}
              />
              <Controls />
              <Background gap={18} size={1} />
            </ReactFlow>
          </div>
        </section>

        <aside className="panel workflow-side-panel right">
          <div className="segmented-control inspector-tabs" role="tablist" aria-label="Editor inspector">
            <button type="button" className={inspectorTab === 'agent' ? 'active' : ''} onClick={() => setInspectorTab('agent')}>Agent</button>
            <button type="button" className={inspectorTab === 'run' ? 'active' : ''} onClick={() => setInspectorTab('run')}>Run</button>
          </div>

          {inspectorTab === 'agent' ? <section className="stack gap-md">
            <div className="panel-subheader">
              <h3>Agent details</h3>
              {validation ? <StatusBadge status={validation.valid ? 'completed' : 'failed'} /> : null}
            </div>
            <div className="field-stack">
              <label className="field-label">Agent name</label>
              <input className="input" value={workflowMeta.name} onChange={(event) => setWorkflowMeta((prev) => ({ ...prev, name: event.target.value }))} />
            </div>
            <div className="field-stack">
              <label className="field-label">Description</label>
              <textarea className="input" rows={4} value={workflowMeta.description} onChange={(event) => setWorkflowMeta((prev) => ({ ...prev, description: event.target.value }))} />
            </div>
            <label className="checkbox-row">
              <input type="checkbox" checked={workflowMeta.isTemplate} onChange={(event) => setWorkflowMeta((prev) => ({ ...prev, isTemplate: event.target.checked }))} />
              <span>Save as template</span>
            </label>
            <section className="workflow-model-defaults">
              <div>
                <strong>Workflow model defaults</strong>
                <p className="muted-text small">These override Settings for this agent. Individual nodes can override again.</p>
              </div>
              {(['chat', 'tools', 'embedding', 'vision'] as const).map((capability) => (
                <ProviderModelPicker
                  key={capability}
                  id={`workflow-default-${capability}`}
                  label={`${capability[0].toUpperCase()}${capability.slice(1)} default`}
                  capability={capability}
                  value={workflowModelDefaults?.[capability]}
                  onChange={(reference) => updateWorkflowModelDefault(capability, reference)}
                  profiles={providerProfiles}
                  settings={settings}
                  allowInherited
                  selectionSource="workflow"
                />
              ))}
            </section>
            <WorkflowInputsPanel
              inputs={declaredInputs}
              onPatch={patchWorkflowInput}
              onRemove={removeWorkflowInput}
              onAdd={addWorkflowInput}
              onFocusNode={() => focusNode(AGENT_START_NODE_ID)}
            />
            {validation ? (
              <div className={`validation-box ${validation.valid ? 'success' : 'danger'}`}>
                {validation.valid ? (
                  <>
                    <strong>Topological order</strong>
                    <p>{validation.order.join(' → ') || 'No nodes yet'}</p>
                  </>
                ) : (
                  <>
                    <strong>Validation errors</strong>
                    <ul>
                      {validation.errors.map((error) => (
                        <li key={error}>{error}</li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            ) : null}
            {selectedWorkflow ? (
              <div className="info-card">
                <strong>Saved agent</strong>
                <p className="muted-text">{selectedWorkflow.name}</p>
                <p className="muted-text small">Updated {formatDateTime(selectedWorkflow.updated_at)}</p>
              </div>
            ) : null}
            {versions.length > 0 ? (
              <div className="stack gap-sm">
                <strong>Versions</strong>
                {versions.map((version) => (
                  <button key={version.id} type="button" className="list-item" onClick={() => loadDefinition(version.definition, { workflow: selectedWorkflow, versions })}>
                    <div>
                      <strong>Version {version.version}</strong>
                      <p className="muted-text">{formatDateTime(version.created_at)}</p>
                    </div>
                  </button>
                ))}
              </div>
            ) : null}
            {templates.length > 0 ? (
              <div className="stack gap-sm">
                <strong>Replace with a template</strong>
                <p className="muted-text small">This discards the current canvas.</p>
                {templates.map((template) => (
                  <button
                    key={template.name}
                    type="button"
                    className="list-item"
                    onClick={() => loadDefinition(template, { isTemplate: true })}
                  >
                    <div className="list-item-main">
                      <strong>{template.name}</strong>
                      <p className="muted-text">{template.description || 'No description'}</p>
                    </div>
                    <span className="tiny-tag">{template.nodes.length}</span>
                  </button>
                ))}
              </div>
            ) : null}
            <div className="stack gap-sm">
              <strong>Open a different agent</strong>
              <p className="muted-text small">Browse your saved agents and starter templates.</p>
              <button type="button" className="button subtle block" onClick={() => setPickerOpen(true)}>
                <Icon name="layers" size={13} />
                Switch agent
              </button>
            </div>
          </section> : null}

          {inspectorTab === 'run' ? <section className="stack gap-md">
            <div className="panel-subheader">
              <h3>Run inputs</h3>
              {signature && signature.inputs.length > 0 ? (
                <div className="segmented-control inline" role="group" aria-label="Input editor">
                  <button type="button" className={inputsMode === 'form' ? 'active' : ''} onClick={() => setInputsMode('form')}>Form</button>
                  <button type="button" className={inputsMode === 'json' ? 'active' : ''} onClick={() => setInputsMode('json')}>JSON</button>
                </div>
              ) : null}
            </div>
            <div className="run-summary-card">
              <strong>Ready to run “{workflowMeta.name}”</strong>
              <span>{stepCount} steps · {edges.length} connections · {versions[0] ? 'saved version' : 'current draft'}</span>
              {sourceDocument ? <span>Paper: {sourceDocument.title}</span> : null}
              {signature && signature.outputs.length > 0 ? (
                <span>Returns: {signature.outputs.map((port) => port.key).join(', ')}</span>
              ) : null}
            </div>
            {signature && signature.inputs.length === 0 ? (
              <>
                <p className="muted-text small">
                  This agent asks for no customer input. Add fields below when it needs a question,
                  document, or other value before it starts.
                </p>
                <WorkflowInputsPanel
                  inputs={declaredInputs}
                  onPatch={patchWorkflowInput}
                  onRemove={removeWorkflowInput}
                  onAdd={addWorkflowInput}
                  onFocusNode={() => focusNode(AGENT_START_NODE_ID)}
                />
              </>
            ) : null}
            {signature && signature.inputs.length > 0 && inputsMode === 'form' ? (
              parsedInputs ? (
                <RunInputsForm
                  ports={signature.inputs}
                  value={parsedInputs}
                  onChange={(next) => setInputsText(JSON.stringify(next, null, 2))}
                />
              ) : (
                <ErrorNotice message="The raw inputs are not valid JSON, so the form is unavailable. Fix them in the JSON tab." />
              )
            ) : (
              <textarea className="json-editor" value={inputsText} onChange={(event) => setInputsText(event.target.value)} />
            )}
            <div className="button-row wrap">
              <button type="button" className="button primary" disabled={busy === 'run'} onClick={() => void runWorkflow('draft')}>
                <Icon name="play" size={13} />
                Run current draft
              </button>
              <button
                type="button"
                className="button"
                disabled={!versions[0] || busy === 'run'}
                title={versions[0] ? `Run saved version ${versions[0].version}` : 'Save the agent first'}
                onClick={() => void runWorkflow('saved')}
              >
                Run saved version
              </button>
            </div>
          </section> : null}

        </aside>
      </div>

      <QuickAddPalette
        open={quickAddOpen}
        nodes={buildableCatalog}
        onAdd={addNodeAtViewportCentre}
        onCreateCustom={() => setCustomAuthor({ definition: null, duplicate: false })}
        onClose={() => setQuickAddOpen(false)}
      />

      {editorNode ? (
        <NodeEditorModal
          node={editorNode}
          onUpdate={(updater) => updateNode(editorNode.id, updater)}
          onDelete={() => {
            setEdges((current) => current.filter((edge) => edge.source !== editorNode.id && edge.target !== editorNode.id));
            setNodes((current) => current.filter((node) => node.id !== editorNode.id));
            setSelectedNodeId('');
            setNodeEditorId('');
          }}
          onClose={() => setNodeEditorId('')}
          profiles={providerProfiles}
          settings={settings}
          workflowDefaults={workflowModelDefaults}
          wiredTools={wiredPortLabels(editorNode.id, 'tools')}
          wiredHandoffs={wiredPortLabels(editorNode.id, 'handoffs')}
          incomingNames={incomingPortNames(editorNode.id)}
        />
      ) : null}

      {customLibraryOpen ? (
        <CustomNodeLibraryModal
          definitions={customDefinitions}
          onClose={() => setCustomLibraryOpen(false)}
          onCreate={() => {
            setCustomLibraryOpen(false);
            setCustomAuthor({ definition: null, duplicate: false });
          }}
          onEdit={(definition) => {
            setCustomLibraryOpen(false);
            setCustomAuthor({ definition, duplicate: false });
          }}
          onDuplicate={(definition) => {
            setCustomLibraryOpen(false);
            setCustomAuthor({ definition, duplicate: true });
          }}
          onArchive={async (definition) => {
            try {
              await api.archiveCustomNode(definition.id);
              await refreshLibrary();
              toast.success('Custom node archived', `${definition.name} is no longer offered for new workflow steps.`);
            } catch (err) {
              toast.failure('Could not archive custom node', toMessage(err, 'Try again.'));
            }
          }}
        />
      ) : null}

      {customAuthor ? (
        <CustomNodeAuthorModal
          definition={customAuthor.definition}
          duplicate={customAuthor.duplicate}
          onClose={() => setCustomAuthor(null)}
          onSaved={() => {
            setCustomAuthor(null);
            void refreshLibrary().catch((err) => toast.failure('Saved, but could not refresh the palette', toMessage(err, 'Refresh the page to reload the node catalog.')));
          }}
        />
      ) : null}

      <WorkflowPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        title="Open an agent"
        description={
          sourceDocument
            ? `“${sourceDocument.title}” stays selected, whichever agent you pick.`
            : 'Pick one of your saved agents or start from a template.'
        }
        highlightInputKey={documentId ? 'document_id' : undefined}
        allowBlank
        onSelect={(choice: WorkflowChoice) => {
          if (choice.kind === 'blank') {
            loadDefinition(blankWorkflow());
            return;
          }
          navigate(workflowEditorPath(choice, { documentId: documentId || undefined }));
        }}
      />
    </div>
  );
}

export function WorkflowsPage() {
  return (
    <ReactFlowProvider>
      <WorkflowEditorInner />
    </ReactFlowProvider>
  );
}
