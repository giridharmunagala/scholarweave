import type { RunStreamEvent } from '../../api/events';
import type { Run } from './api';

type Json = Record<string, unknown>;
export type WorkItemStatus = 'pending' | 'in_progress' | 'completed' | 'blocked';
export type WorkerStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'superseded' | 'interrupted';

export interface SessionTokenTotals {
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  complete: boolean;
  estimated: boolean;
}

export interface SessionRate {
  tokensPerSecond: number | null;
  tokens: number;
  seconds: number;
  timedCalls: number | null;
  complete: boolean;
}

export interface SessionPerformance {
  modelCalls: number | null;
  callsComplete: boolean;
  mainModelCalls: number | null;
  delegatedModelCalls: number | null;
  prefill: SessionRate;
  generation: SessionRate;
}

export interface SessionWorker {
  id: string;
  runId: string;
  invocationId: string | null;
  name: string;
  delegated: boolean;
  request: string | null;
  requestTruncated: boolean;
  status: WorkerStatus;
  phase: string;
  model: string | null;
  startedAt: number | null;
  finishedAt: number | null;
  elapsedSeconds: number | null;
  completedTools: number;
  completedModels: number;
  error: string | null;
}

export interface SessionWorkItem {
  id: string;
  runId: string;
  title: string;
  status: WorkItemStatus;
  notes: string | null;
}

export interface SessionModelCall {
  id: string;
  agentName: string;
  scope: string;
  model: string | null;
  complete: boolean;
  raw: Json;
}

export interface SessionRunSummary {
  id: string;
  label: string;
  status: Run['status'] | 'interrupted';
  totals: SessionTokenTotals;
  performance: SessionPerformance;
  errors: string[];
  workers: SessionWorker[];
  workPlan: SessionWorkItem[];
  calls: SessionModelCall[];
}

export interface SessionObservabilitySummary {
  totals: SessionTokenTotals;
  performance: SessionPerformance;
  runs: SessionRunSummary[];
  workers: SessionWorker[];
  completedWorkers: number;
  activeWorkers: number;
  workPlan: SessionWorkItem[];
  workPlanCounts: Record<WorkItemStatus, number>;
  activeRuns: number;
}

function object(value: unknown): Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Json : {};
}

