export function OptionalNumber({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value?: number | null;
  min?: number;
  max?: number;
  step?: number;
  onChange: (value: number | null) => void;
}) {
  return (
    <label className="field">
      {label}
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value ?? ''}
        onChange={(event) => onChange(event.target.value ? Number(event.target.value) : null)}
      />
    </label>
  );
}
