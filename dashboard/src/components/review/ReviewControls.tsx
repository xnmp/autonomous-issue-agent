import { ReviewControlView } from "./ReviewControlView";
import { useReviewControl } from "./use-review-control";

interface ReviewControlsProps {
  deployment: string;
  pr: number;
}

/**
 * The single owner of review-rejection mutations for one pull request. Keyed
 * by deployment and pull request so a restored retry record or an in-flight
 * command never surfaces under another pull request's heading.
 */
export function ReviewControls(props: ReviewControlsProps) {
  return <ReviewControlsForPullRequest key={JSON.stringify([props.deployment, props.pr])} {...props} />;
}

function ReviewControlsForPullRequest({ deployment, pr }: ReviewControlsProps) {
  const controller = useReviewControl(deployment, pr);
  const { control, readError, operation } = controller;
  // Absent still renders: with the family active, the legacy refusal points here,
  // so this panel says why there is nothing to re-arm.
  const quiet = operation.kind === "idle" && !readError && !controller.actionError && !controller.lookupId
    && (control === null || control.status === "inactive");
  if (quiet) return null;
  return <ReviewControlView
    pr={pr}
    control={control}
    loading={controller.loading}
    readError={readError}
    actionError={controller.actionError}
    operation={operation}
    lookupId={controller.lookupId}
    onPrepare={controller.onPrepare}
    onConfirm={controller.onConfirm}
    onDismiss={controller.onDismiss}
    onRetryCommand={controller.onRetryCommand}
    onRefresh={controller.onRefresh}
    onHistoryLookup={controller.onHistoryLookup}
    onClearLookup={controller.onClearLookup}
  />;
}
