import { useState } from 'react';
import type { RunStreamEvent } from '../../api/events';
import { Link } from '../../app/router';
import { copyToClipboard } from '../../shared/clipboard';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';

interface WorkTask {
  id: string;
  title: string;
  status: 'pending' | 'in_progress' | 'completed' | 'blocked';
  summary?: string | null;
}

interface WorkNote {
  note_id: string;
  task_id: string;
  title: string;
  summary: string;
  content?: string;
  workspace_path?: string;
}

export function ExtendedWorkPanel({
  events,
  sidebar = false,
}: {
  events: readonly RunStreamEvent[];
  sidebar?: boolean;
}) {
  let tasks: WorkTask[] = [];
  const notes: WorkNote[] = [];
  for (const event of events) {
    if (event.event_type === 'extended.plan.updated' && Array.isArray(event.payload.tasks)) {
      tasks = event.payload.tasks.filter(isWorkTask);
    }
    if (event.event_type === 'goal.plan.updated' && Array.isArray(event.payload.steps)) {
      tasks = event.payload.steps.filter(isWorkTask);
    }
    if (event.event_type === 'extended.note.saved' && isWorkNote(event.payload)) {
      notes.push(event.payload);
    }
  }
  if (!tasks.length) return null;
  const complete = tasks.filter((task) => task.status === 'completed').length;
  const remaining = tasks.length - complete;
  const percent = Math.round(100 * complete / tasks.length);
  const body = (
    <div className="extended-work-body">
      <div
        className="extended-work-progress"
        role="progressbar"
        aria-label="Work plan completion"
        aria-valuemin={0}
        aria-valuemax={tasks.length}
        aria-valuenow={complete}
      >
        <span style={{ width: `${percent}%` }} />
      </div>
      {tasks.map((task) => {
        const taskNotes = notes.filter((candidate) => candidate.task_id === task.id);
        const latestNote = taskNotes[taskNotes.length - 1];
        return (
          <div className={`extended-work-item ${task.status}`} key={task.id}>
            <span
              className="extended-work-status"
              aria-label={workStatusLabel(task.status)}
              title={workStatusLabel(task.status)}
            >
              {task.status === 'in_progress'
                ? <span className="spinner tiny" aria-hidden="true" />
                : task.status === 'completed'
                  ? <Icon name="check" size={13} />
                  : task.status === 'blocked'
                    ? '!'
                    : <span className="extended-work-pending-dot" aria-hidden="true" />}
            </span>
            <div>
              <strong>{task.title}</strong>
              {task.summary ? <small>{task.summary}</small> : null}
              {!task.summary && latestNote?.summary ? <small>{latestNote.summary}</small> : null}
              {taskNotes.map((note) => (
                note.content
                  ? <WorkNoteCard content={note.content} note={note} key={note.note_id} />
                  : null
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );

  if (sidebar) {
    return (
      <section className="extended-work sidebar" aria-label="Work plan">
        <header className="extended-work-head">
          <Icon name="agents" size={15} />
          <strong>Work plan</strong>
          <span>{complete} done · {remaining} left</span>
        </header>
        {body}
      </section>
    );
  }

  return (
    <details className="extended-work" open={complete < tasks.length}>
      <summary>
        <Icon name="agents" size={15} />
        <strong>Work plan</strong>
        <span>{complete} done · {remaining} left</span>
      </summary>
      {body}
    </details>
  );
}

function workStatusLabel(status: WorkTask['status']): string {
  if (status === 'in_progress') return 'In progress';
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function WorkNoteCard({ note, content }: { note: WorkNote; content: string }) {
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle');
  const copy = async () => {
    try {
      await copyToClipboard(content);
      setCopyStatus('copied');
      window.setTimeout(() => setCopyStatus('idle'), 1800);
    } catch {
      setCopyStatus('failed');
    }
  };
  const copyLabel =
    copyStatus === 'copied' ? 'Copied' : copyStatus === 'failed' ? 'Copy failed' : 'Copy note';

  return (
    <details className="extended-work-note">
      <summary>
        <Icon name="file" size={13} />
        <span>{note.title}</span>
        <small>Read note</small>
      </summary>
      <div className="extended-work-note-body">
        <div className="extended-work-note-actions">
          {note.workspace_path ? (
            <Link
              className="button secondary small"
              to={`/workspace?path=${encodeURIComponent(note.workspace_path)}`}
            >
              <Icon name="workspace" size={13} />
              Open in Files
            </Link>
          ) : null}
          <button
            type="button"
            className={`button secondary small extended-work-note-copy${copyStatus === 'failed' ? ' failed' : ''}`}
            aria-label={copyLabel}
            title={copyLabel}
            onClick={() => void copy()}
          >
            <Icon name={copyStatus === 'copied' ? 'check' : 'copy'} size={13} />
            {copyLabel}
          </button>
        </div>
        <MarkdownViewer content={content} />
      </div>
    </details>
  );
}

function isWorkTask(value: unknown): value is WorkTask {
  if (typeof value !== 'object' || value === null) return false;
  const task = value as Record<string, unknown>;
  return (
    typeof task.id === 'string'
    && typeof task.title === 'string'
    && ['pending', 'in_progress', 'completed', 'blocked'].includes(String(task.status))
  );
}

function isWorkNote(value: unknown): value is WorkNote {
  if (typeof value !== 'object' || value === null) return false;
  const note = value as Record<string, unknown>;
  return (
    typeof note.note_id === 'string'
    && typeof note.task_id === 'string'
    && typeof note.title === 'string'
    && typeof note.summary === 'string'
    && (note.content === undefined || typeof note.content === 'string')
    && (note.workspace_path === undefined || typeof note.workspace_path === 'string')
  );
}
