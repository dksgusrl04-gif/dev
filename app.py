"""
생산일정 재편성 콘솔 — Streamlit 화면.

실행:  streamlit run app.py
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import agent as A
import parser as P
import scheduler as S

# 접근성 검증을 거친 카테고리 팔레트 (작업 구분색). 순서 고정, 재사용하지 않는다.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
          "#e87ba4", "#008300", "#6250d6", "#e34948"]
GOOD, WARN, CRIT, ACCENT = "#267337", "#8A5C00", "#BD2C0F", "#256EF4"

MENU = ["① 데이터 입력", "② 생산 일정", "③ 재편성 · 비교", "④ AI Agent"]

st.set_page_config(page_title="생산일정 재편성 콘솔", page_icon="📋", layout="wide")

st.markdown("""
<style>
  .block-container{padding-top:1.6rem; max-width:1400px}
  h1,h2,h3{letter-spacing:-.02em}
  [data-testid="stMetricValue"]{font-size:1.7rem; font-variant-numeric:tabular-nums}
  [data-testid="stMetricLabel"]{font-size:.78rem}
  .sysbar{background:#052B57; color:#E8EEF6; padding:9px 14px; border-radius:4px;
          display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:14px}
  .sysbar b{font-size:15px} .sysbar span{font-size:12px; color:#9FB4CC}
  .sysbar .env{background:rgba(255,255,255,.16); padding:2px 7px; border-radius:3px;
               font-size:10.5px; font-weight:700; letter-spacing:.04em}
  .srcnote{font-size:11.5px; color:#6D7882; line-height:1.6; border-top:1px solid #E6E8EA;
           padding-top:10px; margin-top:22px}
  div[data-testid="stDataFrame"] td{font-variant-numeric:tabular-nums}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# 상태
# ─────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def load_instances() -> dict[str, P.Instance]:
    """jobshop1.txt 를 받아 파싱. 실패하면 내장 ft06 만이라도 쓴다."""
    try:
        return P.load("data/jobshop1.txt")
    except Exception:
        return {"ft06": P.load_ft06()}


def ss() -> dict:
    return st.session_state


def _secret(name: str, default: str = "") -> str:
    """secrets.toml 이 없는 환경에서도 터지지 않게 감싼다."""
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def init_state(force: bool = False) -> None:
    s = ss()
    if force or "orders" not in s:
        inst = load_instances()[s.get("instance_key", "ft06")]
        s["instance"] = inst
        s["orders"] = S.build_orders(inst, k=s.get("k", 1.3))
        s["base"] = None
        s["current"] = None
        s["breakdowns"] = []
        s["notes"] = []
        s["urgent_seq"] = 0
        s["chat"] = []
        s["agent_session"] = None


def ensure_base() -> S.Schedule:
    s = ss()
    if s.get("base") is None:
        with st.spinner("일정 계산 중…"):
            s["base"] = S.solve(
                s["orders"], s["instance"].num_machines,
                objective=s.get("objective", "tardiness"),
                solver=s.get("solver", "cpsat"),
                time_limit=s.get("time_limit", 10.0))
        s["current"] = None
    return s["base"]


def active() -> S.Schedule:
    return ss().get("current") or ensure_base()


def invalidate() -> None:
    ss()["base"] = None
    ss()["current"] = None
    ss()["agent_session"] = None


# ─────────────────────────────────────────────────────────────
# 차트
# ─────────────────────────────────────────────────────────────

def color_of(order_ids: list[str], jid: str) -> str:
    try:
        return SERIES[order_ids.index(jid) % len(SERIES)]
    except ValueError:
        return "#6D7882"


def gantt(sched: S.Schedule, orders, *, by="machine", now=None, height=None, title=""):
    """
    설비별 간트. px.timeline 은 x축이 datetime 이어야 해서 정수 시간 단위를
    가짜 날짜로 바꿔야 하는데, go.Bar 의 base 를 쓰면 숫자 그대로 그릴 수 있다.
    """
    ids = [o.id for o in orders]
    rows = ([f"M{m}" for m in range(len(sched.machine_stats))] if by == "machine"
            else [j.id for j in sched.job_stats])

    fig = go.Figure()
    seen = set()
    for op in sorted(sched.ops, key=lambda o: o.start):
        row = f"M{op.machine}" if by == "machine" else op.job_id
        label = op.job_id if by == "machine" else f"M{op.machine}"
        fig.add_trace(go.Bar(
            y=[row], x=[op.duration], base=[op.start], orientation="h",
            marker=dict(color=color_of(ids, op.job_id),
                        line=dict(color="#111" if op.frozen else "#fff",
                                  width=2 if op.frozen else 1)),
            name=op.job_id, legendgroup=op.job_id,
            showlegend=(op.job_id not in seen and by == "machine"),
            text=label, textposition="inside", insidetextanchor="middle",
            textfont=dict(size=10, color="#fff"),
            hovertemplate=(f"<b>{op.job_id}</b> 공정 {op.op_idx + 1}<br>"
                           f"설비 M{op.machine}<br>구간 {op.start} → {op.end}"
                           f"<br>처리시간 {op.duration}"
                           + ("<br>착수 완료(고정)" if op.frozen else "")
                           + "<extra></extra>"),
        ))
        seen.add(op.job_id)

    for b in sched.breakdowns:
        fig.add_shape(type="rect", y0=f"M{b.machine}", y1=f"M{b.machine}",
                      x0=b.start, x1=b.end, xref="x", yref="y",
                      fillcolor=CRIT, opacity=.18, line=dict(color=CRIT, width=1, dash="dot"),
                      layer="below")
        fig.add_annotation(x=(b.start + b.end) / 2, y=f"M{b.machine}", text="가동중단",
                           showarrow=False, font=dict(size=9, color=CRIT), yshift=0)

    if now:
        fig.add_vline(x=now, line=dict(color="#1E2124", width=1.5, dash="dash"),
                      annotation_text=f"t={now}", annotation_position="top")

    fig.update_layout(
        barmode="overlay", title=title or None,
        height=height or max(240, 42 * len(rows) + 110),
        margin=dict(l=8, r=12, t=40 if title else 18, b=8),
        xaxis=dict(title="시간", showgrid=True, gridcolor="#E6E8EA", zeroline=False),
        yaxis=dict(categoryorder="array", categoryarray=rows[::-1], title=None),
        legend=dict(orientation="h", y=-0.22, title=None),
        plot_bgcolor="#FFFFFF", bargap=.28,
    )
    return fig


def tardiness_bar(base: S.Schedule, new: S.Schedule):
    """주문별 지연 변경 전후 막대그래프 (기능 ④)."""
    ids = [j.id for j in new.job_stats]
    before = [(base.stat_of(i).tardiness if base.stat_of(i) else 0) for i in ids]
    after = [j.tardiness for j in new.job_stats]
    fig = go.Figure([
        go.Bar(name="변경 전", x=ids, y=before, marker_color="#B1B8BE",
               text=before, textposition="outside"),
        go.Bar(name="변경 후", x=ids, y=after, marker_color=CRIT,
               text=after, textposition="outside"),
    ])
    fig.update_layout(
        barmode="group", height=320, margin=dict(l=8, r=12, t=46, b=8),
        title=f"주문별 납기지연 · 총계 {base.total_tardiness} → {new.total_tardiness}",
        yaxis=dict(title="지연 시간", gridcolor="#E6E8EA"), xaxis=dict(title=None),
        plot_bgcolor="#FFFFFF", legend=dict(orientation="h", y=-0.18, title=None),
    )
    return fig


def kpi_row(sched: S.Schedule, ref: S.Schedule | None = None):
    inst = ss()["instance"]
    c = st.columns(4)
    c[0].metric("총 완료시간 (makespan)", sched.makespan,
                (sched.makespan - ref.makespan) if ref else None,
                delta_color="inverse",
                help=f"문헌 최적 {inst.optimum}" if inst.optimum else None)
    c[1].metric("납기 준수율", f"{sched.on_time_rate:.0%}",
                f"{(sched.on_time_rate - ref.on_time_rate) * 100:+.0f}%p" if ref else None)
    c[2].metric("총 납기지연", sched.total_tardiness,
                (sched.total_tardiness - ref.total_tardiness) if ref else None,
                delta_color="inverse")
    c[3].metric("평균 설비 가동률", f"{sched.avg_util:.0%}",
                f"{(sched.avg_util - ref.avg_util) * 100:+.0f}%p" if ref else None)


def job_table(sched: S.Schedule) -> pd.DataFrame:
    return pd.DataFrame([{
        "주문": j.id, "투입": j.release, "총가공": j.twk,
        "완료 예정": j.completion, "납기": j.due,
        "지연": j.tardiness, "대기": j.waiting,
        "최다 대기 설비": f"M{j.bottleneck[0]} ({j.bottleneck[1]})" if j.bottleneck else "—",
        "상태": "준수" if j.on_time else "지연",
    } for j in sched.job_stats])


def machine_table(sched: S.Schedule) -> pd.DataFrame:
    return pd.DataFrame([{
        "설비": f"M{m.machine}", "작업 수": m.num_ops, "가동": m.busy,
        "유휴": m.idle, "중단": m.down, "가동률": m.util,
    } for m in sched.machine_stats])


# ─────────────────────────────────────────────────────────────
# 화면
# ─────────────────────────────────────────────────────────────

def page_data():
    s = ss()
    inst = s["instance"]
    st.subheader("① 주문 · 설비 기준정보")
    st.caption("공정 순서·설비·처리시간은 OR-Library 원본값입니다. "
               "납기·우선순위·투입시각은 원본에 없어 이 화면에서 부여합니다.")

    c1, c2, c3 = st.columns([2, 1, 1])
    insts = load_instances()
    keys = sorted(insts, key=lambda k: (insts[k].num_jobs * insts[k].num_machines, k))
    new_key = c1.selectbox(
        "문제 인스턴스", keys, index=keys.index(s.get("instance_key", "ft06")),
        format_func=lambda k: (f"{k} — {insts[k].num_jobs} job × {insts[k].num_machines} machine"
                               + (f" (문헌 최적 {insts[k].optimum})" if insts[k].optimum else "")))
    if new_key != s.get("instance_key"):
        s["instance_key"] = new_key
        init_state(force=True)
        st.rerun()

    new_k = c2.slider("납기 여유계수 k", 1.0, 3.0, s.get("k", 1.3), 0.05,
                      help="납기 = ⌈총 처리시간 × k⌉. 1.2~1.5가 재편성 효과가 가장 잘 보입니다.")
    if abs(new_k - s.get("k", 1.3)) > 1e-9:
        s["k"] = new_k
        s["orders"] = S.build_orders(inst, k=new_k)
        invalidate()
        st.rerun()

    c3.text_input("시간 단위", value="1 = 1시간", disabled=True,
                  help="OR-Library 처리시간은 무단위 정수입니다. 시연에서 1을 1시간으로 봅니다.")

    st.caption(f"출처 설명 줄: _{inst.description}_")

    st.markdown("**주문 정보** — 납기 · 투입 · 우선순위를 직접 고칠 수 있습니다 (우선순위 1=긴급, 3=여유)")
    df = pd.DataFrame([{"주문": o.id, "총가공시간": o.twk, "투입": o.release,
                        "납기": o.due, "우선순위": o.priority} for o in s["orders"]])
    edited = st.data_editor(
        df, hide_index=True, width="stretch", key="order_editor",
        disabled=["주문", "총가공시간"],
        column_config={
            "투입": st.column_config.NumberColumn(min_value=0, step=1),
            "납기": st.column_config.NumberColumn(min_value=1, step=1),
            "우선순위": st.column_config.SelectboxColumn(options=[1, 2, 3]),
        })
    if not edited.equals(df):
        for o, (_, r) in zip(s["orders"], edited.iterrows()):
            o.release, o.due = int(r["투입"]), int(r["납기"])
            o.priority = int(r["우선순위"])
            o.urgent = o.priority == 1
        invalidate()
        st.rerun()

    st.markdown("**공정 라우팅** — 설비번호 · 처리시간 (원본값, 수정 불가)")
    st.dataframe(pd.DataFrame(
        [{"주문": o.id, **{f"공정 {i + 1}": f"M{op.machine} · {op.duration}"
                         for i, op in enumerate(o.ops)}} for o in s["orders"]],
    ), hide_index=True, width="stretch")


def page_plan():
    s = ss()
    st.subheader("② 초기 생산일정 수립")
    c1, c2, c3 = st.columns([1.2, 1.2, 1])
    obj = c1.radio("목적함수", ["tardiness", "makespan"], horizontal=True,
                   index=0 if s.get("objective", "tardiness") == "tardiness" else 1,
                   format_func=lambda v: "총 납기지연 최소화" if v == "tardiness" else "총 완료시간 최소화",
                   help="makespan 으로 ft06 을 풀면 문헌 최적값 55가 나와야 합니다. 모델 검증용입니다.")
    sol = c2.radio("솔버", ["cpsat", "heuristic"], horizontal=True,
                   index=0 if s.get("solver", "cpsat") == "cpsat" else 1,
                   format_func=lambda v: "CP-SAT (최적해)" if v == "cpsat" else "휴리스틱 (즉시)")
    tl = c3.number_input("제한 시간(초)", 1.0, 120.0, s.get("time_limit", 10.0), 1.0)
    if (obj, sol, tl) != (s.get("objective", "tardiness"), s.get("solver", "cpsat"),
                          s.get("time_limit", 10.0)):
        s["objective"], s["solver"], s["time_limit"] = obj, sol, tl
        invalidate()
        st.rerun()

    base = ensure_base()
    kpi_row(base)

    badge = {"OPTIMAL": "최적해 증명됨", "FEASIBLE": "제한시간 내 최선해",
             "HEURISTIC": "휴리스틱 해"}.get(base.status, base.status)
    inst = s["instance"]
    msg = f"{badge} · 계산 {base.solve_seconds:.2f}초"
    if obj == "makespan" and inst.optimum:
        ok = base.makespan == inst.optimum
        msg += f" · 문헌 최적 {inst.optimum} 대비 {'일치 ✅' if ok else f'차이 {base.makespan - inst.optimum:+d}'}"
    st.caption(msg)

    view = st.radio("보기", ["설비별", "주문별"], horizontal=True, key="plan_view")
    st.plotly_chart(gantt(base, s["orders"], by="machine" if view == "설비별" else "job"),
                    width="stretch")

    a, b = st.columns(2)
    with a:
        st.markdown("**주문별 완료 예정 · 납기 준수**")
        st.dataframe(job_table(base), hide_index=True, width="stretch")
    with b:
        st.markdown("**설비별 작업 부하 · 유휴시간**")
        st.dataframe(machine_table(base), hide_index=True, width="stretch",
                     column_config={"가동률": st.column_config.ProgressColumn(
                         format="%.0f%%", min_value=0, max_value=1)})

    with st.expander("공정별 일정표 (작업 · 설비 · 시작 · 종료)"):
        st.dataframe(pd.DataFrame(base.as_rows()), hide_index=True, width="stretch")


def page_reschedule():
    s = ss()
    inst = s["instance"]
    base = ensure_base()
    st.subheader("③ 돌발상황 재편성 · 변경 전후 비교")

    s["now"] = st.slider("재편성 기준시각 t", 0, max(5, base.makespan), s.get("now", 10),
                         help="t 이전에 착수한 공정은 중단하지 않고 그대로 둡니다(비선점).")
    frozen = len(S.freeze_before(base, s["now"]))
    st.caption(f"t={s['now']} 기준 착수 완료 {frozen}개 공정 고정 · "
               f"나머지 {len(base.ops) - frozen}개 재배정 대상")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**설비 고장 · 가동중단**")
        m = st.number_input("설비 번호", 0, inst.num_machines - 1, 2, key="bd_m")
        bs = st.number_input("시작 시각", 0, 10_000, 10, key="bd_s")
        bd = st.number_input("중단 시간", 1, 10_000, 14, key="bd_d")
        if st.button("가동중단 등록", width="stretch"):
            try:
                b = S.Breakdown(int(m), int(bs), int(bs + bd))
                for w in S.validate_breakdown(b, inst.num_machines, s["orders"]):
                    st.warning(w)
                s["breakdowns"].append(b)
                s["current"] = None
                st.rerun()
            except S.InputError as e:
                st.error(str(e))
    with c2:
        st.markdown("**긴급 주문 추가**")
        ur = st.number_input("투입 시각", 0, 10_000, int(s.get("now", 10)), key="uo_r")
        ud = st.number_input("납기", 1, 10_000, 40, key="uo_d")
        un = st.number_input("공정 수", 1, inst.num_machines, 4, key="uo_n")
        if st.button("긴급 주문 등록", width="stretch"):
            try:
                ops = S.random_route(inst.num_machines, int(un))
                for w in S.validate_urgent(int(ur), int(ud), ops, inst.num_machines):
                    st.warning(w)
                s["urgent_seq"] += 1
                s["orders"].append(
                    S.make_urgent_order(f"U{s['urgent_seq']}", int(ur), int(ud), ops))
                s["base"] = None
                s["current"] = None
                st.rerun()
            except S.InputError as e:
                st.error(str(e))

    events = ([f"설비 고장 — M{b.machine} {b.start}~{b.end} ({b.length}시간)"
               for b in s["breakdowns"]]
              + [f"긴급 주문 — {o.id} 투입 {o.release}, 납기 {o.due}, 공정 {len(o.ops)}개"
                 for o in s["orders"] if o.added])
    if events:
        for e in events:
            st.write("·", e)
    else:
        st.info("등록된 돌발상황이 없습니다. 위에서 설비 고장이나 긴급 주문을 추가하세요.")

    r1, r2 = st.columns([1, 4])
    if r1.button("잔여 일정 재계산", type="primary", width="stretch"):
        try:
            with st.spinner("재편성 중…"):
                s["current"], s["notes"] = S.reschedule(
                    s["orders"], inst.num_machines, base, s["now"],
                    breakdowns=s["breakdowns"], objective=s.get("objective", "tardiness"),
                    solver=s.get("solver", "cpsat"), time_limit=s.get("time_limit", 10.0))
            s["agent_session"] = None
        except Exception as e:
            st.error(f"재편성 실패: {e}")
    if r2.button("초기 상태로 복원", width="content"):
        s["breakdowns"] = []
        s["orders"] = [o for o in s["orders"] if not o.added]
        s["urgent_seq"] = 0
        invalidate()
        st.rerun()

    cur = s.get("current")
    if cur is None:
        st.stop()

    for n in s.get("notes", []):
        st.caption("· " + n)
    st.divider()
    st.markdown("### 변경 전후 비교")
    kpi_row(cur, base)
    st.plotly_chart(tardiness_bar(base, cur), width="stretch")

    cmp_df = pd.DataFrame(S.compare(base, cur))
    st.dataframe(cmp_df, hide_index=True, width="stretch")

    g1, g2 = st.columns(2)
    with g1:
        st.plotly_chart(gantt(base, s["orders"], title="변경 전", height=330),
                        width="stretch")
    with g2:
        st.plotly_chart(gantt(cur, s["orders"], now=s["now"], title="변경 후", height=330),
                        width="stretch")


def page_agent():
    s = ss()
    st.subheader("④ AI Agent — 자연어 상황 입력")
    st.caption("일정 계산은 전부 스케줄러(CP-SAT)가 합니다. "
               "Claude 는 도구를 호출하고 결과를 해석·설명만 합니다.")

    base = ensure_base()
    if s.get("agent_session") is None:
        s["agent_session"] = A.SchedulingSession(
            instance=s["instance"], orders=s["orders"], base=base,
            now=s.get("now", 10), objective=s.get("objective", "tardiness"),
            solver=s.get("solver", "cpsat"), time_limit=s.get("time_limit", 10.0))
        s["agent_session"].breakdowns = list(s["breakdowns"])
        s["agent_session"].current = s.get("current")

    key = st.session_state.get("api_key") or _secret("ANTHROPIC_API_KEY")
    mode = "Claude API" if key else "규칙 기반 (키 없음)"
    st.caption(f"실행 모드: **{mode}** · 사용 도구: "
               "`machine_breakdown` `add_urgent_order` `get_current_schedule` "
               "`get_comparison` `reset_schedule`")

    for role, text in s["chat"]:
        with st.chat_message(role):
            st.markdown(text)

    cols = st.columns(3)
    examples = ["3호기가 12시부터 15시간 멈췄어. 어떤 주문이 위험한지 알려줘.",
                "긴급 주문 하나 추가해줘. 납기는 40이야.",
                "변경 전후를 비교해서 설명해줘."]
    picked = None
    for col, ex in zip(cols, examples):
        if col.button(ex, width="stretch"):
            picked = ex

    typed = st.chat_input("상황을 문장으로 입력하세요")
    msg = picked or typed
    if not msg:
        return

    s["chat"].append(("user", msg))
    with st.chat_message("user"):
        st.markdown(msg)

    try:
        ag = A.build_agent(s["agent_session"], key or None,
                           st.session_state.get("model", A.DEFAULT_MODEL))
        with st.chat_message("assistant"), st.spinner("분석 중…"):
            reply = ag.run(msg)
            st.markdown(reply.text)
            if reply.tool_calls:
                with st.expander(f"도구 호출 {len(reply.tool_calls)}건"):
                    for c in reply.tool_calls:
                        st.write(("✅" if c.ok else "⚠️"), f"`{c.name}`", c.args)
                        if not c.ok:
                            st.caption(str(c.result))
        s["chat"].append(("assistant", reply.text))
        if reply.schedule_changed:
            s["current"] = s["agent_session"].current
            s["breakdowns"] = s["agent_session"].breakdowns
            s["notes"] = s["agent_session"].notes
            st.success("일정이 재편성됐습니다. ③ 화면에서 간트와 비교표를 확인하세요.")
    except Exception as e:
        st.error(f"에이전트 오류: {e}")
        s["chat"].append(("assistant", f"오류: {e}"))


# ─────────────────────────────────────────────────────────────
# 조립
# ─────────────────────────────────────────────────────────────

def main():
    st.session_state.setdefault("instance_key", "ft06")
    init_state()
    inst = ss()["instance"]

    st.markdown(
        f'<div class="sysbar"><b>생산일정 재편성 시스템</b>'
        f'<span>Production Rescheduling Console</span>'
        f'<span class="env">DEMO</span>'
        f'<span>OR-Library {inst.name} · {inst.num_jobs} job × {inst.num_machines} machine</span>'
        f'</div>', unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("### 메뉴")
        page = st.radio("메뉴", MENU, label_visibility="collapsed")
        st.divider()
        st.markdown("### Claude API")
        st.text_input("API Key", type="password", key="api_key",
                      help="st.secrets 또는 환경변수 ANTHROPIC_API_KEY 로도 설정할 수 있습니다. "
                           "키는 저장되지 않습니다.")
        st.selectbox("모델", ["claude-sonnet-5-5", "claude-opus-5", "claude-haiku-5-5"],
                     key="model")
        st.divider()
        if st.button("전체 초기화", width="stretch"):
            for k in ["orders", "base", "current", "breakdowns", "chat", "agent_session"]:
                st.session_state.pop(k, None)
            st.rerun()

    {MENU[0]: page_data, MENU[1]: page_plan,
     MENU[2]: page_reschedule, MENU[3]: page_agent}[page]()

    st.markdown(
        '<div class="srcnote">공정 데이터 출처: OR-Library <code>jobshop1.txt</code> (82 instances), '
        'J.E. Beasley, Brunel University London. ft06 원전: H. Fisher &amp; G.L. Thompson (1963), '
        '<i>Industrial Scheduling</i>, Prentice Hall, 225–251. '
        '납기·우선순위·긴급주문·설비고장 정보는 원본에 없어 본 시연에서 설정한 값입니다.</div>',
        unsafe_allow_html=True)


if __name__ == "__main__":
    main()
