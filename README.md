# autonomous-issue-agent

Label a GitHub issue, and an AI agent fixes it. A second, independent AI
reviews the fix. If automated tests pass and the reviewer approves, the change
merges into your development branch (`dev` by default). A human stays in charge
of what reaches production.

It runs on your own machine, against your own repositories, with your own AI
accounts. Nothing is hosted for you.

## How one issue flows

1. **You label the issue `ai-ready`.** That is the only trigger.
2. **Planning.** A small, cheap AI pass writes acceptance criteria (what "done"
   means) and flags likely duplicates. A second pass grades the difficulty.
   Harder issues get a stronger (and pricier) model.
3. **Fix.** The fixer agent works in its own checkout. It writes tests first,
   makes the change, and pushes a branch.
4. **Pull request.** The harness opens a pull request (a "PR": a proposed
   change awaiting review) from that branch.
5. **CI.** Your existing automated checks run (continuous integration, or
   "CI"). If they fail, the fixer gets the failure and tries again. A PR with no
   CI checks at all will not merge.
6. **Independent review.** A separate reviewer agent, under a different GitHub
   identity, reads the change against the acceptance criteria. It approves or
   requests changes.
7. **Merge.** Approved and green means the PR merges into `dev`.

It stops and waits for a person, by adding the label `needs-human` and leaving a
comment, when it:

- is unsure what you want, or the agent asks a question;
- thinks the issue is a duplicate;
- runs out of attempts or reaches a spend limit;
- touches CI configuration (`.github/**`), which always needs human review.

```text
ai-ready -> plan -> fix -> PR -> CI -> review -> merge to dev
                     ^             |       |
                     +-- failures -+-------+      any stuck point -> needs-human
```

Each paid step of this is called a "leg" in the docs: one run of an agent with
its own budget.

## Why you can trust it, and where you can't

What protects you:

- **The reviewer cannot approve its own work.** Fixer and reviewer use two
  different GitHub accounts, and setup refuses to run if they are the same.
- **Branch protection still applies.** The fixer account is not an admin. A
  direct push to a protected branch is refused, so changes go through a PR.
- **Every agent runs in a sandbox.** Each agent can see only its own checkout,
  a private home directory and its tools. It cannot read the harness
  configuration, your SSH keys, your own GitHub login or other repositories.
  The fixer holds one scoped GitHub token. The reviewer holds none.
- **Spend is capped.** Limits apply per leg, per issue, per day and, optionally,
  across all repositories at once. Each leg reserves its maximum cost before it
  starts. The limits are checked in a database transaction, so they hold even
  when several agents run at once.
- **It asks when unsure.** Ambiguity, repeated failures and suspected duplicates
  end in `needs-human`, not a guess.
- **It never touches production.** It merges only to the development branch.
  Promoting to production is your call.

What it does not protect against:

- **Agents can run any command inside their sandbox**, and the network is on.
  A hostile issue or a poisoned file in your repository could try to steer an
  agent. The sandbox limits what that agent can reach, but does not make it
  harmless.
- **The fixer's token can still do things its role allows.** It can push other
  unprotected branches and comment on PRs. Use branch protection that requires
  an approving review.
- **An AI reviewer is not a human reviewer.** It misses things. Treat the
  development branch as a staging area.
- **Reported costs are the harness's own figures**: token counts priced at list
  rates, not provider invoices.

Use it first on repositories where a bad merge to the development branch is
cheap to undo. The full, plain list of accepted risks is in
[docs/open/residuals.md](docs/open/residuals.md).

## What it costs

You pay your AI provider directly. The harness adds nothing on top.

Real results so far:

| Run | Result |
|---|---|
| Overnight run, 14-15 Oct 2026, a Tauri desktop app repository | 10 of 10 issues merged. About $5.60 per merged PR on average (range $0.40 to $37.80). 7 of 10 approved on the first review. |
| Earlier batches | Roughly $5 to $7 per merged PR. |

