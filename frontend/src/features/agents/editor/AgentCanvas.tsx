import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  Panel,
  ReactFlow,
  type Connection,
  type Edge,
  type EdgeMouseHandler,
  type NodeMouseHandler,
} from '@xyflow/react';
import { useMemo, useState } from 'react';
import { projectEdges, projectNodes, type PrimitiveNode } from './canvasProjection';
import { PrimitiveCard } from './PrimitiveCard';
import { useTheme } from '../../../shared/theme/ThemeProvider';
import type { AgentBlueprint, AgentPresentation } from '../types';

/** Read a design token so canvas chrome tracks the active theme. */
function token(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

export function AgentCanvas({
  blueprint,
  presentation,
  selectedId,
  onSelect,
  onConnect,
  onMove,
  onDeleteEdge,
}: {
  blueprint: AgentBlueprint;
  presentation: AgentPresentation;
  selectedId: string | null;
  onSelect: (nodeId: string | null) => void;
  onConnect: (connection: Connection) => void;
  onMove: (nodeId: string, position: { x: number; y: number }) => void;
  onDeleteEdge: (edge: Edge) => void;
}) {
  const { theme } = useTheme();
  const [connecting, setConnecting] = useState(false);
  const nodes = useMemo(
    () =>
      projectNodes(blueprint, presentation).map((node) => ({
        ...node,
        selected: node.id === selectedId,
      })),
    [blueprint, presentation, selectedId],
  );
  const edges = useMemo(() => projectEdges(blueprint), [blueprint]);
  const nodeTypes = useMemo(() => ({ primitive: PrimitiveCard }), []);
  const palette = useMemo(
    () => ({
      grid: token('--border', '#293346'),
      surface: token('--surface', '#131924'),
      accent: token('--accent', '#6f8dff'),
      muted: token('--muted', '#94a2ba'),
    }),
    // The token values change with the theme, so re-read them whenever it does.
    [theme],
  );
  // Markers live in shared SVG defs, so their colour cannot come from CSS classes.
  const edgeColors = useMemo<Record<string, string>>(
    () => ({
      'edge-tool': token('--tool', '#4fd0a0'),
      'edge-handoff': token('--handoff', '#f2c25c'),
      'edge-agent-tool': token('--agent', '#6f8dff'),
      'edge-guardrail': token('--guardrail', '#ff7686'),
    }),
    [theme],
  );
  const displayedEdges = useMemo(
    () =>
      edges.map((edge) => {
        const color = edgeColors[edge.className ?? ''] ?? palette.muted;
        return {
          ...edge,
          selected: edge.id === selectedId,
          style: { ...edge.style, stroke: color, strokeWidth: edge.id === selectedId ? 3 : 1.8 },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 18,
            height: 18,
            color,
          },
        };
      }),
    [edges, edgeColors, palette.muted, selectedId],
  );
  const selectNode: NodeMouseHandler<PrimitiveNode> = (_event, node) => onSelect(node.id);
  const selectEdge: EdgeMouseHandler = (_event, edge) => onSelect(edge.id);

  return (
    <div className={`agent-canvas ${connecting ? 'is-connecting' : ''}`} aria-label="SDK primitive canvas">
      <ReactFlow
        nodes={nodes}
        edges={displayedEdges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ maxZoom: 1, padding: 0.25 }}
        minZoom={0.2}
        onNodeClick={selectNode}
        onEdgeClick={selectEdge}
        onPaneClick={() => onSelect(null)}
        onConnect={onConnect}
        onConnectStart={() => setConnecting(true)}
        onConnectEnd={() => setConnecting(false)}
        connectOnClick
        connectionRadius={32}
        connectionLineStyle={{ stroke: palette.accent, strokeWidth: 2.5 }}
        defaultEdgeOptions={{ interactionWidth: 30 }}
        onNodeDragStop={(_event, node) => onMove(node.id, node.position)}
        onEdgesDelete={(deleted) => deleted.forEach(onDeleteEdge)}
        deleteKeyCode={['Backspace', 'Delete']}
        proOptions={{ hideAttribution: false }}
      >
        <Panel position="top-left" className="connection-help">
          <strong>Connect nodes</strong>
          <span>Drag or click <b>OUT</b>, then choose an <b>IN</b> handle.</span>
        </Panel>
        <Background color={palette.grid} gap={22} size={1.2} />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          maskColor="transparent"
          bgColor={palette.surface}
          nodeColor={palette.muted}
          nodeStrokeColor={palette.accent}
        />
      </ReactFlow>
    </div>
  );
}
