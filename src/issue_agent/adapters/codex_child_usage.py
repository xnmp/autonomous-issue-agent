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

from ..domain.contracts import TokenUsage, UsageSource
from ..domain.pricing import ModelRates, RateTable, UnknownModelPolicy
from ..domain.run_usage import MeasuredRunUsage, ModelUsage, RunUsage, UnavailableRunUsage


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


class RemeterOutcome(str, Enum):
    MEASURED = "measured"


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
    models: dict[str, str] = field(default_factory=dict)
    usage: dict[str, tuple[str, TokenUsage]] = field(default_factory=dict)
    completed_turns: set[str] = field(default_factory=set)
    started_turns: set[str] = field(default_factory=set)


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

    def _directories(self, session_id: str = "") -> set[Path]:
        start = datetime.fromtimestamp(self.started_at, timezone.utc).date()
        today = datetime.now(timezone.utc).date()
        if not 0 <= (today - start).days <= 7:
            raise ValueError("native rollout discovery exceeded its invocation date bound")
        # Codex filenames use local dates, while metadata uses UTC. Adjacent
        # days cover UTC offsets without enumerating the lifetime archive.
        dates = {start + timedelta(days=i) for i in range(-1, (today - start).days + 2)}
        if session_id:
            import uuid
            identity = uuid.UUID(session_id)
            if identity.version != 7:
                raise ValueError("native session identity does not provide its creation date")
            born = datetime.fromtimestamp((identity.int >> 80) / 1000, timezone.utc).date()
            dates.update(born + timedelta(days=i) for i in (-1, 0, 1))
        return {Path(self.home) / "sessions" / day.strftime("%Y/%m/%d") for day in dates}

    def _paths(self, directories: set[Path]):
        entries = 0
        for directory in sorted(directories):
            try:
                stream = os.scandir(directory)
            except FileNotFoundError:
                continue
            with stream:
                for entry in stream:
                    entries += 1
                    if entries > self.max_files:
                        raise ValueError("native rollout discovery exceeded its file bound")
                    if entry.name.startswith("rollout-") and entry.name.endswith(".jsonl"):
                        yield Path(entry.path)

    def _locate(self, session_id: str) -> _Rollout | None:
        if session_id in self.rollouts:
            return self.rollouts[session_id]
        # Native identities are UUIDs, never agent-supplied glob/path fragments.
        import uuid
        try:
            uuid.UUID(session_id)
        except (ValueError, AttributeError):
            return None
        paths = [path for path in self._paths(self._directories(session_id))
                 if path.name.endswith(f"-{session_id}.jsonl")]
        if len(paths) > 1:
            raise ValueError("native session has ambiguous rollout paths")
        if not paths:
            return None
        if len(self.rollouts) >= self.max_children + 1:
            raise ValueError("native rollout observer exceeded its session bound")
        found = _Rollout(Path(paths[0]), session_id)
        self.rollouts[session_id] = found
        return found

    def _candidate_children(self) -> None:
        for path in self._paths(self._directories()):
            meta = self._metadata_headers.get(path)
            if meta is None:
                with path.open("rb") as stream:
                    line = stream.readline(65537)
                self._bytes += len(line)
                if self._bytes > self.max_bytes:
                    raise ValueError("native observer exceeded its byte bound")
                if len(line) > 65536:
                    raise ValueError("native metadata exceeds its read bound")
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "session_meta":
                    continue
                meta = event.get("payload") or {}
                if not isinstance(meta, dict):
                    raise ValueError("native metadata is malformed")
                self._metadata_headers[path] = meta
            source = meta.get("source")
            parent = ((source.get("subagent") or {}).get("thread_spawn") or {}).get("parent_thread_id") if isinstance(source, dict) else None
            sid = meta.get("id") or meta.get("session_id")
            if parent in self.rollouts and isinstance(sid, str) and sid not in self.rollouts:
                self._locate(sid)

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
        if kind == "turn_context":
            turn, model = payload.get("turn_id"), payload.get("model")
            if isinstance(turn, str) and isinstance(model, str) and turn and model:
                prior = rollout.models.get(turn)
                if prior is not None and prior != model:
                    raise ValueError("native model changed within an observed turn")
                if prior is None and sum(len(r.models) for r in self.rollouts.values()) >= self.max_responses:
                    raise ValueError("native observer exceeded its turn bound")
                rollout.models[turn] = model
                rollout.active_turn = turn
            return
        if _timestamp(event.get("timestamp")) < self.started_at:
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
                    sender = (((source.get("subagent") or {}).get("thread_spawn") or {}).get("agent_path")
                              if isinstance(source, dict) else None) or "/root"
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
                    parent_path = (((parent_source.get("subagent") or {}).get("thread_spawn") or {}).get("agent_path")
                                   if isinstance(parent_source, dict) else None) or "/root"
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
            value = (turn, normalize_usage(payload.get("usage") or {}))
            if response in rollout.usage and rollout.usage[response] != value:
                raise ValueError("native response has conflicting usage")
            if sum(len(r.usage) for r in self.rollouts.values()) >= self.max_responses and response not in rollout.usage:
                raise ValueError("native observer exceeded its response bound")
            rollout.usage[response] = value
            return
        if kind != "event_msg":
            return
        event_kind = payload.get("type")
        if event_kind in ("task_complete", "turn_complete"):
            turn = payload.get("turn_id")
            if isinstance(turn, str):
                rollout.completed_turns.add(turn)
        elif event_kind in ("task_started", "turn_started"):
            turn = payload.get("turn_id")
            if isinstance(turn, str):
                if sum(len(r.started_turns) for r in self.rollouts.values()) >= self.max_responses and turn not in rollout.started_turns:
                    raise ValueError("native observer exceeded its turn bound")
                rollout.started_turns.add(turn)
                rollout.active_turn = turn

    def _resolve_spawn_outputs(self, *, final: bool) -> None:
        for (parent, _), (sid, path) in self.spawn_outputs.items():
            if sid is None:
                matches = []
                for rollout in self.rollouts.values():
                    source = (rollout.metadata or {}).get("source") or {}
                    spawn = ((source.get("subagent") or {}).get("thread_spawn") or {}) if isinstance(source, dict) else {}
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

    def _read(self, rollout: _Rollout) -> None:
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
        for raw in records:
            if not raw:
                continue
            if len(raw) > self.max_record_bytes:
                raise ValueError("native usage record exceeds its size bound")
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("native usage event is malformed")
            self._event(rollout, event)

    def _blocking_tail(self, rollout: _Rollout) -> bool:
        """An unterminated record blocks final settlement, except a receipted session's killed write
        (a truncated prefix of one record: its in-flight request has no receipt, so it prices at zero)."""
        if rollout.tail and rollout.usage:
            tail = rollout.tail
            try:
                tail = tail.decode()
            except UnicodeDecodeError as exc:
                # Only a multibyte character cut off at the very end is a killed write.
                if exc.reason != "unexpected end of data" or exc.end != len(tail):
                    return True
                tail = tail[:exc.start].decode()
            if not tail.lstrip().startswith("{"):
                return True
            try:
                json.loads(tail)
            except json.JSONDecodeError as exc:
                return not (exc.msg.startswith("Unterminated string") or exc.pos >= len(tail.rstrip()))
        return bool(rollout.tail)

    def sample(self, session_id: str, model: str, usage: dict | None, *, final: bool = False) -> RunUsage | None:
        """A session with receipts is priced as exactly the sum of its receipts in this run's window
        [started_at, ended_at]; the counter prices only sessions without any. An in-flight request
        has no receipt and so costs zero (operator, 2026-10-11)."""
        samples: list[ModelUsage] = []
        if self.failure:
            return UnavailableRunUsage(self.failure, tuple(self.roots.values()))
        try:
            if session_id and usage is not None:
                normalized = root_usage(usage)
                if model:
                    self.roots[session_id] = ModelUsage(session_id, model, normalized)
            if session_id:
                self._locate(session_id)
            for _ in range(self.max_children + 1):
                before = len(self.rollouts)
                for rollout in tuple(self.rollouts.values()):
                    self._read(rollout)
                self._candidate_children()
                self._resolve_spawn_outputs(final=False)
                if len(self.rollouts) == before:
                    break
            self._resolve_spawn_outputs(final=final)
            receipted: set[str] = set()
            for sid in {*self.roots, session_id} - {""}:
                rollout = self.rollouts.get(sid)
                root_model = self.roots[sid].model if sid in self.roots else (model if sid == session_id else "")
                if rollout is not None and rollout.usage and root_model:
                    self.roots[sid] = ModelUsage(sid, root_model, total_usage_of([u for _, u in rollout.usage.values()]))
                    receipted.add(sid)
                    if final and rollout.completed_turns - {turn for turn, _ in rollout.usage.values()}:
                        raise ValueError(NativeFailure.UNFINISHED_TURNS.value)
            participants = set(self.roots) | set(self.children)
            message_targets = participants | {path for _, path in self.children.values() if path}
            if self.roots:
                message_targets.add("/root")
            for parent, target, operation in self.followups:
                # Notifications cannot activate another turn; every participant
                # still needs its own corroborated baseline and complete usage.
                if operation == "send_message":
                    if parent in participants and target in message_targets:
                        continue
                    raise ValueError("native message lacks this reservation's participant baseline")
                if not any(owner == parent and target in {sid, path}
                           for sid, (owner, path) in self.children.items()):
                    raise ValueError("native follow-up lacks this reservation's child baseline")
            if final and any(r.session_id not in self.children and r.created_at is not None
                             and r.created_at >= self.started_at
                             and isinstance((r.metadata or {}).get("source"), dict)
                             for r in self.rollouts.values()):
                raise ValueError("native child lacks its parent's durable spawn receipt")
            if not self.children:
                if self.rates is not None and self.roots:
                    if final and (session_id not in self.roots or usage is None and session_id not in receipted
                                  or any(self._blocking_tail(r) for r in self.rollouts.values())):
                        raise ValueError("parent native usage is missing or incomplete")
                    if any(not self.rates.priceable(root.model) for root in self.roots.values()):
                        raise ValueError("native parent model has no configured rates")
                    return MeasuredRunUsage(tuple(self.roots.values()))
                return None
            if final and session_id not in self.roots:
                raise ValueError("native parent model or usage is unavailable")
            if final and (usage is None and session_id not in receipted or session_id not in self.roots
                          or any(self._blocking_tail(r) for r in self.rollouts.values())):
                raise ValueError("parent native usage is missing or incomplete")
            samples.extend(self.roots.values())
            if self.rates is None or any(not self.rates.priceable(root.model) for root in self.roots.values()):
                raise ValueError("native parent model has no configured rates")
            for sid, (parent, path) in sorted(self.children.items()):
                rollout = self.rollouts.get(sid)
                if rollout is None or rollout.metadata is None:
                    if final:
                        raise ValueError("native child has no corroborating rollout")
                    continue
                meta = rollout.metadata
                if meta.get("cli_version") != "0.160.0":
                    raise ValueError("native child protocol is unqualified for this CLI version")
                source = meta.get("source") or {}
                spawn = ((source.get("subagent") or {}).get("thread_spawn") or {}) if isinstance(source, dict) else {}
                if (spawn.get("parent_thread_id") != parent
                        or path is not None and spawn.get("agent_path") != path
                        or not isinstance(meta.get("cwd"), str)
                        or os.path.realpath(meta["cwd"]) != os.path.realpath(self.cwd)):
                    raise ValueError("native child disagrees with captured lineage, path or cwd")
                ancestor_turns: set[str] = set()
                ancestor = parent
                while ancestor in self.rollouts:
                    ancestor_turns.update(self.rollouts[ancestor].models)
                    ancestor_turns.update(self.rollouts[ancestor].started_turns)
                    if ancestor not in self.children:
                        break
                    ancestor = self.children[ancestor][0]
                own_turns = rollout.started_turns - ancestor_turns
                if not own_turns:
                    own_turns = {turn for turn, _ in rollout.usage.values()} - ancestor_turns
                usage_turns = {turn for turn, _ in rollout.usage.values()}
                if final and (not own_turns or not own_turns & rollout.completed_turns <= usage_turns):
                    raise ValueError(NativeFailure.UNFINISHED_TURNS.value)
                grouped: dict[str, list[TokenUsage]] = {}
                for turn in own_turns:
                    receipts = [tokens for record_turn, tokens in rollout.usage.values() if record_turn == turn]
                    actual_model = rollout.models.get(turn)
                    # Native task_started can precede turn_context; a turn with no receipt has cost nothing yet.
                    if actual_model is None and not receipts:
                        continue
                    if not actual_model or self.rates is None or actual_model.strip().casefold() not in self.rates.rates:
                        raise ValueError("native child turn model has no explicit configured rates")
                    grouped.setdefault(actual_model, []).extend(receipts)
                samples.extend(ModelUsage(sid, m, total_usage_of(c)) for m, c in grouped.items() if c)
            return MeasuredRunUsage(tuple(samples)) if samples else None
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.failure = str(exc)[:2048] or "native child usage unavailable"
            return UnavailableRunUsage(self.failure, tuple(samples))
