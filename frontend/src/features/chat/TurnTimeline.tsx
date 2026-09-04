import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import type { PromptSnapshot } from './api';
import {
  formatStepDuration,
  humanizeToolName,
  type LiveActivity,
  type ReasoningStep,
  type TimelineSource,
  type ToolStep,
  type TurnStep,
  type TurnTimeline,
} from './chatTimeline';

/**
 * The turn trace reads inline, in the order it happened: a line per thought and per tool call,
 * each collapsed to a sentence and expandable to the detail behind it. Runs of consecutive tool
 * calls fold into a single line so a busy turn stays as short as a quiet one.
 */

type TimelineRow =
  | { kind: 'reasoning'; step: ReasoningStep }
  | { kind: 'tools'; steps: ToolStep[] }
  | { kind: 'handoff'; step: Extract<TurnStep, { kind: 'handoff' }> };

export function TurnTimelineView({ timeline }: { timeline: TurnTimeline }) {
  if (!timeline.steps.length) return null;
  return (
    <div className="turn-timeline" aria-label="Agent activity">
      {groupSteps(timeline.steps).map((row) => {
        if (row.kind === 'reasoning') return <ReasoningRow key={row.step.id} step={row.step} />;
        if (row.kind === 'handoff') {
          return (
            <div className="timeline-row static kind-handoff" key={row.step.id}>
              <Icon className="timeline-glyph" name="agents" size={15} />
              <span className="timeline-label">
                Handed off from <strong>{row.step.from}</strong> to <strong>{row.step.to}</strong>
              </span>
            </div>
          );
        }
        return row.steps.length === 1 ? (
          <ToolRow key={row.steps[0].id} step={row.steps[0]} />
        ) : (
          <ToolGroupRow key={row.steps[0].id} steps={row.steps} />
        );
      })}
    </div>
  );
}

/**
 * A live status line inside the thread: what the agent is doing right now, how long the turn
 * has been running, and a way into the full trace. Without it a long tool call looks like a
 * stalled screen.
 */
export function LiveActivityBar({
  activity,
  onOpenActivity,
}: {
  activity: LiveActivity;
  onOpenActivity?: () => void;
}) {
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setSeconds((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const elapsed = formatStepDuration(seconds);

  return (
    <div className={`live-activity phase-${activity.phase}`} role="status" aria-live="polite">
      <span className="spinner tiny" aria-hidden="true" />
      <span className="live-activity-body">
        <strong>{activity.label}</strong>
        {activity.detail ? <small>{activity.detail}</small> : null}
      </span>
      {elapsed ? <span className="live-activity-elapsed">{elapsed}</span> : null}
      {onOpenActivity && activity.completedSteps ? (
        <button type="button" className="live-activity-open" onClick={onOpenActivity}>
          {activity.completedSteps} step{activity.completedSteps === 1 ? '' : 's'}
        </button>
      ) : null}
    </div>
  );
}

export function ActivitySidebar({
  open,
  timelines,
  onClose,
  resizer,
}: {
  open: boolean;
  timelines: {
    id: string;
    label: string;
    timeline: TurnTimeline;
    snapshot?: PromptSnapshot | null;
  }[];
  onClose: () => void;
  resizer?: ReactNode;
}) {
  return (
    <aside className="activity-sidebar" aria-label="Run activity" hidden={!open}>
      {resizer}
      <header className="activity-sidebar-head">
        <div>
          <strong>Activity</strong>
          <span>Tools and completed reasoning</span>
        </div>
        <button type="button" aria-label="Close activity" onClick={onClose}>
          <Icon name="close" size={15} />
        </button>
      </header>
      <div className="activity-sidebar-scroll">
        {timelines.length ? timelines.map(({ id, label, timeline, snapshot }) => (
          <section className="activity-turn" key={id}>
            <h2>{label}</h2>
            {snapshot ? <PromptSnapshotRow snapshot={snapshot} /> : null}
            <TurnTimelineView timeline={timeline} />
          </section>
        )) : (
          <p className="activity-sidebar-empty">Tool calls and reasoning will appear here.</p>
        )}
      </div>
    </aside>
  );
}

function PromptSnapshotRow({ snapshot }: { snapshot: PromptSnapshot }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`timeline-row kind-prompt${open ? ' open' : ''}`}>
      <button
        type="button"
        className="timeline-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon className="timeline-glyph" name="settings" size={15} />
        <span className="timeline-label">
          <strong>Prompt & tools</strong>
          <small>
            {snapshot.agents.length} agent{snapshot.agents.length === 1 ? '' : 's'} · {snapshot.tools.length} tools
          </small>
        </span>
        <Icon className="timeline-chevron" name="arrowRight" size={13} />
      </button>
      {open ? (
        <div className="timeline-detail prompt-snapshot" aria-label="Effective prompt snapshot">
          {snapshot.prompt_revision ? (
            <section>
              <h4>Revision</h4>
              <code>{snapshot.prompt_revision}</code>
            </section>
          ) : null}
          {snapshot.agents.map((agent) => (
            <section key={String(agent.id)}>
              <h4>{String(agent.name || agent.id)}</h4>
              <pre>{String(agent.effective_instructions || '')}</pre>
            </section>
          ))}
          <section>
            <h4>Model-visible tools</h4>
            {snapshot.tools.length ? snapshot.tools.map((tool, index) => (
              <details key={`${String(tool.name)}-${index}`}>
                <summary>{String(tool.name)}</summary>
                {tool.description ? <p>{String(tool.description)}</p> : null}
                {tool.parameters_schema ? <pre>{formatPayload(tool.parameters_schema)}</pre> : null}
              </details>
            )) : <p>No tools were exposed to this run.</p>}
          </section>
        </div>
      ) : null}
    </div>
  );
}

