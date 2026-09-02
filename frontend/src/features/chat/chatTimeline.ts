import type { RunStreamEvent } from '../../api/events';

/**
 * Rebuilds the chronological trace of one turn - what the agent thought, which tools it
 * reached for, and what came back - from the run events alone. The same builder serves the
 * live stream and a reloaded run, so a turn reads identically while it happens and long after.
 */

export interface TimelineSource {
  url: string;
  title: string;
  host: string;
  imageUrl?: string;
}

export interface ReasoningStep {
  kind: 'reasoning';
  id: string;
  sequence: number;
  text: string;
  seconds: number | null;
  streaming: boolean;
}

export interface ToolStep {
  kind: 'tool';
  id: string;
  sequence: number;
  name: string;
  label: string;
  query: string | null;
  detail: string | null;
  args: unknown;
  result: unknown;
  status: 'running' | 'completed' | 'failed';
  seconds: number | null;
  sources: TimelineSource[];
}

export interface HandoffStep {
  kind: 'handoff';
  id: string;
  sequence: number;
  from: string;
  to: string;
}

export interface AgentStep {
  kind: 'agent';
  id: string;
  sequence: number;
  completedSequence: number | null;
  name: string;
  output: unknown;
  status: 'running' | 'completed' | 'failed' | 'superseded';
  seconds: number | null;
}

export type TurnStep = ReasoningStep | ToolStep | HandoffStep | AgentStep;

export interface TurnTimeline {
  steps: TurnStep[];
  sources: TimelineSource[];
  toolCount: number;
  agentCount: number;
  reasoningSeconds: number | null;
  running: boolean;
}

export const emptyTurnTimeline: TurnTimeline = {
  steps: [],
  sources: [],
  toolCount: 0,
  agentCount: 0,
  reasoningSeconds: null,
  running: false,
};

const REASONING_DELTAS = new Set([
  'response.reasoning_text.delta',
  'response.reasoning_summary_text.delta',
]);

const TERMINAL_EVENTS = new Set([
  'run.completed',
  'run.failed',
  'run.cancelled',
  'run.paused',
]);

type MutableStep =
  | (ReasoningStep & { startedAt: number | null })
  | (ToolStep & { startedAt: number | null; callId: string | null; settled: boolean })
  | (AgentStep & { startedAt: number | null })
  | HandoffStep;

