"""
OR-Library job shop 인스턴스 파서.

원본: https://people.brunel.ac.uk/~mastjjb/jeb/orlib/files/jobshop1.txt  (82 instances)

파일 구조 (실제 원문 확인 결과):

    +++++++++++++++++++++++++++++

    instance ft06

    +++++++++++++++++++++++++++++
    Fisher and Thompson 6x6 instance, alternate name (mt06)
    6 6
    2  1  0  3  1  6  3  7  5  3  4  6
    ...

주의: `instance <이름>` 바로 다음 줄이 차원(6 6)이 아니라 **설명 줄**이다.
"instance 줄 아래 n줄을 읽는다"로 짜면 전 인스턴스에서 파싱이 깨진다.
그래서 이 파서는 "정수 2개만 있는 첫 줄"을 차원 줄로 찾는다.

설비 번호는 0부터 시작한다.
"""
from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ORLIB_URL = "https://people.brunel.ac.uk/~mastjjb/jeb/orlib/files/jobshop1.txt"

# 차원 줄: 공백으로 구분된 정수 정확히 2개
_DIM_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s*$")
_INSTANCE_RE = re.compile(r"^\s*instance\s+(\S+)\s*$", re.IGNORECASE)
_SEP_RE = re.compile(r"^\s*\++\s*$")

# 문헌에 알려진 최적 makespan (검증용). 전체 목록이 아니라 자주 쓰는 소규모 위주.
KNOWN_OPTIMA = {
    "ft06": 55, "ft10": 930, "ft20": 1165,
    "la01": 666, "la02": 655, "la03": 597, "la04": 590, "la05": 593,
    "la06": 926, "la07": 890, "la08": 863, "la09": 951, "la10": 958,
    "la11": 1222, "la12": 1039, "la13": 1150, "la14": 1292, "la15": 1207,
    "la16": 945, "la17": 784, "la18": 848, "la19": 842, "la20": 902,
    "abz5": 1234, "abz6": 943,
    "orb01": 1059, "orb02": 888, "orb03": 1005, "orb04": 1005, "orb05": 887,
}


@dataclass
class Instance:
    """JSSP 인스턴스 하나. jobs[j] = [(설비번호, 처리시간), ...] 공정 순서대로."""
    name: str
    num_jobs: int
    num_machines: int
    jobs: list[list[tuple[int, int]]]
    description: str = ""

    @property
    def optimum(self) -> int | None:
        return KNOWN_OPTIMA.get(self.name)

    def total_work(self, job_index: int) -> int:
        """작업 하나의 총 처리시간 (TWK). 납기 설정의 기준값."""
        return sum(d for _, d in self.jobs[job_index])

    def validate(self) -> None:
        if len(self.jobs) != self.num_jobs:
            raise ValueError(
                f"{self.name}: 작업 수 불일치 (선언 {self.num_jobs}, 실제 {len(self.jobs)})"
            )
        for j, ops in enumerate(self.jobs):
            if len(ops) != self.num_machines:
                raise ValueError(
                    f"{self.name}: 작업 {j}의 공정 수 불일치 "
                    f"(선언 {self.num_machines}, 실제 {len(ops)})"
                )
            seen = [m for m, _ in ops]
            if sorted(seen) != list(range(self.num_machines)):
                raise ValueError(
                    f"{self.name}: 작업 {j}가 설비를 한 번씩 쓰지 않음 -> {seen}"
                )
            for m, d in ops:
                if not (0 <= m < self.num_machines):
                    raise ValueError(f"{self.name}: 설비번호 범위 초과 {m}")
                if d < 0:
                    raise ValueError(f"{self.name}: 음수 처리시간 {d}")

    def __repr__(self) -> str:
        opt = f", 최적 {self.optimum}" if self.optimum else ""
        return f"<Instance {self.name} {self.num_jobs}x{self.num_machines}{opt}>"


