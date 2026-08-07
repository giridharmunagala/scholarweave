import { Handle, Position, type NodeProps } from '@xyflow/react';
import type { PrimitiveNode } from './canvasProjection';

export function PrimitiveCard({ data, selected }: NodeProps<PrimitiveNode>) {
  const canReceivePrimary = data.kind === 'agent';
  const canReceiveGuardrail = data.kind === 'agent' || data.kind === 'function_tool';

  return (
    <div className={`primitive-card kind-${data.kind} ${selected ? 'selected' : ''}`.trim()}>
      {canReceivePrimary ? (
        <Handle
          id="in"
          type="target"
          position={Position.Left}
          className="connection-port connection-port-in"
          aria-label="Connection input"
          title="Drop or click here to connect"
        />
      ) : null}
      <div className="primitive-kind">
        <span>{data.kind.split('_').join(' ')}</span>
        {data.entry ? <span className="entry-flag">Entry</span> : null}
      </div>
      <strong title={data.title}>{data.title}</strong>
      <small title={data.subtitle}>{data.subtitle}</small>
      <Handle
        id="out"
        type="source"
        position={Position.Right}
        className="connection-port connection-port-out"
        aria-label="Connection output"
        title="Drag or click to start a connection"
      />
      {/* Secondary anchors keep guardrail and return-path edges off the
          left/right lanes so their arrowheads never stack. */}
      {canReceiveGuardrail ? (
        <Handle
          id="in-top"
          type="target"
          position={Position.Top}
          className="connection-port connection-port-secondary"
          aria-label="Guardrail input"
          title="Connect a guardrail here"
        />
      ) : null}
      {data.kind === 'agent' ? (
        <>
          <Handle id="out-alt" type="source" position={Position.Bottom} className="connection-port connection-port-secondary" style={{ left: '35%' }} />
          <Handle id="in-alt" type="target" position={Position.Bottom} className="connection-port connection-port-secondary" style={{ left: '65%' }} />
        </>
      ) : null}
    </div>
  );
}
