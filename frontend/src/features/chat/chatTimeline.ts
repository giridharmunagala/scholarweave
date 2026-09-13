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
  startedAt?: number | null;
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
  status: 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
  seconds: number | null;
  sources: TimelineSource[];
  startedAt?: number | null;
  callId?: string | null;
  children?: AgentStep[];
}

export interface AgentStep {
  kind: 'agent';
  id: string;
  sequence: number;
  completedSequence: number | null;
  name: string;
  output: unknown;
  status: 'running' | 'completed' | 'failed' | 'superseded' | 'cancelled' | 'interrupted';
  seconds: number | null;
  startedAt?: number | null;
  request?: string | null;
  requestTruncated?: boolean;
  children?: TurnTimeline;
}

export type TurnStep = ReasoningStep | ToolStep | AgentStep;

export interface TurnTimeline {
  steps: TurnStep[];
  sources: TimelineSource[];
  toolCount: number;
  agentCount: number;
  reasoningSeconds: number | null;
  running: boolean;
  activity?: Omit<LiveActivity, 'completedSteps'> & { sequence: number };
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
]);

type MutableStep =
  | (ReasoningStep & { startedAt: number | null })
  | (ToolStep & { startedAt: number | null; callId: string | null; settled: boolean })
  | (AgentStep & { startedAt: number | null });

export function buildTurnTimeline(
  events: readonly RunStreamEvent[],
  options: { settled?: boolean } = {},
): TurnTimeline {
  type Scope = {
    id: string; name: string; parent: Scope | null; callId: string | null;
    events: RunStreamEvent[]; settled: boolean; activeCalls: Set<string>; output: string;
  };
  const root: Scope = {
    id: '', name: '', parent: null, callId: null, events: [],
    settled: Boolean(options.settled), activeCalls: new Set(), output: '',
  };
  const scopes = new Map<string, Scope>();
  const active: Scope[] = [];
  const namedScope = (name: string | null) => [...active].reverse().find((scope) => scope.name === name);
  for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
    const payload = event.payload;
    const name = stringOr(payload.agent_name) ?? stringOr(payload.delegate_agent_name)
      ?? (isRecord(payload.item) ? stringOr(payload.item.agent_name) : null);
    const invocation = stringOr(payload.invocation_id);
    let scope = invocation ? scopes.get(invocation)
      : payload.delegated !== true && name === root.name ? undefined : namedScope(name);
    if (event.event_type === 'agent.started' && name) {
      if (payload.delegated !== true && !payload.parent_agent_name && (!root.name || name === root.name)) {
        root.name = name;
        root.events.push(event);
        continue;
      }
      const parent = (stringOr(payload.parent_invocation_id)
        ? scopes.get(String(payload.parent_invocation_id)) : undefined)
        ?? namedScope(stringOr(payload.parent_agent_name)) ?? root;
      scope = {
        id: invocation ?? `agent-${event.sequence}`, name, parent,
        callId: stringOr(payload.parent_tool_call_id)
          ?? (parent.activeCalls.size === 1 ? [...parent.activeCalls][0] : null),
        events: [], settled: false, activeCalls: new Set(), output: '',
      };
      scopes.set(scope.id, scope);
      active.push(scope);
      parent.events.push({ ...event, payload: { ...payload, invocation_id: scope.id, delegated: true } });
      scope.events.push({ ...event, event_type: 'model.started' });
      continue;
    }
    if (scope && ['agent.completed', 'agent.failed', 'agent.superseded'].includes(event.event_type)) {
      scope.parent?.events.push({ ...event, payload: { ...payload, invocation_id: scope.id } });
      scope.events.push({ ...event, event_type: event.event_type === 'agent.completed' ? 'run.completed' : 'run.failed' });
      scope.settled = true;
      const index = active.indexOf(scope);
      if (index >= 0) active.splice(index, 1);
      continue;
    }
    const owner = scope ?? root;
    if (scope && event.event_type === 'agent.stream'
      && payload.raw_type === 'response.output_text.delta' && typeof payload.delta === 'string') {
      scope.output = payload.snapshot === true ? payload.delta : scope.output + payload.delta;
    }
    if (scope && event.event_type === 'model.retry'
      && typeof payload.discarded_text_characters === 'number' && payload.discarded_text_characters > 0) {
      scope.output = scope.output.slice(0, -payload.discarded_text_characters);
    }
    const callId = stringOr(payload.tool_call_id);
    if (callId && event.event_type === 'tool.started') owner.activeCalls.add(callId);
    if (callId && ['tool.completed', 'tool.failed'].includes(event.event_type)) owner.activeCalls.delete(callId);
    owner.events.push(
      event.event_type === 'agent.stream' ? { ...event, event_type: 'model.stream' } : event,
    );
    if (TERMINAL_EVENTS.has(event.event_type)) {
      for (const pending of active) {
        pending.events.push(event);
        pending.settled = true;
      }
    }
  }

  const build = (scope: Scope): TurnTimeline => {
    const timeline = buildScopeTimeline(scope.events, { settled: scope.settled || root.settled });
    const nested: TurnTimeline[] = [];
    for (const step of [...timeline.steps]) {
      if (step.kind !== 'agent') continue;
      const child = scopes.get(step.id);
      if (!child) continue;
      step.children = build(child);
      step.output ??= child.output || null;
      nested.push(step.children);
      const tool = child.callId
        ? [...timeline.steps].reverse().find((candidate): candidate is ToolStep =>
          candidate.kind === 'tool' && candidate.callId === child.callId && candidate.sequence < step.sequence)
        : null;
      if (tool) {
        (tool.children ??= []).push(step);
        timeline.steps = timeline.steps.filter((candidate) => candidate !== step);
      }
    }
    return {
      ...timeline,
      sources: dedupeSources([...timeline.sources, ...nested.flatMap((child) => child.sources)]),
      toolCount: timeline.toolCount + nested.reduce((sum, child) => sum + child.toolCount, 0),
      agentCount: timeline.agentCount + nested.reduce((sum, child) => sum + child.agentCount, 0),
    };
  };
  return build(root);
}

