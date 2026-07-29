import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import { verificationTone } from "../lib/semantics";
import { Chip } from "./ui/Chip";
import { EmptyState, ErrorBanner, Loading, WarningList } from "./ui/Feedback";

const POLL_MS = 60_000; // a markdown ledger the human edits — no need to hammer it

/**
 * The field-verification ledger (docs/field-verification.md): what has shipped
 * vs what a live deployment has proven. Pending rows are the watch list — the
 * manager's "before" step reads them, the "after" step gives each a verdict.
 */
export function VerificationTab() {
  const v = usePolled("verification", (signal) => api.verification(signal), POLL_MS);
  const rows = v.data?.rows ?? [];

  const counts = rows.reduce<Record<string, number>>((acc, r) => {
    const t = verificationTone(r.status);
    acc[t] = (acc[t] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="tabbody">
      {v.error ? <ErrorBanner message={v.error} onRetry={v.refresh} stale={!!v.data} /> : null}
      <WarningList warnings={v.data?.warnings} />

      <div className="row">
        <Chip tone="ok">{counts["ok"] ?? 0} verified</Chip>
        <Chip tone="fail">{counts["fail"] ?? 0} misbehaved</Chip>
        <Chip tone="running">{counts["running"] ?? 0} pending</Chip>
        <span className="spacer" />
        <span className="faint">docs/field-verification.md</span>
      </div>

      {v.loading ? <Loading what="the verification ledger" /> : null}

      {!v.loading && rows.length === 0 ? (
        <EmptyState>no rows parsed from docs/field-verification.md</EmptyState>
      ) : null}

      {rows.length > 0 ? (
        <div className="tbl-wrap panel">
          <table className="tbl">
            <thead>
              <tr>
                <th style={{ width: 110 }}>ID</th>
                <th>Change</th>
                <th style={{ width: 120 }}>Status</th>
                <th style={{ width: 110 }}>Shipped</th>
                <th>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => {
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
          </table>
        </div>
      ) : null}
    </div>
  );
}
