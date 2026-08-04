export const ARROW_ID = 'wf-arrow';
export const ARROW_ACTIVE_ID = 'wf-arrow-active';

// A swept arrowhead: longer and thinner than React Flow's stock triangle, with a
// notched tail so it reads as a point rather than a blob where edges converge.
const ARROW_PATH = 'M2,0.9 L13,5 L2,9.1 L5.2,5 Z';

type ArrowProps = { id: string; fill: string };

function Arrow({ id, fill }: ArrowProps) {
  return (
    <marker
      id={id}
      viewBox="0 0 20 10"
      markerWidth={14}
      markerHeight={7}
      markerUnits="userSpaceOnUse"
      // refX sits 7 viewBox units past the tip, which pulls the arrow back off
      // the target handle instead of burying the point underneath it.
      refX={20}
      refY={5}
      orient="auto-start-reverse"
    >
      <path d={ARROW_PATH} fill={fill} />
    </marker>
  );
}

/**
 * Arrowhead markers are referenced from CSS (`marker-end`) rather than from edge
 * data, so hovered and selected edges can swap to the accent-coloured variant.
 * `context-stroke` would be tidier but is unsupported in WebKitGTK.
 */
export default function EdgeMarkers() {
  return (
    <svg className="edge-marker-defs" aria-hidden="true" focusable="false">
      <defs>
        <Arrow id={ARROW_ID} fill="var(--edge)" />
        <Arrow id={ARROW_ACTIVE_ID} fill="var(--accent)" />
      </defs>
    </svg>
  );
}