def parse_all(text: str) -> dict[str, Instance]:
    """jobshop1.txt 전문을 파싱해 {이름: Instance} 로 반환."""
    lines = text.splitlines()
    out: dict[str, Instance] = {}

    i = 0
    while i < len(lines):
        m = _INSTANCE_RE.match(lines[i])
        if not m:
            i += 1
            continue

        name = m.group(1).lower()
        i += 1

        # 1) 차원 줄을 찾는다. 그 전에 나오는 비어있지 않은 줄은 설명.
        desc_parts: list[str] = []
        dim = None
        while i < len(lines):
            line = lines[i]
            if _INSTANCE_RE.match(line):       # 데이터 없이 다음 인스턴스로 넘어간 경우
                break
            d = _DIM_RE.match(line)
            if d:
                dim = (int(d.group(1)), int(d.group(2)))
                i += 1
                break
            if line.strip() and not _SEP_RE.match(line):
                desc_parts.append(line.strip())
            i += 1

        if dim is None:
            continue
        n_jobs, n_machines = dim

        # 2) 데이터 줄 n_jobs 개를 읽는다. 한 줄 = 작업 하나.
        jobs: list[list[tuple[int, int]]] = []
        while i < len(lines) and len(jobs) < n_jobs:
            line = lines[i]
            i += 1
            if not line.strip() or _SEP_RE.match(line):
                continue
            nums = [int(x) for x in line.split()]
            if len(nums) < 2:
                continue
            if len(nums) % 2 != 0:
                raise ValueError(
                    f"{name}: 숫자 개수가 홀수인 줄 -> {line.strip()!r}"
                )
            jobs.append([(nums[k], nums[k + 1]) for k in range(0, len(nums), 2)])

        inst = Instance(
            name=name,
            num_jobs=n_jobs,
            num_machines=n_machines,
            jobs=jobs,
            description=" ".join(desc_parts),
        )
        inst.validate()
        out[name] = inst

    return out


def parse_one(text: str, name: str) -> Instance:
    """인스턴스 하나만 뽑아온다."""
    all_inst = parse_all(text)
    key = name.lower()
    if key not in all_inst:
        raise KeyError(
            f"'{name}' 인스턴스를 찾지 못했습니다. "
            f"사용 가능: {', '.join(sorted(all_inst)[:12])} ... (총 {len(all_inst)}개)"
        )
    return all_inst[key]


def load(path: str | Path = "data/jobshop1.txt", *, download: bool = True) -> dict[str, Instance]:
    """
    로컬 jobshop1.txt 를 읽어 파싱한다. 파일이 없으면 OR-Library 에서 받아 저장한다.
    (Streamlit Cloud 같이 외부 통신이 막힌 환경이면 download=False 로 두고
     저장소에 data/jobshop1.txt 를 같이 커밋하세요.)
    """
    p = Path(path)
    if not p.exists():
        if not download:
            raise FileNotFoundError(
                f"{p} 가 없습니다. {ORLIB_URL} 에서 받아 해당 경로에 두세요."
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(ORLIB_URL, timeout=30) as r:
            p.write_bytes(r.read())
    return parse_all(p.read_text(encoding="utf-8", errors="replace"))


# ─────────────────────────────────────────────────────────────
# 파일을 못 받는 환경을 위한 ft06 내장본 (원문과 동일)
# ─────────────────────────────────────────────────────────────
FT06_TEXT = """\
 +++++++++++++++++++++++++++++

 instance ft06

 +++++++++++++++++++++++++++++
 Fisher and Thompson 6x6 instance, alternate name (mt06)
 6 6
 2  1  0  3  1  6  3  7  5  3  4  6
 1  8  2  5  4 10  5 10  0 10  3  4
 2  5  3  4  5  8  0  9  1  1  4  7
 1  5  0  5  2  5  3  3  4  8  5  9
 2  9  1  3  4  5  5  4  0  3  3  1
 1  3  3  3  5  9  0 10  4  4  2  1
"""


def load_ft06() -> Instance:
    """외부 통신 없이 ft06 만 바로 얻는다."""
    return parse_one(FT06_TEXT, "ft06")


if __name__ == "__main__":
    inst = load_ft06()
    print(inst)
    print("설명:", inst.description)
    for j, ops in enumerate(inst.jobs):
        print(f"  J{j+1} TWK={inst.total_work(j):3d}  " +
              "  ".join(f"M{m}·{d}" for m, d in ops))
