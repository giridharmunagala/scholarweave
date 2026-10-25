import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigationGuard } from '../../app/router';
import { useAsk } from '../../app/ask';
import { ModelSelect, type ModelSelectValue } from '../../shared/components/ModelSelect';
import { ErrorNotice, IconButton } from '../../shared/components/Ui';
import { SettingsDrawer } from '../../shared/components/SettingsDrawer';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { Icon } from '../../shared/components/Icons';
import { providersApi, type Provider } from '../providers/api';
import { capabilityOptions } from '../providers/ModelDefaultsPanel';
import { IdeasPanel } from '../chat/IdeasPanel';
import { ArxivSearch } from '../documents/ArxivSearch';
import { reviewsApi, type Candidate, type PaperReview, type Review, type ReviewControl, type SummaryRole, type Understanding } from './api';
import { ReviewActivityPanel } from './ReviewActivityPanel';
import './reviews.css';

type ReviewDraft = { chosen: string[]; focus: string };
type WorkBudget = NonNullable<Review['work_budget']>;
type WorkBudgetKey = keyof WorkBudget;

const REVIEW_DRAFTS_KEY = 'scholarweave:literature-review-drafts:v1';
const DEFAULT_WORK_BUDGET: WorkBudget = { screened_papers: 100, read_papers: 20, comparison_passes: 1 };
const WORK_BUDGET_FIELDS: { key: WorkBudgetKey; label: string; cap: number }[] = [
  { key: 'screened_papers', label: 'Screened papers', cap: 5000 },
  { key: 'read_papers', label: 'Read papers', cap: 5000 },
  { key: 'comparison_passes', label: 'Comparison passes', cap: 100 },
];

function readReviewDrafts(): Record<string, ReviewDraft> {
  try {
    const stored: unknown = JSON.parse(window.sessionStorage.getItem(REVIEW_DRAFTS_KEY) ?? '{}');
    if (!stored || typeof stored !== 'object' || Array.isArray(stored)) return {};
    return Object.fromEntries(Object.entries(stored).flatMap(([id, value]) => {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
      const draft = value as Partial<ReviewDraft>;
      return [[id, {
        chosen: Array.isArray(draft.chosen) ? draft.chosen.filter((item): item is string => typeof item === 'string') : [],
        focus: typeof draft.focus === 'string' ? draft.focus : '',
      }]];
    }));
  } catch {
    return {};
  }
}

function sameModel(left: ModelSelectValue, right: ModelSelectValue | null | undefined): boolean {
  return left.provider_profile_id === right?.provider_profile_id && left.model === right?.model;
}

function modelValue(reference: ModelSelectValue | null | undefined): ModelSelectValue {
  return reference ? { provider_profile_id: reference.provider_profile_id, model: reference.model } : {};
}

