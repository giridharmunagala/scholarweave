import { Link } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import type { Track } from './api';

const STATUS_ORDER = ['Completed', 'Running', 'Planned', 'Suggested', 'Blocked', 'Cancelled'] as const;
const CORE_SECTIONS = ['problem', 'literature', 'experiments', 'results'];

function relative(value: string): string {
  const days = Math.floor((Date.now() - new Date(value).getTime()) / 86_400_000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 30) return `${days} days ago`;
  return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

/** A section counts as written once it has been edited after the track's template was created. */
function writtenSections(track: Track): string[] {
  const created = new Date(track.created_at).getTime();
  return track.sections
    .filter((item) => CORE_SECTIONS.includes(item.section) && new Date(item.modified_at).getTime() - created > 5_000)
    .map((item) => item.section);
}

export function TrackOverview({ tracks }: { tracks: Track[] }) {
  if (!tracks.length) return null;
  const totals = tracks.reduce(
    (sum, track) => {
      sum.papers += track.paper_ids.length;
      sum.chats += track.conversation_ids.length;
      sum.experiments += track.experiments.length;
      sum.done += track.experiments.filter((item) => item.status === 'Completed').length;
      sum.running += track.experiments.filter((item) => item.status === 'Running').length;
      return sum;
    },
    { papers: 0, chats: 0, experiments: 0, done: 0, running: 0 },
  );
  const sorted = [...tracks].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  return (
    <section className="track-overview" aria-label="Research progress">
      <div className="track-stats">
        <div><strong>{tracks.length}</strong><span>tracks</span></div>
        <div><strong>{totals.papers}</strong><span>linked papers</span></div>
        <div><strong>{totals.done}<small>/{totals.experiments}</small></strong><span>experiments done</span></div>
        <div><strong>{totals.running}</strong><span>running now</span></div>
        <div><strong>{totals.chats}</strong><span>linked chats</span></div>
      </div>
      <div className="track-cards">
        {sorted.map((track) => {
          const written = writtenSections(track);
          const total = track.experiments.length;
          const counts = STATUS_ORDER.map((status) => [status, track.experiments.filter((item) => item.status === status).length] as const)
            .filter(([, count]) => count > 0);
          return (
            <Link key={track.id} className="track-card" to={`/tracks?id=${encodeURIComponent(track.id)}`}>
              <strong className="track-card-title">{track.title}</strong>
              <span className="track-card-meta">
                <span><Icon name="papers" size={12} />{track.paper_ids.length} papers</span>
                <span><Icon name="chat" size={12} />{track.conversation_ids.length} chats</span>
                {track.exploration?.length ? <span>{track.exploration.length} explorations</span> : null}
              </span>
              <span className="track-sections" title={`Written: ${written.join(', ') || 'none yet'}`}>
                {CORE_SECTIONS.map((section) => (
                  <span key={section} className={written.includes(section) ? 'done' : ''}>{section}</span>
                ))}
              </span>
              {total ? (
                <>
                  <span className="experiment-bar" role="img" aria-label={counts.map(([status, count]) => `${count} ${status}`).join(', ')}>
                    {counts.map(([status, count]) => (
                      <span key={status} className={`exp-${status.toLowerCase()}`} style={{ flexGrow: count }} />
                    ))}
                  </span>
                  <small className="muted">
                    {counts.map(([status, count]) => `${count} ${status.toLowerCase()}`).join(' · ')}
                  </small>
                </>
              ) : <small className="muted">No experiments planned yet</small>}
              <small className="track-card-updated">Updated {relative(track.updated_at)}</small>
              {track.warnings.length ? <small className="track-card-warning">{track.warnings.length} warning{track.warnings.length === 1 ? '' : 's'}</small> : null}
            </Link>
          );
        })}
      </div>
    </section>
  );
}
