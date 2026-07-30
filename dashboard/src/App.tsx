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
import { IssuesTab } from "./components/IssuesTab";
import { useIssues } from "./features/issues/useIssues";
import { IssueTab } from "./components/IssueTab";
import { VerificationTab } from "./components/VerificationTab";
import { EmptyState } from "./components/ui/Feedback";
import { Chip } from "./components/ui/Chip";
import { stageTone } from "./lib/semantics";
import {
  DASHBOARD_THEMES,
  type DashboardOptions,
} from "./components/ui/OptionsMenu";
import { ToastStack } from "./components/ToastStack";

const TOP_POLL_MS = 10_000;
const OPTIONS_STORAGE = "issue-agent.dashboard.options";

function storedOptions(): DashboardOptions {
  const defaults: DashboardOptions = {
    showProjectedCost: true,
    showPerRepoQuotas: true,
    showDurationSeconds: true,
    theme: "cosmic-dusk",
  };
  try {
    const stored = {
      ...defaults,
      ...JSON.parse(localStorage.getItem(OPTIONS_STORAGE) ?? "{}"),
    } as DashboardOptions;
    return DASHBOARD_THEMES.some(([theme]) => theme === stored.theme)
      ? stored
      : defaults;
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
  const [promotedBacklog, setPromotedBacklog] = useState<Record<string, number[]>>({});
  const hiddenBacklog = selectedRepo ? (promotedBacklog[selectedRepo] ?? []) : [];
  const displayedIssues = {
    ...issues,
    data: issues.data
      ? {
          ...issues.data,
          backlog: issues.data.backlog.filter((card) => !hiddenBacklog.includes(card.issue)),
        }
      : null,
  };
  useEffect(() => {
    if (!selectedRepo || !issues.data) return;
    setPromotedBacklog((current) => {
      const hidden = current[selectedRepo] ?? [];
      const stillPending = hidden.filter((issue) => (
        issues.data?.backlog.some((card) => card.issue === issue)
      ));
      if (stillPending.length === hidden.length) return current;
      return { ...current, [selectedRepo]: stillPending };
    });
  }, [issues.data, selectedRepo]);
  const setBacklogPromotion = useCallback((issue: number, pending: boolean) => {
    if (!selectedRepo) return;
    setPromotedBacklog((current) => {
      const existing = current[selectedRepo] ?? [];
      const next = pending
        ? Array.from(new Set([...existing, issue]))
        : existing.filter((candidate) => candidate !== issue);
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
          ? displayedIssues.data.in_flight.length + displayedIssues.data.pending.length
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
      const card =
        displayedIssues.data?.in_flight.find((c) => c.issue === n) ??
        displayedIssues.data?.done.find((c) => c.issue === n) ??
        null;
      tabs.push({
        key: `issue-${n}`,
        title: `#${n}${card?.title ? ` — ${card.title}` : ""}`,
        label: (
          <>
            <span className="mono">#{n}</span>
            {card?.stage ? (
              <Chip
                tone={stageTone(card.stage)}
                pulse={!card.done && stageTone(card.stage) === "running"}
              >
                {card.stage}
              </Chip>
            ) : null}
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
            <TabStrip tabs={tabs} />
          )}
        </div>
        <div className="mainbody">
          <Body
            route={route}
            selectedRepo={selectedRepo}
            issues={displayedIssues}
            onSetSub={setSub}
            onOpenIssue={openIssue}
            onBacklogPromotion={setBacklogPromotion}
            hasRepos={repoList.length > 0}
            reposLoading={repos.loading}
            showProjectedCost={options.showProjectedCost}
            showDurationSeconds={options.showDurationSeconds}
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
  issues,
  onSetSub,
  onOpenIssue,
  onBacklogPromotion,
  hasRepos,
  reposLoading,
  showProjectedCost,
  showDurationSeconds,
}: {
  route: Route;
  selectedRepo: string | null;
  issues: ReturnType<typeof useIssues>;
  onSetSub: (s: IssuesSub) => void;
  onOpenIssue: (n: number) => void;
  onBacklogPromotion: (issue: number, pending: boolean) => void;
  hasRepos: boolean;
  reposLoading: boolean;
  showProjectedCost: boolean;
  showDurationSeconds: boolean;
}) {
  if (route.view === "verification") return <VerificationTab />;
  if (route.view === "issue") {
    return (
      <IssueTab
        repo={route.repo}
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
        showProjectedCost={showProjectedCost}
        showDurationSeconds={showDurationSeconds}
      />
    );
  if (selectedRepo) return <OverviewTab repo={selectedRepo} onOpenIssues={onSetSub} />;
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