/** Consecutive tool calls belong together the way a reader remembers them: as one detour. */
function groupSteps(steps: TurnStep[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  for (const step of steps) {
    if (step.kind === 'tool') {
      const last = rows[rows.length - 1];
      if (last?.kind === 'tools') last.steps.push(step);
      else rows.push({ kind: 'tools', steps: [step] });
      continue;
    }
    if (step.kind === 'reasoning') {
      if (!step.text.trim()) continue;
      rows.push({ kind: 'reasoning', step });
      continue;
    }
    if (step.kind === 'agent') continue;
    rows.push({ kind: 'handoff', step });
  }
  return rows;
}

function ReasoningRow({ step }: { step: ReasoningStep }) {
  const [open, setOpen] = useState(step.streaming);
  const bodyRef = useRef<HTMLDivElement>(null);
  const followTailRef = useRef(true);
  const wasStreamingRef = useRef(step.streaming);

  // A finished thought collapses on its own; the reader opted into watching, not into keeping it.
  useLayoutEffect(() => {
    if (wasStreamingRef.current && !step.streaming) setOpen(false);
    wasStreamingRef.current = step.streaming;
  }, [step.streaming]);

  useLayoutEffect(() => {
    const element = bodyRef.current;
    if (!element || !step.streaming || !followTailRef.current) return;
    element.scrollTop = element.scrollHeight;
  }, [step.text, step.streaming]);

  const elapsed = formatStepDuration(step.seconds);
  const label = step.streaming ? 'Thinking' : elapsed ? `Thought for ${elapsed}` : 'Thought process';

  return (
    <div className={`timeline-row kind-reasoning${open ? ' open' : ''}${step.streaming ? ' live' : ''}`}>
      <button type="button" className="timeline-head" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        {step.streaming
          ? <span className="spinner tiny timeline-glyph" aria-hidden="true" />
          : <Icon className="timeline-glyph" name="bulb" size={15} />}
        <span className="timeline-label">{label}</span>
        <Icon className="timeline-chevron" name="arrowRight" size={13} />
      </button>
      {open ? (
        <div
          className="timeline-detail reasoning"
          ref={bodyRef}
          tabIndex={0}
          role="region"
          aria-label="Reasoning trace"
          onScroll={(event) => {
            const element = event.currentTarget;
            followTailRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
          }}
        >
          <MarkdownViewer content={step.text} />
          {step.streaming ? <i className="stream-cursor" aria-hidden="true" /> : null}
        </div>
      ) : null}
    </div>
  );
}

function ToolRow({ step }: { step: ToolStep }) {
  const [open, setOpen] = useState(false);
  const elapsed = formatStepDuration(step.seconds);

  return (
    <div className={`timeline-row kind-tool${open ? ' open' : ''}${step.status === 'running' ? ' live' : ''}${step.status === 'failed' ? ' failed' : ''}`}>
      <button type="button" className="timeline-head" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        {step.status === 'running'
          ? <span className="spinner tiny timeline-glyph" aria-hidden="true" />
          : <Icon className="timeline-glyph" name={step.sources.length ? 'globe' : 'tools'} size={15} />}
        <span className="timeline-label">
          <span className="timeline-lead">Used tool:</span>
          <strong>{step.label}</strong>
          {elapsed ? <small>{elapsed}</small> : null}
          {step.status === 'failed' ? <em className="timeline-failed">failed</em> : null}
        </span>
        <Icon className="timeline-chevron" name="arrowRight" size={13} />
      </button>
      {open ? <ToolDetail step={step} /> : null}
    </div>
  );
}

function ToolGroupRow({ steps }: { steps: ToolStep[] }) {
  const [open, setOpen] = useState(false);
  const running = steps.some((step) => step.status === 'running');

  return (
    <div className={`timeline-row group kind-tool${open ? ' open' : ''}`}>
      <button type="button" className="timeline-head" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        {running
          ? <span className="spinner tiny timeline-glyph" aria-hidden="true" />
          : <Icon className="timeline-glyph" name="tools" size={15} />}
        <span className="timeline-label">
          <strong>{steps.length} tool calls</strong>
          <small>{previewNames(steps)}</small>
        </span>
        <Icon className="timeline-chevron" name="arrowRight" size={13} />
      </button>
      {open ? (
        <div className="timeline-detail group">
          {steps.map((step) => (
            <Fragment key={step.id}>
              <ToolRow step={step} />
            </Fragment>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function previewNames(steps: ToolStep[]): string {
  return [...new Set(steps.map((step) => humanizeToolName(step.name)))].slice(0, 3).join(' · ');
}

function ToolDetail({ step }: { step: ToolStep }) {
  return (
    <div className="timeline-detail tool">
      {step.detail ? <p className="tool-detail-note">{step.detail}</p> : null}
      {step.args != null ? (
        <section>
          <h4>Request</h4>
          <pre>{formatPayload(step.args)}</pre>
        </section>
      ) : null}
      {step.result != null ? (
        <section>
          <h4>Result</h4>
          <pre>{formatPayload(step.result)}</pre>
        </section>
      ) : null}
      {step.sources.length ? <SourceChips sources={step.sources} compact /> : null}
      {step.args == null && step.result == null && !step.detail ? (
        <p className="tool-detail-note">No payload was recorded for this call.</p>
      ) : null}
    </div>
  );
}

function formatPayload(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const VISIBLE_SOURCES = 6;

/** Sources sit under the answer so the evidence is one glance away from the claim. */
export function SourceChips({
  sources,
  compact = false,
}: {
  sources: TimelineSource[];
  compact?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!sources.length) return null;
  const limit = compact ? 4 : VISIBLE_SOURCES;
  const shown = expanded ? sources : sources.slice(0, limit);
  const hidden = sources.length - shown.length;

  return (
    <div className={`source-chips${compact ? ' compact' : ''}`} aria-label="Sources">
      {shown.map((source) => (
        <a
          className="source-chip"
          key={source.url}
          href={source.url}
          target="_blank"
          rel="noreferrer"
          title={`${source.title} — ${source.host}`}
        >
          <span className="source-favicon" aria-hidden="true" style={hostTint(source.host)}>
            {source.host.replace(/^www\./, '').charAt(0).toLocaleUpperCase()}
          </span>
          <span>{source.title}</span>
        </a>
      ))}
      {hidden > 0 ? (
        <button type="button" className="source-chip more" onClick={() => setExpanded(true)}>
          +{hidden} more
        </button>
      ) : null}
      {expanded && sources.length > limit ? (
        <button type="button" className="source-chip more" onClick={() => setExpanded(false)}>
          Show less
        </button>
      ) : null}
    </div>
  );
}

export function SourceImages({ sources }: { sources: TimelineSource[] }) {
  const images = sources.filter((source) => source.imageUrl);
  if (!images.length) return null;

  return (
    <div className="source-images" aria-label="Images from search results" tabIndex={images.length > 3 ? 0 : undefined}>
      {images.map((source) => <SourceImage key={`${source.url}:${source.imageUrl}`} source={source} />)}
    </div>
  );
}

function SourceImage({ source }: { source: TimelineSource }) {
  const [failed, setFailed] = useState(false);
  if (!source.imageUrl || failed) return null;

  return (
    <a
      className="source-image"
      href={source.url}
      target="_blank"
      rel="noreferrer"
      title={`${source.title} — ${source.host}`}
    >
      <img
        src={source.imageUrl}
        alt={source.title}
        loading="lazy"
        decoding="async"
        onError={() => setFailed(true)}
      />
      <span>{source.title}</span>
    </a>
  );
}

/** A stable colour per host, derived locally so no favicon request ever leaves the machine. */
function hostTint(host: string): { background: string; color: string } {
  let hash = 0;
  for (let index = 0; index < host.length; index += 1) hash = (hash * 31 + host.charCodeAt(index)) % 360;
  return { background: `hsl(${hash} 62% 88%)`, color: `hsl(${hash} 62% 28%)` };
}