/** Saved tasks are never migrated or cleared by this workspace. */
export function LiteratureReviews({ conversationId, trackId, initialModel = {}, initialObjective = '', webEnabled = true,
  ensureConversation, onReviewChange, embedded = false, selectedReview, showSetup = true,
  progress, activity, onDiscuss, onIntentChange, onWebEnabledChange, disabled = false, onBusyChange,
  settingsControl,
}: {
  conversationId?: string; trackId?: string; initialModel?: ModelSelectValue; initialObjective?: string; webEnabled?: boolean;
  ensureConversation?: (model: ModelSelectValue) => Promise<string>;
  onReviewChange?: (review: Review | undefined) => void;
  embedded?: boolean; selectedReview?: Review; showSetup?: boolean;
  progress?: ReactNode; activity?: ReactNode;
  onDiscuss?: (draft: string) => void;
  onIntentChange?: (intent: 'chat' | 'ideation') => void;
  onWebEnabledChange?: (enabled: boolean) => void;
  disabled?: boolean; onBusyChange?: (busy: boolean) => void;
  settingsControl?: { open: boolean; onOpenChange: (open: boolean) => void };
}) {
  const ask = useAsk();
  const [reviews, setReviews] = useState<Review[]>([]);
  const params = new URLSearchParams(window.location.search);
  const [id, setId] = useState(params.get('new') === '1' ? '' : params.get('review') ?? '');
  const [newDraft, setNewDraft] = useState(params.get('new') === '1');
  const [objective, setObjective] = useState(initialObjective || params.get('objective') || params.get('question') || '');
  const [main, setMain] = useState(initialModel);
  const [summary, setSummary] = useState<ModelSelectValue>({});
  const [embedding, setEmbedding] = useState<ModelSelectValue>({});
  const [localSettingsOpen, setLocalSettingsOpen] = useState(false);
  const settingsOpen = settingsControl?.open ?? localSettingsOpen;
  const setSettingsOpen = settingsControl?.onOpenChange ?? setLocalSettingsOpen;
  const [providers, setProviders] = useState<Provider[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const busy = pending || disabled;
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [papers, setPapers] = useState<PaperReview[]>([]);
  const [reviewDrafts, setReviewDrafts] = useState<Record<string, ReviewDraft>>(readReviewDrafts);
  const [paperId, setPaperId] = useState('');
  const [query, setQuery] = useState('');
  const [showPdfInput, setShowPdfInput] = useState(false);
  const [pdfUrl, setPdfUrl] = useState('');
  const [pdfTitle, setPdfTitle] = useState('');
  const [topicId, setTopicId] = useState('');
  const [selectedOnly, setSelectedOnly] = useState(false);
  const [replacementMain, setReplacementMain] = useState<ModelSelectValue>({});
  const [replacementSummary, setReplacementSummary] = useState<ModelSelectValue>({});
  const [budgetProposal, setBudgetProposal] = useState<WorkBudget>(DEFAULT_WORK_BUDGET);
  const [tab, setTab] = useState('Papers');
  const openedReview = useRef('');
  const questionInput = useRef<HTMLTextAreaElement>(null);
  const paperReader = useRef<HTMLElement>(null);
  const [understanding, setUnderstanding] = useState<Understanding | null>(null);
  const review = embedded ? selectedReview : reviews.find((item) => item.id === id);
  const draftReviewId = review?.id ?? id;
  const reviewDraft = reviewDrafts[draftReviewId] ?? { chosen: [], focus: '' };
  const chosen = reviewDraft.chosen;
  const focus = reviewDraft.focus;
  const archived = review?.reading_mode !== 'targeted';
  const paper = papers.find((item) => item.candidate_id === paperId);
  const candidate = candidates.find((item) => item.id === paperId);
  const summaryModel = summary.model || summary.provider_profile_id ? summary : main;
  const valid = !!(main.model && main.provider_profile_id && embedding.model && embedding.provider_profile_id
    && summaryModel.model && summaryModel.provider_profile_id);
  const landscapeStage = review && ['discovery', 'screening'].includes(review.stage);
  const recovering = !!review && !archived && ['paused', 'blocked'].includes(review.status);
  const workBudget = review?.work_budget ?? review?.scope?.suggested_work_budget ?? DEFAULT_WORK_BUDGET;
  const workUsed = review?.work_used ?? { screened_papers: 0, read_papers: 0, comparison_passes: 0 };
  const validBudgetProposal = WORK_BUDGET_FIELDS.every(({ key, cap }) =>
    Number.isInteger(budgetProposal[key]) && budgetProposal[key] >= Math.max(workBudget[key], workUsed[key]) &&
      budgetProposal[key] <= cap);
  const extendsExhaustedBudget = WORK_BUDGET_FIELDS.some(({ key }) =>
    workUsed[key] >= workBudget[key] && budgetProposal[key] > workBudget[key]);
  useEffect(() => {
    if (!review) {
      openedReview.current = '';
      setTab('Papers');
      return;
    }
    if (openedReview.current === review.id) return;
    openedReview.current = review.id;
    setTab(review.status === 'completed' ? 'Findings' : 'Papers');
  }, [review?.id, review?.status]);
  useEffect(() => { onBusyChange?.(pending); }, [pending, onBusyChange]);
  useNavigationGuard(!review && showSetup && !!objective.trim() || busy, 'Discard this research question? Running work will continue.', !busy);

  useEffect(() => {
    try { window.sessionStorage.setItem(REVIEW_DRAFTS_KEY, JSON.stringify(reviewDrafts)); } catch { /* Storage is optional. */ }
  }, [reviewDrafts]);

  useEffect(() => {
    if (embedded) return;
    let active = true;
    let loading = false;
    const load = async () => {
      if (loading) return;
      loading = true;
      try {
        const items = await reviewsApi.list(conversationId, trackId);
        const requested = params.get('review') || id;
        if (!newDraft && requested && !items.some((item) => item.id === requested)) items.unshift(await reviewsApi.get(requested));
        if (active) setReviews((old) => items.map((item) => {
          const previous = old.find((value) => value.id === item.id);
          return previous && previous.revision > item.revision ? previous : item;
        }));
      } catch (reason) { if (active) setError(reason); }
      finally { loading = false; }
    };
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [conversationId, trackId, embedded, newDraft, id]);

  useEffect(() => {
    if (embedded && !showSetup && !recovering) return;
    if (embedded && selectedReview && !recovering) return;
    let active = true;
    void Promise.all([providersApi.list(), providersApi.settings()]).then(([items, settings]) => {
      if (!active) return;
      setProviders(items);
      setMain((value) => value.model ? value : settings.default_model_references.chat ?? {});
      setSummary((value) => value.model || value.provider_profile_id ? value
        : settings.default_model_references.summary ?? settings.default_model_references.small_tasks ?? {});
      setEmbedding(settings.default_model_references.embedding ?? {});
    }).catch((reason) => { if (active) setError(reason); });
    return () => { active = false; };
  }, [embedded, showSetup, selectedReview?.id, recovering]);

  useEffect(() => {
    setCandidates([]); setPapers([]); setPaperId('');
    setUnderstanding(null); setQuery(''); setTopicId(''); setSelectedOnly(false);
    setShowPdfInput(false); setPdfUrl(''); setPdfTitle('');
  }, [embedded ? selectedReview?.id : id]);

  useEffect(() => {
    if (!review) return;
    setReplacementMain(modelValue(review.main_model));
    setReplacementSummary(modelValue(review.summary_model?.model_reference));
  }, [review?.id, recovering]);

  useEffect(() => {
    if (!review || review.approval !== 'budget') return;
    setBudgetProposal(review.suggested_work_budget ?? workBudget);
  }, [review?.id, review?.approval]);

  useEffect(() => {
    if (!review) return;
    let active = true;
    void Promise.all([reviewsApi.allCandidates(review.id, 'relevance'), reviewsApi.papers(review.id), reviewsApi.understanding(review.id)]).then(([page, items, comparison]) => {
      if (!active) return;
      setCandidates(page.items); setPapers(items); setUnderstanding(comparison);
      const candidateIds = new Set(page.items.map((item) => item.id));
      setReviewDrafts((old) => {
        const saved = old[review.id];
        if (!saved || saved.chosen.every((item) => candidateIds.has(item))) return old;
        return { ...old, [review.id]: { ...saved, chosen: saved.chosen.filter((item) => candidateIds.has(item)) } };
      });
    }).catch((reason) => { if (active) setError(reason); });
    return () => { active = false; };
  }, [review?.id, review?.revision]);

  const updateReviewDraft = (update: Partial<ReviewDraft>) => {
    if (!draftReviewId) return;
    setReviewDrafts((items) => ({
      ...items,
      [draftReviewId]: { ...(items[draftReviewId] ?? { chosen: [], focus: '' }), ...update },
    }));
  };

  const perform = async (operation: () => Promise<Review>) => {
    if (busy) return false;
    setPending(true); setError(null);
    try {
      const result = await operation();
      setReviews((items) => [result, ...items.filter((item) => item.id !== result.id)]);
      setId(result.id);
      setNewDraft(false);
      onReviewChange?.(result);
      return true;
    } catch (reason) { setError(reason); }
    finally { setPending(false); }
    return false;
  };
  const control = (action: ReviewControl['action'], values: Partial<ReviewControl> = {}) => {
    if (review) void perform(() => reviewsApi.control(review.id, { action, expected_revision: review.revision, ...values }));
  };
  const addPdfPaper = async () => {
    if (!review || busy || !pdfUrl.trim()) return;
    const added = await perform(() => reviewsApi.control(review.id, {
      action: 'add_pdf_paper', expected_revision: review.revision,
      pdf_paper: { url: pdfUrl.trim(), title: pdfTitle.trim() || null },
    }));
    if (added) {
      setPdfUrl(''); setPdfTitle(''); setShowPdfInput(false);
    }
  };
  const toggle = (value: string) => updateReviewDraft({
    chosen: chosen.includes(value) ? chosen.filter((item) => item !== value) : [...chosen, value],
  });
  const askAboutPaper = (draft?: string) => {
    if (!candidate?.document_id) return;
    if (onDiscuss) {
      onDiscuss([
        draft || `Help me understand "${candidate.title}".`,
        `Local paper ID: ${candidate.document_id}`,
        context,
      ].join('\n\n'));
    } else ask.ask(draft, { context: { document_id: candidate.document_id, title: candidate.title } });
  };
  const context = [
    `Research question: ${review?.objective ?? objective}`,
    `Direction: ${focus || review?.question_focus || 'Not narrowed yet'}`,
    ...candidates.filter((item) => item.id === paperId || chosen.includes(item.id)).slice(0, 6).map((item) => {
      const source = papers.find((value) => value.candidate_id === item.id);
      return `${item.title}\nAbstract (metadata, not source verification): ${(item.abstract ?? 'Unavailable').slice(0, 1200)}\n` +
        (source?.brief?.findings.slice(0, 3).map((finding) => `Verified source quote, p.${finding.page}: "${finding.quote}"`).join('\n') ?? 'Source not yet read.');
    }),
  ].join('\n\n').slice(0, 10000);

  const step = !review ? 0 : review.stage === 'scope' || review.approval === 'plan' ? 1 : landscapeStage || review.approval === 'topics' ? 2 : 3;
  const selecting = !archived && review?.approval === 'topics';
  const briefCount = papers.filter((item) => item.brief).length;
  const selectedTopic = review?.topics?.find((item) => item.id === topicId);
  const visible = candidates.filter((item) => (!selectedOnly || chosen.includes(item.id)) &&
    (!selectedTopic || selectedTopic.candidate_ids.includes(item.id)) &&
    `${item.title} ${item.abstract ?? ''}`.toLowerCase().includes(query.toLowerCase()));
  const nextStep = review ? researchNextStep(review) : null;
  const showEvidence = archived || candidates.length > 0 || step === 3 || selecting;
  const budget = review?.scope?.suggested_work_budget;
  const recoveryValues = (): Partial<ReviewControl> => {
    const values: Partial<ReviewControl> = {};
    if (replacementMain.model && replacementMain.provider_profile_id && !sameModel(replacementMain, review?.main_model)) {
      values.main_model = {
        provider_profile_id: replacementMain.provider_profile_id,
        model: replacementMain.model,
      };
    }
    if (replacementSummary.model && replacementSummary.provider_profile_id &&
      !sameModel(replacementSummary, review?.summary_model?.model_reference)) {
      const prior = review?.summary_model;
      const summary: SummaryRole = {
        model_reference: {
          provider_profile_id: replacementSummary.provider_profile_id,
          model: replacementSummary.model,
        },
        max_output_tokens: prior?.max_output_tokens ?? 4096,
        parallel_requests: prior?.parallel_requests ?? 1,
        reasoning_effort: prior?.reasoning_effort ?? null,
      };
      values.summary_model = summary;
    }
    return values;
  };

  return <section className="research-workspace" aria-label="Research workspace">
    <ErrorNotice error={error} />
    {!embedded ? <div className="research-saved">
      <details>
        <summary><span>Saved research</span><span className="research-count">{reviews.length}</span></summary>
        <p className="field-hint">Existing research and files are preserved. Full-reading legacy tasks are available as read-only history.</p>
        <ul className="research-saved-list">{reviews.map((item) => <li key={item.id}>
          <button type="button" className={item.id === review?.id ? 'is-current' : ''} onClick={() => { setId(item.id); setNewDraft(false); }}>
            <span>{item.title}</span><span className={`status-pill status-${item.status}`}>{statusText(item.status)}</span>
          </button>
        </li>)}</ul>
      </details>
      <button className="button secondary small" onClick={() => { setId(''); setNewDraft(true); }}>New paper question</button>
    </div> : null}

    {!review ? showSetup ? <form className="research-setup" onSubmit={(event) => {
      event.preventDefault();
      if (!valid || !objective.trim()) return;
      void perform(async () => reviewsApi.create({
        title: objective.trim().slice(0, 200), objective: objective.trim(), main_model: main, main_parallel_requests: 1,
        summary_model: { model_reference: summaryModel, max_output_tokens: 4096, parallel_requests: 1, reasoning_effort: null },
        embedding_model: { model_reference: embedding, batch_size: 16 }, reading_mode: 'targeted',
        input_papers: [],
        conversation_id: conversationId ?? (ensureConversation ? await ensureConversation(main) : null),
        track_id: trackId || null, web_enabled: webEnabled, openalex_enabled: false,
      }));
    }}>
      <header className="research-setup-head">
        <div>
          <h2>What do you want to understand?</h2>
          <p>Turn a research question into a comparison of evidence, with source quotes you can check.</p>
        </div>
        {!settingsControl ? <IconButton icon="settings" size={20} className="settings-trigger" visibleLabel="Settings" label="Research settings" onClick={() => setSettingsOpen(true)} /> : null}
      </header>
      <div className="research-question-box">
      <label className="research-question">Research question
        <textarea ref={questionInput} value={objective} disabled={busy} onChange={(event) => setObjective(event.target.value)} required
          placeholder="e.g. How well do calibration methods transfer across domain shift?" />
      </label>
      {!objective.trim() ? <div className="research-question-examples" aria-label="Question starters">
        <span className="field-hint">Start with a direction:</span>
        {[
          { label: 'Compare approaches', question: 'Compare the main approaches to [your topic]. How do their results and limitations differ?' },
          { label: 'Find a research gap', question: 'What is known about [your topic], and which important questions remain unanswered?' },
          { label: 'Understand a topic', question: 'Help me understand [your topic]: its main ideas, supporting evidence and open debates.' },
        ].map((example) => <button key={example.label} type="button" disabled={busy} onClick={() => {
          setObjective(example.question);
          questionInput.current?.focus();
        }}>{example.label}</button>)}
      </div> : null}
      <div className="research-submit">
        <div>
          {onWebEnabledChange ? <label className="check-row"><input type="checkbox" checked={webEnabled} disabled={busy} onChange={(event) => onWebEnabledChange(event.target.checked)} /> Allow web access</label> : null}
          <p className="field-hint">First, review a proposed scope. No papers are read yet.</p>
        </div>
        <button className="button" disabled={busy || !valid || !objective.trim()}>{pending ? 'Preparing your research...' : embedded ? 'Start research' : 'Explore the landscape'}<Icon name="arrowRight" size={16} /></button>
      </div>
      </div>
      <ol className="research-steps" aria-label="How research works">
        <li><strong>Confirm the scope</strong><span>Approve the question and work limits before the search begins.</span></li>
        <li><strong>Choose your papers</strong><span>Browse topics and abstracts. You choose which papers to read.</span></li>
        <li><strong>Build an answer</strong><span>Compare focused findings with checked source quotes, then explore gaps.</span></li>
      </ol>
      {!valid ? <div role="alert" className="research-warning">
        <p>Choose a research model and embedding model before starting.</p>
        <button type="button" className="button secondary small" onClick={() => setSettingsOpen(true)}>Open research settings</button>
      </div> : null}
    </form> : null : <>
      {!embedded ? <ResearchHeading review={review} /> : null}

      <div className="button-row research-run-controls" aria-label="Review controls">
        {!archived && !embedded && review.status === 'running' ? <button className="button secondary small" disabled={busy} onClick={() => control('pause')}>Pause</button> : null}
        {!settingsControl ? <IconButton icon="settings" size={20} className="settings-trigger" visibleLabel="Settings" label="Research settings" onClick={() => setSettingsOpen(true)} /> : null}
      </div>

      {!archived ? <ol className="research-stepper" aria-label="Research progress">
        {['Confirm scope', 'Choose papers', 'Read & compare'].map((name, index) => <li key={name}
          className={index + 1 < step ? 'is-done' : index + 1 === step ? 'is-current' : ''} aria-current={index + 1 === step ? 'step' : undefined}>
          <span className="research-stepper-dot">{index + 1 < step ? '\u2713' : index + 1}</span>{name}
        </li>)}
      </ol> : null}

      {!archived && nextStep ? <section className="research-next-step" aria-label="Next step">
        <div className="research-next-step-intro" aria-live="polite">
        <div>
          <span className="research-eyebrow">{review.status === 'running' ? 'In progress' : review.status === 'completed' ? 'Ready to explore' : 'Your next step'}</span>
          <h3>{nextStep.title}</h3>
          <p>{nextStep.description}</p>
        </div>
        {review.status === 'completed' && tab !== 'Findings' ? <button className="button" onClick={() => setTab('Findings')}>View findings</button> : null}
        {selecting && tab !== 'Papers' ? <button className="button" onClick={() => setTab('Papers')}>Choose papers</button> : null}
        {review.status === 'running' ? <span className="research-spinner" aria-hidden="true" /> : null}
        </div>

      {recovering ? <section className="research-recovery" aria-label="Resume review">
        <div className="button-row">
          <button className="button" disabled={busy} onClick={() => control('resume', recoveryValues())}>Resume research</button>
        </div>
      </section> : null}

      {review.approval === 'plan' ? <section className="research-plan">
        {review.scope?.rationale ? <p>{review.scope.rationale}</p> : null}
        {review.scope?.questions.length ? <ul>{review.scope.questions.map((question) => <li key={question}>{question}</li>)}</ul> : null}
        <dl className="research-stats" aria-label="Work limits">
          <div><dt>Papers to screen</dt><dd>{budget?.screened_papers ?? 'Not set'}</dd></div>
          <div><dt>Papers to read</dt><dd>{budget?.read_papers ?? 'Not set'}</dd></div>
          <div><dt>Comparison passes</dt><dd>{budget?.comparison_passes ?? 'Not set'}</dd></div>
        </dl>
        <div className="button-row"><button className="button" disabled={busy} onClick={() => control('approve_plan')}>Approve scope and find papers</button></div>
      </section> : null}

      {review.approval === 'budget' ? <section className="research-plan" aria-label="Extend work limits">
        <p>Choose total limits for the next batch. This approval covers all work up to those totals, so you can request several papers at once.</p>
        <dl className="research-stats" aria-label="Current work budget">
          {WORK_BUDGET_FIELDS.map(({ key, label }) => <div key={key}>
            <dt>{label}</dt><dd>{workUsed[key]} used / {workBudget[key]} current / {budgetProposal[key]} proposed</dd>
          </div>)}
        </dl>
        <div className="research-options" aria-label="Proposed work limits">
          {WORK_BUDGET_FIELDS.map(({ key, label, cap }) => <label key={key}>Proposed {label.toLowerCase()}
            <input aria-label={`Proposed ${label.toLowerCase()}`} type="number" step={1}
              min={Math.max(workBudget[key], workUsed[key])} max={cap}
              disabled={busy}
              value={Number.isFinite(budgetProposal[key]) ? budgetProposal[key] : ''}
              onChange={(event) => {
                const value = event.currentTarget.value === '' ? Number.NaN : event.currentTarget.valueAsNumber;
                setBudgetProposal((current) => ({ ...current, [key]: value }));
              }} />
          </label>)}
        </div>
        <p className="field-hint">Limits cannot be reduced below approved or used work; maxima are 5,000 papers and 100 comparison passes.</p>
        <div className="button-row"><button className="button" disabled={busy || !validBudgetProposal || !extendsExhaustedBudget}
          onClick={() => control('extend_budget', { work_budget: budgetProposal })}>Approve additional work limits</button></div>
      </section> : null}

        {selecting && tab === 'Papers' ? <section className="research-actionbar" aria-label="Reading batch">
          <label>What should the reading focus on?<input aria-label="Reading question / follow-up focus" value={focus} disabled={busy}
            onChange={(event) => updateReviewDraft({ focus: event.target.value })} placeholder={review.question_focus || review.objective} /></label>
          <div className="research-batch-submit">
            <span className="field-hint" role="status">{chosen.length} paper{chosen.length === 1 ? '' : 's'} selected</span>
            <button className="button" disabled={busy || !chosen.length} onClick={() => control('approve_topics', {
              topic_ids: (review.topics ?? []).filter((topic) => topic.candidate_ids.some((value) => chosen.includes(value))).map((topic) => topic.id),
              candidate_ids: chosen, feedback: focus || review.question_focus || review.objective, reading_mode: 'targeted',
            })}>Read selected papers ({chosen.length})</button>
          </div>
          <p className="field-hint research-batch-hint">Only focused passages are checked; references are not added automatically.</p>
        </section> : null}
      </section> : <p role="note" className="research-warning">Archived full-reading workspace (read-only). Start a new question for focused research.</p>}

      {progress}

      {showEvidence ? <>
      <nav className="segmented research-tabs" aria-label="Research views">{(embedded ? ['Papers', 'Findings'] : ['Papers', 'Findings', 'Ideas']).map((value) =>
        <button key={value} type="button" aria-pressed={tab === value} onClick={() => {
          setTab(value); onIntentChange?.(value === 'Ideas' ? 'ideation' : 'chat');
        }}><Icon name={value === 'Papers' ? 'papers' : value === 'Findings' ? 'overview' : 'bulb'} size={16} />{value}{value === 'Papers' && candidates.length ? <span className="research-count">{candidates.length}</span> : null}
          {value === 'Findings' && briefCount ? <span className="research-count">{briefCount}</span> : null}</button>)}</nav>

      {tab === 'Papers' ? <>
        <div className="research-discovery">
        {review.topics?.length ? <details className="research-landscape">
          <summary>Explore topics <span className="field-hint">({review.topics.length} groups from titles and abstracts)</span></summary>
          <div className="research-topics">{review.topics.map((topic) => <article key={topic.id} className="card">
            <h3>{topic.title}</h3><p>{topic.description}</p>
            <small className="field-hint">{topic.candidate_ids.length} paper{topic.candidate_ids.length === 1 ? '' : 's'}
              {selecting ? ` / ${topic.candidate_ids.filter((value) => chosen.includes(value)).length} selected` : ''}</small>
            {topic.exploration ? <MarkdownViewer content={topic.exploration} /> : null}
            {selecting ? <div className="button-row">
              <button className="button ghost small" disabled={busy} onClick={() => control('explore_topics', { topic_ids: [topic.id], feedback: focus || null })}>Explain fit and recommend reading</button>
            </div> : null}
          </article>)}</div>
        </details> : null}
        {selecting && webEnabled ? <details className="research-more-papers"><summary>Expand the shortlist</summary>
          <p className="field-hint">Find more options without starting a reading batch. Your current choices are preserved.</p>
          <button className="button secondary small" disabled={busy} title="Discovers up to 100 additional papers without downloading PDFs"
            onClick={() => control('find_more_topics', { feedback: focus || null })}>Find more topics</button>
        </details> : null}
        {selecting ? <details className="research-add-papers"><summary>Add papers from arXiv</summary>
          <p className="field-hint">Add search results to this shortlist. PDFs are collected only after you approve a reading batch.</p>
          {webEnabled ? <ArxivSearch actionLabel="Add to shortlist" disabled={busy} chosenIds={candidates.flatMap((item) => item.arxiv_id ? [item.arxiv_id] : [])}
            onChoose={({ arxiv_id, title, summary, authors }) => control('add_arxiv_papers', {
              input_papers: [{ arxiv_id, title, summary, authors }],
            })} /> : <p className="field-hint">Enable web access to search arXiv. Your local papers remain available.</p>}
        </details> : null}
        {selecting ? <section className="research-pdf-input">
          <button className="button secondary small" type="button" disabled={busy} aria-expanded={showPdfInput}
            aria-controls="research-pdf-form" onClick={() => setShowPdfInput((value) => !value)}>
            <Icon name="plus" size={15} />Add PDF link
          </button>
          {showPdfInput ? <form id="research-pdf-form" onSubmit={(event) => { event.preventDefault(); void addPdfPaper(); }}>
            <p className="field-hint">Paste a direct public PDF URL. This only adds it to the shortlist; download and reading wait for your approval.</p>
            <label>PDF URL<input aria-label="PDF URL" type="url" required maxLength={2048} value={pdfUrl} disabled={busy}
              onChange={(event) => setPdfUrl(event.target.value)} placeholder="https://example.org/paper.pdf" /></label>
            <label>Paper title (optional)<input aria-label="PDF paper title" maxLength={2000} value={pdfTitle} disabled={busy}
              onChange={(event) => setPdfTitle(event.target.value)} placeholder="Helps identify and group this paper" /></label>
            <button className="button small" type="submit" disabled={busy || !pdfUrl.trim()}>
              {busy ? 'Adding paper…' : 'Add PDF to shortlist'}
            </button>
          </form> : null}
        </section> : null}
        </div>

        <div className="research-reader">
          <div className="research-list">
            <div className="research-list-heading">
              <h3>Paper shortlist</h3>
              <span className="field-hint">{visible.length} of {candidates.length}</span>
            </div>
            <input className="research-search" type="search" aria-label="Search shortlist" placeholder={`Search ${candidates.length} papers...`}
              value={query} onChange={(event) => setQuery(event.target.value)} />
            {review.topics?.length ? <label className="research-topic-filter">Browse by topic
              <select aria-label="Filter shortlist by topic" value={selectedTopic?.id ?? ''} onChange={(event) => setTopicId(event.target.value)}>
                <option value="">All topics</option>
                {review.topics.map((topic) => <option key={topic.id} value={topic.id}>{topic.title} ({topic.candidate_ids.length})</option>)}
              </select>
            </label> : null}
            {selectedTopic ? <p className="field-hint">{selectedTopic.description}</p> : null}
            {selecting ? <div className="research-selection-tools">
              <button className="button secondary small" aria-pressed={selectedOnly} onClick={() => setSelectedOnly((value) => !value)}>Selected only ({chosen.length})</button>
              <button className="button ghost small" disabled={busy || !chosen.length} onClick={() => updateReviewDraft({ chosen: [] })}>Clear selection</button>
            </div> : null}
            <div className="research-paper-list" aria-label="Paper shortlist">
              {visible.map((item) => {
                const read = papers.find((value) => value.candidate_id === item.id)?.brief;
                return <article key={item.id} className={`research-paper${item.id === paperId ? ' is-open' : ''}${chosen.includes(item.id) ? ' is-chosen' : ''}`}>
                  {selecting ? <input type="checkbox" disabled={busy} aria-label={`Read ${item.title}`} checked={chosen.includes(item.id)} onChange={() => toggle(item.id)} /> : null}
                  <button type="button" aria-pressed={item.id === paperId} onClick={() => {
                    setPaperId(item.id);
                    paperReader.current?.focus();
                  }}>
                    <span className="research-paper-title">{item.title}</span>
                    <small className={read ? 'research-badge is-verified' : 'research-badge'}>{read ? 'Verified excerpts' : 'Abstract only'}</small>
                  </button>
                </article>;
              })}
              {!visible.length ? <div className="research-empty"><p className="field-hint">{selectedOnly && !chosen.length ? 'No papers selected yet.' : candidates.length ? 'No papers match these filters.' : review.status === 'running' ? 'Papers will appear here as they are discovered...' : 'No papers on the shortlist yet.'}</p>
                {query || selectedOnly || selectedTopic ? <button className="button ghost small" onClick={() => { setQuery(''); setTopicId(''); setSelectedOnly(false); }}>Show all papers</button> : null}
              </div> : null}
            </div>
          </div>
          <article ref={paperReader} tabIndex={-1} aria-label="Paper reader" className="research-paper-view">
            {candidate ? <>
              <h3>{candidate.title}</h3>
              <p className="research-abstract">{candidate.abstract || 'No abstract available.'}</p>
              {candidate.document_id ? <div className="button-row paper-reading-actions">
                <button className="button secondary small" onClick={() => askAboutPaper('Summarize this paper: explain its motivation, approach, main findings and limitations. Distinguish source evidence from inference and disclose unread coverage.')}>Summarize paper</button>
                <button className="button secondary small" onClick={() => askAboutPaper()}>Ask a question about this paper</button>
                <span className="row-spacer" />
                <Link to={`/library?paper=${encodeURIComponent(candidate.document_id)}`}>Open full source</Link>
              </div> : <p className="field-hint">Select this paper and approve the reading batch to collect its source. Until then, only its metadata is available.</p>}
              {paper?.brief ? <>
                <p className="field-hint">Reading focus: {paper.brief.question_focus || review.objective}. Brief prose is model interpretation; only the quotations below are source-verified.</p>
                {paper.brief.sections.map((section) => <section key={section.title}><h4>{section.title}</h4><MarkdownViewer content={section.content} /></section>)}
                <h4>Verified source quotations</h4>
                {paper.brief.findings.map((finding) => <blockquote key={finding.id}>{finding.quote}<footer>Page {finding.page}: {finding.finding}</footer></blockquote>)}
                {paper.brief.limitations.length ? <ul className="field-hint">{paper.brief.limitations.map((item) => <li key={item}>{item}</li>)}</ul> : null}
              </> : <p className="field-hint">{paper?.error || 'No verified brief yet. Abstracts are leads, not source evidence.'}</p>}
            </> : <div className="research-empty"><Icon name="papers" size={28} /><strong>Preview before you choose</strong>
              <p className="field-hint">Click a paper title to see its abstract or verified brief.{selecting ? ' Use the checkbox to add it to your reading batch.' : ''}</p></div>}
          </article>
        </div>

      </> : tab === 'Findings' ? <section className="research-understanding">
        <div><h3>What the evidence says</h3><p className="field-hint">Compare the findings below, then return to Papers to inspect their sources.</p></div>
        <dl className="research-stats" aria-label="Evidence coverage">
          <div><dt>Verified briefs</dt><dd>{briefCount}</dd></div>
          <div><dt>Selected papers</dt><dd>{review.selected_count}</dd></div>
          <div><dt>Compared</dt><dd>{understanding?.candidate_ids.length ?? 0}</dd></div>
        </dl>
        <p className="field-hint">Coverage is limited to admitted source passages, not full-paper understanding.</p>
        <div className="button-row">
          <button className="button secondary" onClick={() => {
            const draft = `${context}\n\nCompare agreements, disagreements and open questions. Distinguish source evidence from inference and missing coverage.`;
            if (onDiscuss) onDiscuss(draft);
            else ask.ask(draft, { intent: 'chat' });
          }}>{embedded ? 'Compare this evidence' : 'Compare selected evidence in chat'}</button>
          {!archived && ['completed', 'paused', 'blocked'].includes(review.status) ? <>
            <button className="button" disabled={busy || !papers.some((item) => item.brief)} onClick={() => control('refine_understanding')}>Refine understanding</button>
            <button className="button ghost" disabled={busy} onClick={() => { setTab('Papers'); control('revise_selection'); }}>Choose follow-up reading</button>
          </> : null}
          {review.output_path ? <Link to={`/library/notes?path=${encodeURIComponent(review.output_path)}`}>Open saved cross-paper synthesis</Link> : null}
        </div>
        {understanding ? <article className="card research-comparison">
          <p className="field-hint">Model comparison of {understanding.candidate_ids.length} papers for "{understanding.question_focus || review.objective}". New briefs are not included until you refine. Separate comparison groups do not establish relationships across groups.</p>
          {understanding.question_focus !== review.question_focus ? <p role="note" className="research-warning">This comparison belongs to an earlier focus. Finish the new reading batch before treating it as an answer to the current question.</p> : null}
          <MarkdownViewer content={understanding.markdown} />
        </article> : <div className="research-empty"><strong>No cross-paper comparison yet</strong><p className="field-hint">Incoming briefs appear below without regenerating earlier comparisons.</p></div>}
        {briefCount ? <h3>Findings per paper</h3> : null}
        {papers.filter((item) => item.brief).map((item) => <article key={item.candidate_id} className="card">
          <h4>{candidates.find((value) => value.id === item.candidate_id)?.title}</h4>
          <MarkdownViewer content={item.brief?.sections.find((section) => section.title === 'Pertinent findings')?.content ?? ''} />
        </article>)}
      </section> : <IdeasPanel objective={review.objective} context={context} trackId={trackId} />}
      </> : null}

      <details className="research-controls" open={review.approval === 'budget' || ['paused', 'blocked', 'failed'].includes(review.status) || undefined}>
        <summary>Activity and errors{review.notices.length ? <span className="research-count">{review.notices.length}</span> : null}</summary>
        <ErrorNotice error={review.error} />
        {review.notices.map((notice) => <p key={notice} className="field-hint">{notice}</p>)}
        <ReviewActivityPanel review={review} />
        {activity}
      </details>
    </>}
    <SettingsDrawer id="research-settings" title="Research settings" open={settingsOpen} onClose={() => setSettingsOpen(false)}>
        <div className="research-settings-body">
          {!review ? <>
            <p className="field-hint">These models apply to this question when you start research. Your global defaults stay unchanged.</p>
            <label>Research model<ModelSelect ariaLabel="Research model" inline disabled={busy}
              options={capabilityOptions(providers, 'chat')} value={main} onChange={setMain} /></label>
            <p className="field-hint">Plans research and compares findings.</p>
            <label>Paper summary model<ModelSelect ariaLabel="Paper summary model" inline disabled={busy}
              options={capabilityOptions(providers, 'summary')} value={summary} onChange={setSummary}
              emptyOptionLabel="Use research model" /></label>
            <p className="field-hint">Summarizes individual papers. Defaults to your separate summary model when configured.</p>
            <label>Embedding model<ModelSelect ariaLabel="Embedding model" inline disabled={busy}
              options={capabilityOptions(providers, 'embedding')} value={embedding} onChange={setEmbedding} /></label>
            <p className="field-hint">Groups related papers and finds relevant passages.</p>
            {!valid ? <p className="research-warning">Choose both models before starting research.</p> : null}
          </> : recovering ? <>
            <p className="field-hint">Keep the saved models or choose replacements for unfinished work. Changes apply when you resume research.</p>
            <label>Research model<ModelSelect ariaLabel="Replacement research model" inline disabled={busy}
              options={capabilityOptions(providers, 'chat')} value={replacementMain} onChange={setReplacementMain} /></label>
            <label>Synthesis model<ModelSelect ariaLabel="Replacement synthesis model" inline disabled={busy}
              options={capabilityOptions(providers, 'chat')} value={replacementSummary}
              emptyOptionLabel="Keep current synthesis model" onChange={setReplacementSummary} /></label>
            {!capabilityOptions(providers, 'chat').length ? <p className="research-warning">No enabled chat models are available. Configure a model in Settings before replacing one.</p> : null}
          </> : <>
            <dl className="research-settings-models">
              <div><dt>Research model</dt><dd>{review.main_model?.model || 'Not recorded'}</dd></div>
              <div><dt>Embedding model</dt><dd>{review.embedding_model?.model_reference.model || 'Not recorded'}</dd></div>
              <div><dt>Synthesis model</dt><dd>{review.summary_model?.model_reference.model || 'Not recorded'}</dd></div>
            </dl>
            <p className="field-hint">{review.status === 'running' && !archived
              ? 'Pause research to choose replacement models for unfinished work.'
              : 'These are the models saved with this research. New questions use your configured defaults.'}</p>
          </>}
        </div>
        <footer>
          <Link to="/settings">Global model settings</Link>
          <button type="button" className="button" onClick={() => setSettingsOpen(false)}>Done</button>
        </footer>
    </SettingsDrawer>
  </section>;
}

function statusText(status: string) {
  const text = status.replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function ResearchHeading({ review }: { review: Review }) {
  return <header className="research-head">
    <div>
      <span className="research-eyebrow">Research question</span>
      <h2>{review.objective}</h2>
      {review.question_focus && review.question_focus !== review.objective ? <p>Reading focus: {review.question_focus}</p> : null}
    </div>
    <span className={`status-pill status-${review.status}`}>{statusText(review.status)}</span>
  </header>;
}

function researchNextStep(review: Review): { title: string; description: string } {
  if (review.approval === 'budget') return {
    title: 'A work limit has been reached',
    description: 'Review the work already used and approve new total limits below before more work can run.',
  };
  if (['paused', 'blocked'].includes(review.status)) return {
    title: review.status === 'paused' ? 'Research is paused' : 'Research is blocked',
    description: 'Your evidence and selections are saved. Review any errors below, then resume the same research when you are ready.',
  };
  if (review.approval === 'plan') return {
    title: 'Confirm your search',
    description: 'Check the proposed questions and work limits below, then approve the search. You will choose the papers before reading starts.',
  };
  if (review.approval === 'topics') return {
    title: 'Choose your reading batch',
    description: 'Preview the shortlist below and select paper checkboxes. Nothing is read until you approve this batch.',
  };
  if (review.status === 'completed') return {
    title: 'Your findings are ready',
    description: 'Review the findings and their source quotes, or choose follow-up reading to investigate remaining gaps.',
  };
  if (review.status === 'running') {
    if (review.stage === 'scope') return {
      title: 'Preparing your search plan',
      description: 'You will confirm the proposed questions and work limits before discovery starts.',
    };
    if (['discovery', 'screening'].includes(review.stage)) return {
      title: 'Finding relevant papers',
      description: 'Searching and grouping titles and abstracts. No PDFs are being collected yet. You can leave this page and return later.',
    };
    return {
      title: 'Building your evidence',
      description: 'Focused briefs appear as papers are read. Browse available findings now; leaving this page will not cancel the work.',
    };
  }
  return {
    title: 'Review your research progress',
    description: 'Your saved evidence is available below. Check Activity and errors for the current state and any decisions still needed.',
  };
}
