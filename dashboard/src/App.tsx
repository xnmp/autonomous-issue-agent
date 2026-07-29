import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api/client";
import { usePolled } from "./api/hooks";
import { hashFor, routeRepo, useRoute, type IssuesSub, type Route } from "./routing/hash";
import { Sidebar } from "./components/Sidebar";
import { TabStrip, type TabDef } from "./components/TabStrip";
import { OverviewTab } from "./components/OverviewTab";
import { IssuesTab, useIssues } from "./components/IssuesTab";
import { IssueTab } from "./components/IssueTab";
import { VerificationTab } from "./components/VerificationTab";
import { EmptyState } from "./components/ui/Feedback";
import { Chip } from "./components/ui/Chip";
import { stageTone } from "./lib/semantics";

const TOP_POLL_MS = 10_000;

/**
 * App shell: sidebar + tab strip + one mounted view.
 *
 * Two pieces of state, kept apart on purpose:
 *   - the ROUTE (URL hash) says what is on screen, so reload restores it;
 *   - `openIssues` / `verificationOpen` say what tabs EXIST, because a
 *     closeable tab must survive navigating away from it and cannot live in a
 *     single-valued hash.
 * Visiting an issue route always adds that issue to the open set, so a pasted
 * `#/repo/x/issue/7` link materialises its tab.
 */
export default function App() {
  const { route, navigate } = useRoute();

  const repos = usePolled("repos", (signal) => api.repos(signal), TOP_POLL_MS);
  const usage = usePolled("usage", (signal) => api.usage(signal), TOP_POLL_MS);

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

  const [openIssues, setOpenIssues] = useState<Record<string, number[]>>({});
  const [verificationOpen, setVerificationOpen] = useState(false);

  useEffect(() => {
    if (route.view === "issue") {
      const { repo, issue } = route;
      setOpenIssues((cur) => {
        const list = cur[repo] ?? [];
        return list.includes(issue) ? cur : { ...cur, [repo]: [...list, issue] };
      });
    }
    if (route.view === "verification") setVerificationOpen(true);
  }, [route]);

  const issues = useIssues(selectedRepo);

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

  const closeVerification = useCallback(() => {
    setVerificationOpen(false);
    if (route.view === "verification") {
      navigate(selectedRepo ? { view: "overview", repo: selectedRepo } : { view: "home" }, true);
    }
  }, [navigate, route, selectedRepo]);

  /* --------------------------------------------------------- tab strip */

  const tabs: TabDef[] = [];
  if (selectedRepo) {
    const repo = selectedRepo;
    tabs.push({
      key: "overview",
      title: "Overview",
      label: "Overview",
      active: route.view === "overview",
      onSelect: () => navigate({ view: "overview", repo }),
    });
    tabs.push({
      key: "issues",
      title: "Issues",
      label: (
        <>
          Issues
          {issues.data ? (
            <span className="tab-count num">
              {issues.data.in_flight.length + issues.data.pending.length}
            </span>
          ) : null}
        </>
      ),
      active: route.view === "issues",
      onSelect: () => navigate({ view: "issues", repo, sub: "inflight" }),
    });

    for (const n of openIssues[repo] ?? []) {
      const card =
        issues.data?.in_flight.find((c) => c.issue === n) ??
        issues.data?.done.find((c) => c.issue === n) ??
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
  if (verificationOpen) {
    tabs.push({
      key: "verification",
      title: "Verification",
      label: "Verification",
      active: route.view === "verification",
      onSelect: () => navigate({ view: "verification" }),
      onClose: closeVerification,
    });
  }

  const setSub = useCallback(
    (sub: IssuesSub) => {
      if (selectedRepo) navigate({ view: "issues", repo: selectedRepo, sub }, true);
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
        onSelectRepo={(name) => navigate({ view: "overview", repo: name })}
        onOpenVerification={() => {
          setVerificationOpen(true);
          navigate({ view: "verification" });
        }}
      />
      <main className="main">
        <div className="mainbar">
          <TabStrip tabs={tabs} />
          <span className="spacer" />
          <RepoCrumb route={route} />
        </div>
        <div className="mainbody">
          <Body
            route={route}
            selectedRepo={selectedRepo}
            issues={issues}
            onSetSub={setSub}
            onOpenIssue={openIssue}
            hasRepos={repoList.length > 0}
            reposLoading={repos.loading}
          />
        </div>
      </main>
    </div>
  );
}

function Body({
  route,
  selectedRepo,
  issues,
  onSetSub,
  onOpenIssue,
  hasRepos,
  reposLoading,
}: {
  route: Route;
  selectedRepo: string | null;
  issues: ReturnType<typeof useIssues>;
  onSetSub: (s: IssuesSub) => void;
  onOpenIssue: (n: number) => void;
  hasRepos: boolean;
  reposLoading: boolean;
}) {
  if (route.view === "verification") return <VerificationTab />;
  if (route.view === "issue") return <IssueTab repo={route.repo} issue={route.issue} />;
  if (route.view === "issues" && selectedRepo)
    return (
      <IssuesTab
        repo={selectedRepo}
        sub={route.sub}
        issues={issues}
        onSetSub={onSetSub}
        onOpenIssue={onOpenIssue}
      />
    );
  if (selectedRepo) return <OverviewTab repo={selectedRepo} />;
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

/** A copyable deep link for the current view — handy for handover notes. */
function RepoCrumb({ route }: { route: Route }) {
  const [copied, setCopied] = useState(false);
  const hash = hashFor(route);
  return (
    <button
      className="btn btn-xs btn-ghost crumb"
      title="copy a deep link to this view"
      onClick={() => {
        const url = `${window.location.origin}${window.location.pathname}${hash}`;
        void navigator.clipboard?.writeText(url).then(
          () => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          },
          () => setCopied(false),
        );
      }}
    >
      <span className="mono faint">{hash}</span>
      {copied ? <span className="tone-ok"> copied</span> : null}
    </button>
  );
}
