import type { ReactNode } from 'react';
import { Link } from '../../app/router';
import { Icon, type IconName } from './Icons';

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        {eyebrow ? <span className="eyebrow">{eyebrow}</span> : null}
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="button-row">{actions}</div> : null}
    </header>
  );
}

export function PageTabs({
  label,
  active,
  items,
}: {
  label: string;
  active: string;
  items: { id: string; label: string; to: string; icon: IconName }[];
}) {
  return (
    <nav className="page-tabs" aria-label={label}>
      {items.map((item) => (
        <Link
          className={active === item.id ? 'active' : ''}
          to={item.to}
          aria-current={active === item.id ? 'page' : undefined}
          key={item.id}
        >
          <Icon name={item.icon} size={15} />
          {item.label}
        </Link>
      ))}
    </nav>
  );
}

const LIBRARY_TABS = [
  { id: 'papers', label: 'Papers', to: '/library', icon: 'papers' },
  { id: 'notes', label: 'Notes', to: '/library/notes', icon: 'workspace' },
] satisfies { id: string; label: string; to: string; icon: IconName }[];

export function LibraryTabs({ active }: { active: 'papers' | 'notes' }) {
  return <PageTabs label="Library view" active={active} items={LIBRARY_TABS} />;
}

export function Panel({
  title,
  description,
  actions,
  children,
  className = '',
}: {
  title?: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`.trim()}>
      {title || description || actions ? (
        <header className="panel-header">
          <div>
            {title ? <h2>{title}</h2> : null}
            {description ? <p>{description}</p> : null}
          </div>
          {actions ? <div className="button-row">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
}

/** Placeholder blocks that keep layout stable while data is in flight. */
export function Skeleton({ height = 16, width = '100%' }: { height?: number | string; width?: number | string }) {
  return <span className="skeleton" style={{ display: 'block', height, width }} aria-hidden="true" />;
}

export function SkeletonCards({ count = 3 }: { count?: number }) {
  return (
    <div className="card-grid" aria-hidden="true">
      {Array.from({ length: count }, (_, index) => (
        <div className="card" key={index}>
          <Skeleton height={10} width="35%" />
          <Skeleton height={18} width="65%" />
          <Skeleton height={12} />
          <Skeleton height={12} width="80%" />
        </div>
      ))}
    </div>
  );
}

export function ErrorNotice({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="notice error" role="alert">
      <Icon name="shield" size={16} />
      <span>{message}</span>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  icon = 'inbox',
  action,
}: {
  title: string;
  description: string;
  icon?: IconName;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon name={icon} size={22} />
      </span>
      <h2>{title}</h2>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function StatusPill({ value }: { value: string }) {
  return <span className={`status-pill status-${value}`}>{value.split('_').join(' ')}</span>;
}
