import type { ReviewCommand } from "../../api/types";
import { createRetryJournal } from "../../lib/retry-journal";
import { parseReviewCommand, type ReviewOperation } from "./model";

// A namespace distinct from resolve's: an issue and a pull request may share a number.
const journal = createRetryJournal<ReviewCommand>({
  prefix: "issue-agent.review-request.v1:",
  parse: parseReviewCommand,
  target: (command) => command.expected.pull_request,
  noun: "pull request",
});

export function reviewRetryKey(deployment: string, pr: number): string {
  return journal.key(deployment, pr);
}

export function recoverReviewRequest(storage: Storage, deployment: string, pr: number): ReviewOperation {
  return journal.recover(storage, deployment, pr);
}

export function retainReviewRequest(storage: Storage, deployment: string, command: ReviewCommand): void {
  journal.retain(storage, deployment, command);
}

export function acknowledgeReviewRequest(storage: Storage, deployment: string, command: ReviewCommand): void {
  journal.acknowledge(storage, deployment, command);
}
