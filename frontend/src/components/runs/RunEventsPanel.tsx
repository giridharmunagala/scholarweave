import { useEffect, useRef } from 'react';
import { EmptyState } from '../common/EmptyState';
import { Icon } from '../common/Icon';
import type { RunEventResponse } from '../../types/api';
import { formatDateTime } from '../../lib/format';

const eventTone: Record<string, string> = {
  'run.completed': 'success',
  'node.completed': 'success',
  'node.skipped': 'warning',
  'run.failed': 'danger',
  'node.failed': 'danger',
  'run.cancelled': 'muted',
  'run.cancel_requested': 'warning',
  'run.started': 'info',
  'node.started': 'info',
  'node.warning': 'warning',
  'node.tool_call': 'info',
  'node.tool_result': 'muted',
  'node.agent_tool_call': 'info',
  'node.agent_tool_output': 'muted',
  'node.agent_handoff': 'warning',
};

const eventLabel: Record<string, string> = {
  'node.tool_call': 'tool call',
  'node.tool_result': 'tool result',
  'node.agent_tool_call': 'agent used a tool',
  'node.agent_tool_output': 'tool returned',
  'node.agent_handoff': 'handoff',
  'node.warning': 'warning',
  'node.skipped': 'skipped',
  'node.stdout': 'print output',
};

function text(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  return typeof value === 'string' ? value : value === undefined || value === null ? '' : JSON.stringify(value);
}

/** One readable line per event, so the timeline is scannable without opening payloads. */
function summarise(event: RunEventResponse): string {
  const payload = (event.payload || {}) as Record<string, unknown>;
  const where = text(payload, 'node_path');
  switch (event.event_type) {
    case 'node.tool_call':
    case 'node.agent_tool_call':
      return [where, text(payload, 'tool')].filter(Boolean).join(' → ');
    case 'node.tool_result':
      return [where, payload.ok === false ? 'failed' : 'ok'].filter(Boolean).join(' → ');
    case 'node.agent_tool_output':
      return text(payload, 'output').slice(0, 160);
    case 'node.agent_handoff':
      return [where, text(payload, 'stage')].filter(Boolean).join(' → ');
    case 'node.warning':
      return text(payload, 'message');
    case 'node.stdout':
      return text(payload, 'text').slice(0, 160);
    case 'node.started':
    case 'node.completed':
    case 'node.failed':
      return where || text(payload, 'node_id');
    case 'node.skipped':
      return [where || text(payload, 'node_id'), text(payload, 'reason')].filter(Boolean).join(' — ');
    default:
      return '';
  }
}

export function RunEventsPanel({ events, tokenLog }: { events: RunEventResponse[]; tokenLog: Record<string, string> }) {
  const timelineEvents = events.filter((event) => event.event_type !== 'node.progress');
  const tokenEntries = Object.entries(tokenLog);
  const streamEnd = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    streamEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [tokenLog]);

  return (
    <div className="stack gap-lg">
      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="eyebrow">Live token stream</p>
            <h3>Generated text</h3>
          </div>
          {tokenEntries.length ? <span className="status-badge info">Streaming</span> : null}
        </div>
        {tokenEntries.length === 0 ? (
          <EmptyState
            icon="sparkle"
            title="Nothing generated yet"
            description="Tokens appear here in real time as soon as a generation node starts."
          />
        ) : (
          <div className="stack gap-md scroll-area">
            {tokenEntries.map(([nodePath, text]) => (
              <div className="token-block" key={nodePath}>
                <div className="token-block-header">{nodePath}</div>
                <pre>{text}</pre>
              </div>
            ))}
            <div ref={streamEnd} />
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="eyebrow">Server-sent events</p>
            <h3>Timeline</h3>
          </div>
          {timelineEvents.length ? <span className="tiny-tag">{timelineEvents.length}</span> : null}
        </div>
        {timelineEvents.length === 0 ? (
          <p className="empty-state">No events received yet.</p>
        ) : (
          <div className="event-list">
            {timelineEvents
              .slice()
              .reverse()
              .map((event) => (
                <article className="event-item" key={event.id}>
                  <div className="event-item-header">
                    <span className={`status-badge ${eventTone[event.event_type] || 'muted'}`}>
                      {eventLabel[event.event_type] || event.event_type}
                    </span>
                    <span>{formatDateTime(event.created_at)}</span>
                  </div>
                  {summarise(event) ? <p className="event-item-summary">{summarise(event)}</p> : null}
                  <details>
                    <summary className="muted-text small">
                      <Icon name="chevronRight" size={11} /> Payload
                    </summary>
                    <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                  </details>
                </article>
              ))}
          </div>
        )}
      </section>
    </div>
  );
}
