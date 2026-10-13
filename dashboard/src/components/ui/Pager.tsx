export const PAGE_SIZE = 50;

export type Page<T> = {
  rows: T[];
  /** Zero-based and clamped: a shrinking dataset can never strand the reader past the end. */
  page: number;
  pages: number;
  from: number;
  to: number;
  total: number;
};

export function paginate<T>(rows: readonly T[], requested: number, size = PAGE_SIZE): Page<T> {
  const pages = Math.max(1, Math.ceil(rows.length / size));
  const page = Math.min(Math.max(0, Number.isFinite(requested) ? Math.floor(requested) : 0), pages - 1);
  const start = page * size;
  const slice = rows.slice(start, start + size);
  return {
    rows: slice,
    page,
    pages,
    from: slice.length ? start + 1 : 0,
    to: start + slice.length,
    total: rows.length,
  };
}

/** Hidden when everything fits on one page: controls with nothing to do are noise. */
export function Pager({ page, onPage }: { page: Page<unknown>; onPage: (page: number) => void }) {
  if (page.pages <= 1) return null;
  return (
    <nav className="pager" aria-label="Pagination">
      <span className="pager-range">{page.from}–{page.to} of {page.total}</span>
      <span className="pager-nav">
        <button type="button" className="seg-btn" disabled={page.page === 0} onClick={() => onPage(page.page - 1)}>
          Prev
        </button>
        <span className="pager-page" aria-current="page">Page {page.page + 1} of {page.pages}</span>
        <button type="button" className="seg-btn" disabled={page.page >= page.pages - 1} onClick={() => onPage(page.page + 1)}>
          Next
        </button>
      </span>
    </nav>
  );
}