function number(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function timestamp(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function sumKnown(values: (number | null)[]): number | null {
  const known = values.filter((value): value is number => value !== null);
  return known.length ? known.reduce((sum, value) => sum + value, 0) : values.length ? null : 0;
}

function eventsOf(events: readonly RunStreamEvent[]): RunStreamEvent[] {
  return [...new Map(events.map((event) => [event.sequence, event])).values()]
    .sort((left, right) => left.sequence - right.sequence);
}

function isTerminal(status: string): boolean {
  return status !== 'running' && status !== 'pending';
}

function telemetrySnapshot(calls: SessionModelCall[]): Json | null {
  if (!calls.length) return null;
  const result: Json = {
    model_calls: calls.length, main_model_calls: 0, delegated_model_calls: 0,
    input_tokens: 0, output_tokens: 0, usage_complete: true, timing_source: 'server',
    timed_prompt_tokens: 0, timed_output_tokens: 0, prompt_seconds: 0, generation_seconds: 0,
    prompt_timed_calls: 0, generation_timed_calls: 0,
  };
  const add = (key: string, amount: number) => { result[key] = Number(result[key]) + amount; };
  for (const call of calls) {
    const usage = object(call.raw.usage);
    add('input_tokens', number(usage.input_tokens) ?? 0);
    add('output_tokens', number(usage.output_tokens) ?? 0);
    result.usage_complete = result.usage_complete === true && call.raw.usage_complete === true;
    if (call.raw.delegated === true || call.scope === 'delegate') add('delegated_model_calls', 1);
    else if (call.scope === 'main') add('main_model_calls', 1);
    const timings = object(call.raw.timings);
    for (const [tokensKey, millisKey, tokensTotal, secondsTotal, countKey] of [
      ['prompt_n', 'prompt_ms', 'timed_prompt_tokens', 'prompt_seconds', 'prompt_timed_calls'],
      ['predicted_n', 'predicted_ms', 'timed_output_tokens', 'generation_seconds', 'generation_timed_calls'],
    ]) {
      const tokens = number(timings[tokensKey]);
      const millis = number(timings[millisKey]);
      if (tokens !== null && millis !== null && millis > 0) {
        add(tokensTotal, tokens);
        add(secondsTotal, millis / 1000);
        add(countKey, 1);
      }
    }
  }
  return result;
}

function rate(performance: Json, phase: 'prompt' | 'generation', modelCalls: number | null): SessionRate {
  const tokens = number(performance[phase === 'prompt' ? 'timed_prompt_tokens' : 'timed_output_tokens']);
  const seconds = number(performance[`${phase}_seconds`]);
  const valid = performance.timing_source === 'server' && tokens !== null && seconds !== null && seconds > 0;
  const timedCalls = valid ? number(performance[`${phase}_timed_calls`]) : 0;
  return {
    tokensPerSecond: valid ? tokens / seconds : null,
    tokens: valid ? tokens : 0,
    seconds: valid ? seconds : 0,
    timedCalls,
    complete: valid && modelCalls !== null && timedCalls !== null && timedCalls >= modelCalls,
  };
}

function metrics(run: Run, events: RunStreamEvent[], calls: SessionModelCall[]) {
  let usage = object(run.usage);
  let performance = object(usage.performance);
  // Every performance payload is a cumulative snapshot. A stale replay must never
  // replace a persisted snapshot that already contains more model calls.
  const consider = (candidate: unknown) => {
    const next = object(candidate);
    if (Object.keys(next).length
      && (number(next.model_calls) ?? -1) >= (number(performance.model_calls) ?? -1)) {
      performance = next;
    }
  };
  for (const event of events) {
    if (event.event_type === 'usage.updated') consider(event.payload.performance);
    if (['run.completed', 'run.failed', 'run.cancelled'].includes(event.event_type)) {
      const terminalUsage = object(event.payload.usage);
      if (Object.keys(terminalUsage).length) usage = terminalUsage;
      consider(terminalUsage.performance);
    }
  }
  const fromCalls = telemetrySnapshot(calls);
  const hasLegacyUsage = !Object.keys(performance).length
    && [usage.input_tokens, usage.output_tokens, usage.total_tokens].some((value) => number(value) !== null);
  // A native-only replay can omit older lifecycle usage. It must not replace
  // authoritative cumulative spend, even when it contains more call records.
  if (fromCalls && !Object.keys(performance).length && !hasLegacyUsage) {
    performance = fromCalls;
  }
  const hasPerformance = Object.keys(performance).length > 0;
  const source = hasPerformance ? performance : usage;
  const inputTokens = number(source.input_tokens);
  const outputTokens = number(source.output_tokens);
  const totalTokens = inputTokens !== null && outputTokens !== null
    ? inputTokens + outputTokens : hasPerformance ? null : number(usage.total_tokens);
  const modelCalls = number(performance.model_calls) ?? number(usage.requests) ?? number(fromCalls?.model_calls);
  const timingPerformance = hasPerformance ? performance : fromCalls ?? {};
  const callsComplete = modelCalls !== null && (!hasLegacyUsage || number(usage.requests) !== null);
  return {
    totals: {
      inputTokens, outputTokens, totalTokens,
      complete: source.usage_complete === true && inputTokens !== null && outputTokens !== null,
      estimated: source.input_tokens_estimated === true || source.output_tokens_estimated === true,
    },
    performance: {
      modelCalls,
      callsComplete,
      mainModelCalls: number(performance.main_model_calls),
      delegatedModelCalls: number(performance.delegated_model_calls),
      prefill: rate(timingPerformance, 'prompt', callsComplete ? modelCalls : null),
      generation: rate(timingPerformance, 'generation', callsComplete ? modelCalls : null),
    },
  };
}

function workItems(value: unknown, runId: string): SessionWorkItem[] | null {
  const state = object(value);
  const rawItems = Array.isArray(state.items) ? state.items
    : Array.isArray(state.work_plan) ? state.work_plan : null;
  if (rawItems === null) return null;
  const items = new Map<string, SessionWorkItem>();
  for (const raw of rawItems) {
    const item = object(raw);
    const id = text(item.id);
    const title = text(item.title);
    const status = text(item.status);
    if (!id || !title || !['pending', 'in_progress', 'completed', 'blocked'].includes(status ?? '')) continue;
    items.set(id, {
      id, runId, title, status: status as WorkItemStatus,
      notes: text(item.summary) ?? text(item.notes),
    });
  }
  return [...items.values()];
}

function projectWorkPlan(run: Run, events: RunStreamEvent[]): SessionWorkItem[] {
  let items = workItems(run.goal_state, run.id) ?? [];
  let version = number(object(run.goal_state).version) ?? -1;
  for (const event of events) {
    const payload = event.payload;
    if (['goal.plan.updated', 'goal.blocked', 'goal.completed'].includes(event.event_type)) {
      const eventVersion = number(payload.version) ?? -1;
      const next = workItems(payload, run.id) ?? workItems(payload.goal_state, run.id);
      if (next && eventVersion >= version) {
        items = next;
        version = eventVersion;
      }
    }
    if (event.event_type === 'tool.completed'
      && ['create_work_plan', 'read_work_plan', 'update_work_item'].includes(String(payload.tool_name))) {
      const next = workItems(payload.result, run.id);
      if (next) items = next;
    }
  }
  return items;
}

function projectWorkers(run: Run, events: RunStreamEvent[]): SessionWorker[] {
  type MutableWorker = SessionWorker & {
    tools: Map<string, string>;
    models: Set<string>;
    finishedModelCalls: Set<string>;
  };
  const workers = new Map<string, MutableWorker>();
  const isDelegatedEvent = (payload: Json) => payload.delegated === true
    || payload.context_scope === 'delegate'
    || Boolean(text(payload.parent_invocation_id))
    || Boolean(text(payload.parent_agent_name));
  // The run stores the blueprint name, not necessarily the coordinator's name.
  const rootName = text(events.find((event) =>
    event.event_type === 'agent.started' && !isDelegatedEvent(event.payload))?.payload.agent_name)
    ?? run.agent_name;
  const match = (payload: Json): MutableWorker | undefined => {
    const invocation = text(payload.invocation_id);
    if (invocation) return workers.get(invocation);
    const active = [...workers.values()].filter((worker) =>
      worker.status === 'running' && worker.name === text(payload.agent_name));
    // Old events without invocation IDs cannot safely identify concurrent workers.
    return active.length === 1 ? active[0] : undefined;
  };
  let terminalAt = timestamp(run.finished_at);
  let terminalStatus: string = run.status;
  const settleActive = (status: string, at: number | null) => {
    for (const worker of workers.values()) {
      if (worker.status !== 'running') continue;
      worker.status = status === 'failed' ? 'failed'
        : status === 'cancelled' ? 'cancelled' : 'interrupted';
      worker.phase = 'Stopped with run';
      worker.finishedAt = at;
    }
  };
  for (const event of events) {
    const payload = event.payload;
    const at = timestamp(event.created_at);
    if (['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(event.event_type)) {
      terminalAt = at ?? terminalAt;
      terminalStatus = event.event_type.slice(4);
      settleActive(terminalStatus, terminalAt);
    } else if (['run.started', 'run.recovered'].includes(event.event_type)) {
      terminalStatus = 'running';
      terminalAt = null;
    }
    if (event.event_type === 'agent.started') {
      const invocationId = text(payload.invocation_id);
      const key = invocationId ?? `legacy-${event.sequence}`;
      if (workers.has(key)) continue;
      const name = text(payload.agent_name) ?? 'Agent';
      const delegated = isDelegatedEvent(payload)
        || (payload.delegated !== false && name !== rootName);
      workers.set(key, {
        id: `${run.id}:${key}`, runId: run.id, invocationId, name,
        delegated,
        request: text(payload.assignment) ?? (!delegated ? text(run.input) : null),
        requestTruncated: payload.assignment_truncated === true,
        status: 'running', phase: 'Starting', model: text(payload.model),
        startedAt: at, finishedAt: null, elapsedSeconds: null,
        completedTools: 0, completedModels: 0, error: null,
        tools: new Map(), models: new Set(), finishedModelCalls: new Set(),
      });
      continue;
    }
    const worker = match(payload);
    if (!worker || worker.status !== 'running') continue;
    if (['agent.completed', 'agent.failed', 'agent.superseded'].includes(event.event_type)) {
      worker.status = event.event_type.slice(6) as WorkerStatus;
      worker.finishedAt = at;
      worker.error = text(payload.error);
      worker.phase = worker.status === 'completed' ? 'Finished' : worker.status;
    } else if (event.event_type === 'tool.started') {
      worker.tools.set(text(payload.tool_call_id) ?? `tool-${event.sequence}`, text(payload.tool_name) ?? 'Tool');
    } else if (['tool.completed', 'tool.failed'].includes(event.event_type)) {
      const callId = text(payload.tool_call_id);
      if (callId && worker.tools.delete(callId) && event.event_type === 'tool.completed') worker.completedTools += 1;
    } else if (event.event_type === 'model.started') {
      worker.models.add(text(payload.model_call_id) ?? 'model');
      worker.model = text(payload.model) ?? worker.model;
    } else if (event.event_type === 'model.completed') {
      const callId = text(payload.model_call_id) ?? 'model';
      if (worker.models.delete(callId)) worker.completedModels += 1;
    } else if (event.event_type === 'model.telemetry') {
      worker.model = text(payload.model) ?? worker.model;
      const callId = text(payload.model_call_id);
      if (callId && payload.completed === true) worker.finishedModelCalls.add(callId);
    } else if (event.event_type === 'model.retry') {
      worker.phase = 'Retrying model';
    } else if (event.event_type === 'context.compaction_started') {
      worker.phase = 'Compacting context';
    } else if (['context.compacted', 'context.compaction_failed'].includes(event.event_type)) {
      worker.phase = 'Working';
    }
    if (worker.status === 'running' && !['model.retry', 'context.compaction_started'].includes(event.event_type)) {
      worker.phase = worker.tools.size ? `Tool: ${[...worker.tools.values()].join(', ')}`
        : worker.models.size ? 'Model call' : 'Working';
    }
  }
  // A terminal snapshot can be available before its corresponding SSE event.
  if (isTerminal(run.status)) {
    settleActive(run.status, timestamp(run.finished_at) ?? terminalAt);
  } else if (isTerminal(terminalStatus)) {
    settleActive(terminalStatus, terminalAt);
  }
  return [...workers.values()].map(({ tools: _tools, models: _models, finishedModelCalls, ...worker }) => {
    worker.completedModels = Math.max(worker.completedModels, finishedModelCalls.size);
    if (worker.startedAt !== null && worker.finishedAt !== null) {
      worker.elapsedSeconds = Math.max(0, (worker.finishedAt - worker.startedAt) / 1000);
    }
    return worker;
  });
}

function summarizeRun(run: Run): SessionRunSummary {
  const events = eventsOf(run.events);
  const calls = new Map<string, SessionModelCall>();
  const errors = new Set<string>();
  if (run.error) errors.add(run.error);
  for (const event of events) {
    if (event.event_type === 'model.telemetry') {
      const id = text(event.payload.model_call_id);
      if (id && !calls.has(id)) calls.set(id, {
        id, agentName: text(event.payload.agent_name) ?? 'Model',
        scope: text(event.payload.context_scope) ?? (event.payload.delegated === true ? 'delegate' : 'main'),
        model: text(event.payload.model), complete: event.payload.usage_complete === true, raw: event.payload,
      });
    }
    if (event.event_type.endsWith('.failed') || event.event_type === 'run.interrupted') {
      const error = text(event.payload.error) ?? text(event.payload.message) ?? text(event.payload.reason);
      if (error) errors.add(error);
    }
  }
  const callList = [...calls.values()];
  const lifecycleEvent = [...events].reverse().find((event) =>
    ['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted', 'run.started', 'run.recovered'].includes(event.event_type));
  const lifecycleStatus = lifecycleEvent?.event_type.slice(4);
  const eventStatus = lifecycleStatus === 'started' || lifecycleStatus === 'recovered' ? 'running'
    : lifecycleStatus === 'completed' || lifecycleStatus === 'failed'
      || lifecycleStatus === 'cancelled' || lifecycleStatus === 'interrupted'
      ? lifecycleStatus : run.status;
  return {
    id: run.id, label: run.agent_name, status: isTerminal(run.status) ? run.status
      : eventStatus,
    ...metrics(run, events, callList),
    errors: [...errors], workers: projectWorkers(run, events),
    workPlan: projectWorkPlan(run, events), calls: callList,
  };
}

function sumRates(rates: SessionRate[], modelCalls: number | null): SessionRate {
  const tokens = rates.reduce((sum, value) => sum + value.tokens, 0);
  const seconds = rates.reduce((sum, value) => sum + value.seconds, 0);
  const timedCalls = rates.every((value) => value.timedCalls !== null)
    ? rates.reduce((sum, value) => sum + (value.timedCalls ?? 0), 0) : null;
  return {
    tokens, seconds, timedCalls,
    tokensPerSecond: seconds > 0 ? tokens / seconds : null,
    complete: rates.length > 0 && rates.every((value) => value.complete)
      && modelCalls !== null && timedCalls !== null && timedCalls >= modelCalls,
  };
}

export function buildSessionObservability(runs: readonly Run[]): SessionObservabilitySummary {
  const uniqueRuns = new Map<string, Run>();
  for (const run of runs) {
    const previous = uniqueRuns.get(run.id);
    if (!previous) uniqueRuns.set(run.id, run);
    else {
      const latest = isTerminal(previous.status) && !isTerminal(run.status) ? previous : run;
      const oldCalls = number(object(object(previous.usage).performance).model_calls) ?? -1;
      const newCalls = number(object(object(run.usage).performance).model_calls) ?? -1;
      const latestUsage = Object.keys(object(latest.usage)).length ? latest.usage
        : latest === run ? previous.usage : run.usage;
      uniqueRuns.set(run.id, {
        ...latest,
        usage: newCalls === oldCalls ? latestUsage : newCalls > oldCalls ? run.usage : previous.usage,
        events: eventsOf([...previous.events, ...run.events]) as Run['events'],
      });
    }
  }
  const summaries = [...uniqueRuns.values()]
    .sort((left, right) => left.created_at.localeCompare(right.created_at))
    .map(summarizeRun);
  const workers = summaries.flatMap((run) => run.workers);
  const workPlan = summaries.flatMap((run) => run.workPlan);
  const modelCalls = sumKnown(summaries.map((run) => run.performance.modelCalls));
  const workPlanCounts: Record<WorkItemStatus, number> = { pending: 0, in_progress: 0, completed: 0, blocked: 0 };
  for (const item of workPlan) workPlanCounts[item.status] += 1;
  return {
    totals: {
      inputTokens: sumKnown(summaries.map((run) => run.totals.inputTokens)),
      outputTokens: sumKnown(summaries.map((run) => run.totals.outputTokens)),
      totalTokens: sumKnown(summaries.map((run) => run.totals.totalTokens)),
      complete: summaries.every((run) => run.totals.complete),
      estimated: summaries.some((run) => run.totals.estimated),
    },
    performance: {
      modelCalls,
      callsComplete: summaries.every((run) => run.performance.callsComplete),
      mainModelCalls: sumKnown(summaries.map((run) => run.performance.mainModelCalls)),
      delegatedModelCalls: sumKnown(summaries.map((run) => run.performance.delegatedModelCalls)),
      prefill: sumRates(summaries.map((run) => run.performance.prefill), modelCalls),
      generation: sumRates(summaries.map((run) => run.performance.generation), modelCalls),
    },
    runs: summaries, workers,
    completedWorkers: workers.filter((worker) => worker.delegated && worker.status === 'completed').length,
    activeWorkers: workers.filter((worker) => worker.delegated && worker.status === 'running').length,
    workPlan, workPlanCounts,
    activeRuns: summaries.filter((run) => !isTerminal(run.status)).length,
  };
}
