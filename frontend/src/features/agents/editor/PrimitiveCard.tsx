import { Handle, Position, type NodeProps } from '@xyflow/react';
import type { PrimitiveNode } from './canvasProjection';

export function PrimitiveCard({ data, selected }: NodeProps<PrimitiveNode>) {
  return (
    <div className={`primitive-card kind-${data.kind} ${selected ? 'selected' : ''}`.trim()}>
      <Handle id="in" type="target" position={Position.Left} />
      <div className="primitive-kind">
        <span>{data.kind.split('_').join(' ')}</span>
        {data.entry ? <span className="entry-flag">Entry</span> : null}
      </div>
      <strong title={data.title}>{data.title}</strong>
      <small title={data.subtitle}>{data.subtitle}</small>
      <Handle id="out" type="source" position={Position.Right} />
      {/* Secondary anchors keep guardrail and return-path edges off the
          left/right lanes so their arrowheads never stack. */}
      <Handle id="in-top" type="target" position={Position.Top} />
      <Handle id="out-alt" type="source" position={Position.Bottom} style={{ left: '35%' }} />
      <Handle id="in-alt" type="target" position={Position.Bottom} style={{ left: '65%' }} />
    </div>
  );
}
