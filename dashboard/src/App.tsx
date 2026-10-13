import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api/client";
import { usePolled } from "./api/hooks";
import {
  ISSUES_SUB_LABELS,
  ISSUES_SUBS,
  routeRepo,
  useRoute,
  type IssuesSub,
  type Route,
} from "./routing/hash";
import { Sidebar } from "./components/Sidebar";
import { TabStrip, type TabDef } from "./components/TabStrip";
import { OverviewTab } from "./components/OverviewTab";
import {
  IssuesTab,
  type BacklogPromotionPhase,
} from "./components/IssuesTab";
import { useIssues } from "./features/issues/useIssues";
import type {
  BacklogCard,
  IssuesPayload,
} from "./api/types";
import { IssueTab } from "./components/IssueTab";
import { VerificationTab } from "./components/VerificationTab";
import { EmptyState } from "./components/ui/Feedback";
import { Chip, Dot } from "./components/ui/Chip";
import { HEALTH_LABEL, HEALTH_TONE, roleHealth, type Tone } from "./lib/semantics";
import { boardItems, GROUP_META, queuedCard, type BoardGroup } from "./features/issues/board";
import {
  DASHBOARD_THEMES,
  type DashboardOptions,
} from "./components/ui/OptionsMenu";
import { ToastStack } from "./components/ToastStack";

const TOP_POLL_MS = 10_000;
const OPTIONS_STORAGE = "issue-agent.dashboard.options";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function storedBoolean(
  stored: Record<string, unknown>,
  key: keyof DashboardOptions,
  fallback: boolean,
): boolean {
  return stored[key] === undefined ? fallback : stored[key] === true;
}

/** An open issue tab names the board group its issue is in. */
const BOARD_TONE: Record<BoardGroup, Tone> = { needs_you: "fail", working: "ok", waiting: "neutral" };

interface OptimisticPromotion {
  card: BacklogCard;
  requestSucceeded: boolean;
}

function promotionConfirmed(
  promotion: OptimisticPromotion,
  issues: IssuesPayload,
): boolean {
  const issue = promotion.card.issue;
  const active = issues.in_flight.some((card) => card.issue === issue);
  const stillBacklogged = issues.backlog.some((card) => card.issue === issue);
  return active || (
    promotion.requestSucceeded
    && issues.gh_ok
    && !stillBacklogged
  );
}

function storedOptions(): DashboardOptions {
  const defaults: DashboardOptions = {
    showPerRepoQuotas: true,
    showDurationSeconds: true,
    showOverviewDaemonHealth: false,
    showOverviewIssueHealthTrend: false,
    theme: "cosmic-dusk",
  };
  try {
    const parsed: unknown = JSON.parse(
      localStorage.getItem(OPTIONS_STORAGE) ?? "{}",
    );
    if (!isRecord(parsed)) return defaults;
    const theme = DASHBOARD_THEMES.some(([candidate]) => candidate === parsed.theme)
      ? parsed.theme as DashboardOptions["theme"]
      : defaults.theme;
    return {
      showPerRepoQuotas: storedBoolean(
        parsed, "showPerRepoQuotas", defaults.showPerRepoQuotas,
      ),
      showDurationSeconds: storedBoolean(
        parsed, "showDurationSeconds", defaults.showDurationSeconds,
      ),
      showOverviewDaemonHealth: storedBoolean(
        parsed, "showOverviewDaemonHealth", defaults.showOverviewDaemonHealth,
      ),
      showOverviewIssueHealthTrend: storedBoolean(
        parsed,
        "showOverviewIssueHealthTrend",
        defaults.showOverviewIssueHealthTrend,
      ),
      theme,
    };
  } catch {
    return defaults;
  }
}

/**
 * App shell: sidebar + tab strip + one mounted view.
 *
 * Two pieces of state, kept apart on purpose:
 *   - the ROUTE (URL hash) says what is on screen, so reload restores it;
 *   - `openIssues` says which closeable issue tabs exist across navigation.
 * Visiting an issue route always adds that issue to the open set, so a pasted
 * `#/repo/x/issue/7` link materialises its tab.
 */
