import { useEffect, useState, type ReactNode } from 'react';
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Icon } from '../../shared/components/Icons';
import { ErrorNotice, Loading } from '../../shared/components/Ui';

export type TopicMap = components['schemas']['PaperTopicMapResponse'];
type Paper = { id: string; title: string };

export const MIN_TOPIC_PAPERS = 8;
export const topicsApi = {
  get: (folderId: string | null) =>
    request<TopicMap | null>(`/paper-topics${folderId ? `?folder_id=${encodeURIComponent(folderId)}` : ''}`),
  build: (folderId: string | null) =>
    request<TopicMap>('/paper-topics', json('POST', { folder_id: folderId, naming_model: {} })),
  assign: (folderId: string | null, topicId: string, paperIds: string[]) =>
    request<TopicMap>('/paper-topics/assign', json('PATCH', { folder_id: folderId, topic_id: topicId, paper_ids: paperIds })),
};

/** Saved topic names and assignments survive updates; only new papers need grouping. */
export function PaperTopicsView<P extends Paper>({ folderId, revision = 0, papers, renderRow }: {
  folderId: string | null;
  revision?: number;
  papers: P[];
  renderRow: (paper: P, topicControl: ReactNode) => ReactNode;
}) {
  const [map, setMap] = useState<TopicMap | null>(null);
  const [loading, setLoading] = useState(true);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [assigning, setAssigning] = useState(false);
  const [openTopic, setOpenTopic] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    topicsApi.get(folderId)
      .then((value) => { if (active) setMap(value); })
      .catch((reason: unknown) => { if (active) setError(reason); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [folderId, revision]);

  const grouped = new Set(map?.topics.flatMap((topic) => topic.paper_ids) ?? []);
  const ungrouped = papers.filter((paper) => !grouped.has(paper.id));

  const build = async () => {
    setBuilding(true);
    setError(null);
    try {
      setMap(await topicsApi.build(folderId));
    } catch (reason) {
      setError(reason);
    } finally {
      setBuilding(false);
    }
  };

  const assign = async (paperId: string, topicId: string) => {
    setAssigning(true);
    setError(null);
    try {
      setMap(await topicsApi.assign(folderId, topicId, [paperId]));
    } catch (reason) {
      setError(reason);
    } finally {
      setAssigning(false);
    }
  };

  const controls = (
    <div className="topic-controls">
      <button className="button small" type="button" disabled={building || assigning || (map ? !ungrouped.length : papers.length < MIN_TOPIC_PAPERS)} onClick={() => void build()}>
        {building ? <><span className="button-spinner" aria-hidden="true" />Grouping…</> : <><Icon name="sparkle" size={13} />{map ? `Group ${ungrouped.length} new paper${ungrouped.length === 1 ? '' : 's'}` : 'Group by topic'}</>}
      </button>
    </div>
  );

  if (loading) return <Loading label="Loading topics…" />;
  return (
    <section className="topic-view" aria-label="Papers by topic" aria-busy={building}>
      {error ? <ErrorNotice error={error} /> : null}
      {!map ? (
        <div className="topic-empty">
          <p>
            <strong>Group these {papers.length} papers into topics.</strong>{' '}
            <span className="muted">The configured embedding model groups papers and the main model names each topic once. Available from {MIN_TOPIC_PAPERS} papers.</span>
          </p>
          {controls}
        </div>
      ) : (
        <>
          <div className="topic-summary">
            <small className="muted">
              {map.topics.length} topics · named by {map.naming_model.model ?? 'default model'} · {new Date(map.created_at).toLocaleDateString()}
            </small>
            <span className="row-spacer" />
            {controls}
          </div>
          <small className="muted">Saved topic names and assignments are kept. Group new papers to enrich these topics, or open a topic to assign a paper manually.</small>
          {(() => {
            const topics = [...map.topics.map((topic) => {
              const members = new Set(topic.paper_ids);
              return { ...topic, papers: papers.filter((paper) => members.has(paper.id)) };
            }),
              ...(ungrouped.length ? [{ id: 'ungrouped', name: 'Not yet grouped', description: 'New papers can be grouped into saved topics or assigned manually.', paper_ids: [], papers: ungrouped }] : []),
            ];
            const active = topics.find((topic) => topic.id === openTopic);
            if (active) return (
              <section className="topic-detail" aria-label={active.name}>
                <div className="topic-detail-head">
                  <button className="button small secondary" type="button" onClick={() => setOpenTopic(null)}>
                    <Icon name="arrowRight" size={13} className="flip" />All topics
                  </button>
                  <strong>{active.name}</strong>
                  <small className="muted">{active.papers.length} papers · {active.description}</small>
                </div>
                <div className="paper-table">{active.papers.map((paper) => renderRow(paper, (
                  <label className="paper-topic-assignment">
                    <span>Topic</span>
                    <select aria-label={`Assign topic for ${paper.title}`} value={active.id === 'ungrouped' ? '' : active.id}
                      disabled={building || assigning} onChange={(event) => void assign(paper.id, event.target.value)}>
                      <option value="" disabled>Assign to a topic…</option>
                      {map.topics.map((topic) => <option value={topic.id} key={topic.id}>{topic.name}</option>)}
                    </select>
                  </label>
                )))}</div>
                {!active.papers.length ? <p className="muted">No papers in this topic match the current filters.</p> : null}
              </section>
            );
            return (
              <div className="topic-grid">
                {topics.map((topic) => (
                  <button type="button" key={topic.id} className={`topic-card${topic.id === 'ungrouped' ? ' ungrouped' : ''}`} aria-label={`${topic.name}, ${topic.papers.length} papers`} onClick={() => setOpenTopic(topic.id)}>
                    <span className="topic-card-head"><strong>{topic.name}</strong><span className="topic-count">{topic.papers.length}</span></span>
                    <span className="topic-description">{topic.description}</span>
                    <ul className="topic-card-titles">
                      {topic.papers.slice(0, 3).map((paper) => <li key={paper.id}>{paper.title}</li>)}
                      {topic.papers.length > 3 ? <li className="muted">+{topic.papers.length - 3} more</li> : null}
                    </ul>
                  </button>
                ))}
              </div>
            );
          })()}
        </>
      )}
    </section>
  );
}
