# Full test gate

Run `make test` before every commit. It prepares one development environment
with the dev, SDK and dashboard extras, then runs every required Python,
shell, architecture and type contract. Dependency synchronization finishes
before concurrent suites start. Dashboard dependencies remain optional for
daemon installations.

The default is three Make jobs and four pytest workers per adapter/app tier.
The two longer tiers start first; a third job runs the smaller contracts.
The domain tier uses one process alongside the eight adapter/app workers. Their
independent tests use xdist's standard `load` distribution with one-item
requested maximum chunks; xdist may start workers with two items. The long
scale contracts are collected first, with the original test bodies unchanged.
Domain tests and shell contracts retain their existing
execution paths. [xdist distribution](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)
documents the scheduler's behavior.

Leave `TMPDIR` unset for routine runs. The wrapper prefers a writable
`XDG_RUNTIME_DIR`, normally RAM-backed on the reference Linux host, and removes
its private scratch directory after the complete run. An explicit `TMPDIR`
still takes precedence; disk-backed scratch can be substantially slower for
the real SQLite histories. Do not point `UV_PROJECT_ENVIRONMENT` at an external
environment when qualifying this checkout: CLI installation/healing tests
exercise the checkout's normal `.venv` and entrypoint paths.

## Measurements

Qualification uses a Ryzen 9 5900X, 64 GiB RAM, Linux 7.1.5, Python 3.12.12,
SQLite 3.50.4, warm uv dependencies and `/run/user/1000` scratch. Timings include
the entire `make test` command, including dependency preparation. CPU times
sum all child processes and can exceed wall time.

The accepted pre-change run at `7963876` took 930 seconds with two Make jobs
and serial Python tiers. The issue's older serial baseline was 1,849 seconds.
Both retained the 5,000 real-round and 4,097 real-generation contracts.
The first implementation qualification took 483 seconds and failed two newly
added retirement-boundary cases; it is diagnostic evidence, not acceptance.
A corrected complete run passed all 10,567 Python cases and the shell,
architecture and type checks in 342 seconds. Its generation test took 301
seconds and started about 40 seconds into the run. Early scale collection and
three workers per tier then passed in 319 seconds, still over target.
A fourth complete run passed in 309 seconds, still over target.
A fifth passed in 304 seconds after the real-child fixture improvement below.
A two-adapter-worker trial passed in 364 seconds: the generation case improved
to 291 seconds, but the remaining adapter cases queued behind the scale tests.
The final four-worker candidate passed twice on 2026-10-05, with the same
source and workloads: **290.51 seconds (4m50.5s)** and **279.27 seconds
(4m39.3s)**. Both include dependency preparation and every required gate.
The main tiers contain 10,585 passing cases, 56 more than the accepted
pre-change source. The ten existing residual xfails remain unchanged.

| Tier | Pre-change cases / seconds | Qualification 1 | Qualification 2 |
| --- | ---: | ---: | ---: |
| Domain | 3,673 / 6.05 | 3,673 / 7.21 | 3,673 / 6.76 |
| Adapters | 3,462 / 920.54 | 3,511 / 289.85 | 3,511 / 278.58 |
| Application | 3,394 / 860.93 | 3,401 / 167.48 | 3,401 / 163.43 |
| Complete gate | 930 | 290.51 | 279.27 |

The final scale tests took 287.51/276.16 seconds for 4,097 generations and
162.98/158.51 seconds for 5,000 rounds. Their original function bodies and
outcome/read-bound assertions are byte-identical to `7963876`; only their
collection location changed. The older issue measurement of 477.61/204.42
seconds used an earlier source and is historical context, not the same-source
comparison above.

The qualified source is this change on top of `7963876`; the uncommitted
source manifest was identical before and after both runs. Qualification 1 ran
05:13:16–05:18:06 UTC, qualification 2 ran 05:19:46–05:24:25 UTC. Child CPU
was 1,267.61 user + 162.55 system seconds and 1,235.52 + 156.97 seconds.
Maximum individual child RSS was 773,876/785,228 KiB, not aggregate memory.
The first run's recorded process identities were gone afterward, and both
private scratch roots and observed suite processes were gone after exit.