function buildScopeTimeline(
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
  let unfinishedStatus: 'failed' | 'cancelled' | 'interrupted' = 'interrupted';
  let rootAgent: { id: string | null; name: string } | null = null;
  let activity: TurnTimeline['activity'];

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

    if (event.event_type === 'model.phase' || event.event_type === 'model.started' || event.event_type === 'model.retry') {
      const phase = event.event_type === 'model.phase' ? String(event.payload.phase) : 'waiting';
      const labels: Record<string, string> = {
        queued: 'Waiting in queue', waiting: 'Waiting for model', processing: 'Processing context',
        thinking: 'Thinking', tool: 'Preparing tool call', writing: 'Writing the answer',
      };
      if (labels[phase]) activity = {
        phase: phase === 'tool' ? 'preparing' : phase as LiveActivity['phase'],
        label: event.event_type === 'model.retry' ? 'Retrying model response' : labels[phase],
        detail: null, sequence: event.sequence, startedAt: at,
      };
    }
    if (event.event_type === 'context.compaction_started') activity = {
      phase: 'processing', label: 'Shortening conversation context',
      detail: null, sequence: event.sequence, startedAt: at,
    };
    if (event.event_type === 'tool.started' || event.event_type === 'tool.completed'
      || event.event_type === 'tool.failed') activity = undefined;

    if (event.event_type === 'model.stream') {
      const rawType = String(event.payload.raw_type ?? '');
      const delta = event.payload.delta;
      if (typeof delta !== 'string') continue;
      if (delta && event.payload.snapshot !== true) {
        const phase = REASONING_DELTAS.has(rawType) ? 'thinking'
          : rawType === 'response.output_text.delta' ? 'writing'
            : rawType === 'response.function_call_arguments.delta' ? 'preparing' : null;
        if (phase && activity?.phase !== phase) activity = {
          phase, label: phase === 'thinking' ? 'Thinking' : phase === 'writing' ? 'Writing the answer' : 'Preparing tool call',
          detail: null, sequence: event.sequence, startedAt: at,
        };
      }
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
      if (event.payload.delegated !== true && (rootAgent === null || rootAgent.name === name)) {
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
        request: stringOr(event.payload.assignment),
        requestTruncated: event.payload.assignment_truncated === true,
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
        target.detail = readableToolFailure(event.payload);
      }
      if (at !== null) phaseStart = at;
      continue;
    }

    function readableToolFailure(payload: Record<string, unknown>): string {
      const displayMessage = stringOr(payload.display_message);
      if (displayMessage) return displayMessage;
      if (payload.failure_limit_reached === true) {
        return 'This tool was paused after repeated failures. The agent will use another available source.';
      }
      if (payload.unknown_outcome === true) {
        return 'The tool stopped before it could confirm whether the change was saved.';
      }
      const category = stringOr(payload.category);
      return {
        timeout: 'The tool took too long to respond. The agent can retry or use another source.',
        rate_limited: 'The service is temporarily limiting requests. The agent can retry shortly.',
        upstream_unavailable: 'The external service is temporarily unavailable. The agent can retry or use another source.',
        transport: 'The tool could not reach its service. Check the connection or try again.',
        invalid_input: 'The tool could not use this request. The agent will correct it before trying again.',
      }[category ?? '']
        ?? 'The tool could not complete this request. The agent will try another available approach.';
    }

    if (event.event_type === 'run.item') {
      applyRunItem(steps, event, at, closeReasoning);
      continue;
    }

    if (TERMINAL_EVENTS.has(event.event_type)) {
      closeReasoning(at);
      settled = true;
      unfinishedStatus = event.event_type === 'run.failed' ? 'failed'
        : event.event_type === 'run.cancelled' ? 'cancelled' : 'interrupted';
    }
  }

  if (settled) {
    activity = undefined;
    closeReasoning(lastAt);
    for (const step of steps) {
      if (step.kind !== 'reasoning' && step.status === 'running') {
        step.status = unfinishedStatus;
        step.seconds = duration(step.startedAt, lastAt);
      }
    }
  }

  const running =
    !settled
    && (
      Boolean(openReasoning)
      || Boolean(activity)
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
    activity,
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
      (callId ? findTool(steps, (tool) => tool.callId === callId && !tool.settled) : null)
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
  if (step.kind === 'tool') {
    const { settled: _settled, ...rest } = step;
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

/* ---------------------------------------------------------- live status --- */

export interface LiveActivity {
  phase: 'starting' | 'thinking' | 'tool' | 'agent' | 'writing' | 'queued' | 'waiting' | 'processing' | 'preparing';
  label: string;
  detail: string | null;
  completedSteps: number;
  startedAt?: number | null;
}

export function traceSteps(timeline: TurnTimeline): TurnStep[] {
  return timeline.steps.flatMap(function flatten(step): TurnStep[] {
    if (step.kind === 'tool') return [step, ...(step.children ?? []).flatMap(flatten)];
    if (step.kind === 'agent') return [step, ...(step.children?.steps ?? []).flatMap(flatten)];
    return [step];
  }).sort((left, right) => left.sequence - right.sequence);
}

/**
 * The one sentence that answers "what is it doing right now?". The newest unfinished step
 * wins, because that is the work the reader is actually waiting on.
 */
export function describeLiveActivity(
  timeline: TurnTimeline,
  options: { writing?: boolean } = {},
): LiveActivity {
  const steps = traceSteps(timeline);
  const modelActivity = [timeline.activity, ...steps.flatMap((step) =>
    step.kind === 'agent' && step.children?.activity ? [{ ...step.children.activity, detail: step.name }] : [])]
    .filter((value) => value !== undefined).sort((a, b) => b.sequence - a.sequence)[0];
  const completedSteps = steps.filter(
    (step) => !(step.kind === 'reasoning' && step.streaming)
      && !((step.kind === 'tool' || step.kind === 'agent') && step.status === 'running'),
  ).length;

  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const step = steps[index];
    if (modelActivity && modelActivity.sequence > step.sequence) return { ...modelActivity, completedSteps };
    if (step.kind === 'tool' && step.status === 'running') {
      return {
        phase: 'tool',
        label: `Using ${humanizeToolName(step.name)}`,
        detail: step.query ? truncate(prettyTarget(step.query), 90) : step.detail,
        completedSteps,
        startedAt: step.startedAt,
      };
    }
    if (step.kind === 'agent' && step.status === 'running') {
      return {
        phase: 'agent',
        label: step.output ? `${step.name} is writing` : `Running agent ${step.name}`,
        detail: null,
        completedSteps,
        startedAt: step.startedAt,
      };
    }
    if (step.kind === 'reasoning' && step.streaming) {
      return { phase: 'thinking', label: 'Thinking', detail: null, completedSteps, startedAt: step.startedAt };
    }
  }

  if (modelActivity) return { ...modelActivity, completedSteps };
  if (options.writing) {
    return { phase: 'writing', label: 'Writing the answer', detail: null, completedSteps };
  }
  const lastTool = [...timeline.steps].reverse().find((step) => step.kind === 'tool');
  return {
    phase: 'starting',
    label: completedSteps ? 'Working' : 'Starting the run',
    detail: lastTool && lastTool.kind === 'tool' ? `Finished ${humanizeToolName(lastTool.name)}` : null,
    completedSteps,
  };
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
