import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import { verificationTone } from "../lib/semantics";
import { Chip } from "./ui/Chip";
import { EmptyState, ErrorBanner, SectionTitle, SkeletonRows, WarningList } from "./ui/Feedback";
import { ResizableTable, useSortedRows } from "./ui/ResizableTable";

const POLL_MS = 60_000; // a markdown ledger the human edits — no need to hammer it

/**
 * The field-verification ledger (docs/field-verification.md): what has shipped
 * vs what a live deployment has proven. Pending rows are the watch list — the
 * manager's "before" step reads them, the "after" step gives each a verdict.
 */
export function VerificationTab() {
  const v = usePolled("verification", (signal) => api.verification(signal), POLL_MS);
  const rows = v.data?.rows ?? [];
  const sorted = useSortedRows(
    rows,
    {
      id: (row) => row.id,
      change: (row) => row.change,
      status: (row) => row.status,
      shipped: (row) => row.shipped,
      evidence: (row) => row.evidence,
    },
    { key: "id", direction: "asc" },
    "verification-ledger",
  );

  const counts = rows.reduce<Record<string, number>>((acc, r) => {
    const t = verificationTone(r.status);
    acc[t] = (acc[t] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="tabbody">
      <SectionTitle>Verification ledger</SectionTitle>
      {v.error ? <ErrorBanner message={v.error} onRetry={v.refresh} stale={!!v.data} /> : null}
      <WarningList warnings={v.data?.warnings} />

      <div className="row">
        <Chip tone="ok">{counts["ok"] ?? 0} verified</Chip>
        <Chip tone="fail">{counts["fail"] ?? 0} misbehaved</Chip>
        <Chip tone="running">{counts["running"] ?? 0} pending</Chip>
        <span className="spacer" />
        <span className="faint">docs/field-verification.md</span>
      </div>

      {v.loading ? <SkeletonRows what="the verification ledger" rows={6} /> : null}

      {!v.loading && rows.length === 0 ? (
        <EmptyState>no rows parsed from docs/field-verification.md</EmptyState>
      ) : null}

      {rows.length > 0 ? (
        <div className="tbl-wrap panel">
          <ResizableTable storageKey="verification-ledger" columns={[
            { key: "id", label: "ID", width: 110 },
            { key: "change", label: "Change", width: 360 },
            { key: "status", label: "Status", width: 130 },
            { key: "shipped", label: "Shipped", width: 130 },
            { key: "evidence", label: "Evidence", width: 500 },
          ]} sort={sorted.sort} onSort={sorted.onSort}>
            <tbody>
              {sorted.rows.map((r, i) => {
                const tone = verificationTone(r.status);
                return (
                  <tr key={i} className={tone === "fail" ? "row-alert" : undefined} title={r.raw}>
                    <td className="mono">{r.id ?? "—"}</td>
                    <td className="title-cell">{r.change ?? <span className="faint">—</span>}</td>
                    <td>
                      {r.status ? (
                        <Chip tone={tone}>{r.status}</Chip>
                      ) : (
                        <span className="faint">—</span>
                      )}
                    </td>
                    <td className="mono faint">{r.shipped ?? "—"}</td>
                    <td className="evidence">{r.evidence ?? <span className="faint">—</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </ResizableTable>
        </div>
      ) : null}
    </div>
  );
}