Reproduce on the reference host with warm dependencies:

```bash
env -u TMPDIR -u UV_PROJECT_ENVIRONMENT XDG_RUNTIME_DIR=/run/user/1000 \
  /usr/bin/time -p make test
```

These times apply to the documented host and scratch storage. Smaller CI hosts
can take longer; the required checks and serial diagnostic stay the same.
CI uses the runner's disk-backed temporary directory because its runtime
directory filled during the full SQLite histories. It runs two pytest workers
and one Make job to fit the private runner's two CPUs, and records memory,
disk capacity and inode availability before testing. The later hostile-environment
check keeps the dependency environment prepared by the complete gate.

Profiling identified delivered-outbox scans, repeated retirement decoding,
repeated same-transaction proofs, SQL metadata preparation and real retry waits
in the immediate executor fake. Fixture setup was negligible; no shared mutable
fixture cache was added. Changes retain fresh authority reads, cumulative SQL
byte/row limits, FULL SQLite durability and emit-before-ack outbox semantics.
Schema 40 adds partial indexes only; malformed or noncanonical retirement
receipts still take the complete decoder path. [SQLite partial indexes](https://www.sqlite.org/partialindex.html)
describe the query predicate requirement.

An app acceptance case spent 20.22 of its 25.20 seconds launching 60 Python
children which each imported the control stack only to register a lease.
The fixture now holds a minimal real child on stdin while the supervisor
registers its actual PID, start ticks and isolated process group through the
same public guarded API, then releases and reaps it. The same case passes in
4.38 seconds. Refusal, interruption and timeout cases verify child cleanup;
production child-side bootstrap and shutdown contracts remain unchanged.

SQL column templates are bounded and reused only inside one outer read-budget
scope, where schema changes are prohibited; at most 128 templates of 16,384
characters are retained. Ending the scoped transaction refuses further SQL.
Every execution still queries
fresh metadata and charges every active budget. Canonical outcome reuse
requires exact bytes matching a receipt decoded in that same read; other
encodings retain full decoding and all accounting/effect reconstruction.

The codec retains at most 128 private JSON parse trees for inputs up to 16 KiB
of UTF-8. Its decoder reads these trees without mutating or exposing them;
every call reconstructs typed values and runs constructor/root validation.
Database values, budget charges, evidence validation and revision checks stay
fresh. A 200-generation sample improved from 20.93 to 18.94 seconds under
cProfile, and 13.97 to 12.91 seconds without profiling. The full-gate result,
not this small sample, determines acceptance. This uses the standard bounded
[functools cache](https://docs.python.org/3.12/library/functools.html#functools.lru_cache).

## Fast feedback and serial diagnosis

Prepare dependencies once, then run the specific behavioral contract:

```bash
uv sync --extra dev --extra sdk --extra dash
uv run --no-sync pytest tests/app/test_loop.py
make test-domain
```

These focused commands support iteration; the commit gate remains `make test`.
On the reference host, the combined executor/SQL/daemon selection passed in
2.70 seconds, `make test-domain` in 6.54 seconds
with dependencies already prepared. All are below 60 seconds.
The real 4,097/5,000 scale tests and broad acceptance replay selections are
slower categories; select a specific outcome contract for iteration, and keep
their complete coverage in the full gate.
For process isolation or ordering diagnosis, run the same complete gate serially:

```bash
make TEST_WORKERS=0 TEST_SUITE_JOBS=1 test
```

The default prints the ten slowest pytest setup/call/teardown durations per
tier. Override `PYTEST_TIMING='--durations=30'` for more detail. Measure full
wall time with `/usr/bin/time -p make test`, recording the source revision,
worker counts, scratch filesystem and dependency state. Do not compare
cProfile timings directly with unprofiled qualification.
