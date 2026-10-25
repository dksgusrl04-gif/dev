"""
생산일정 재편성 콘솔 — 웹 서버 (HTML 화면 + 계산 API).

화면(static/index.html)은 브라우저에서 돌고, 계산은 전부 이 서버가 한다.
    - 일정 수립·재편성 : scheduler.py (OR-Tools CP-SAT / 휴리스틱)
    - AI Agent       : agent.py (Claude API tool calling, 키 없으면 규칙 기반)

서버는 상태를 저장하지 않는다. 화면이 현재 주문·일정을 요청에 담아 보내고,
서버는 계산 결과만 돌려준다. 무료 호스팅이 재시작돼도 화면 상태가 날아가지 않는다.

실행 (로컬):
    pip install -r requirements.txt
    uvicorn server:app --reload          →  http://localhost:8000

환경변수:
    ANTHROPIC_API_KEY   서버에 둘 Claude API 키 (없으면 화면에서 입력하거나 규칙 기반으로 동작)
    APP_PASSCODE        설정하면, 서버 키로 Claude 를 부를 때 화면에서 이 접속 코드를 넣어야 한다
    AGENT_RATE_LIMIT    AI 분석 요청 한도, IP당 '횟수/초' (기본 30/600 = 10분에 30회, 0/1 이면 끔)
    SOLVE_RATE_LIMIT    일정 계산 요청 한도 (기본 300/600)
    CLAUDE_MODEL        기본 모델 (기본값 claude-sonnet-5-5)
    CPSAT_WORKERS       CP-SAT 병렬 탐색 수 (기본 8)
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import agent as A
import parser as P
import scheduler as S

HERE = Path(__file__).resolve().parent
DATA_FILE = HERE / "data" / "jobshop1.txt"
STATIC_DIR = HERE / "static"

MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", A.DEFAULT_MODEL)
CPSAT_WORKERS = max(1, int(os.environ.get("CPSAT_WORKERS", "8") or 8))
HEURISTIC_BUDGET = max(0.5, float(os.environ.get("HEURISTIC_BUDGET", "5") or 5))

# 남용 방지용 상한 (공개 주소로 배포되므로)
MAX_ORDERS, MAX_OPS, MAX_MACHINES = 80, 2000, 50
MAX_TIME_LIMIT, MAX_RESTARTS = 60.0, 3000
MAX_MESSAGE, MAX_HISTORY = 1000, 12

# 무료 서버는 CPU 가 작다. 동시에 여러 CP-SAT 가 돌면 전부 느려지니 2개로 제한한다.
_SOLVE_SLOTS = threading.BoundedSemaphore(2)


# ─────────────────────────────────────────────────────────────
# 남이 내 키로 질문하는 것 막기
#   키는 서버 환경변수에만 있고 응답에 실리지 않는다. 남는 위험은 '남이 이 주소로 질문을 보내
#   사용료가 나가는 것'이라 ① 다른 사이트에서 온 요청 거절 ② IP당 횟수 제한을 건다.
#   마음먹고 직접 호출하는 것까지 완전히 막지는 못하므로 Anthropic 콘솔의 월 사용 한도를 꼭 건다.
# ─────────────────────────────────────────────────────────────

def _rate(name: str, default: str) -> tuple[int, float]:
    """'30/600' = 600초에 30회. 0/... 이면 제한 없음."""
    raw = os.environ.get(name, default)
    try:
        n, w = raw.split("/")
        return max(0, int(n)), max(1.0, float(w))
    except ValueError:
        n, w = default.split("/")
        return int(n), float(w)


AGENT_RATE = _rate("AGENT_RATE_LIMIT", "30/600")     # AI 분석: IP당 10분에 30회
SOLVE_RATE = _rate("SOLVE_RATE_LIMIT", "300/600")    # 일정 계산: IP당 10분에 300회
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def wait_seconds(request: Request, bucket: str, limit: tuple[int, float]) -> int:
    """한도를 넘었으면 몇 초 기다려야 하는지, 아니면 0."""
    n, window = limit
    if n <= 0:
        return 0
    now = time.monotonic()
    with _hits_lock:
        q = _hits[(bucket, client_ip(request))]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= n:
            return int(window - (now - q[0])) + 1
        q.append(now)
        if len(_hits) > 5000:                      # 오래된 IP 기록 정리
            for k in [k for k, v in _hits.items() if not v]:
                del _hits[k]
    return 0


def same_site(request: Request) -> bool:
    """이 서버가 내려준 화면에서 온 요청만 받는다. (브라우저는 POST 에 Origin 을 붙인다)"""
    origin = request.headers.get("origin")
    if not origin:
        return True          # 브라우저가 아닌 직접 호출 — 막을 수 없으니 횟수 제한으로만 다룬다
    return origin.split("://", 1)[-1].rstrip("/") == request.headers.get("host", "")


def guard(request: Request, bucket: str, limit: tuple[int, float]) -> JSONResponse | None:
    if not same_site(request):
        return err("허용되지 않은 사이트에서 온 요청입니다.", 403)
    wait = wait_seconds(request, bucket, limit)
    if wait:
        return err(f"요청이 너무 많습니다. 약 {math.ceil(wait / 60)}분 뒤 다시 시도하세요.", 429)
    return None


# ─────────────────────────────────────────────────────────────
# 인스턴스 (OR-Library jobshop1.txt, 82개)
# ─────────────────────────────────────────────────────────────

_instances: dict[str, P.Instance] | None = None
_instances_lock = threading.Lock()


def instances() -> dict[str, P.Instance]:
    """처음 부를 때 한 번 읽는다. 파일이 없으면 받아 보고, 그래도 안 되면 ft06 만."""
    global _instances
    with _instances_lock:
        if _instances is None:
            try:
                _instances = P.load(DATA_FILE)
            except Exception:
                _instances = {"ft06": P.load_ft06()}
        return _instances


def instance_meta(inst: P.Instance) -> dict:
    return {"key": inst.name, "n": inst.name, "nj": inst.num_jobs, "nm": inst.num_machines,
            "opt": inst.optimum, "description": inst.description}


# ─────────────────────────────────────────────────────────────
# 화면 ↔ 서버 변환 (화면 JS 의 자료 모양에 그대로 맞춘다)
# ─────────────────────────────────────────────────────────────

class BadRequest(ValueError):
    pass


def _int(v, name: str, lo: int | None = None, hi: int | None = None) -> int:
    try:
        x = int(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} 값이 정수가 아닙니다: {v!r}")
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        rng = (f"{lo}~{hi} 범위" if lo is not None and hi is not None
               else f"{lo} 이상" if lo is not None else f"{hi} 이하")
        raise BadRequest(f"{name}: {rng}여야 합니다 (입력 {x}).")
    return x


def orders_in(raw, nm: int) -> list[S.Order]:
    if not isinstance(raw, list) or not raw:
        raise BadRequest("주문 목록이 비어 있습니다.")
    if len(raw) > MAX_ORDERS:
        raise BadRequest(f"주문은 최대 {MAX_ORDERS}개까지 계산합니다.")
    out, total_ops, seen = [], 0, set()
    for r in raw:
        oid = str(r.get("id", "")).strip()[:20]
        if not oid or oid in seen:
            raise BadRequest(f"주문 번호가 비었거나 중복됩니다: {oid!r}")
        seen.add(oid)
        ops = []
        for i, op in enumerate(r.get("ops") or []):
            m = _int(op.get("machine"), f"{oid} 공정{i + 1} 설비", 0, nm - 1)
            d = _int(op.get("dur", op.get("duration")), f"{oid} 공정{i + 1} 처리시간", 0, 100000)
            ops.append(S.Operation(oid, i, m, d))
        if not ops:
            raise BadRequest(f"{oid} 에 공정이 없습니다.")
        total_ops += len(ops)
        prio = _int(r.get("priority", 2), f"{oid} 우선순위", 1, 3)
        out.append(S.Order(
            id=oid, name=str(r.get("name") or oid)[:40], ops=ops,
            due=_int(r.get("due"), f"{oid} 납기", 0),
            release=_int(r.get("release", 0), f"{oid} 투입", 0),
            priority=prio, urgent=bool(r.get("urgent")) or prio == 1,
            added=bool(r.get("added")),
        ))
    if total_ops > MAX_OPS:
        raise BadRequest(f"공정 수가 너무 많습니다 ({total_ops} > {MAX_OPS}).")
    return out


def breakdowns_in(raw, nm: int) -> list[S.Breakdown]:
    out = []
    for b in raw or []:
        bd = S.Breakdown(_int(b.get("machine"), "고장 설비", 0, nm - 1),
                         _int(b.get("start"), "고장 시작", 0), _int(b.get("end"), "고장 종료", 0))
        if bd.end <= bd.start:
            raise BadRequest(f"고장 종료({bd.end})가 시작({bd.start})보다 뒤여야 합니다.")
        out.append(bd)
    return out


def ops_in(raw, live: set[str]) -> list[S.ScheduledOp]:
    out = []
    for p in raw or []:
        jid = str(p.get("jobId"))
        if jid not in live:
            continue
        out.append(S.ScheduledOp(jid, int(p["opIdx"]), int(p["machine"]),
                                 int(p["start"]), int(p["end"]), bool(p.get("frozen"))))
    return out


def order_out(o: S.Order) -> dict:
    return {"id": o.id, "name": o.name, "release": o.release, "twk": o.twk, "due": o.due,
            "priority": o.priority, "urgent": o.urgent, "added": o.added,
            "ops": [{"machine": op.machine, "dur": op.duration, "idx": op.idx} for op in o.ops]}


def schedule_out(s: S.Schedule, orders: list[S.Order]) -> dict:
    names = {o.id: o.name for o in orders}
    jobs = []
    for j in s.job_stats:
        bn = j.bottleneck
        jobs.append({
            "id": j.id, "name": names.get(j.id, j.id), "due": j.due, "priority": j.priority,
            "urgent": j.urgent, "twk": j.twk, "release": j.release,
            "completion": j.completion, "lateness": j.lateness, "tardiness": j.tardiness,
            "onTime": j.on_time, "waiting": j.waiting,
            "waitBy": {str(k): v for k, v in j.wait_by_machine.items()},
            "bnM": bn[0] if bn else None, "bnW": bn[1] if bn else 0,
        })
    return {
        "makespan": s.makespan,
        "placed": [{"jobId": o.job_id, "opIdx": o.op_idx, "machine": o.machine,
                    "start": o.start, "end": o.end, "dur": o.duration, "frozen": o.frozen}
                   for o in s.ops],
        "breakdowns": [{"machine": b.machine, "start": b.start, "end": b.end} for b in s.breakdowns],
        "jobStats": jobs,
        "machineStats": [{"machine": m.machine, "busy": m.busy, "down": m.down, "idle": m.idle,
                          "util": m.util, "numOps": m.num_ops} for m in s.machine_stats],
        "totalTardiness": s.total_tardiness, "maxTardiness": s.max_tardiness,
        "numTardy": s.num_tardy, "onTimeRate": s.on_time_rate, "avgUtil": s.avg_util,
        "objective": s.objective, "solver": s.solver, "status": s.status,
        "solveSeconds": round(s.solve_seconds, 3), "now": s.now,
    }


def solver_opts(body: dict) -> dict:
    solver = body.get("solver", "cpsat")
    if solver not in ("cpsat", "heuristic", "rule"):
        raise BadRequest(f"알 수 없는 솔버입니다: {solver}")
    if solver == "cpsat" and not S.cpsat_available():
        raise BadRequest("이 서버에는 ortools 가 없어 CP-SAT 를 쓸 수 없습니다. 휴리스틱을 선택하세요.")
    objective = body.get("objective", "tardiness")
    if objective not in ("tardiness", "makespan"):
        raise BadRequest(f"알 수 없는 목적함수입니다: {objective}")
    rule = body.get("rule", "MINSLACK")
    if rule not in S.RULES:
        raise BadRequest(f"알 수 없는 우선순위 규칙입니다: {rule}")
    try:
        tl = min(MAX_TIME_LIMIT, max(1.0, float(body.get("timeLimit", 10.0) or 10.0)))
    except (TypeError, ValueError):
        raise BadRequest("제한 시간이 숫자가 아닙니다.")
    return {"solver": solver, "objective": objective, "rule": rule, "time_limit": tl,
            "restarts": min(MAX_RESTARTS, max(0, int(body.get("restarts", 1500) or 0))),
            "workers": CPSAT_WORKERS,
            # 휴리스틱 다중시작은 시간으로 끊는다 (무료 서버 CPU 가 작아 횟수로 두면 대규모에서 수 분이 걸린다)
            "time_budget": min(tl, HEURISTIC_BUDGET)}


def rebuild(orders, nm, raw_ops, breakdowns, meta: dict | None, *, objective, solver, now=0):
    """화면이 보낸 일정(공정 시작·종료)을 Schedule 객체로 되살린다. 통계는 다시 계산."""
    meta = meta or {}
    placed = ops_in(raw_ops, {o.id for o in orders})
    return S._summarize(orders, nm, placed, breakdowns, objective=objective,
                        solver=meta.get("solver", solver), status=meta.get("status", "UNKNOWN"),
                        seconds=float(meta.get("solveSeconds", 0) or 0), now=now)


# ─────────────────────────────────────────────────────────────
# 엔드포인트
# ─────────────────────────────────────────────────────────────

def err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


async def read_json(request: Request) -> dict:
    # JSON 형식만 받는다. 그래야 다른 사이트가 폼 전송 같은 '단순 요청'으로 몰래 부를 수 없다.
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise BadRequest("Content-Type 은 application/json 이어야 합니다.")
    try:
        body = await request.json()
    except Exception:
        raise BadRequest("요청 본문이 JSON 이 아닙니다.")
    if not isinstance(body, dict):
        raise BadRequest("요청 본문 형식이 잘못됐습니다.")
    return body


async def health(request: Request):
    return JSONResponse({
        "ok": True,
        "cpsat": S.cpsat_available(),
        "serverKey": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "passcodeRequired": bool(os.environ.get("APP_PASSCODE")),
        "defaultModel": DEFAULT_MODEL, "models": MODELS,
        "cpsatWorkers": CPSAT_WORKERS,
    })


async def list_instances(request: Request):
    insts = await run_in_threadpool(instances)
    items = sorted(insts.values(), key=lambda i: (i.num_jobs * i.num_machines, i.name))
    return JSONResponse({"source": "jobshop1.txt" if len(insts) > 1 else "builtin",
                         "instances": [instance_meta(i) for i in items]})


async def get_instance(request: Request):
    key = request.path_params["key"].lower()
    insts = await run_in_threadpool(instances)
    inst = insts.get(key)
    if inst is None:
        return err(f"'{key}' 인스턴스가 없습니다.", 404)
    return JSONResponse({**instance_meta(inst),
                         "jobs": [[[m, d] for m, d in ops] for ops in inst.jobs]})


def _solve_sync(body: dict) -> dict:
    nm = _int(body.get("numMachines"), "설비 수", 1, MAX_MACHINES)
    orders = orders_in(body.get("orders"), nm)
    bds = breakdowns_in(body.get("breakdowns"), nm)
    opt = solver_opts(body)
    now = _int(body.get("now", 0), "기준시각", 0)
    with _SOLVE_SLOTS:
        if body.get("baseOps") is not None and (now > 0 or bds):
            base = rebuild(orders, nm, body["baseOps"], [], body.get("baseMeta"),
                           objective=opt["objective"], solver=opt["solver"])
            sched, notes = S.reschedule(orders, nm, base, now, breakdowns=bds, **opt)
        else:
            sched = S.solve(orders, nm, breakdowns=bds, now=now, **opt)
            notes = []
    return {"schedule": schedule_out(sched, orders), "notes": notes}


async def solve(request: Request):
    blocked = guard(request, "solve", SOLVE_RATE)
    if blocked:
        return blocked
    try:
        body = await read_json(request)
        return JSONResponse(await run_in_threadpool(_solve_sync, body))
    except (BadRequest, S.InputError) as e:
        return err(str(e))
    except RuntimeError as e:
        return err(str(e), 422)


def _clean_history(raw) -> list[dict]:
    """이전 대화는 글자만 넘긴다 (도구 호출 내역은 매 요청마다 새로 만든다)."""
    msgs = []
    for m in (raw or [])[-MAX_HISTORY:]:
        role, text = m.get("role"), str(m.get("content", ""))[:4000]
        if role not in ("user", "assistant") or not text.strip():
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + text
        else:
            msgs.append({"role": role, "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    while msgs and msgs[-1]["role"] != "assistant":
        msgs.pop()
    return msgs


def _agent_sync(body: dict) -> tuple[int, dict]:
    message = str(body.get("message", "")).strip()
    if not message:
        raise BadRequest("메시지가 비어 있습니다.")
    message = message[:MAX_MESSAGE]
    st = body.get("state") or {}
    nm = _int(st.get("numMachines"), "설비 수", 1, MAX_MACHINES)
    orders = orders_in(st.get("orders"), nm)
    raw_bds = breakdowns_in(st.get("breakdowns"), nm)
    opt = solver_opts(st)
    now = _int(st.get("now", 10), "기준시각", 0)

    base = rebuild(orders, nm, st.get("baseOps"), [], st.get("baseMeta"),
                   objective=opt["objective"], solver=opt["solver"])
    if not base.ops:
        raise BadRequest("기준 일정이 없습니다. 먼저 일정을 수립하세요.")
    current = None
    if st.get("currentOps"):
        cur_bds = breakdowns_in((st.get("currentMeta") or {}).get("breakdowns"), nm) or raw_bds
        current = rebuild(orders, nm, st["currentOps"], cur_bds, st.get("currentMeta"),
                          objective=opt["objective"], solver=opt["solver"], now=now)

    session = A.SchedulingSession(
        instance=SimpleNamespace(num_machines=nm), orders=orders, base=base, now=now,
        objective=opt["objective"], solver=opt["solver"], time_limit=opt["time_limit"],
        rule=opt["rule"], restarts=opt["restarts"], workers=opt["workers"],
        time_budget=opt["time_budget"], current=current, breakdowns=raw_bds,
        urgent_seq=_int(st.get("urgentSeq", 0), "긴급주문 번호", 0))

    user_key = str(body.get("apiKey") or "").strip()
    server_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    key, key_source = (user_key, "user") if user_key else ((server_key, "server") if server_key else ("", None))
    if key_source == "server":
        code = os.environ.get("APP_PASSCODE", "")
        if code and str(body.get("passcode") or "") != code:
            return 403, {"error": "서버에 저장된 API 키를 쓰려면 접속 코드가 필요합니다. "
                                  "API 설정에서 접속 코드를 입력하거나 본인 키를 넣으세요.",
                         "needPasscode": True}

    model = str(body.get("model") or DEFAULT_MODEL)
    if not re.fullmatch(r"claude-[a-z0-9.\-]{2,60}", model):
        raise BadRequest(f"모델 이름 형식이 아닙니다: {model}")

    with _SOLVE_SLOTS:
        if key:
            ag = A.ClaudeAgent(session, key, model)
            ag.messages = _clean_history(body.get("history"))
            mode = "claude"
        else:
            ag = A.RuleBasedAgent(session)
            mode = "rule"
        try:
            reply = ag.run(message)
        except Exception as e:                     # Claude API 쪽 오류 (키·모델·네트워크)
            name = type(e).__name__
            hint = {"AuthenticationError": "API 키가 올바르지 않습니다.",
                    "PermissionDeniedError": "이 키로는 해당 모델을 쓸 수 없습니다.",
                    "NotFoundError": f"모델 '{model}' 을 찾을 수 없습니다.",
                    "RateLimitError": "요청 한도를 넘었습니다. 잠시 후 다시 시도하세요.",
                    "APIConnectionError": "Claude API 에 연결하지 못했습니다.",
                    "APITimeoutError": "Claude API 응답이 너무 늦습니다."}.get(name, "")
            return 502, {"error": f"Claude API 오류 ({name}). {hint}".strip(), "mode": mode}

    calls = [{"name": c.name, "args": c.args, "ok": c.ok,
              "result": json.loads(json.dumps(c.result, ensure_ascii=False, default=str))}
             for c in reply.tool_calls]
    # 이번 요청에서 일정을 실제로 바꾼 도구 호출이 있었는지 (화면에 '재편성됨' 안내용)
    changed = any(c.ok and c.name in ("machine_breakdown", "add_urgent_order")
                  for c in reply.tool_calls)
    return 200, {
        "mode": mode, "keySource": key_source, "model": model if mode == "claude" else None,
        "text": reply.text, "toolCalls": calls, "usage": reply.usage,
        "changed": changed,
        "state": {
            "orders": [order_out(o) for o in session.orders],
            "breakdowns": [{"machine": b.machine, "start": b.start, "end": b.end}
                           for b in session.breakdowns],
            "current": schedule_out(session.current, session.orders) if session.current else None,
            "notes": session.notes, "urgentSeq": session.urgent_seq, "now": session.now,
        },
    }


async def agent(request: Request):
    blocked = guard(request, "agent", AGENT_RATE)
    if blocked:
        return blocked
    try:
        body = await read_json(request)
        code, payload = await run_in_threadpool(_agent_sync, body)
        return JSONResponse(payload, status_code=code)
    except (BadRequest, S.InputError) as e:
        return err(str(e))
    except RuntimeError as e:
        return err(str(e), 422)


async def api_not_found(request: Request):
    return err("없는 API 경로입니다.", 404)


routes = [
    Route("/api/health", health),
    Route("/api/instances", list_instances),
    Route("/api/instances/{key}", get_instance),
    Route("/api/solve", solve, methods=["POST"]),
    Route("/api/agent", agent, methods=["POST"]),
    Route("/api/{rest:path}", api_not_found, methods=["GET", "POST"]),
    Mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static"),
]

app = Starlette(routes=routes)