export function buildTurnTimeline(
  events: readonly RunStreamEvent[],
  options: { settled?: boolean } = {},
): TurnTimeline {
  const sorted = [...events].sort((left, right) => left.sequence - right.sequence);
  const steps: MutableStep[] = [];
  let reasoningSoFar = '';
  let openReasoning: (ReasoningStep & { startedAt: number | null }) | null = null;
  let phaseStart: number | null = null;
  let lastAt: number | null = null;
  let settled = options.settled === true;
  let rootAgent: { id: string | null; name: string } | null = null;

  const closeReasoning = (at: number | null) => {
    if (!openReasoning) return;
    openReasoning.streaming = false;
    openReasoning.seconds = duration(openReasoning.startedAt, at ?? lastAt);
    openReasoning = null;
    if (at !== null) phaseStart = at;
  };

  for (const event of sorted) {
    const at = eventTime(event);
    if (at !== null) {
      lastAt = at;
      if (phaseStart === null) phaseStart = at;
    }

    if (event.event_type === 'model.stream') {
      const rawType = String(event.payload.raw_type ?? '');
      const delta = event.payload.delta;
      if (typeof delta !== 'string') continue;
      if (REASONING_DELTAS.has(rawType)) {
        const full = event.payload.snapshot === true ? delta : reasoningSoFar + delta;
        const addition = full.startsWith(reasoningSoFar)
          ? full.slice(reasoningSoFar.length)
          : full;
        reasoningSoFar = full;
        if (!addition) continue;
        if (openReasoning) {
          openReasoning.text += addition;
        } else {
          openReasoning = {
            kind: 'reasoning',
            id: `reasoning-${event.sequence}`,
            sequence: event.sequence,
            text: addition,
            seconds: null,
            streaming: true,
            startedAt: phaseStart ?? at,
          };
          steps.push(openReasoning);
        }
        continue;
      }
      // The answer starting is the moment thinking stopped.
      if (rawType === 'response.output_text.delta') closeReasoning(at);
      continue;
    }

    if (event.event_type === 'agent.started') {
      const name = stringOr(event.payload.agent_name);
      if (!name) continue;
      const invocationId = stringOr(event.payload.invocation_id);
      if (rootAgent === null) {
        rootAgent = { id: invocationId, name };
        continue;
      }
      steps.push({
        kind: 'agent',
        id: invocationId ?? `agent-${event.sequence}`,
        sequence: event.sequence,
        completedSequence: null,
        name,
        output: null,
        status: 'running',
        seconds: null,
        startedAt: at,
      });
      continue;
    }

    if (
      event.event_type === 'agent.completed'
      || event.event_type === 'agent.failed'
      || event.event_type === 'agent.superseded'
    ) {
      const name = stringOr(event.payload.agent_name);
      const invocationId = stringOr(event.payload.invocation_id);
      if (!name) continue;
      const isRoot = rootAgent?.name === name
        && (!invocationId || !rootAgent.id || invocationId === rootAgent.id);
      if (isRoot) continue;
      const target = findAgent(
        steps,
        (agent) => agent.status === 'running'
          && (invocationId ? agent.id === invocationId : agent.name === name),
      );
      if (!target) continue;
      target.status = event.event_type === 'agent.completed'
        ? 'completed'
        : event.event_type === 'agent.failed' ? 'failed' : 'superseded';
      target.completedSequence = event.sequence;
      target.seconds = duration(target.startedAt, at);
      target.output = event.payload.output
        ?? event.payload.error
        ?? event.payload.reason
        ?? null;
      continue;
    }

    if (event.event_type === 'tool.started') {
      const name = stringOr(event.payload.tool_name);
      if (!name) continue;
      const callId = stringOr(event.payload.tool_call_id);
      closeReasoning(at);
      const pending =
        (callId
          ? findTool(steps, (tool) => tool.callId === callId && !tool.settled)
          : null)
        ?? findTool(
          steps,
          (tool) => tool.name === name && !tool.settled && tool.startedAt === null,
        );
      if (pending) {
        pending.startedAt = at;
        pending.callId = callId ?? pending.callId;
        continue;
      }
      const step = toolStep(event.sequence, name, at);
      step.callId = callId;
      steps.push(step);
      continue;
    }

    if (event.event_type === 'tool.completed' || event.event_type === 'tool.failed') {
      const name = stringOr(event.payload.tool_name);
      if (!name) continue;
      const callId = stringOr(event.payload.tool_call_id);
      closeReasoning(at);
      const status = event.event_type === 'tool.failed' ? 'failed' : 'completed';
      const target =
        (callId
          ? findTool(steps, (tool) => tool.callId === callId && !tool.settled)
          : null)
        ?? findTool(steps, (tool) => tool.name === name && !tool.settled)
        ?? pushTool(steps, toolStep(event.sequence, name, at));
      target.callId = callId ?? target.callId;
      target.status = status;
      target.settled = true;
      target.seconds = duration(target.startedAt, at);
      if (event.payload.result !== undefined && target.result === null) {
        target.result = event.payload.result;
        target.sources = extractSources(event.payload.result);
      }
      if (status === 'failed') {
        const message = stringOr(event.payload.error) ?? stringOr(event.payload.message);
        if (message) target.detail = message;
      }
      if (at !== null) phaseStart = at;
      continue;
    }

    if (event.event_type === 'run.item') {
      applyRunItem(steps, event, at, closeReasoning);
      continue;
    }

    if (event.event_type === 'handoff.completed') {
      closeReasoning(at);
      const from = stringOr(event.payload.from_agent);
      const to = stringOr(event.payload.to_agent);
      if (!from || !to) continue;
      steps.push({ kind: 'handoff', id: `handoff-${event.sequence}`, sequence: event.sequence, from, to });
      continue;
    }

    if (TERMINAL_EVENTS.has(event.event_type)) {
      closeReasoning(at);
      settled = true;
    }
  }

  if (settled) closeReasoning(lastAt);

  const running =
    !settled
    && (
      Boolean(openReasoning)
      || steps.some(
        (step) => (step.kind === 'tool' || step.kind === 'agent') && step.status === 'running',
      )
    );

  return {
    steps: steps.map(freezeStep),
    sources: dedupeSources(steps.flatMap((step) => (step.kind === 'tool' ? step.sources : []))),
    toolCount: steps.filter((step) => step.kind === 'tool').length,
    agentCount: steps.filter((step) => step.kind === 'agent').length,
    reasoningSeconds: totalReasoningSeconds(steps),
    running,
  };
}

