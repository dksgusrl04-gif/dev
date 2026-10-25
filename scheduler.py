"""
생산일정 수립 · 재편성 엔진.

변수   : 공정별 시작시각
제약   : ① 같은 작업의 공정 순서 ② 같은 설비에서 공정 겹침 금지
         ③ 설비 가동불가 구간 ④ 기준시각 이전 착수 공정 고정(비선점) ⑤ 투입시각
목적   : 총 납기지연(tardiness) 합 최소화  (검증용으로 makespan 최소화도 지원)

solver:
  - "cpsat"     OR-Tools CP-SAT. 소규모는 최적해를 증명까지 한다. (기본값)
  - "heuristic" Giffler-Thompson 능동일정 + 우선순위 규칙 + 다중시작(GRASP).
                대규모에서 즉시 해가 필요할 때.

검증 포인트: objective="makespan" 으로 ft06 을 풀면 55(OPTIMAL)가 나와야 한다.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field, replace

try:
    from ortools.sat.python import cp_model
except ImportError:          # ortools 가 없어도 휴리스틱은 돌 수 있게 한다
    cp_model = None


def cpsat_available() -> bool:
    return cp_model is not None

# ─────────────────────────────────────────────────────────────
# 자료구조
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Operation:
    job_id: str
    idx: int          # 작업 내 공정 순번 (0부터)
    machine: int
    duration: int


@dataclass
class Order:
    """주문 하나 = JSSP 의 작업(job) 하나 + 원본에 없는 납기·우선순위."""
    id: str
    name: str
    ops: list[Operation]
    due: int
    release: int = 0
    priority: int = 2        # 1=긴급, 2=보통, 3=여유
    urgent: bool = False
    added: bool = False      # 시연 중 추가된 긴급주문인지

    @property
    def twk(self) -> int:
        """총 처리시간 (total work content)."""
        return sum(o.duration for o in self.ops)

    @property
    def weight(self) -> int:
        """우선순위 → 지연 가중치. 긴급 주문의 지연을 더 비싸게 본다."""
        return {1: 4, 2: 2, 3: 1}.get(self.priority, 2)


@dataclass(frozen=True)
class Breakdown:
    machine: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return max(0, self.end - self.start)


@dataclass
class ScheduledOp:
    job_id: str
    op_idx: int
    machine: int
    start: int
    end: int
    frozen: bool = False

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass
class JobStat:
    id: str
    due: int
    release: int
    twk: int
    priority: int
    urgent: bool
    completion: int
    tardiness: int
    lateness: int
    on_time: bool
    waiting: int
    wait_by_machine: dict[int, int]

    @property
    def bottleneck(self) -> tuple[int, int] | None:
        """(설비번호, 그 앞에서 기다린 시간) — 지연 원인 설명용."""
        if not self.wait_by_machine:
            return None
        m = max(self.wait_by_machine, key=lambda k: self.wait_by_machine[k])
        return m, self.wait_by_machine[m]


@dataclass
class MachineStat:
    machine: int
    busy: int
    idle: int
    down: int
    num_ops: int
    util: float


@dataclass
class Schedule:
    ops: list[ScheduledOp]
    job_stats: list[JobStat]
    machine_stats: list[MachineStat]
    makespan: int
    total_tardiness: int
    max_tardiness: int
    num_tardy: int
    on_time_rate: float
    avg_util: float
    objective: str
    solver: str
    status: str
    solve_seconds: float
    breakdowns: list[Breakdown] = field(default_factory=list)
    now: int = 0

    @property
    def proven_optimal(self) -> bool:
        return self.status == "OPTIMAL"

    def ops_of(self, job_id: str) -> list[ScheduledOp]:
        return sorted((o for o in self.ops if o.job_id == job_id), key=lambda o: o.op_idx)

    def stat_of(self, job_id: str) -> JobStat | None:
        return next((j for j in self.job_stats if j.id == job_id), None)

    def as_rows(self) -> list[dict]:
        """공정별 (작업, 설비, 시작, 종료) 표."""
        return [
            {"주문": o.job_id, "공정": o.op_idx + 1, "설비": f"M{o.machine}",
             "시작": o.start, "종료": o.end, "처리시간": o.duration,
             "고정": "고정" if o.frozen else ""}
            for o in sorted(self.ops, key=lambda x: (x.machine, x.start))
        ]


# ─────────────────────────────────────────────────────────────
# 주문 생성 — 원본에 없는 납기/우선순위를 여기서 부여 (기능 ①)
# ─────────────────────────────────────────────────────────────

def build_orders(instance, k: float = 1.3, releases: list[int] | None = None) -> list[Order]:
    """
    납기 = ceil(총 처리시간 × k).

    k 가 작을수록 납기가 빡빡해진다. 1.2~1.5 를 권장하는 이유는,
    이 구간이어야 초기 일정에서는 대부분 납기를 지키다가
    고장/긴급주문이 들어오면 지연이 눈에 띄게 터지기 때문이다.
    k 가 2 이상이면 무슨 사고를 쳐도 납기를 지켜서 시연이 밋밋해진다.
    """
    orders = []
    for j, ops in enumerate(instance.jobs):
        jid = f"J{j + 1}"
        rel = releases[j] if releases else 0
        twk = sum(d for _, d in ops)
        orders.append(Order(
            id=jid,
            name=f"주문 {j + 1:03d}",
            ops=[Operation(jid, i, m, d) for i, (m, d) in enumerate(ops)],
            due=rel + math.ceil(twk * k),
            release=rel,
            priority=2,
        ))
    return orders


def make_urgent_order(
    order_id: str, release: int, due: int, operations: list[tuple[int, int]],
    name: str | None = None,
) -> Order:
    """긴급 주문 생성. operations = [(설비번호, 처리시간), ...]"""
    ops = [Operation(order_id, i, m, d) for i, (m, d) in enumerate(operations)]
    return Order(
        id=order_id, name=name or f"긴급주문 {order_id}", ops=ops,
        due=due, release=release, priority=1, urgent=True, added=True,
    )


def random_route(num_machines: int, n_ops: int, rng: random.Random | None = None
                 ) -> list[tuple[int, int]]:
    rng = rng or random
    machines = list(range(num_machines))
    rng.shuffle(machines)
    return [(m, rng.randint(3, 10)) for m in machines[:min(n_ops, num_machines)]]


# ─────────────────────────────────────────────────────────────
# 집계
# ─────────────────────────────────────────────────────────────

def _summarize(orders, num_machines, placed, breakdowns, *, objective, solver,
               status, seconds, now) -> Schedule:
    by_id = {o.id: o for o in orders}
    makespan = max((p.end for p in placed), default=0)

    job_stats = []
    for o in orders:
        ops = sorted((p for p in placed if p.job_id == o.id), key=lambda p: p.op_idx)
        if not ops:
            continue
        completion = max(p.end for p in ops)
        wait_by: dict[int, int] = {}
        prev_end = o.release
        for p in ops:
            w = p.start - prev_end
            if w > 0:
                wait_by[p.machine] = wait_by.get(p.machine, 0) + w
            prev_end = p.end
        tard = max(0, completion - o.due)
        job_stats.append(JobStat(
            id=o.id, due=o.due, release=o.release, twk=o.twk,
            priority=o.priority, urgent=o.urgent,
            completion=completion, tardiness=tard, lateness=completion - o.due,
            on_time=tard == 0, waiting=max(0, completion - o.release - o.twk),
            wait_by_machine=wait_by,
        ))

    machine_stats = []
    for m in range(num_machines):
        ops = [p for p in placed if p.machine == m]
        busy = sum(p.duration for p in ops)
        down = sum(max(0, min(b.end, makespan) - b.start)
                   for b in breakdowns if b.machine == m)
        machine_stats.append(MachineStat(
            machine=m, busy=busy, down=down,
            idle=max(0, makespan - busy - down),
            num_ops=len(ops), util=busy / makespan if makespan else 0.0,
        ))

    on_time = sum(1 for j in job_stats if j.on_time)
    return Schedule(
        ops=placed, job_stats=job_stats, machine_stats=machine_stats,
        makespan=makespan,
        total_tardiness=sum(j.tardiness for j in job_stats),
        max_tardiness=max((j.tardiness for j in job_stats), default=0),
        num_tardy=len(job_stats) - on_time,
        on_time_rate=on_time / len(job_stats) if job_stats else 0.0,
        avg_util=sum(x.util for x in machine_stats) / num_machines if num_machines else 0.0,
        objective=objective, solver=solver, status=status, solve_seconds=seconds,
        breakdowns=list(breakdowns), now=now,
    )


class InputError(ValueError):
    """사용자 입력이 잘못됐을 때. 화면에 그대로 보여줄 수 있는 문구를 담는다."""


def validate_breakdown(bd: Breakdown, num_machines: int, orders) -> list[str]:
    """
    고장 입력 검증. 말이 안 되는 값이면 InputError, 수상하면 경고 문자열로 돌려준다.

    주의: 고장 시간이 아무리 길어도 CP-SAT 는 '고장이 끝난 뒤'로 일정을 밀어
    항상 해를 찾는다. 즉 불능(infeasible)이 안 나므로 여기서 막아야 한다.
    """
    if not (0 <= bd.machine < num_machines):
        raise InputError(f"설비 번호는 0~{num_machines - 1} 사이여야 합니다 (입력 M{bd.machine}).")
    if bd.start < 0:
        raise InputError("고장 시작 시각은 0 이상이어야 합니다.")
    if bd.end <= bd.start:
        raise InputError(f"고장 종료({bd.end})가 시작({bd.start})보다 뒤여야 합니다.")

    warns = []
    total_work = sum(o.twk for o in orders)
    if bd.length > total_work:
        warns.append(
            f"중단 시간 {bd.length}이 전체 작업량 {total_work}보다 깁니다. "
            f"일정 전체가 고장 종료 시점 뒤로 밀려 비교가 무의미해집니다."
        )
    only_machine = [o.id for o in orders
                    if all(op.machine == bd.machine for op in o.ops)]
    if only_machine:
        warns.append(f"{', '.join(only_machine)}는 M{bd.machine}만 사용해 전량 대기합니다.")
    return warns


def validate_urgent(release: int, due: int, operations, num_machines: int) -> list[str]:
    """긴급주문 입력 검증."""
    if not operations:
        raise InputError("공정이 최소 1개는 있어야 합니다.")
    for m, d in operations:
        if not (0 <= m < num_machines):
            raise InputError(f"설비 번호는 0~{num_machines - 1} 사이여야 합니다 (입력 M{m}).")
        if d <= 0:
            raise InputError(f"처리시간은 1 이상이어야 합니다 (입력 {d}).")
    if release < 0:
        raise InputError("투입 시각은 0 이상이어야 합니다.")
    twk = sum(d for _, d in operations)
    if due <= release:
        raise InputError(f"납기({due})가 투입 시각({release})보다 뒤여야 합니다.")
    warns = []
    if due - release < twk:
        warns.append(
            f"납기까지 {due - release}시간인데 순수 가공만 {twk}시간이라 "
            f"대기 없이 전용 라인을 써도 납기를 지킬 수 없습니다."
        )
    return warns


def _clip_breakdowns(breakdowns, frozen) -> tuple[list[Breakdown], list[str]]:
    """
    고정된(이미 착수한) 공정과 겹치는 고장 구간은 그 공정이 끝난 뒤로 밀어준다.
    설비는 돌던 작업을 마치고 정지한다는 현장 가정이고,
    이 처리를 안 하면 '고정 구간'과 '고장 구간'이 같은 설비에서 충돌해 모델이 불능이 된다.
    """
    out, notes = [], []
    for b in breakdowns:
        s, e = b.start, b.end
        for f in frozen:
            if f.machine == b.machine and f.start < e and s < f.end:
                if f.end > s:
                    s = f.end
        if s >= e:
            notes.append(f"M{b.machine} {b.start}~{b.end} 고장 구간은 "
                         f"이미 착수한 작업에 모두 가려져 무시했습니다.")
            continue
        if s != b.start:
            notes.append(f"M{b.machine} 고장 시작을 {b.start} → {s} 로 조정했습니다 "
                         f"(착수 중이던 작업 완료 후 정지).")
        out.append(Breakdown(b.machine, s, e))
    return out, notes


# ─────────────────────────────────────────────────────────────
# CP-SAT 정확해
# ─────────────────────────────────────────────────────────────

def solve_cpsat(orders, num_machines, *, objective="tardiness",
                breakdowns=(), frozen=(), now=0, time_limit=10.0,
                use_priority_weights=True, workers=8):
    if cp_model is None:
        raise RuntimeError("ortools 가 설치되지 않아 CP-SAT 를 쓸 수 없습니다. "
                           "pip install ortools 후 다시 실행하거나 휴리스틱을 선택하세요.")
    t0 = time.perf_counter()
    breakdowns, _ = _clip_breakdowns(list(breakdowns), list(frozen))
    frozen_by = {(f.job_id, f.op_idx): f for f in frozen}

    horizon = (max((o.release for o in orders), default=0)
               + sum(o.twk for o in orders)
               + sum(b.length for b in breakdowns) + 1)

    mdl = cp_model.CpModel()
    starts, ends, per_machine = {}, {}, {m: [] for m in range(num_machines)}

    for o in orders:
        for op in o.ops:
            key = (o.id, op.idx)
            fz = frozen_by.get(key)
            if fz is not None:
                s = mdl.NewConstant(fz.start)
                e = mdl.NewConstant(fz.end)
                iv = mdl.NewIntervalVar(s, fz.duration, e, f"fz_{o.id}_{op.idx}")
            else:
                lo = max(o.release, now)
                s = mdl.NewIntVar(lo, horizon, f"s_{o.id}_{op.idx}")
                e = mdl.NewIntVar(lo, horizon, f"e_{o.id}_{op.idx}")
                iv = mdl.NewIntervalVar(s, op.duration, e, f"iv_{o.id}_{op.idx}")
            starts[key], ends[key] = s, e
            per_machine[op.machine].append(iv)

        # ① 공정 순서
        for a, b in zip(o.ops, o.ops[1:]):
            mdl.Add(starts[(o.id, b.idx)] >= ends[(o.id, a.idx)])

    # ③ 설비 가동불가 구간을 고정 구간으로 투입
    for b in breakdowns:
        if 0 <= b.machine < num_machines and b.length > 0:
            per_machine[b.machine].append(
                mdl.NewIntervalVar(b.start, b.length, b.end, f"down_{b.machine}_{b.start}"))

    # ② 설비 겹침 금지
    for m, ivs in per_machine.items():
        if len(ivs) > 1:
            mdl.AddNoOverlap(ivs)

    # 완료시각 / 지연
    makespan = mdl.NewIntVar(0, horizon, "makespan")
    mdl.AddMaxEquality(makespan, [ends[(o.id, o.ops[-1].idx)] for o in orders])

    tard_terms = []
    for o in orders:
        c = ends[(o.id, o.ops[-1].idx)]
        t = mdl.NewIntVar(0, horizon, f"T_{o.id}")
        mdl.AddMaxEquality(t, [c - o.due, 0])
        tard_terms.append((o.weight if use_priority_weights else 1) * t)

    if objective == "makespan":
        mdl.Minimize(makespan)
    else:
        # 총 지연을 1순위, makespan 을 2순위로 (사전식)
        mdl.Minimize(sum(tard_terms) * (horizon + 1) + makespan)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_search_workers = int(workers)
    st = solver.Solve(mdl)
    name = solver.StatusName(st)

    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            f"일정을 찾지 못했습니다 (CP-SAT: {name}). "
            "고장 구간이 너무 길거나 기준시각과 충돌하는지 확인하세요."
        )

    placed = [
        ScheduledOp(o.id, op.idx, op.machine,
                    solver.Value(starts[(o.id, op.idx)]),
                    solver.Value(ends[(o.id, op.idx)]),
                    frozen=(o.id, op.idx) in frozen_by)
        for o in orders for op in o.ops
    ]
    return _summarize(orders, num_machines, placed, breakdowns,
                      objective=objective, solver="cpsat", status=name,
                      seconds=time.perf_counter() - t0, now=now)


# ─────────────────────────────────────────────────────────────
# 휴리스틱 (대규모 / 즉시 응답용)
# ─────────────────────────────────────────────────────────────

RULES = {
    "MINSLACK": ("최소 여유 우선", lambda c, t: c["order"].due - t - c["work_remaining"]),
    "EDD":      ("최단 납기 우선", lambda c, t: c["order"].due),
    "CR":       ("임계비 우선",    lambda c, t: (c["order"].due - t) / max(1, c["work_remaining"])),
    "SPT":      ("최단 작업 우선", lambda c, t: c["duration"]),
    "MWKR":     ("최다 잔여작업",  lambda c, t: -c["work_remaining"]),
    "FIFO":     ("투입 순서",      lambda c, t: c["order"].release * 1000 + c["idx"]),
}


def _earliest_feasible(t, dur, windows):
    s = t
    for _ in range(400):
        moved = False
        for w in windows:
            if s < w.end and s + dur > w.start:
                s, moved = w.end, True
        if not moved:
            return s
    return s


def _dispatch_once(orders, num_machines, rule, breakdowns, frozen, now, rng=None):
    prio = RULES[rule][1]
    win = {m: [b for b in breakdowns if b.machine == m] for m in range(num_machines)}
    machine_ready = [0] * num_machines
    job_ready = {o.id: o.release for o in orders}
    next_idx = {o.id: 0 for o in orders}
    placed: list[ScheduledOp] = []

    for f in frozen:
        placed.append(ScheduledOp(f.job_id, f.op_idx, f.machine, f.start, f.end, True))
        machine_ready[f.machine] = max(machine_ready[f.machine], f.end)
        job_ready[f.job_id] = max(job_ready.get(f.job_id, 0), f.end)
        next_idx[f.job_id] = max(next_idx.get(f.job_id, 0), f.op_idx + 1)
    for m in range(num_machines):
        machine_ready[m] = max(machine_ready[m], now)

    total_ops = sum(len(o.ops) for o in orders)
    guard = 0
    while len(placed) < total_ops and guard < 100000:
        guard += 1
        cand = []
        for o in orders:
            i = next_idx[o.id]
            if i >= len(o.ops):
                continue
            op = o.ops[i]
            est = _earliest_feasible(
                max(job_ready[o.id], machine_ready[op.machine], o.release),
                op.duration, win[op.machine])
            cand.append({"order": o, "idx": i, "machine": op.machine,
                         "duration": op.duration, "est": est, "ect": est + op.duration,
                         "work_remaining": sum(x.duration for x in o.ops[i:])})
        if not cand:
            break
        min_ect = min(c["ect"] for c in cand)
        pivot = next(c for c in cand if c["ect"] == min_ect)
        pool = [c for c in cand if c["machine"] == pivot["machine"] and c["est"] < min_ect] or [pivot]
        pool.sort(key=lambda c: (not c["order"].urgent, c["order"].priority,
                                 prio(c, min_ect), c["order"].id))
        pick = pool[0]
        if rng and len(pool) > 1 and rng.random() > 0.65:
            pick = pool[rng.randrange(1, len(pool))]
        o = pick["order"]
        placed.append(ScheduledOp(o.id, pick["idx"], pick["machine"],
                                  pick["est"], pick["est"] + pick["duration"]))
        machine_ready[pick["machine"]] = pick["est"] + pick["duration"]
        job_ready[o.id] = pick["est"] + pick["duration"]
        next_idx[o.id] = pick["idx"] + 1
    return placed


def solve_heuristic(orders, num_machines, *, objective="tardiness", rule="MINSLACK",
                    breakdowns=(), frozen=(), now=0, restarts=1500, seed=20251025,
                    time_budget: float | None = None):
    """time_budget(초)를 주면 다중시작을 그 시간 안에서만 반복한다 (느린 서버·대규모 대비)."""
    t0 = time.perf_counter()
    breakdowns, _ = _clip_breakdowns(list(breakdowns), list(frozen))
    rng = random.Random(seed)

    def score(sc: Schedule):
        return ((sc.makespan, sc.total_tardiness) if objective == "makespan"
                else (sc.total_tardiness, sc.max_tardiness, sc.makespan))

    def build(r):
        placed = _dispatch_once(orders, num_machines, rule, breakdowns, frozen, now, r)
        return _summarize(orders, num_machines, placed, breakdowns,
                          objective=objective, solver="heuristic", status="HEURISTIC",
                          seconds=0.0, now=now)

    best = build(None)
    for _ in range(max(0, restarts)):
        if time_budget is not None and time.perf_counter() - t0 > time_budget:
            break
        cand = build(rng)
        if score(cand) < score(best):
            best = cand
    best.solve_seconds = time.perf_counter() - t0
    return best


# ─────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────

def solve(orders, num_machines, *, objective="tardiness", solver="cpsat",
          breakdowns=(), frozen=(), now=0, time_limit=10.0, rule="MINSLACK",
          restarts=1500, workers=8, time_budget: float | None = None) -> Schedule:
    """
    일정 수립. objective ∈ {"tardiness","makespan"},
    solver ∈ {"cpsat","heuristic","rule"}  ("rule" = 우선순위 규칙 1회 적용, 탐색 없음)
    time_budget 은 휴리스틱 다중시작 시간 상한(초). CP-SAT 는 time_limit 을 쓴다.
    """
    if solver in ("heuristic", "rule"):
        sched = solve_heuristic(orders, num_machines, objective=objective, rule=rule,
                                breakdowns=breakdowns, frozen=frozen, now=now,
                                restarts=0 if solver == "rule" else restarts,
                                time_budget=time_budget)
        if solver == "rule":
            sched.solver, sched.status = "rule", "RULE"
        return sched
    return solve_cpsat(orders, num_machines, objective=objective,
                       breakdowns=breakdowns, frozen=frozen, now=now,
                       time_limit=time_limit, workers=workers)


def freeze_before(schedule: Schedule, t: int) -> list[ScheduledOp]:
    """기준시각 t 이전에 '착수한' 공정 = 고정 대상 (비선점)."""
    return [replace(o, frozen=True) for o in schedule.ops if o.start < t]


def reschedule(orders, num_machines, base: Schedule, now: int, *,
               breakdowns=(), objective="tardiness", solver="cpsat",
               time_limit=10.0, rule="MINSLACK", restarts=1500,
               workers=8, time_budget: float | None = None) -> tuple[Schedule, list[str]]:
    """
    돌발상황 반영 후 잔여 일정 재계산.
    반환: (새 일정, 사용자에게 보여줄 안내문 목록)
    """
    live = {o.id for o in orders}
    frozen = [f for f in freeze_before(base, now) if f.job_id in live]
    _, notes = _clip_breakdowns(list(breakdowns), frozen)
    sched = solve(orders, num_machines, objective=objective, solver=solver,
                  breakdowns=breakdowns, frozen=frozen, now=now, time_limit=time_limit,
                  rule=rule, restarts=restarts, workers=workers, time_budget=time_budget)
    notes.insert(0, f"기준시각 t={now} 이전에 착수한 {len(frozen)}개 공정을 고정하고 "
                    f"나머지 {len(sched.ops) - len(frozen)}개를 재배정했습니다.")
    return sched, notes


def compare(base: Schedule, new: Schedule) -> list[dict]:
    """변경 전후 주문별 비교표 (기능 ④)."""
    rows = []
    for j in new.job_stats:
        b = base.stat_of(j.id)
        rows.append({
            "주문": j.id,
            "납기": j.due,
            "변경 전 완료": b.completion if b else None,
            "변경 후 완료": j.completion,
            "완료 변화": (j.completion - b.completion) if b else None,
            "변경 전 지연": b.tardiness if b else None,
            "변경 후 지연": j.tardiness,
            "지연 변화": (j.tardiness - b.tardiness) if b else j.tardiness,
            "상태": ("신규 투입" if not b else
                    "준수 → 지연" if b.on_time and not j.on_time else
                    "지연 → 준수" if not b.on_time and j.on_time else
                    "준수 유지" if j.on_time else "지연 유지/변동"),
        })
    return rows


def summary_dict(s: Schedule) -> dict:
    """에이전트에게 넘길 요약. LLM 은 이 숫자를 '해석'만 한다."""
    return {
        "총_완료시간_makespan": s.makespan,
        "총_납기지연": s.total_tardiness,
        "최대_납기지연": s.max_tardiness,
        "지연_주문수": s.num_tardy,
        "납기_준수율": round(s.on_time_rate, 3),
        "평균_설비가동률": round(s.avg_util, 3),
        "해_상태": s.status,
        "계산초": round(s.solve_seconds, 2),
        "주문별": [
            {"주문": j.id, "납기": j.due, "완료": j.completion, "지연": j.tardiness,
             "대기": j.waiting,
             "최다대기설비": (f"M{j.bottleneck[0]}" if j.bottleneck else None),
             "최다대기시간": (j.bottleneck[1] if j.bottleneck else 0)}
            for j in s.job_stats
        ],
        "설비별": [
            {"설비": f"M{m.machine}", "가동": m.busy, "유휴": m.idle,
             "중단": m.down, "가동률": round(m.util, 3)}
            for m in s.machine_stats
        ],
    }
