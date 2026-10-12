import type { components } from "../../api/generated";

type Recovery = components["schemas"]["IssueRecoveryAvailable"] | components["schemas"]["PRRepairAvailable"] | components["schemas"]["RecoveryUnavailable"];

export function FixRecoveryView({ recovery }: { recovery?: Recovery | null }) {
  if (!recovery) return null;
  return <div className="resolve-history">
    <h3>Fix recovery</h3>
    {recovery.status !== "available" ? <p className="resolve-status-note">{recovery.reason} Recovery debt cannot be shown.</p> : <>
      <p>{recovery.family === "issue_recovery" ? "Issue recovery" : "PR repair"} · generation {recovery.generation} · revision {recovery.version}</p>
      <dl className="resolve-facts">
        <dt>Resumes</dt><dd>{recovery.resumes}</dd>
        {recovery.family === "issue_recovery" && <>
        <dt>CI failures</dt><dd>{recovery.ci_failures}</dd>
        <dt>Review failures</dt><dd>{recovery.review_failures}</dd>
        <dt>Rebase failures</dt><dd>{recovery.rebase_failures}</dd>
        <dt>Infrastructure failures</dt><dd>{recovery.infra_failures}</dd>
        </>}
      </dl>
      {recovery.family === "issue_recovery"
        ? <p>{recovery.holds.length ? `Recovery holds: ${recovery.holds.join(", ")}.` : "No recorded recovery holds."}</p>
        : <p>{recovery.workflow_held ? "PR continuity held." : "No recorded PR continuity holds."} Repair allowance belongs to issue recovery.</p>}
      {recovery.family === "pr_repair" && recovery.pending_effects.length > 0 && <p role="status">Awaiting verified completion: {recovery.pending_effects.map((effect) => effect.replaceAll("_", " ")).join(", ")}.</p>}
      <p className="resolve-status-note">Current SQL state used by scheduling and admission. Other checks still apply.</p>
    </>}
  </div>;
}