function applyRunItem(
  steps: MutableStep[],
  event: RunStreamEvent,
  at: number | null,
  closeReasoning: (at: number | null) => void,
): void {
  const item = event.payload.item;
  if (!isRecord(item)) return;
  const type = stringOr(item.type);
  const raw = isRecord(item.raw_item) ? item.raw_item : null;

  if (type === 'tool_call_item') {
    closeReasoning(at);
    const name = stringOr(raw?.name) ?? stringOr(item.title) ?? 'tool';
    const args = parseArguments(raw?.arguments);
    const callId = stringOr(raw?.call_id) ?? stringOr(raw?.id);
    const existing =
      (callId ? findTool(steps, (tool) => tool.callId === callId) : null)
      ?? findTool(
        steps,
        (tool) => tool.name === name && !tool.settled && tool.args === null,
      );
    const target = existing ?? pushTool(steps, toolStep(event.sequence, name, null));
    target.args = args;
    target.callId = callId ?? target.callId;
    target.detail = stringOr(item.description) ?? target.detail;
    if (stringOr(item.title) && !stringOr(raw?.name)) target.name = stringOr(item.title)!;
    const query = primaryArgument(args);
    target.query = query;
    target.label = toolLabel(target.name, query);
    return;
  }

  if (type === 'tool_call_output_item') {
    const callId = stringOr(raw?.call_id) ?? stringOr(raw?.id);
    const target =
      (callId ? findTool(steps, (tool) => tool.callId === callId) : null)
      ?? findTool(steps, (tool) => tool.result === null);
    if (!target) return;
    target.result = item.output ?? null;
    target.sources = extractSources(item.output);
    return;
  }

  if (type === 'message_output_item') closeReasoning(at);
}

function toolStep(sequence: number, name: string, at: number | null) {
  const step: ToolStep & { startedAt: number | null; callId: string | null; settled: boolean } = {
    kind: 'tool',
    id: `tool-${sequence}`,
    sequence,
    name,
    label: toolLabel(name, null),
    query: null,
    detail: null,
    args: null,
    result: null,
    status: 'running',
    seconds: null,
    sources: [],
    startedAt: at,
    callId: null,
    settled: false,
  };
  return step;
}

type MutableTool = ReturnType<typeof toolStep>;
type MutableAgent = AgentStep & { startedAt: number | null };

function pushTool(steps: MutableStep[], step: MutableTool): MutableTool {
  steps.push(step);
  return step;
}

function findTool(
  steps: MutableStep[],
  predicate: (tool: MutableTool) => boolean,
): MutableTool | null {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const step = steps[index];
    if (step.kind === 'tool' && predicate(step as MutableTool)) return step as MutableTool;
  }
  return null;
}

function findAgent(
  steps: MutableStep[],
  predicate: (agent: MutableAgent) => boolean,
): MutableAgent | null {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const step = steps[index];
    if (step.kind === 'agent' && predicate(step)) return step;
  }
  return null;
}

function freezeStep(step: MutableStep): TurnStep {
  if (step.kind === 'reasoning') {
    const { startedAt: _startedAt, ...rest } = step;
    return rest;
  }
  if (step.kind === 'tool') {
    const { startedAt: _startedAt, callId: _callId, settled: _settled, ...rest } = step as MutableTool;
    return rest;
  }
  if (step.kind === 'agent') {
    const { startedAt: _startedAt, ...rest } = step;
    return rest;
  }
  return step;
}

function totalReasoningSeconds(steps: MutableStep[]): number | null {
  const values = steps
    .filter((step): step is ReasoningStep & { startedAt: number | null } => step.kind === 'reasoning')
    .map((step) => step.seconds)
    .filter((value): value is number => value !== null);
  return values.length ? values.reduce((total, value) => total + value, 0) : null;
}

/* ------------------------------------------------------------------ labels --- */

/** Search-shaped calls read best as the phrase a person would use for them. */
export function toolLabel(name: string, query: string | null): string {
  const readable = humanizeToolName(name);
  if (!query) return readable;
  if (/search|find|lookup|query/i.test(name)) return `Searched "${truncate(query, 70)}"`;
  if (/fetch|download|read|open|browse/i.test(name)) return `Read ${truncate(prettyTarget(query), 70)}`;
  return `${readable}: ${truncate(prettyTarget(query), 70)}`;
}

/** A URL reads as its destination; anything else is left as written. */
function prettyTarget(value: string): string {
  if (!/^https?:\/\//i.test(value)) return value;
  return value.replace(/^https?:\/\//i, '').replace(/^www\./i, '').replace(/\/$/, '');
}

export function humanizeToolName(value: string): string {
  const words = value.replace(/^[a-z]+\./i, '').replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toLocaleUpperCase() + words.slice(1) : 'Tool';
}

const QUERY_KEYS = ['query', 'q', 'search', 'question', 'term', 'url', 'title', 'name', 'path', 'topic'];
const OPAQUE_ID = /^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24,})$/i;

