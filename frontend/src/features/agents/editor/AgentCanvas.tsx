import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Connection,
  type Edge,
  type EdgeMouseHandler,
  type NodeMouseHandler,
} from '@xyflow/react';
import { useMemo } from 'react';
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
  const nodes = useMemo(
    () =>
      projectNodes(blueprint, presentation).map((node) => ({
        ...node,
        selected: node.id === selectedId,
      })),
    [blueprint, presentation, selectedId],
  );
  const edges = useMemo(() => projectEdges(blueprint), [blueprint]);
  const displayedEdges = useMemo(
    () => edges.map((edge) => ({ ...edge, selected: edge.id === selectedId })),
    [edges, selectedId],
  );
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
  const selectNode: NodeMouseHandler<PrimitiveNode> = (_event, node) => onSelect(node.id);
  const selectEdge: EdgeMouseHandler = (_event, edge) => onSelect(edge.id);

  return (
    <div className="agent-canvas" aria-label="SDK primitive canvas">
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
        onNodeDragStop={(_event, node) => onMove(node.id, node.position)}
        onEdgesDelete={(deleted) => deleted.forEach(onDeleteEdge)}
        deleteKeyCode={['Backspace', 'Delete']}
        proOptions={{ hideAttribution: false }}
      >
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