These are our numbers on our repositories, not a promise for yours.

Three things drive cost:

- **Model tier.** Easy issues run on small models. Hard ones escalate to
  stronger, pricier models. You choose the models.
- **Issue difficulty.** Bigger changes take longer and use more tokens.
- **Review rounds.** Each time the reviewer asks for changes, the fixer and
  reviewer both run again.

The default limits are $10 per fix leg, $15 per review leg, $25 per issue and
$700 per repository per day. You can change all of them. When an issue hits its
cap, it stops and asks a human.

## What you need

- **Linux.** macOS is not supported for running agents, because the sandbox
  needs Linux. Paid agent runs are refused there rather than run without a
  sandbox.
- **bubblewrap** (`bwrap`), version 0.10 or newer, with unprivileged user
  namespaces. Some older distributions ship an older version. See
  [HARNESS.md](HARNESS.md).
- **A GitHub repository** that has CI checks, ideally with branch protection.
- **Two GitHub accounts or tokens:** one for the fixer, one for the reviewer.
- **An AI provider:** a Claude subscription or API key. OpenAI Codex is
  optional.
- `git`, `gh`, `jq` and `uv`.

## Getting started

```bash
make install                 # install the issue-agent command
cp config/shared.example.env config/shared.env   # add your tokens

cd /path/to/your-repo
issue-agent doctor           # checks your setup
issue-agent start            # runs in the foreground; Ctrl-C stops it
issue-agent ready 42         # label issue #42 ai-ready
```

Start with one small issue and watch it. When you are happy,
run `issue-agent install-service` to keep it running in the background under
systemd.

This is a quickstart, not a full guide. [HARNESS.md](HARNESS.md) covers
accounts, tokens, configuration and recovery.

## Day to day

- **Dashboard.** Run `issue-agent dashboard`, then open `http://localhost:8321`.
  You see what is running, what is waiting on a person and what has been spent.
  It can pause an issue (add `needs-human`) or release it (remove the label).
  It changes nothing else.
- **Labels.** `ai-ready` hands an issue to the agent. `needs-human` means it is
  waiting for you.
- **Answering a question.** Reply on the issue or PR thread where the agent left
  its comment. Your reply releases it and it carries on. If you just want to
  resume it, removing `needs-human` also works.
- **Status.** `issue-agent status` prints a summary in the terminal.

## FAQ

**Will it merge to `main`?**
No. It merges only into the development branch (`dev` by default). Promoting to
`main` or production is a human decision.

**What if it gets stuck?**
It labels the issue `needs-human` and explains why in a comment. Failures back
off rather than retry in a tight loop, and each issue has a limit on attempts
and spend. Nothing burns money in the background without bound.

**Can I stop it?**
Yes. Press Ctrl-C in the foreground, or run `issue-agent stop`. Running agents
get a short window to finish, then they are killed. Money already spent is
recorded. To stop a single issue, add `needs-human` to it.

**Which repositories does it work on?**
Any GitHub repository that has CI checks. It works
best where tests are reliable. One install can serve several repositories.

**Does my code leave my machine?**
Your code goes to the AI provider you configure, as it would with any AI coding
tool. The harness itself runs locally and has no hosted service.

**Do I need to babysit it?**
Not constantly. Check the dashboard now and then, and answer `needs-human`
questions. Early on, watch it more closely.

## For developers

- [Architecture](docs/reference/architecture.md): how it works and why.
- [HARNESS.md](HARNESS.md): the operator manual (setup, running, recovery).
- [Documentation index](docs/README.md): which documents are current.
- [Accepted risks](docs/open/residuals.md) and [design decisions](docs/adr/).
- [CLAUDE.md](CLAUDE.md): working rules for contributors and coding agents.
- Run the tests with `make test`; it must pass before any commit. Package
  management uses `uv`, and type checking uses `make typecheck`.