/** Internal handles say nothing to a reader, so they never become the label. */
function readableArgument(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed || OPAQUE_ID.test(trimmed)) return null;
  return trimmed;
}

/** The argument a reader cares about - the query, the URL, the file - not the whole payload. */
export function primaryArgument(args: unknown): string | null {
  if (typeof args === 'string') return readableArgument(args);
  if (!isRecord(args)) return null;
  for (const key of QUERY_KEYS) {
    const value = readableArgument(args[key]);
    if (value) return value;
  }
  for (const value of Object.values(args)) {
    const readable = readableArgument(value);
    if (readable) return readable;
  }
  return null;
}

function parseArguments(value: unknown): unknown {
  if (typeof value !== 'string') return value ?? null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    return trimmed;
  }
}

function truncate(value: string, limit: number): string {
  const collapsed = value.replace(/\s+/g, ' ').trim();
  return collapsed.length > limit ? `${collapsed.slice(0, limit - 1)}…` : collapsed;
}

/* ----------------------------------------------------------------- sources --- */

const URL_PATTERN = /https?:\/\/[^\s"'<>)\]]+/g;
const TITLE_KEYS = ['title', 'name', 'headline', 'label', 'display_name'];

/** Tool results carry the evidence behind an answer, so their links become the turn's sources. */
export function extractSources(value: unknown): TimelineSource[] {
  const found: TimelineSource[] = [];
  walk(value, 0, found);
  return dedupeSources(found);
}

function walk(value: unknown, depth: number, found: TimelineSource[]): void {
  if (depth > 6 || found.length > 200) return;
  if (typeof value === 'string') {
    for (const match of value.match(URL_PATTERN) ?? []) {
      const source = toSource(match, null);
      if (source) found.push(source);
    }
    return;
  }
  if (Array.isArray(value)) {
    for (const entry of value) walk(entry, depth + 1, found);
    return;
  }
  if (!isRecord(value)) return;

  const url = firstString(value, ['url', 'link', 'href', 'source_url', 'pdf_url']);
  if (url) {
    const source = toSource(
      url,
      firstString(value, TITLE_KEYS),
      firstHttpUrl(value, ['image_url', 'imageUrl', 'thumbnail', 'thumbnail_url', 'img_src']),
    );
    if (source) found.push(source);
  }
  for (const [key, entry] of Object.entries(value)) {
    if ([
      'url',
      'link',
      'href',
      'image_url',
      'imageUrl',
      'thumbnail',
      'thumbnail_url',
      'img_src',
    ].includes(key)) continue;
    walk(entry, depth + 1, found);
  }
}

function firstString(record: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function firstHttpUrl(record: Record<string, unknown>, keys: string[]): string | undefined {
  const value = firstString(record, keys);
  return value && /^https?:\/\//i.test(value) ? value : undefined;
}

function toSource(
  url: string,
  title: string | null,
  imageUrl?: string,
): TimelineSource | null {
  const cleaned = url.replace(/[.,;]+$/, '');
  if (!/^https?:\/\//i.test(cleaned)) return null;
  let host = '';
  try {
    host = new URL(cleaned).hostname.replace(/^www\./, '');
  } catch {
    return null;
  }
  if (!host) return null;
  return { url: cleaned, title: title || host, host, ...(imageUrl ? { imageUrl } : {}) };
}

export function dedupeSources(sources: TimelineSource[]): TimelineSource[] {
  const seen = new Map<string, TimelineSource>();
  for (const source of sources) {
    const existing = seen.get(source.url);
    if (!existing) {
      seen.set(source.url, source);
      continue;
    }
    seen.set(source.url, {
      ...existing,
      ...(existing.title === existing.host && source.title !== source.host
        ? { title: source.title }
        : {}),
      ...(source.imageUrl ? { imageUrl: source.imageUrl } : {}),
    });
  }
  return [...seen.values()];
}

/* -------------------------------------------------------------- formatting --- */

export function formatStepDuration(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds)) return null;
  // Sub-second steps still read as a beat of work, so they round up rather than vanish.
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

function duration(from: number | null, to: number | null): number | null {
  if (from === null || to === null) return null;
  const elapsed = (to - from) / 1000;
  return Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : null;
}

function eventTime(event: RunStreamEvent): number | null {
  if (!event.created_at) return null;
  const time = new Date(event.created_at).getTime();
  return Number.isFinite(time) ? time : null;
}

function stringOr(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
