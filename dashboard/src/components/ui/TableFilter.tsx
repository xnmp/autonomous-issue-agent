interface TableFilterProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  shown: number;
  total: number;
}

/** Compact filtering for long operator tables; short tables stay scan-only. */
export function TableFilter({ label, value, onChange, shown, total }: TableFilterProps) {
  return (
    <label className="table-filter">
      <span className="table-filter-label">Filter</span>
      <input
        type="search"
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        placeholder={label}
        aria-label={label}
      />
      <span className="table-filter-count mono" aria-live="polite">
        {shown}/{total}
      </span>
    </label>
  );
}
