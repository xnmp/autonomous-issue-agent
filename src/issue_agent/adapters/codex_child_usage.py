"""Bounded native-child observation within one Codex reservation.

Codex 0.160 persists per-response usage but excludes it from the parent token
counter. Forks drop inherited usage records; copied event messages remain.
Owned native call/output receipts and child metadata establish lineage before pricing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import os
import uuid
from pathlib import Path

from ..domain.contracts import Money, TokenUsage, UsageSource
from ..domain.pricing import ModelRates, RateTable, UnknownModelPolicy
from ..domain.run_usage import MeasuredRunUsage, ModelUsage, RunUsage, EstimatedRunUsage, usage_lower_bound


class NativeFailure(str, Enum):
    """Meter failure classes recovery may act on; the value is the persisted text.

    Failures are frozen into spend evidence as prose. Only a class whose cause is
    fully determined by the retained rollouts may be re-metered, so recovery
    parses the persisted text here once instead of matching strings itself.
    """

    AMBIGUOUS_TOKEN_COUNTS = "native response usage is missing or token counts are ambiguous"
    UNFINISHED_TURNS = "native child has unfinished turns or turns without usage"

    @classmethod
    def parse(cls, raw: object) -> "NativeFailure | None":
        return next((member for member in cls if raw == member.value), None)


def normalize_usage(raw: dict) -> TokenUsage:
    if not isinstance(raw, dict) or not {"input_tokens", "output_tokens"} <= raw.keys():
        raise ValueError("native usage record lacks explicit input/output counters")
    values = tuple(raw.get(k, 0) for k in (
        "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens"))
    if any(type(v) is not int or not 0 <= v <= 10**12 for v in values):
        raise ValueError("usage counters are malformed or outside the supported bound")
    inputs, outputs, cached, written = values
    if cached + written > inputs:
        raise ValueError("cache tokens exceed inclusive input")
    return TokenUsage(inputs - cached - written, outputs, cached, written, UsageSource.ROLLOUT)


def total_usage_of(usages: list[TokenUsage]) -> TokenUsage:
    return TokenUsage(sum(u.input_tokens for u in usages), sum(u.output_tokens for u in usages),
                      sum(u.cache_read_tokens for u in usages), sum(u.cache_write_tokens for u in usages),
                      UsageSource.ROLLOUT)


def root_usage(raw: dict) -> TokenUsage:
    if (not isinstance(raw, dict) or raw.get("source") != UsageSource.ROLLOUT.value
            or not {"input_tokens", "output_tokens"} <= raw.keys()):
        raise ValueError("parent usage lacks trusted rollout input/output counters")
    return normalize_usage({
        "input_tokens": raw.get("input_tokens", 0),
        "output_tokens": raw.get("output_tokens", 0),
        "cached_input_tokens": raw.get("cache_read_input_tokens", 0),
        "cache_write_input_tokens": raw.get("cache_creation_input_tokens", 0),
    })


def _counter_delta(current: tuple[int, ...], baseline: tuple[int, ...]) -> TokenUsage:
    values = tuple(max(0, a - b) for a, b in zip(current, baseline))
    return TokenUsage(values[0], values[1], values[2], values[3], UsageSource.ROLLOUT)


def _counters(usage: TokenUsage) -> tuple[int, ...]:
    return usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens


def _thread_spawn(source: object) -> dict:
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    return spawn if isinstance(spawn, dict) else {}


def _timestamp(raw: object) -> float:
    if not isinstance(raw, str):
        raise ValueError("native usage event lacks a timestamp")
    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("native usage timestamp is not timezone-aware")
    return stamp.timestamp()


def rate_table_evidence(table: RateTable | None) -> dict | None:
    if table is None:
        return None
    return {"id": table.rate_card_id, "version": table.version, "currency": table.currency,
            "rates": {name: asdict(rates) for name, rates in table.rates.items()},
            "unknown_model_policy": table.unknown_model_policy.value,
            "fallback": asdict(table.fallback) if table.fallback is not None else None}


def recovered_rate_table(raw: dict | None) -> RateTable | None:
    if raw is None:
        return None
    if (not isinstance(raw, dict) or not isinstance(raw.get("rates"), dict) or len(raw["rates"]) > 256
            or not all(isinstance(raw.get(key), str) and 0 < len(raw[key]) <= 256
                       for key in ("id", "version", "currency"))
            or not all(isinstance(name, str) and 0 < len(name) <= 256 for name in raw["rates"])):
        raise ValueError("native recovery price card is malformed")
    def rates(value: dict) -> ModelRates:
        if not isinstance(value, dict) or any(type(v) is not int or v <= 0 for v in value.values()):
            raise ValueError("native recovery rates require positive integers")
        return ModelRates(**value)
    return RateTable(raw["id"], raw["version"], raw["currency"],
                     {name: rates(value) for name, value in raw["rates"].items()},
                     UnknownModelPolicy(raw["unknown_model_policy"]),
                     rates(raw["fallback"]) if raw.get("fallback") is not None else None)


@dataclass
class _Rollout:
    path: Path
    session_id: str
    offset: int = 0
    tail: bytes = b""
    inode: tuple[int, int] | None = None
    metadata: dict | None = None
    created_at: float | None = None
    own_start: int = 0
    active_turn: str = ""
    models: dict[str, tuple[str, float]] = field(default_factory=dict)
    usage: dict[str, tuple[str, TokenUsage, float, Money | None]] = field(default_factory=dict)
    cumulative: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0, source=UsageSource.ROLLOUT))
    baseline_counter: tuple[int, ...] | None = None
    snapshot_baseline: tuple[int, ...] = (0, 0, 0, 0)


@dataclass
class CodexChildUsageMeter:
    home: str
    started_at: float
    rates: RateTable | None
    cwd: str
    max_children: int = 64
    max_files: int = 512
    max_bytes: int = 256 * 1024 * 1024
    max_record_bytes: int = 16 * 1024 * 1024
    max_responses: int = 4096
    # Recovery bound: events after the run's harness-owned end belong to a later
    # leg that may resume the same sessions. A missing timestamp fails closed.
    ended_at: float | None = None
    roots: dict[str, ModelUsage] = field(default_factory=dict, init=False)
    rollouts: dict[str, _Rollout] = field(default_factory=dict, init=False)
    children: dict[str, tuple[str, str | None]] = field(default_factory=dict, init=False)
    spawn_calls: dict[tuple[str, str], str | None] = field(default_factory=dict, init=False)
    spawn_outputs: dict[tuple[str, str], tuple[str | None, str | None]] = field(default_factory=dict, init=False)
    followups: set[tuple[str, str, str]] = field(default_factory=set, init=False)
    failure: str = field(default="", init=False)
    _bytes: int = field(default=0, init=False)
    _metadata_headers: dict[Path, dict] = field(default_factory=dict, init=False)

    def seed_baseline(self, session_id: str, usage: dict | None) -> None:
        """The harness's pre-launch snapshot overrides copied history counters."""
        if usage is None:
            return
        try:
            rollout = self._locate(session_id)
            tokens = root_usage(usage)
            if rollout is not None and rollout.offset == 0:
                rollout.baseline_counter = rollout.snapshot_baseline = _counters(tokens)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.failure = str(exc)[:2048]

    def _directories(self, session_id: str = "") -> set[Path]:
        start = datetime.fromtimestamp(self.started_at, timezone.utc).date()
        today = datetime.now(timezone.utc).date()
        if self.ended_at is not None:
            today = min(today, datetime.fromtimestamp(self.ended_at, timezone.utc).date())
        if not 0 <= (today - start).days <= 7:
            raise ValueError("native rollout discovery exceeded its invocation date bound")
        # Codex filenames use local dates, while metadata uses UTC. Adjacent
        # days cover UTC offsets without enumerating the lifetime archive.
        dates = {start + timedelta(days=i) for i in range(-1, (today - start).days + 2)}
        if session_id:
            identity = uuid.UUID(session_id)
            if identity.version != 7:
                raise ValueError("native session identity does not provide its creation date")
            born = datetime.fromtimestamp((identity.int >> 80) / 1000, timezone.utc).date()
            dates.update(born + timedelta(days=i) for i in (-1, 0, 1))
        return {Path(self.home) / "sessions" / day.strftime("%Y/%m/%d") for day in dates}

    def _paths(self, directories: set[Path], session_id: str = ""):
        entries = 0
        for directory in sorted(directories):
            try:
                stream = os.scandir(directory)
            except FileNotFoundError:
                continue
            with stream:
                for entry in stream:
                    entries += 1
                    # Shared homes include operator sessions. Bound enumeration
                    # separately from the owned files we open for accounting.
                    if entries > 65536:
                        raise ValueError("native rollout discovery exceeded its scan bound")
                    if (entry.name.startswith("rollout-")
                            and entry.name.endswith(f"-{session_id}.jsonl" if session_id else ".jsonl")):
                        yield Path(entry.path)

    def _locate(self, session_id: str) -> _Rollout | None:
        if session_id in self.rollouts:
            return self.rollouts[session_id]
        # Native identities are UUIDs, never agent-supplied glob/path fragments.
        try:
            uuid.UUID(session_id)
        except (ValueError, AttributeError):
            return None
        paths = list(self._paths(self._directories(session_id), session_id))
        if len(paths) > self.max_files:
            raise ValueError("native rollout discovery exceeded its file bound")
        if len(paths) > 1:
            raise ValueError("native session has ambiguous rollout paths")
        if not paths:
            return None
        if len(self.rollouts) >= self.max_files:
            raise ValueError("native rollout discovery exceeded its file bound")
        if len(self.rollouts) >= self.max_children + 1:
            raise ValueError("native rollout observer exceeded its session bound")
        found = _Rollout(Path(paths[0]), session_id)
        self.rollouts[session_id] = found
        return found

    def _candidate_children(self) -> None:
        for path in self._paths(self._directories()):
            meta = self._metadata_headers.get(path)
            if meta is None:
                try:
                    with path.open("rb") as stream:
                        line = stream.readline(65537)
                except OSError:
                    continue
                self._bytes += len(line)
                if self._bytes > self.max_bytes:
                    raise ValueError("native observer exceeded its byte bound")
                if len(line) > 65536:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "session_meta":
                    continue
                meta = event.get("payload") or {}
                if not isinstance(meta, dict):
                    continue
                self._metadata_headers[path] = meta
            source = meta.get("source")
            parent = _thread_spawn(source).get("parent_thread_id")
            sid = meta.get("id") or meta.get("session_id")
            if parent in self.rollouts and isinstance(sid, str) and sid not in self.rollouts:
                self._locate(sid)

    def _ancestor_turns(self, rollout: _Rollout) -> set[str]:
        turns: set[str] = set()
        visited = {rollout.session_id}
        parent = _thread_spawn((rollout.metadata or {}).get("source")).get("parent_thread_id")
        while parent in self.rollouts and parent not in visited:
            visited.add(parent)
            ancestor = self.rollouts[parent]
            turns.update(ancestor.models)
            parent = _thread_spawn((ancestor.metadata or {}).get("source")).get("parent_thread_id")
        return turns

    def _event(self, rollout: _Rollout, event: dict) -> None:
        if self.ended_at is not None and _timestamp(event.get("timestamp")) > self.ended_at:
            return
        kind, payload = event.get("type"), event.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("native rollout payload is malformed")
        if rollout.metadata is None and kind == "session_meta":
            if (payload.get("id") or payload.get("session_id")) != rollout.session_id:
                raise ValueError("native rollout disagrees with its canonical session")
            rollout.metadata = payload
            rollout.created_at = _timestamp(event.get("timestamp"))
            start = payload.get("subagent_history_start_ordinal")
            if start is not None:
                if type(start) is not int or not 1 <= start <= 10**9:
                    raise ValueError("native inherited-context boundary is malformed")
                rollout.own_start = start
            return
        if rollout.metadata is None:
            return
        if rollout.own_start:
            ordinal = event.get("ordinal")
            if type(ordinal) is not int:
                raise ValueError("native copied fork lacks projected ordinals")
            if ordinal < rollout.own_start:
                return
        if kind == "session_meta":
            raise ValueError("native canonical metadata was replaced outside inherited context")
        observed_at = _timestamp(event.get("timestamp"))
        if kind == "turn_context":
            turn, model = payload.get("turn_id"), payload.get("model")
            if isinstance(turn, str) and isinstance(model, str) and turn and model:
                prior = rollout.models.get(turn)
                if prior and prior[0] and prior[0] != model:
                    raise ValueError("native model changed within an observed turn")
                if prior is None and sum(len(r.models) for r in self.rollouts.values()) >= self.max_responses:
                    raise ValueError("native observer exceeded its turn bound")
                rollout.models[turn] = (model, observed_at)
                rollout.active_turn = turn
            return
        if kind == "event_msg" and payload.get("type") in {"task_started", "turn_started", "task_complete", "turn_complete"}:
            turn = payload.get("turn_id")
            if isinstance(turn, str) and turn:
                if turn not in rollout.models and sum(len(r.models) for r in self.rollouts.values()) >= self.max_responses:
                    raise ValueError("native observer exceeded its turn bound")
                rollout.models[turn] = (rollout.models.get(turn, ("", 0))[0], observed_at)
                rollout.active_turn = turn
            return
        # Forks without ordinal boundaries retain ancestor event messages.
        # Exclude them before they can seed the child's cumulative baseline.
        if payload.get("turn_id", rollout.active_turn) in self._ancestor_turns(rollout):
            return
        before_start = observed_at < self.started_at
        if kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                return
            total = normalize_usage(info.get("total_token_usage"))
            if before_start:
                rollout.baseline_counter = _counters(usage_lower_bound(total,
                    _counter_delta(rollout.baseline_counter or (0, 0, 0, 0), (0, 0, 0, 0))))
            else:
                rollout.cumulative = usage_lower_bound(rollout.cumulative, total)
            return
        if before_start and kind != "token_usage_record":
            return
        if kind == "response_item":
            item_kind, call_id = payload.get("type"), payload.get("call_id")
            if item_kind == "function_call" and payload.get("name") in {"followup_task", "send_input", "send_message", "resume_agent"}:
                args = json.loads(payload.get("arguments", ""))
                target = (args.get("target") or args.get("id") or args.get("agent_id")) if isinstance(args, dict) else None
                if not isinstance(target, str) or not target or len(self.followups) >= self.max_responses:
                    raise ValueError("native follow-up lacks bounded target identity")
                try:
                    uuid.UUID(target)
                except ValueError:
                    source = (rollout.metadata or {}).get("source") or {}
                    sender = _thread_spawn(source).get("agent_path") or "/root"
                    target = target if target.startswith("/") else sender + "/" + target
                self.followups.add((rollout.session_id, target, payload["name"]))
            if item_kind == "function_call" and payload.get("name") == "spawn_agent":
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("native spawn call lacks identity")
                args = json.loads(payload.get("arguments", ""))
                if not isinstance(args, dict):
                    raise ValueError("native spawn arguments are malformed")
                task_name = args.get("task_name")
                if task_name is not None and (not isinstance(task_name, str) or not task_name
                                               or len(task_name) > 256 or "/" in task_name):
                    raise ValueError("native spawn task name is malformed")
                key = (rollout.session_id, call_id)
                if key in self.spawn_calls and self.spawn_calls[key] != task_name:
                    raise ValueError("native spawn call changed its task identity")
                if len(self.spawn_calls) >= self.max_children and key not in self.spawn_calls:
                    raise ValueError("native observer exceeded its child bound")
                self.spawn_calls[key] = task_name
            elif item_kind == "function_call_output" and (rollout.session_id, call_id) in self.spawn_calls:
                key = (rollout.session_id, call_id)
                output = payload.get("output")
                try:
                    result = json.loads(output) if isinstance(output, str) else None
                except ValueError:
                    result = None
                # A failed native tool has no success-shaped output. New child
                # metadata without a matching success receipt remains unknown.
                if not isinstance(result, dict):
                    return
                sid, path = result.get("agent_id"), result.get("task_name")
                if (sid is not None and path is not None
                        or result.keys() - {"agent_id", "task_name", "nickname"}):
                    raise ValueError("native spawn output shape is unqualified")
                if sid is not None:
                    uuid.UUID(sid)
                elif path is not None:
                    requested = self.spawn_calls[key]
                    parent_source = (rollout.metadata or {}).get("source") or {}
                    parent_path = _thread_spawn(parent_source).get("agent_path") or "/root"
                    if (not isinstance(path, str) or not path.startswith("/")
                            or requested is None or path != parent_path + "/" + requested):
                        raise ValueError("native spawn output disagrees with requested task")
                else:
                    return
                identity = (sid, path)
                nickname = result.get("nickname")
                if nickname is not None and not isinstance(nickname, str):
                    raise ValueError("native spawn nickname is malformed")
                if key in self.spawn_outputs and self.spawn_outputs[key] != identity:
                    raise ValueError("native spawn output changed child identity")
                self.spawn_outputs[key] = identity
            return
        if kind == "token_usage_record":
            if payload.get("thread_id") != rollout.session_id:
                raise ValueError("native usage belongs to another thread")
            response, turn = payload.get("response_id"), payload.get("turn_id")
            if not isinstance(response, str) or not response or not isinstance(turn, str) or not turn:
                raise ValueError("native usage lacks response or turn identity")
            try:
                tokens = normalize_usage(payload.get("usage") or {})
            except ValueError:
                tokens = TokenUsage(0, 0, source=UsageSource.ROLLOUT)
                self.failure = "receipt token usage unavailable; using its reported cost"
            counter = payload.get("thread_token_usage")
            try:
                cumulative = normalize_usage(counter) if counter is not None else None
            except ValueError:
                cumulative = None
                self.failure = "receipt cumulative counters malformed; using its request usage"
            if cumulative is not None:
                if before_start:
                    rollout.baseline_counter = _counters(usage_lower_bound(cumulative,
                        _counter_delta(rollout.baseline_counter or (0, 0, 0, 0), (0, 0, 0, 0))))
                else:
                    rollout.cumulative = usage_lower_bound(rollout.cumulative, cumulative)
            cost = payload.get("cost_usd")
            try:
                reported = Money.from_major("USD", cost) if cost is not None else None
                if reported is not None and reported.micros > 2**63 - 1:
                    raise ValueError("receipt cost exceeds SQLite's money bound")
            except (ValueError, TypeError, ArithmeticError):
                reported = None
                self.failure = "receipt cost malformed; using its token usage"
            value = (turn, tokens, observed_at, reported)
            if response in rollout.usage:
                prior = rollout.usage[response]
                if (prior[0], prior[1], prior[3]) != (turn, tokens, reported):
                    raise ValueError("native response has conflicting usage")
                return
            if sum(len(r.usage) for r in self.rollouts.values()) >= self.max_responses:
                raise ValueError("native observer exceeded its response bound")
            rollout.usage[response] = value
            return

    def _resolve_spawn_outputs(self, *, final: bool) -> None:
        for (parent, _), (sid, path) in self.spawn_outputs.items():
            if sid is None:
                matches = []
                for rollout in self.rollouts.values():
                    source = (rollout.metadata or {}).get("source") or {}
                    spawn = _thread_spawn(source)
                    if spawn.get("parent_thread_id") == parent and spawn.get("agent_path") == path:
                        matches.append(rollout.session_id)
                if len(matches) > 1:
                    raise ValueError("native spawn path has ambiguous children")
                if not matches:
                    if final:
                        raise ValueError("successful native spawn has no corroborating child")
                    continue
                sid = matches[0]
            else:
                self._locate(sid)
            identity = (parent, path)
            if sid in self.children and self.children[sid] != identity:
                raise ValueError("native child has conflicting lineage")
            self.children[sid] = identity

    def _read(self, rollout: _Rollout, *, final: bool) -> None:
        info = rollout.path.stat()
        identity = (info.st_dev, info.st_ino)
        if rollout.inode is not None and (rollout.inode != identity or info.st_size < rollout.offset):
            raise ValueError("native usage rollout was replaced or truncated")
        rollout.inode = identity
        remaining = self.max_bytes - self._bytes
        with rollout.path.open("rb") as stream:
            stream.seek(rollout.offset)
            data = stream.read(remaining + 1)
        if len(data) > remaining:
            raise ValueError("native observer exceeded its byte bound")
        self._bytes += len(data)
        rollout.offset += len(data)
        records = (rollout.tail + data).split(b"\n")
        rollout.tail = records.pop()
        if len(rollout.tail) > self.max_record_bytes:
            raise ValueError("native usage record exceeds its size bound")
        if final and rollout.tail:
            try:
                json.loads(rollout.tail)
            except ValueError:
                pass
            else:
                records.append(rollout.tail)
                rollout.tail = b""
        for raw in records:
            if not raw:
                continue
            if len(raw) > self.max_record_bytes:
                raise ValueError("native usage record exceeds its size bound")
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("native usage event is malformed")
                self._event(rollout, event)
            except (ValueError, TypeError, AttributeError) as exc:
                self.failure = str(exc)[:2048] or "native usage record is malformed"

    def sample(self, session_id: str, model: str, usage: dict | None, *, final: bool = False) -> RunUsage:
        """Price each request's best evidence plus per-category counter remainders.

        The same lower bound drives live caps and terminal settlement. Copied
        ancestor history and pre-launch usage cannot become this leg's spend.
        """
        try:
            if session_id and model and (usage is not None or session_id not in self.roots):
                try:
                    self.roots[session_id] = ModelUsage(session_id, model,
                        root_usage(usage) if usage is not None else TokenUsage(0, 0, source=UsageSource.ROLLOUT))
                except (ValueError, TypeError) as exc:
                    self.failure = str(exc)[:2048]
            if session_id:
                self._locate(session_id)
            for _ in range(self.max_children + 1):
                before = len(self.rollouts)
                for rollout in tuple(self.rollouts.values()):
                    self._read(rollout, final=final)
                self._candidate_children()
                self._resolve_spawn_outputs(final=False)
                if len(self.rollouts) == before:
                    break
            self._resolve_spawn_outputs(final=final)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.failure = str(exc)[:2048] or "native child usage unavailable"
        samples: list[ModelUsage] = []
        gap = self.failure
        for sid in sorted(set(self.roots) | set(self.children) | ({session_id} if session_id else set())):
            rollout = self.rollouts.get(sid)
            root_model = self.roots[sid].model if sid in self.roots else model if sid == session_id else ""
            if sid in self.children:
                parent, path = self.children[sid]
                meta = rollout.metadata if rollout else None
                source = meta.get("source") if meta else None
                spawn = _thread_spawn(source)
                if (not meta or meta.get("cli_version") != "0.160.0"
                        or spawn.get("parent_thread_id") != parent
                        or path is not None and spawn.get("agent_path") != path
                        or not isinstance(meta.get("cwd"), str)
                        or os.path.realpath(meta["cwd"]) != os.path.realpath(self.cwd)):
                    self.failure = self.failure or "native child lacks corroborated lineage, protocol or cwd"
                    continue
            grouped: dict[str, list[TokenUsage]] = {}
            inherited = self._ancestor_turns(rollout) if rollout else set()
            receipts = {response: value for response, value in rollout.usage.items()
                        if value[0] not in inherited and value[2] >= self.started_at} if rollout else {}
            prior = total_usage_of([tokens for turn, tokens, at, _ in rollout.usage.values()
                                   if turn not in inherited and at < self.started_at]) if rollout else TokenUsage(0, 0)
            baseline = usage_lower_bound(prior, _counter_delta(
                rollout.baseline_counter or (0, 0, 0, 0), (0, 0, 0, 0))) if rollout else prior
            cumulative = _counter_delta(_counters(rollout.cumulative), _counters(baseline)) if rollout else TokenUsage(0, 0)
            if sid in self.roots:
                # Parent deltas use the frozen snapshot, not subsequently observed
                # pre-launch totals; receipt lag must be subtracted from that base.
                lag = _counter_delta(_counters(baseline), rollout.snapshot_baseline) if rollout else TokenUsage(0, 0)
                cumulative = usage_lower_bound(cumulative, _counter_delta(_counters(self.roots[sid].usage), _counters(lag)))
            receipt_total = total_usage_of([tokens for _, tokens, _, _ in receipts.values()])
            remainder = _counter_delta(_counters(cumulative), _counters(receipt_total))
            for response, (turn, tokens, _, cost) in receipts.items():
                actual = (rollout.models.get(turn, ("", 0))[0] or root_model) if rollout else root_model
                actual = actual.strip().casefold() or "<unreported-model>"
                if cost is None and (self.rates is None or not self.rates.priceable(actual)):
                    self.failure = self.failure or "request model has no configured rates; request charged zero"
                else:
                    try:
                        samples.append(ModelUsage(sid, actual, tokens, cost, response))
                    except ValueError:
                        self.failure = self.failure or "request identity or counters malformed; request charged zero"
            if remainder.total_tokens:
                gap = gap or "request receipts unavailable; cumulative usage priced at configured rates"
                possible = {(actual or root_model).strip().casefold() for turn, (actual, at) in rollout.models.items()
                            if at >= self.started_at and turn not in inherited} if rollout else set()
                possible |= {root_model.strip().casefold()} if root_model else set()
                priced = {name: self.rates.rates_for(name) for name in sorted(possible)
                          if self.rates is not None and self.rates.priceable(name)}
                if len(priced) != len(possible) or not priced:
                    self.failure = self.failure or "cumulative usage has an unpriced possible model"
                # Counters are lower bounds, never complete model boundaries.
                # Missing receipts cannot justify choosing a cheaper model.
                for i, count in enumerate(_counters(remainder)):
                    if count and priced:
                        field = ("input_micros", "output_micros", "cache_read_micros", "cache_write_micros")[i]
                        chosen = max(priced, key=lambda name: getattr(priced[name], field))
                        grouped.setdefault(chosen, []).append(TokenUsage(*(count if j == i else 0 for j in range(4)), source=UsageSource.ROLLOUT))
            if final and rollout and {turn for turn, (_, at) in rollout.models.items() if at >= self.started_at} - inherited - {turn for turn, _, _, _ in receipts.values()}:
                gap = gap or "request receipts absent; usable cumulative usage estimated, otherwise charged zero"
            if rollout and rollout.tail:
                gap = gap or "truncated or malformed request tail; unusable request charged zero"
            if final and not grouped and not receipts:
                gap = gap or "participant request receipts and usage absent; unusable requests charged zero"
            for actual, tokens in grouped.items():
                try:
                    samples.append(ModelUsage(sid, actual, total_usage_of(tokens)))
                except ValueError:
                    self.failure = self.failure or "request identity or counters malformed; request charged zero"
        if any(r.session_id not in self.children and r.created_at is not None
                         and r.created_at >= self.started_at
                         and isinstance((r.metadata or {}).get("source"), dict)
                         for r in self.rollouts.values()):
            self.failure = self.failure or "native child lacks its parent's durable spawn receipt"
        participants = set(self.roots) | set(self.children)
        paths = {path for _, path in self.children.values() if path} | {"/root"}
        for parent, target, operation in self.followups:
            if (parent not in participants or target not in participants | paths
                    or operation != "send_message" and not any(owner == parent and target in {sid, path}
                         for sid, (owner, path) in self.children.items())):
                self.failure = self.failure or "native follow-up lacks this reservation's participant baseline"
        if not samples:
            gap = gap or "request receipts and usable streamed usage absent; interrupted requests charged zero"
        gap = self.failure or gap
        if gap:
            return EstimatedRunUsage(gap, tuple(samples), self.rates)
        return MeasuredRunUsage(tuple(samples), self.rates)