export default function App() {
  const { route, navigate } = useRoute();

  const repos = usePolled("repos", (signal) => api.repos(signal), TOP_POLL_MS);
  const usage = usePolled("usage", (signal) => api.usage(signal), TOP_POLL_MS);
  const [options, setOptions] = useState<DashboardOptions>(storedOptions);
  const updateOptions = (next: DashboardOptions) => {
    setOptions(next);
    try {
      localStorage.setItem(OPTIONS_STORAGE, JSON.stringify(next));
    } catch {
      // The options remain active for this session.
    }
  };
  useEffect(() => {
    document.documentElement.dataset.theme = options.theme;
  }, [options.theme]);

  const repoList = useMemo(() => repos.data?.repos ?? [], [repos.data]);
  const routedRepo = routeRepo(route);

  // Default landing: the first (highest-frecency) repo. `replace` so the empty
  // hash does not become a history entry the back button lands on.
  useEffect(() => {
    if (route.view === "home" && repoList.length > 0 && repoList[0]) {
      navigate({ view: "overview", repo: repoList[0].name }, true);
    }
  }, [route.view, repoList, navigate]);

  const selectedRepo = routedRepo ?? repoList[0]?.name ?? null;
  const selectedRepoSummary = repoList.find((repo) => repo.name === selectedRepo);

  const [openIssues, setOpenIssues] = useState<Record<string, number[]>>({});

  useEffect(() => {
    if (route.view === "issue") {
      const { repo, issue } = route;
      setOpenIssues((cur) => {
        const list = cur[repo] ?? [];
        return list.includes(issue) ? cur : { ...cur, [repo]: [...list, issue] };
      });
    }
  }, [route]);

  const issues = useIssues(selectedRepo);
  const [promotedBacklog, setPromotedBacklog] = useState<
    Record<string, OptimisticPromotion[]>
  >({});
  const optimisticPromotions = selectedRepo ? (promotedBacklog[selectedRepo] ?? []) : [];
  const currentIssues = issues.data;
  const unresolvedPromotions = currentIssues
    ? optimisticPromotions.filter((promotion) => (
        !promotionConfirmed(promotion, currentIssues)
      ))
    : optimisticPromotions;
  const promotedIssueNumbers = new Set(
    optimisticPromotions.map(({ card }) => card.issue),
  );
  const displayedIssues = {
    ...issues,
    data: issues.data
      ? {
          ...issues.data,
          backlog: issues.data.backlog.filter(
            (card) => !promotedIssueNumbers.has(card.issue),
          ),
          in_flight: [
            ...issues.data.in_flight,
            ...unresolvedPromotions.map(({ card }) => queuedCard(card)),
          ],
        }
      : null,
  };
  useEffect(() => {
    const data = issues.data;
    if (!selectedRepo || !data) return;
    setPromotedBacklog((current) => {
      const optimistic = current[selectedRepo] ?? [];
      const unresolved = optimistic.filter((promotion) => (
        !promotionConfirmed(promotion, data)
      ));
      if (unresolved.length === optimistic.length) return current;
      return { ...current, [selectedRepo]: unresolved };
    });
  }, [issues.data, selectedRepo]);
  const setBacklogPromotion = useCallback((
    card: BacklogCard,
    phase: BacklogPromotionPhase,
  ) => {
    if (!selectedRepo) return;
    setPromotedBacklog((current) => {
      const existing = current[selectedRepo] ?? [];
      const matching = existing.find((candidate) => candidate.card.issue === card.issue);
      const withoutCard = existing.filter(
        (candidate) => candidate.card.issue !== card.issue,
      );
      const next = phase === "failed"
        ? withoutCard
        : phase === "succeeded"
          ? matching
            ? [...withoutCard, { ...matching, requestSucceeded: true }]
            : existing
          : [...withoutCard, { card, requestSucceeded: false }];
      return { ...current, [selectedRepo]: next };
    });
  }, [selectedRepo]);

  const openIssue = useCallback(
    (n: number) => {
      if (!selectedRepo) return;
      navigate({ view: "issue", repo: selectedRepo, issue: n });
    },
    [navigate, selectedRepo],
  );

  const closeIssue = useCallback(
    (repo: string, n: number) => {
      setOpenIssues((cur) => ({ ...cur, [repo]: (cur[repo] ?? []).filter((x) => x !== n) }));
      if (route.view === "issue" && route.repo === repo && route.issue === n) {
        navigate({ view: "issues", repo, sub: "inflight" }, true);
      }
    },
    [navigate, route],
  );

  /* --------------------------------------------------------- tab strip */

  const tabs: TabDef[] = [];
  if (selectedRepo && route.view !== "verification") {
    const repo = selectedRepo;
    tabs.push({
      key: "overview",
      title: "Overview",
      label: "Overview",
      active: route.view === "overview",
      onSelect: () => navigate({ view: "overview", repo }),
    });
    for (const sub of ISSUES_SUBS) {
      const count = displayedIssues.data
        ? sub === "inflight"
          ? boardItems(displayedIssues.data).length
          : displayedIssues.data[sub].length
        : null;
      tabs.push({
        key: sub,
        title: ISSUES_SUB_LABELS[sub],
        label: (
          <>
            {ISSUES_SUB_LABELS[sub]}
            {count !== null ? <span className="tab-count num">{count}</span> : null}
          </>
        ),
        active: route.view === "issues" && route.sub === sub,
        onSelect: () => navigate({ view: "issues", repo, sub }),
      });
    }

    for (const n of openIssues[repo] ?? []) {
      const card = displayedIssues.data?.in_flight.find((c) => c.issue === n) ?? null;
      const done = displayedIssues.data?.done.find((c) => c.issue === n) ?? null;
      tabs.push({
        key: `issue-${n}`,
        title: `#${n}${(card ?? done)?.title ? ` — ${(card ?? done)?.title}` : ""}`,
        label: (
          <>
            <span className="mono">#{n}</span>
            {card?.board ? (
              <Chip
                tone={BOARD_TONE[card.board.status]}
                pulse={card.board.status === "working" && card.board.activity === "agent"}
              >
                {GROUP_META[card.board.status].label}
              </Chip>
            ) : done ? <Chip tone="ok">done</Chip> : null}
          </>
        ),
        active: route.view === "issue" && route.issue === n,
        onSelect: () => navigate({ view: "issue", repo, issue: n }),
        onClose: () => closeIssue(repo, n),
      });
    }
  }
  const setSub = useCallback(
    (sub: IssuesSub) => {
      if (!selectedRepo) return;
      navigate({ view: "issues", repo: selectedRepo, sub }, true);
    },
    [navigate, selectedRepo],
  );

  return (
    <div className="app">
      <Sidebar
        repos={repos}
        usage={usage}
        activeRepo={selectedRepo}
        verificationActive={route.view === "verification"}
        options={options}
        onOptionsChange={updateOptions}
        onSelectRepo={(name) => navigate({ view: "overview", repo: name })}
        onOpenVerification={() => navigate({ view: "verification" })}
      />
      <main className="main">
        <div className="mainbar">
          {route.view === "verification" ? (
            <span className="global-view-title">Verification ledger</span>
          ) : (
            <>
              <div className="repo-context">
                <div className="repo-context-title">
                  <span className="repo-context-name mono">{selectedRepo ?? "No deployment"}</span>
                  {selectedRepoSummary?.active ? (
                    <span className="repo-live"><span className="repo-live-dot" />Live</span>
                  ) : null}
                </div>
                <div className="repo-context-meta">
                  {selectedRepoSummary?.repo ?? "Select a deployment"}
                  {selectedRepoSummary?.roles.map((role) => {
                    const health = roleHealth(role);
                    return (
                      <Dot
                        key={role.role}
                        tone={HEALTH_TONE[health]}
                        title={`${role.role}: ${HEALTH_LABEL[health]}`}
                      />
                    );
                  })}
                </div>
              </div>
              <TabStrip tabs={tabs} />
            </>
          )}
        </div>
        <div className="mainbody">
          <Body
            route={route}
            selectedRepo={selectedRepo}
            githubRepo={selectedRepoSummary?.repo ?? null}
            issues={displayedIssues}
            onSetSub={setSub}
            onOpenIssue={openIssue}
            onBacklogPromotion={setBacklogPromotion}
            hasRepos={repoList.length > 0}
            reposLoading={repos.loading}
            showDurationSeconds={options.showDurationSeconds}
            showOverviewDaemonHealth={options.showOverviewDaemonHealth}
            showOverviewIssueHealthTrend={options.showOverviewIssueHealthTrend}
          />
        </div>
      </main>
      <ToastStack
        key={selectedRepo ?? "no-repo"}
        repo={selectedRepo}
        notifications={displayedIssues.data?.notifications ?? null}
        roles={selectedRepoSummary?.roles ?? []}
      />
    </div>
  );
}

