import { Handle, Position, type NodeProps } from '@xyflow/react';
import type { PrimitiveNode } from './canvasProjection';

export function PrimitiveCard({ data, selected }: NodeProps<PrimitiveNode>) {
  return (
    <div className={`primitive-card kind-${data.kind} ${selected ? 'selected' : ''}`.trim()}>
      <Handle type="target" position={Position.Left} />
      <div className="primitive-kind">
        <span>{data.kind.split('_').join(' ')}</span>
        {data.entry ? <span className="entry-flag">Entry</span> : null}
      </div>
      <strong title={data.title}>{data.title}</strong>
      <small title={data.subtitle}>{data.subtitle}</small>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
