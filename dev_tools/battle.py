#!/usr/bin/env python3
"""공식 엔진으로 연세(Y) 봇과 고려(K) 봇을 대전시킨다.

    python3 dev_tools/battle.py codex lv2
    python3 dev_tools/battle.py codex lv2 --seeds 0-9 --swap
    python3 dev_tools/battle.py out/py_sample out/codex_bot/koyeon-bot.zip --replay out/replays
    python3 dev_tools/battle.py --list

봇은 별칭(--list 참고), .py 파일, .cpp 파일, submission.json이 있는 폴더,
제출 ZIP 중 하나로 지정한다. C++는 C++20으로 컴파일해 dev_tools/.build에 캐시한다.
대회와 같은 시간 제한(첫 턴 3000ms, 이후 300ms)과 몰수 규칙을 적용하고,
엔진이 조용히 무시하는 잘못된 명령 줄도 공식 검사 도구(run_tests.CheckedBot)로 찾아 보여준다.
서버의 메모리·CPU 격리는 재현하지 않는다.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

DEV_TOOLS = Path(__file__).resolve().parent
ROOT = DEV_TOOLS.parent
STARTER = DEV_TOOLS / "bots" / "dist" / "starter"
BUILD = DEV_TOOLS / ".build"
sys.path[:0] = [str(STARTER), str(DEV_TOOLS)]

from _support import first_turn_timeout_ms, turn_timeout_ms  # noqa: E402
from engine.config import load_config  # noqa: E402
from run_tests import CheckedBot  # noqa: E402
from runner.match import run_match  # noqa: E402
from submission import extract_zip, inspect_zip  # noqa: E402

ALIASES = {
    "codex": (ROOT / "out" / "codex_bot", "Codex 봇 (제출본)"),
    "py": (ROOT / "out" / "py_sample" / "main.py", "Python 샘플 main.py"),
    "cpp": (ROOT / "out" / "cpp_sample", "C++ 샘플 main.cpp"),
    "lv1": (STARTER / "python" / "example_lv1.py", "공식 예제 lv1 (Python)"),
    "lv2": (STARTER / "python" / "example_lv2.py", "공식 예제 lv2 (Python)"),
    "lv1-cpp": (STARTER / "cpp" / "example_lv1.cpp", "공식 예제 lv1 (C++)"),
    "lv2-cpp": (STARTER / "cpp" / "example_lv2.cpp", "공식 예제 lv2 (C++)"),
}
TEAM_KO = {"Y": "연세", "K": "고려"}


class BotError(Exception):
    pass


@dataclass(frozen=True)
class Bot:
    label: str
    command: str


class TimedBot(CheckedBot):
    """첫 턴(기동 포함, 3000ms)과 이후 턴(300ms)의 최대 응답 시간을 따로 잰다."""

    first_turn_ms = 0.0

    def collect_turn(self):
        result = super().collect_turn()
        if self.turn == 1:
            self.first_turn_ms, self.max_turn_ms = self.max_turn_ms, 0.0
        return result


def compile_cpp(sources, include_dir, compiler):
    sources = sorted(Path(s).resolve() for s in sources)
    digest = hashlib.sha256(compiler.encode())
    for path in sources + sorted(Path(include_dir).glob("*.h*")):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    binary = BUILD / digest.hexdigest()[:16] / "bot"
    if not binary.exists():
        binary.parent.mkdir(parents=True, exist_ok=True)
        argv = [compiler, "-std=c++20", "-O2", "-I", str(include_dir),
                *map(str, sources), "-o", str(binary)]
        done = subprocess.run(argv, capture_output=True, text=True)
        if done.returncode:
            raise BotError("C++ 컴파일 실패:\n" + done.stderr.strip())
    return binary


def folder_bot(folder, label, compiler):
    meta = folder / "submission.json"
    language = json.loads(meta.read_text(encoding="utf-8")).get("language") if meta.exists() else None
    if language == "cpp" or (language is None and (folder / "main.cpp").exists()):
        sources = sorted(folder.rglob("*.cpp"))
        if not sources:
            raise BotError(f"{folder}: .cpp 파일이 없습니다")
        return Bot(label, "exec " + shlex.quote(str(compile_cpp(sources, folder, compiler))))
    if (folder / "main.py").exists():
        return Bot(label, "exec " + shlex.join([sys.executable, str(folder / "main.py")]))
    raise BotError(f"{folder}: main.py 또는 main.cpp가 없습니다")


def resolve(spec, compiler, workdir):
    """별칭·경로를 실행 명령으로 바꾼다."""
    if spec in ALIASES:
        path, label = ALIASES[spec][0], spec
    else:
        path = Path(spec).expanduser()
        if not path.exists():
            raise BotError(f"'{spec}'을(를) 찾을 수 없습니다. 별칭은 --list로 확인하세요")
        path = path.resolve()
        label = path.name if path.is_dir() else f"{path.parent.name}/{path.name}"
    path = path.resolve()
    if path.is_dir():
        return folder_bot(path, label, compiler)
    if path.suffix == ".zip":
        report = inspect_zip(path)
        if not report["ok"]:
            raise BotError(f"{path.name}: 제출 ZIP 검사 실패\n  " + "\n  ".join(report["issues"]))
        target = Path(tempfile.mkdtemp(prefix="zip-", dir=workdir))
        extract_zip(path, target)
        return folder_bot(target, label, compiler)
    if path.suffix == ".py":
        return Bot(label, "exec " + shlex.join([sys.executable, str(path)]))
    if path.suffix == ".cpp":
        return Bot(label, "exec " + shlex.quote(str(compile_cpp([path], path.parent, compiler))))
    raise BotError(f"{spec}: .py, .cpp, 폴더, .zip 중 하나로 지정하세요")


def parse_seeds(tokens):
    seeds = []
    for token in tokens:
        if "-" in token.strip("-"):
            start, end = token.split("-", 1)
            seeds.extend(range(int(start), int(end) + 1))
        else:
            seeds.append(int(token))
    return list(dict.fromkeys(seeds))


def play(job):
    """경기 한 판. 병렬 실행을 위해 직렬화 가능한 값만 주고받는다."""
    seed, bot_y, bot_k, max_turns, replay_path = job
    config = load_config()
    config["first_turn_timeout_ms"] = first_turn_timeout_ms()
    bots = {}
    try:
        bots["Y"] = TimedBot(bot_y.command, config)
        bots["K"] = TimedBot(bot_k.command, config)
        replay, result = run_match(seed, config, bots["Y"], bots["K"],
                                   turn_timeout_ms=turn_timeout_ms(), max_turns=max_turns,
                                   names={"Y": bot_y.label, "K": bot_k.label})
    finally:
        for bot in bots.values():
            bot.close()
    if replay_path:
        replay_path.parent.mkdir(parents=True, exist_ok=True)
        replay_path.write_text(json.dumps(replay, ensure_ascii=False), encoding="utf-8")
    diagnostics = {}
    for team, bot in bots.items():
        usage = bot.usage.snapshot()
        diagnostics[team] = {"issues": bot.issues, "warnings": usage["warnings"],
                             "first_turn_ms": round(bot.first_turn_ms, 2),
                             "max_response_ms": round(bot.max_turn_ms, 2)}
    return {"seed": seed, "Y": bot_y.label, "K": bot_k.label, "result": result,
            "diagnostics": diagnostics, "replay": str(replay_path) if replay_path else None}


def describe(record):
    r = record["result"]
    y, k = r["score"]["Y"], r["score"]["K"]
    if r["winner"] == "DRAW":
        head = "무승부"
    else:
        head = f"{TEAM_KO[r['winner']]}({record[r['winner']]}) 승"
    how = {"instant": f"{r['turns']}턴 즉시 승리", "score": "160턴 점수 판정",
           "draw": "160턴 완전 동률"}.get(r["reason"], r["reason"])
    if r["reason"] == "forfeit":
        loser = r["forfeit"]["team"]
        how = f"{r['turns']}턴 {TEAM_KO.get(loser, loser)} 몰수패 ({r['forfeit']['cause']})"
    elif r["reason"] == "score" and r["turns"] != 160:
        how = f"{r['turns']}턴 점수 판정"
    d = record["diagnostics"]
    timing = (f"응답 최대 Y {d['Y']['max_response_ms']:.0f}ms / K {d['K']['max_response_ms']:.0f}ms"
              f" (첫 턴 {d['Y']['first_turn_ms']:.0f} / {d['K']['first_turn_ms']:.0f}ms)")
    return f"{head:<16} {y:>2}:{k:<2}  {how}  · {timing}"


def print_issues(record, limit=3):
    for team in ("Y", "K"):
        d = record["diagnostics"][team]
        for issue in d["issues"][:limit]:
            print(f"      ! {TEAM_KO[team]}({record[team]}) {issue['turn']}턴 "
                  f"'{issue['line']}': {issue['issue']}")
        if len(d["issues"]) > limit:
            print(f"      ! {TEAM_KO[team]}({record[team]}) 외 {len(d['issues']) - limit}건")
        for warning in d["warnings"]:
            print(f"      ! {TEAM_KO[team]}({record[team]}) 출력 경고: {warning}")


def summarize(records, first, second):
    """진영과 관계없이 명령줄에 적은 두 봇 기준으로 합산한다."""
    table = {first: {"W": 0, "D": 0, "L": 0, "margin": 0}, second: {"W": 0, "D": 0, "L": 0, "margin": 0}}
    if first == second:
        return None
    for rec in records:
        r = rec["result"]
        for team, other in (("Y", "K"), ("K", "Y")):
            row = table[rec[team]]
            row["margin"] += r["score"][team] - r["score"][other]
            row["W" if r["winner"] == team else "D" if r["winner"] == "DRAW" else "L"] += 1
    return table


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bot_y", nargs="?", help="연세(Y) 봇: 별칭 또는 경로")
    parser.add_argument("bot_k", nargs="?", help="고려(K) 봇: 별칭 또는 경로")
    parser.add_argument("--seeds", nargs="+", default=["0"], metavar="S",
                        help="맵 시드. 여러 개나 범위 가능 (예: 0 7 42, 0-9). 기본 0")
    parser.add_argument("--swap", action="store_true",
                        help="시드마다 진영을 바꾼 경기도 추가 (공정한 비교용)")
    parser.add_argument("--replay", type=Path, metavar="DIR",
                        help="리플레이 JSON을 저장할 폴더")
    parser.add_argument("--json", type=Path, metavar="FILE", help="전체 결과를 JSON으로 저장")
    parser.add_argument("--max-turns", type=int, help="테스트용 턴 상한 (기본: 공식 160턴)")
    parser.add_argument("-j", "--jobs", type=int, default=1,
                        help="동시에 돌릴 경기 수. 늘리면 빠르지만 응답 시간이 부풀 수 있음 (기본 1)")
    parser.add_argument("--compiler", default="c++", help="C++20 컴파일러 (기본 c++)")
    parser.add_argument("--list", action="store_true", help="봇 별칭 목록")
    args = parser.parse_args(argv)

    if args.list:
        print("봇 별칭 (경로 대신 쓸 수 있음)")
        for name, (path, label) in ALIASES.items():
            print(f"  {name:<8} {label:<20} {path.relative_to(ROOT)}")
        print("\n그 외: .py 파일, .cpp 파일, submission.json이 있는 폴더, 제출 .zip")
        return 0
    if not (args.bot_y and args.bot_k):
        parser.error("연세 봇과 고려 봇을 순서대로 지정하세요. 예: battle.py codex lv2")
    if args.max_turns is not None and args.max_turns <= 0:
        parser.error("--max-turns는 양의 정수여야 합니다")
    try:
        seeds = parse_seeds(args.seeds)
    except ValueError:
        parser.error("--seeds는 정수 또는 '시작-끝' 범위로 적으세요")

    with tempfile.TemporaryDirectory(prefix="battle-") as workdir:
        try:
            first = resolve(args.bot_y, args.compiler, workdir)
            second = resolve(args.bot_k, args.compiler, workdir)
        except BotError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 2
        if first.label == second.label and args.bot_y != args.bot_k:
            second = Bot(second.label + "#2", second.command)
        jobs = []
        for seed in seeds:
            pairs = [(first, second)] + ([(second, first)] if args.swap else [])
            for y, k in pairs:
                path = None
                if args.replay:
                    safe = lambda s: "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
                    path = args.replay / f"s{seed}_{safe(y.label)}-vs-{safe(k.label)}.json"
                jobs.append((seed, y, k, args.max_turns, path))

        print(f"공식 엔진 대전 {len(jobs)}경기 · 시간 제한 첫 턴 {first_turn_timeout_ms()}ms, "
              f"이후 {turn_timeout_ms()}ms")
        records = []
        width = len(str(len(jobs)))

        def report(i, record):
            print(f"[{i:>{width}}/{len(jobs)}] 시드 {record['seed']:<4} "
                  f"연세 {record['Y']} vs 고려 {record['K']}")
            print(f"    → {describe(record)}", flush=True)
            print_issues(record)

        if args.jobs > 1:
            with ProcessPoolExecutor(max_workers=args.jobs) as pool:
                for i, record in enumerate(pool.map(play, jobs), 1):
                    records.append(record)
                    report(i, record)
        else:
            for i, job in enumerate(jobs, 1):
                record = play(job)
                records.append(record)
                report(i, record)

    table = summarize(records, first.label, second.label)
    if len(records) > 1 and table:
        print(f"\n요약 ({len(records)}경기, 진영 합산)")
        for label, row in table.items():
            print(f"  {label:<14} {row['W']}승 {row['D']}무 {row['L']}패 · "
                  f"평균 점수차 {row['margin'] / len(records):+.1f}")
    problems = sum(len(r["diagnostics"][t]["issues"]) + len(r["diagnostics"][t]["warnings"])
                   for r in records for t in ("Y", "K"))
    forfeits = sum(r["result"]["reason"] == "forfeit" for r in records)
    slowest = max(r["diagnostics"][t]["max_response_ms"] for r in records for t in ("Y", "K"))
    slowest_first = max(r["diagnostics"][t]["first_turn_ms"] for r in records for t in ("Y", "K"))
    print(f"\n진단: 몰수 {forfeits}경기 · 명령/출력 문제 {problems}건 · "
          f"최대 응답 {slowest:.0f}ms (제한 {turn_timeout_ms()}) · "
          f"첫 턴 최대 {slowest_first:.0f}ms (제한 {first_turn_timeout_ms()})")
    if args.replay:
        print(f"리플레이: {args.replay.resolve()}")
        print("  → site/index.html 리플레이 섹션의 '폴더 열기'로 이 폴더를 고르면 재생할 수 있습니다")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"summary": table, "matches": records},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"JSON: {args.json.resolve()}")
    return 1 if forfeits else 0


if __name__ == "__main__":
    raise SystemExit(main())
