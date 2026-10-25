"""
AI Agent — Claude API tool calling.

핵심 원칙 하나만 지키면 된다.

    일정 계산은 전부 scheduler.py 가 한다. LLM 은 도구를 부르고 결과를 설명만 한다.

LLM 에게 숫자를 직접 계산시키면 그럴듯한 거짓말을 한다. 완료시각·지연시간은
CP-SAT 가 푼 값만 쓰고, LLM 에는 그 결과 JSON 만 넘긴다.

흐름:
    사용자 "설비 2가 10부터 24까지 고장났어"
      → Claude 가 machine_breakdown(machine=2, start=10, end=24) 호출
      → SchedulingSession 이 실제로 재편성 (CP-SAT)
      → 결과 JSON 을 tool_result 로 반환
      → Claude 가 원인·영향을 자연어로 요약

API 키가 없거나 외부 호출이 막힌 환경을 위해 RuleBasedAgent 도 같이 둔다.
시연 당일 네트워크가 끊겨도 같은 도구를 같은 방식으로 호출한다.
"""
from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass, field

import scheduler as S

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """\
당신은 제조 현장 생산일정 담당자를 돕는 어시스턴트입니다.

지켜야 할 규칙:
1. 일정 계산은 절대 직접 하지 마세요. 완료시각, 지연시간, makespan 같은 숫자는
   반드시 도구 호출 결과에 있는 값만 사용합니다. 도구를 부르지 않고 숫자를 말하면 안 됩니다.
2. 사용자가 설비 고장이나 긴급 주문을 언급하면 해당 도구를 호출해 일정을 재계산하세요.
   현재 상태만 묻는다면 get_current_schedule 을, 변경 전후 비교를 원하면 get_comparison 을 쓰세요.
3. 시각은 모두 정수 시간 단위입니다. "3호기"는 M3, "20시부터 10시간"은 start=20, end=30 입니다.
   사용자가 시작 시각을 말하지 않으면 현재 기준시각을 쓰고, 그 사실을 답변에 밝히세요.
4. 입력이 모호하면 추측해서 실행하지 말고 무엇이 불분명한지 한 문장으로 되물으세요.
   단, 시작 시각처럼 합리적 기본값이 있는 항목은 기본값을 쓰고 밝히면 됩니다.

답변 형식 (한국어, 간결하게):
- 무엇을 반영했는지 한 줄
- 재편성 결과: makespan과 총 납기지연의 변경 전 → 후
- 납기 영향이 큰 주문 2~3건. 각각 완료시각 변화와 지연시간, 그리고 어느 설비 앞에서
  얼마나 기다리는지(도구 결과의 최다대기설비 항목)를 근거로 제시
- 우선 조치 제안 2~3가지

불릿은 쓰되 표는 쓰지 마세요. 화면에 이미 표가 있습니다.
도구 결과에 없는 수치는 절대 만들어내지 마세요.
"""

