import type { ResolveControl } from "../../api/types";
import type { ResolveDraft, ResolveOperation } from "./model";

export interface ResolveControlViewProps {
  issue: number;
  control: ResolveControl | null;
  loading: boolean;
  readError: string | null;
  actionError: string | null;
  operation: ResolveOperation;
  legacyAllowed: boolean;
  /** The request ID whose history is being read, or "" for the current intent. */
  lookupId: string;
  onPrepare: (draft: ResolveDraft) => void;
  onConfirm: () => void;
  onDismiss: () => void;
  onRetryCommand: () => void;
  onRefresh: () => void;
  onHistoryLookup: (requestId: string) => void;
  onClearLookup: () => void;
  onLegacy: () => void;
}
