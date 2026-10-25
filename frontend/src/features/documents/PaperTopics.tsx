import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Icon } from '../../shared/components/Icons';
import { ModelSelect, type ModelSelectValue } from '../../shared/components/ModelSelect';
import { ErrorNotice, Loading } from '../../shared/components/Ui';
import { capabilityOptions } from '../providers/ModelDefaultsPanel';
import { defaultSmallTaskModel, providersApi, type Provider } from '../providers/api';

export type TopicMap = components['schemas']['PaperTopicMapResponse'];
type Paper = { id: string; title: string };

export const MIN_TOPIC_PAPERS = 8;
const NAMER_KEY = 'scholarweave.topicNamer';

export const topicsApi = {
  get: (folderId: string | null) =>
    request<TopicMap | null>(`/paper-topics${folderId ? `?folder_id=${encodeURIComponent(folderId)}` : ''}`),
  build: (folderId: string | null, namingModel: ModelSelectValue) =>
    request<TopicMap>('/paper-topics', json('POST', { folder_id: folderId, naming_model: namingModel })),
};

function storedNamer(): ModelSelectValue | null {
  try {
    const value = JSON.parse(localStorage.getItem(NAMER_KEY) ?? 'null') as ModelSelectValue | null;
    return value?.model ? value : null;
  } catch {
    return null;
  }
}

/** Papers grouped into named topic shelves; new papers wait in "Not yet grouped" until regrouped. */
export function PaperTopicsView<P extends Paper>({ folderId, papers, renderRow }: {
  folderId: string | null;
  papers: P[];
  renderRow: (paper: P) => ReactNode;
}) {
  const [map, setMap] = useState<TopicMap | null>(null);
  const [loading, setLoading] = useState(true);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [namer, setNamer] = useState<ModelSelectValue>(() => storedNamer() ?? {});
  const [defaultNamer, setDefaultNamer] = useState<ModelSelectValue>({});
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
  }, [folderId]);

  useEffect(() => {
    let active = true;
    Promise.all([providersApi.list(), providersApi.settings()]).then(([items, settings]) => {
      if (!active) return;
      setProviders(items);
      setDefaultNamer(defaultSmallTaskModel(settings));
    }).catch((reason) => { if (active) setError(reason); });
    return () => { active = false; };
  }, []);

  const byId = useMemo(() => new Map(papers.map((paper) => [paper.id, paper])), [papers]);
  const grouped = useMemo(() => new Set(map?.topics.flatMap((topic) => topic.paper_ids) ?? []), [map]);
  const ungrouped = papers.filter((paper) => !grouped.has(paper.id));

  const build = async () => {
    setBuilding(true);
    setError(null);
    try {
      if (namer.model) localStorage.setItem(NAMER_KEY, JSON.stringify(namer));
      else localStorage.removeItem(NAMER_KEY);
      setMap(await topicsApi.build(folderId, namer.model ? namer : {}));
    } catch (reason) {
      setError(reason);
    } finally {
      setBuilding(false);
    }
  };

  const controls = (
    <div className="topic-controls">
      <ModelSelect
        options={capabilityOptions(providers, 'chat')}
        value={namer}
        emptyOptionLabel="Small tasks default"
        emptyOptionHint={defaultNamer.model ?? 'Uses the main model when no helper is configured'}
        onChange={setNamer}
        ariaLabel="Topic naming model"
        disabled={building}
      />
      <button className="button small" type="button" disabled={building} onClick={() => void build()}>
        {building ? <><span className="button-spinner" aria-hidden="true" />Grouping…</> : <><Icon name="sparkle" size={13} />{map ? 'Regroup' : 'Group by topic'}</>}
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
            <span className="muted">Papers are embedded locally, clustered, and each cluster is named by the model you choose.</span>
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
          {(() => {
            const topics = [...map.topics.map((topic) => ({ ...topic, papers: topic.paper_ids.flatMap((id) => byId.get(id) ?? []) })),
              ...(ungrouped.length ? [{ id: 'ungrouped', name: 'Not yet grouped', description: 'Added since the last grouping. Regroup to place them.', paper_ids: [], papers: ungrouped }] : []),
            ].filter((topic) => topic.papers.length);
            const active = topics.find((topic) => topic.id === openTopic);
            if (active) return (
              <section className="topic-detail" aria-label={active.name}>
                <div className="topic-detail-head">
                  <button className="button small secondary" type="button" onClick={() => setOpenTopic(null)}>
                    <Icon name="arrowRight" size={13} className="flip" />All topics
                  </button>
                  <strong>{active.name}</strong>
                  <small className="muted">{active.papers.length} papers ? {active.description}</small>
                </div>
                <div className="paper-table">{active.papers.map(renderRow)}</div>
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