TOOLS = [
    {
        "name": "machine_breakdown",
        "description": (
            "특정 설비를 지정한 구간 동안 사용 불가로 등록하고 잔여 생산일정을 재계산한다. "
            "기준시각 이전에 착수한 공정은 고정되고 미착수 공정만 다시 배정된다. "
            "변경 전후 요약을 반환한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "machine": {"type": "integer", "description": "설비 번호 (0부터 시작)"},
                "start": {"type": "integer", "description": "가동 중단 시작 시각"},
                "end": {"type": "integer", "description": "가동 재개 시각 (start보다 커야 함)"},
            },
            "required": ["machine", "start", "end"],
        },
    },
    {
        "name": "add_urgent_order",
        "description": (
            "긴급 주문을 추가하고 잔여 생산일정을 재계산한다. "
            "공정 라우팅(operations)을 주지 않으면 설비 순서를 무작위로 생성한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "due": {"type": "integer", "description": "납기 시각"},
                "release": {"type": "integer", "description": "투입 시각. 생략하면 현재 기준시각"},
                "num_operations": {"type": "integer", "description": "공정 수. operations 생략 시 사용. 기본 4"},
                "operations": {
                    "type": "array",
                    "description": "공정 라우팅. 순서대로 사용할 설비와 처리시간",
                    "items": {
                        "type": "object",
                        "properties": {
                            "machine": {"type": "integer"},
                            "duration": {"type": "integer"},
                        },
                        "required": ["machine", "duration"],
                    },
                },
            },
            "required": ["due"],
        },
    },
    {
        "name": "get_current_schedule",
        "description": "현재 적용 중인 생산일정의 요약을 반환한다. 일정을 바꾸지 않는다.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_comparison",
        "description": (
            "최초 일정과 현재(재편성 후) 일정의 주문별 완료시각·지연시간 비교표를 반환한다. "
            "일정을 바꾸지 않는다."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reset_schedule",
        "description": "등록한 돌발상황을 모두 지우고 최초 일정으로 되돌린다.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def agent_view(sched: S.Schedule, limit: int = 12) -> dict:
    """LLM 에 넘길 요약. 주문이 많으면 지연 큰 순으로 잘라 토큰을 아낀다."""
    d = S.summary_dict(sched)
    rows = sorted(d["주문별"], key=lambda r: -r["지연"])
    if len(rows) > limit:
        d["주문별"] = rows[:limit]
        d["주문별_비고"] = f"지연 상위 {limit}건만 표시 (전체 {len(rows)}건)"
    else:
        d["주문별"] = rows
    return d


# ─────────────────────────────────────────────────────────────
# 도구를 실제로 실행하는 쪽. LLM 은 여기 들어오지 못한다.
# ─────────────────────────────────────────────────────────────

@dataclass
class SchedulingSession:
    instance: object
    orders: list[S.Order]
    base: S.Schedule
    now: int = 10
    objective: str = "tardiness"
    solver: str = "cpsat"
    time_limit: float = 10.0
    rule: str = "MINSLACK"
    restarts: int = 1500
    workers: int = 8
    time_budget: float | None = None
    current: S.Schedule | None = None
    breakdowns: list[S.Breakdown] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    urgent_seq: int = 0
    rng: random.Random = field(default_factory=lambda: random.Random(20251025))

    @property
    def num_machines(self) -> int:
        return self.instance.num_machines

    @property
    def active(self) -> S.Schedule:
        return self.current or self.base

    @property
    def changed(self) -> bool:
        return self.current is not None

    def _rerun(self) -> list[str]:
        self.current, notes = S.reschedule(
            self.orders, self.num_machines, self.base, self.now,
            breakdowns=self.breakdowns, objective=self.objective,
            solver=self.solver, time_limit=self.time_limit,
            rule=self.rule, restarts=self.restarts, workers=self.workers,
            time_budget=self.time_budget)
        self.notes = notes
        return notes

    # ── 도구 본체 ──
    def machine_breakdown(self, machine: int, start: int, end: int) -> dict:
        bd = S.Breakdown(int(machine), int(start), int(end))
        warns = S.validate_breakdown(bd, self.num_machines, self.orders)
        before = self.active
        self.breakdowns.append(bd)
        try:
            notes = self._rerun()
        except Exception:
            self.breakdowns.pop()
            raise
        return {
            "적용": f"M{bd.machine} {bd.start}~{bd.end} 가동중단 ({bd.length}시간)",
            "안내": notes + warns,
            "변경_전": agent_view(before),
            "변경_후": agent_view(self.active),
        }

    def add_urgent_order(self, due: int, release: int | None = None,
                         operations: list[dict] | None = None,
                         num_operations: int | None = None) -> dict:
        rel = self.now if release is None else int(release)
        if operations:
            ops = [(int(o["machine"]), int(o["duration"])) for o in operations]
        else:
            ops = S.random_route(self.num_machines, int(num_operations or 4), self.rng)
        warns = S.validate_urgent(rel, int(due), ops, self.num_machines)

        self.urgent_seq += 1
        oid = f"U{self.urgent_seq}"
        order = S.make_urgent_order(oid, rel, int(due), ops)
        before = self.active
        self.orders.append(order)
        try:
            notes = self._rerun()
        except Exception:
            self.orders.pop()
            self.urgent_seq -= 1
            raise
        return {
            "적용": (f"긴급주문 {oid} 추가 — 투입 {rel}, 납기 {due}, "
                   f"라우팅 " + " → ".join(f"M{m}·{d}" for m, d in ops)),
            "안내": notes + warns,
            "변경_전": agent_view(before),
            "변경_후": agent_view(self.active),
        }

    def get_current_schedule(self) -> dict:
        return {"재편성_여부": self.changed,
                "기준시각": self.now,
                "등록된_고장": [f"M{b.machine} {b.start}~{b.end}" for b in self.breakdowns],
                "일정": agent_view(self.active)}

    def get_comparison(self) -> dict:
        if not self.changed:
            return {"비교불가": "아직 재편성을 하지 않아 변경 전후가 같습니다.",
                    "일정": agent_view(self.base)}
        return {"기준시각": self.now,
                "요약": {"makespan": [self.base.makespan, self.current.makespan],
                       "총_납기지연": [self.base.total_tardiness, self.current.total_tardiness],
                       "납기_준수율": [round(self.base.on_time_rate, 3),
                                  round(self.current.on_time_rate, 3)]},
                "주문별": S.compare(self.base, self.current)}

    def reset_schedule(self) -> dict:
        self.breakdowns.clear()
        self.orders[:] = [o for o in self.orders if not o.added]
        self.current, self.notes = None, []
        return {"적용": "돌발상황을 모두 제거하고 최초 일정으로 되돌렸습니다.",
                "일정": agent_view(self.base)}

    def dispatch(self, name: str, args: dict) -> dict:
        fn = {
            "machine_breakdown": self.machine_breakdown,
            "add_urgent_order": self.add_urgent_order,
            "get_current_schedule": self.get_current_schedule,
            "get_comparison": self.get_comparison,
            "reset_schedule": self.reset_schedule,
        }.get(name)
        if fn is None:
            raise S.InputError(f"알 수 없는 도구입니다: {name}")
        return fn(**(args or {}))


@dataclass
class ToolCall:
    name: str
    args: dict
    ok: bool
    result: dict | str


@dataclass
class AgentReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    schedule_changed: bool = False
    usage: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────
# Claude API 에이전트
# ─────────────────────────────────────────────────────────────

class ClaudeAgent:
    """
    api_key 는 st.secrets["ANTHROPIC_API_KEY"] 또는 환경변수 ANTHROPIC_API_KEY 에서 받는다.
    절대 코드에 적어 커밋하지 말 것.
    """

    def __init__(self, session: SchedulingSession, api_key: str | None = None,
                 model: str = DEFAULT_MODEL, max_tokens: int = 1600):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise S.InputError(
                "Claude API 키가 없습니다. 사이드바에 키를 입력하거나 "
                "환경변수 ANTHROPIC_API_KEY 를 설정하세요."
            )
        try:
            import anthropic
        except ImportError as e:
            raise S.InputError("anthropic 패키지가 없습니다. pip install anthropic") from e

        self.client = anthropic.Anthropic(api_key=key)
        self.session = session
        self.model = model
        self.max_tokens = max_tokens
        self.messages: list[dict] = []

    def run(self, user_message: str) -> AgentReply:
        self.messages.append({"role": "user", "content": user_message})
        calls: list[ToolCall] = []
        usage = {"input_tokens": 0, "output_tokens": 0}

        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT, tools=TOOLS, messages=self.messages,
            )
            usage["input_tokens"] += getattr(resp.usage, "input_tokens", 0)
            usage["output_tokens"] += getattr(resp.usage, "output_tokens", 0)
            self.messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                return AgentReply(text or "(응답 없음)", calls,
                                  self.session.changed, usage)

            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                try:
                    out = self.session.dispatch(block.name, dict(block.input))
                    calls.append(ToolCall(block.name, dict(block.input), True, out))
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(out, ensure_ascii=False)})
                except Exception as e:                      # 도구 실패도 모델에게 알려 복구시킨다
                    msg = str(e)
                    calls.append(ToolCall(block.name, dict(block.input), False, msg))
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"오류: {msg}", "is_error": True})
            self.messages.append({"role": "user", "content": results})

        return AgentReply(
            "도구 호출이 너무 많이 반복돼 중단했습니다. 요청을 더 구체적으로 적어 주세요.",
            calls, self.session.changed, usage)


