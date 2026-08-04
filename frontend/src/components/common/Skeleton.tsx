export function SkeletonList({ rows = 3 }: { rows?: number }) {
  return (
    <div className="stack gap-sm" aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeleton-row" key={index}>
          <span className="skeleton skeleton-line title" />
          <span className="skeleton skeleton-line short" />
        </div>
      ))}
    </div>
  );
}

export function SkeletonLines({ lines = 3 }: { lines?: number }) {
  return (
    <div className="stack gap-xs" aria-hidden="true">
      {Array.from({ length: lines }, (_, index) => (
        <span className={`skeleton skeleton-line${index === lines - 1 ? ' short' : ''}`} key={index} />
      ))}
    </div>
  );
}
