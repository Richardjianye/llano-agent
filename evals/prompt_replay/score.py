"""给一批回放会话打分，可读取自行提供的测试基线。

用法（回放跑完之后；见 README）：

    python -m evals.prompt_replay.score rows.tsv --prompts evals/prompt_replay/prompts.jsonl \\
        --map session_map.json --baseline baseline.json

rows.tsv 是 export_rows.sql 的 `mysql --batch` 输出（也接受 .jsonl）。
session_map.json 把回放的 session_id 对应到 prompts.jsonl 里的 id：{"20260922_101500_ab12cd": "help-01"}。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from evals.prompt_replay.metrics import summarize_session, tool_calls


def load_rows(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        rows = []
        for record in reader:
            record = {k: (None if v == "NULL" else v) for k, v in record.items()}
            rows.append(record)
        return rows


def load_prompts(path: Path) -> dict[str, dict]:
    prompts = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            prompts[item["id"]] = item
    return prompts


def check_expectations(prompt: dict, summary: dict, tools: list[str]) -> list[str]:
    """能自动判的期望：只判可量化的（工具、轮数）。答案质量、语气仍然要人看。"""
    problems = []
    forbidden = set(prompt.get("forbid_tools") or [])
    used = sorted(forbidden & set(tools))
    if used:
        problems.append(f"用了不该用的工具：{', '.join(used)}")
    limit = prompt.get("max_rounds_before_first_text")
    first = summary["rounds_before_first_text"]
    if limit is not None and (first is None or first > limit):
        problems.append(f"首个文字出现在第 {first} 轮之后，超过上限 {limit}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rows", type=Path)
    parser.add_argument("--prompts", type=Path, default=Path(__file__).with_name("prompts.jsonl"))
    parser.add_argument("--map", type=Path, required=True, help="session_id -> prompt id 的 JSON")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)

    prompts = load_prompts(args.prompts)
    mapping = json.loads(args.map.read_text(encoding="utf-8"))
    by_session: dict[str, list[dict]] = defaultdict(list)
    for row in load_rows(args.rows):
        by_session[row["session_id"]].append(row)

    print(f"{'id':<14}{'轮数':>5}{'首文字轮':>9}{'首文字秒':>9}{'工具':>5}{'终端':>5}{'边做边说':>9}{'最长沉默轮':>10}  自动检查")
    failed = 0
    for session_id, prompt_id in mapping.items():
        prompt = prompts.get(prompt_id)
        rows = by_session.get(session_id)
        if not prompt or not rows:
            print(f"{prompt_id:<14}  （没有找到 {'提示词' if not prompt else '会话'} {session_id}）")
            failed += 1
            continue
        summary = summarize_session(rows)
        problems = check_expectations(prompt, summary, tool_calls(rows))
        failed += bool(problems)
        rate = summary["narration_rate"]
        first_seconds = summary["seconds_before_first_text"]
        first_seconds_text = f"{first_seconds:.0f}" if first_seconds is not None else "-"
        rate_text = f"{rate:.0%}" if rate is not None else "-"
        print(
            f"{prompt_id:<14}{summary['rounds']:>5}{str(summary['rounds_before_first_text']):>9}"
            f"{first_seconds_text:>9}{summary['tool_calls']:>5}{summary['terminal_calls']:>5}"
            f"{rate_text:>9}{summary['longest_silent_rounds']:>10}  "
            f"{'；'.join(problems) or '通过'}"
        )
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        print("\n测试基线：" + json.dumps(baseline, ensure_ascii=False))
    print("\n答案质量、语气、是否夸得真实，仍然要人对着 prompts.jsonl 的 expect 逐条看。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