# ─────────────────────────────────────────────────────────────
# 오프라인 대체 — API 없이도 같은 도구를 호출한다 (시연 보험)
# ─────────────────────────────────────────────────────────────

class RuleBasedAgent:
    """키워드로 의도를 뽑아 같은 도구를 호출하고, 결과를 템플릿으로 설명한다."""

    def __init__(self, session: SchedulingSession):
        self.session = session

    @staticmethod
    def parse(text: str, num_machines: int, now: int) -> list[tuple[str, dict]]:
        t = text.strip()
        intents: list[tuple[str, dict]] = []

        m = (re.search(r"(?:설비\s*)?M\s*(\d{1,2})", t, re.I)
             or re.search(r"(\d{1,2})\s*호기", t)
             or re.search(r"설비\s*(\d{1,2})", t))
        machine = min(num_machines - 1, max(0, int(m.group(1)))) if m else 0

        start = dur = None
        # 두 번째 숫자 뒤에 붙는 말이 "구간의 끝"인지 "지속 시간"인지를 가른다.
        #   "12시부터 15시간"  → 12 에서 15시간 동안   (지속)
        #   "12시부터 27시까지" → 12~27               (구간)
        #   "10~24"            → 10~24               (구간)
        # 물결표(~)는 언제나 구간이다. "20~30 동안" = 20부터 30까지지 30시간이 아니다.
        # "부터/에서" 뒤에 시간/동안이 붙은 경우에만 지속 시간으로 읽는다.
        rng = re.search(
            r"(\d+)\s*(?:시간|시|h)?\s*(부터|에서|~|-|–)\s*(\d+)\s*(시간|h|동안|간|시|까지)?", t)
        sm = re.search(r"(\d+)\s*시\s*(?:부터|에|쯤|경)", t)
        if sm:
            start = int(sm.group(1))
        if rng:
            a, sep, b = int(rng.group(1)), rng.group(2), int(rng.group(3))
            tail = rng.group(4) or ""
            is_duration = sep in ("부터", "에서") and tail in ("시간", "h", "동안", "간")
            if is_duration or b < a:
                start, dur = (start if start is not None else a), b
            else:
                start, dur = a, b - a
        if dur is None:
            dm = re.findall(r"(\d+)\s*(?:시간|h)\s*(?:동안|간|정도)?", t)
            if dm:
                dur = int(dm[-1])

        if re.search(r"고장|정지|중단|멈|다운|점검|불가|셧다운", t):
            s = start if start is not None else now
            d = dur if dur else 10
            intents.append(("machine_breakdown",
                            {"machine": machine, "start": s, "end": s + d}))
        if re.search(r"긴급|급한|끼워|신규\s*주문|주문\s*추가|추가\s*주문", t):
            dm = re.search(r"납기\s*(?:는|가|을|를)?\s*(\d+)", t) or re.search(r"(\d+)\s*(?:까지|이내)", t)
            nm_ = re.search(r"공정\s*(\d+)", t)
            intents.append(("add_urgent_order", {
                "due": int(dm.group(1)) if dm else now + 30,
                "release": now,
                "num_operations": int(nm_.group(1)) if nm_ else 4,
            }))
        if re.search(r"되돌|원복|초기화|리셋|취소", t):
            intents.append(("reset_schedule", {}))
        if not intents:
            intents.append(("get_current_schedule", {}))
        return intents

    def run(self, user_message: str) -> AgentReply:
        sess = self.session
        calls: list[ToolCall] = []
        for name, args in self.parse(user_message, sess.num_machines, sess.now):
            try:
                calls.append(ToolCall(name, args, True, sess.dispatch(name, args)))
            except Exception as e:
                calls.append(ToolCall(name, args, False, str(e)))
        return AgentReply(self._describe(calls), calls, sess.changed)

    def _describe(self, calls: list[ToolCall]) -> str:
        sess = self.session
        a, b = sess.base, sess.active
        lines = ["**규칙 기반 분석** (Claude API 미사용 모드)", ""]

        for c in calls:
            if not c.ok:
                lines.append(f"- `{c.name}` 실패: {c.result}")
            elif isinstance(c.result, dict) and "적용" in c.result:
                lines.append(f"- {c.result['적용']}")
                for n in c.result.get("안내", []):
                    lines.append(f"  - {n}")
        lines.append("")

        if sess.changed:
            lines += [
                f"- 총 완료시간 **{a.makespan} → {b.makespan}** "
                f"({b.makespan - a.makespan:+d})",
                f"- 총 납기지연 **{a.total_tardiness} → {b.total_tardiness}** "
                f"({b.total_tardiness - a.total_tardiness:+d})",
                f"- 납기 준수율 **{a.on_time_rate:.0%} → {b.on_time_rate:.0%}**",
                "",
            ]

        impact = sorted(b.job_stats, key=lambda j: -j.tardiness)[:3]
        if impact and impact[0].tardiness > 0:
            lines.append("**납기 영향이 큰 주문**")
            for j in impact:
                if j.tardiness == 0:
                    continue
                prev = a.stat_of(j.id)
                move = f"{prev.completion} → {j.completion}" if prev else f"{j.completion} (신규)"
                bn = j.bottleneck
                why = f", M{bn[0]} 앞에서 {bn[1]}시간 대기" if bn else ""
                lines.append(f"- {j.id}: 완료 {move}, 납기 {j.due} 대비 "
                             f"**{j.tardiness}시간 지연**{why}")
            lines.append("")

        busiest = max(b.machine_stats, key=lambda m: m.util)
        lines.append("**조치 제안**")
        lines.append(f"- 병목은 M{busiest.machine} (가동률 {busiest.util:.0%}) — "
                     f"대체설비 배정이나 잔업 투입 검토")
        if impact and impact[0].tardiness > 0:
            lines.append(f"- {impact[0].id} 잔여 공정 우선 배정, 어려우면 납기 "
                         f"{impact[0].completion} 로 재협의")
        down = {bd.machine for bd in sess.breakdowns}
        idle = [m for m in b.machine_stats if m.util < 0.4 and m.machine not in down]
        if idle:
            lines.append("- 가동률이 낮은 "
                         + ", ".join(f"M{m.machine}" for m in idle)
                         + " 로 공정 분산 가능 여부 확인")
        return "\n".join(lines)


def build_agent(session: SchedulingSession, api_key: str | None,
                model: str = DEFAULT_MODEL):
    """키가 있으면 Claude, 없으면 규칙 기반으로 자동 전환."""
    if api_key or os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeAgent(session, api_key, model)
    return RuleBasedAgent(session)
