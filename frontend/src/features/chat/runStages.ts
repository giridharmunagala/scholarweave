import type { Run } from './api';
import type { RunStreamEvent } from '../../api/events';
import { humanizeToolName } from './chatTimeline';

export interface RunStage {
  id: string;
  label: string;
  status: 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'superseded';
  startedAt: number | null;
  finishedAt: number | null;
}

const PHASE_LABELS: Record<string, string> = {
  queued: 'Waiting in queue',
  waiting: 'Waiting for model',
  processing: 'Processing context',
  thinking: 'Thinking',
  tool: 'Preparing a tool',
  writing: 'Writing the answer',
};

function timestamp(value: string | null | undefined): number | null {
  const parsed = value ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
}

/** Replay the quiet TUI stage ledger without exposing reasoning or tool payloads. */
export function buildRunStages(
  run: Pick<Run, 'status' | 'started_at' | 'created_at' | 'finished_at'> & { events: readonly RunStreamEvent[] },
): RunStage[] {
  const stages: RunStage[] = [];
  const active = new Map<string, RunStage>();
  const models = new Set<string>();
  const start = (key: string, label: string, at: number | null) => {
    if (active.has(key)) return;
    const stage: RunStage = {
      id: `${key}:${stages.length}`, label, status: 'running', startedAt: at, finishedAt: null,
    };
    stages.push(stage);
    active.set(key, stage);
  };
  const finish = (key: string, at: number | null, status: RunStage['status'] = 'completed', label?: string) => {
    if (!active.has(key) && label) start(key, label, at);
    const stage = active.get(key);
    if (!stage) return;
    stage.status = status;
    stage.finishedAt = at;
    if (label) stage.label = label;
    active.delete(key);
  };
  const events = [...new Map(run.events.map((event) => [event.sequence, event])).values()]
    .sort((a, b) => a.sequence - b.sequence);
  for (const event of events) {
    const { event_type: kind, payload } = event;
    const at = timestamp(event.created_at);
    const delegated = payload.delegated === true;
    if (!delegated && (kind === 'run.started' || kind === 'run.recovered')) {
      start('context', 'Preparing context', at);
    } else if (!delegated && kind === 'context.prepared') {
      start('context', 'Preparing context', timestamp(run.started_at ?? run.created_at) ?? at);
      finish('context', at, 'completed', 'Context prepared');
    } else if (!delegated && kind === 'model.phase' && typeof payload.model_call_id === 'string') {
      const key = `model:${payload.model_call_id}`;
      finish(key, at);
      models.delete(key);
      const label = typeof payload.phase === 'string' ? PHASE_LABELS[payload.phase] : undefined;
      if (label) {
        start(key, label, at);
        models.add(key);
      }
    } else if (!delegated && kind === 'model.completed' && typeof payload.model_call_id === 'string') {
      const key = `model:${payload.model_call_id}`;
      finish(key, at);
      models.delete(key);
    } else if (kind.startsWith('tool.') && typeof payload.tool_name === 'string') {
      const key = `tool:${String(payload.invocation_id ?? '')}:${String(payload.tool_call_id ?? payload.tool_name)}`;
      const label = humanizeToolName(payload.tool_name);
      if (kind === 'tool.started') {
        if (!delegated) {
          for (const model of models) finish(model, at);
          models.clear();
        }
        start(key, label, at);
      } else if (kind === 'tool.completed' || kind === 'tool.failed') {
        finish(key, at, kind === 'tool.failed' ? 'failed' : 'completed', label);
      }
    } else if (delegated && typeof payload.agent_name === 'string') {
      const key = `agent:${String(payload.invocation_id ?? payload.agent_name)}`;
      const label = `Delegated to ${payload.agent_name}`;
      if (kind === 'agent.started') start(key, label, at);
      else if (kind === 'agent.completed') finish(key, at, 'completed', label);
      else if (kind === 'agent.failed') finish(key, at, 'failed', label);
      else if (kind === 'agent.superseded') finish(key, at, 'superseded', label);
    }
    if (!delegated && ['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(kind)) {
      const status = kind === 'run.completed' ? 'completed' : kind === 'run.failed' ? 'failed'
        : kind === 'run.cancelled' ? 'cancelled' : 'interrupted';
      for (const key of active.keys()) finish(key, at, status);
      models.clear();
    }
  }
  if (run.status !== 'running' && run.status !== 'pending') {
    const status = run.status === 'completed' ? 'completed' : run.status === 'failed' ? 'failed'
      : run.status === 'cancelled' ? 'cancelled' : 'interrupted';
    const at = timestamp(run.finished_at) ?? timestamp(events[events.length - 1]?.created_at);
    for (const key of active.keys()) finish(key, at, status);
  } else if (!stages.length) {
    start('starting', 'Starting', timestamp(run.started_at ?? run.created_at));
  }
  return stages;
}
