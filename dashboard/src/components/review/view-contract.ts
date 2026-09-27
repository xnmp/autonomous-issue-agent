import type { ReviewControl } from "../../api/types";
import type { ReviewDraft, ReviewOperation } from "./model";

export interface ReviewControlViewProps {
  pr: number;
  control: ReviewControl | null;
  loading: boolean;
  readError: string | null;
  actionError: string | null;
  operation: ReviewOperation;
  /** The request ID whose history is being read, or "" for the current intent. */
  lookupId: string;
  onPrepare: (draft: ReviewDraft) => void;
  onConfirm: () => void;
  onDismiss: () => void;
  onRetryCommand: () => void;
  onRefresh: () => void;
  onHistoryLookup: (requestId: string) => void;
  onClearLookup: () => void;
}