function Body({
  route,
  selectedRepo,
  githubRepo,
  issues,
  onSetSub,
  onOpenIssue,
  onBacklogPromotion,
  hasRepos,
  reposLoading,
  showDurationSeconds,
  showOverviewDaemonHealth,
  showOverviewIssueHealthTrend,
}: {
  route: Route;
  selectedRepo: string | null;
  githubRepo: string | null;
  issues: ReturnType<typeof useIssues>;
  onSetSub: (s: IssuesSub) => void;
  onOpenIssue: (n: number) => void;
  onBacklogPromotion: (card: BacklogCard, phase: BacklogPromotionPhase) => void;
  hasRepos: boolean;
  reposLoading: boolean;
  showDurationSeconds: boolean;
  showOverviewDaemonHealth: boolean;
  showOverviewIssueHealthTrend: boolean;
}) {
  if (route.view === "verification") return <VerificationTab />;
  if (route.view === "issue") {
    return (
      <IssueTab
        repo={route.repo}
        githubRepo={githubRepo ?? route.repo}
        issue={route.issue}
        showDurationSeconds={showDurationSeconds}
      />
    );
  }
  if (route.view === "issues" && selectedRepo)
    return (
      <IssuesTab
        repo={selectedRepo}
        sub={route.sub}
        issues={issues}
        onOpenIssue={onOpenIssue}
        onBacklogPromotion={onBacklogPromotion}
      />
    );
  if (selectedRepo) {
    return (
      <OverviewTab
        repo={selectedRepo}
        onOpenIssues={onSetSub}
        showDaemonHealth={showOverviewDaemonHealth}
        showIssueHealthTrend={showOverviewIssueHealthTrend}
      />
    );
  }
  return (
    <div className="tabbody">
      <EmptyState>
        {reposLoading
          ? "loading deployments…"
          : hasRepos
            ? "select a deployment from the left"
            : "no deployments found — point the harness at a repo with `make point-at REPO=<name>`"}
      </EmptyState>
    </div>
  );
}
