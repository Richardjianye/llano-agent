"""回放集的打分口径与数据本身（决策见 docs/design/maintenance-backlog-2026-09-21.md §2.4）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from evals.prompt_replay import score
from evals.prompt_replay.metrics import (
    narration_rate,
    rounds_before_first_text,
    seconds_before_first_text,
    silent_streaks,
    summarize_session,
    tool_calls,
)

ROOT = Path(__file__).resolve().parents[2] / "evals" / "prompt_replay"
T0 = datetime(2026, 9, 20, 14, 8, 55)


def _rows(spec):
    """spec: [(距开始秒数, 文字长度, 累计工具骨架)]"""
    return [
        {"id": index, "created_at": T0 + timedelta(seconds=seconds), "response_chars": chars,
         "tool_skeleton": tools, "latency_ms": 3000, "prompt_tokens": 8000 + index * 1000}
        for index, (seconds, chars, tools) in enumerate(spec)
    ]


def _incident():
    """09-20「更改登录密码」的形状：29 轮里前 28 轮没有文字，44 次工具调用（42 次终端）。"""
    skeleton = [{"name": "skill_view"}] * 2 + [{"name": "terminal"}] * 42
    spec = [(round(i * 18.2), 0, skeleton[: i + 1]) for i in range(28)]
    spec.append((510, 932, skeleton))
    return _rows(spec)


def test_the_incident_reads_the_way_the_logs_do():
    summary = summarize_session(_incident())

    assert summary["rounds"] == 29
    assert summary["rounds_before_first_text"] == 28
    assert summary["seconds_before_first_text"] == 510
    assert summary["tool_calls"] == 44 and summary["terminal_calls"] == 42
    assert summary["longest_silent_rounds"] == 28


def test_a_directly_answered_question_is_zero_rounds_and_no_tools():
    rows = _rows([(0, 400, [])])

    assert rounds_before_first_text(rows) == 0
    assert seconds_before_first_text(rows) == 0
    assert tool_calls(rows) == []
    assert narration_rate(rows) is None  # 没有中间回合，比例无意义


def test_narration_rate_only_counts_mid_rounds():
    # 4 轮：0、1、2 是中间回合，3 是最后一轮；其中 0 和 2 带文字
    rows = _rows([(0, 30, []), (10, 0, []), (20, 25, []), (30, 500, [])])

    assert narration_rate(rows) == 2 / 3


def test_silent_streaks_split_on_text_and_on_long_gaps():
    rows = _rows([(0, 0, []), (20, 0, []), (40, 10, []), (60, 0, []), (400, 0, []), (420, 0, [])])

    assert [s["rounds"] for s in silent_streaks(rows)] == [2, 1, 2]  # 间隔超过 180 秒另起一段


def test_tool_calls_survive_bad_skeleton_data():
    assert tool_calls(_rows([(0, 0, "not json")])) == []
    assert tool_calls([]) == []


def test_the_replay_set_is_well_formed():
    prompts = [json.loads(line) for line in (ROOT / "prompts.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    assert len(prompts) == len({p["id"] for p in prompts})  # id 唯一
    for prompt in prompts:
        assert prompt["prompt"].strip() and prompt["expect"], prompt["id"]
        assert prompt["source"], prompt["id"]
    assert all(p["source"] == "合成" for p in prompts)
    assert {p["category"] for p in prompts} >= {"product-help", "emotion", "boundary", "multi-step", "skill-authoring"}


def test_score_flags_forbidden_tools_and_slow_first_text(tmp_path, capsys):
    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text(
        json.dumps({"id": "help-01", "forbid_tools": ["terminal"], "max_rounds_before_first_text": 1}) + "\n",
        encoding="utf-8",
    )
    rows = tmp_path / "rows.jsonl"
    rows.write_text(
        "\n".join(
            json.dumps({**row, "session_id": "s1", "created_at": row["created_at"].isoformat()})
            for row in _incident()
        ),
        encoding="utf-8",
    )
    mapping = tmp_path / "map.json"
    mapping.write_text(json.dumps({"s1": "help-01"}), encoding="utf-8")

    code = score.main([str(rows), "--prompts", str(prompts), "--map", str(mapping)])

    out = capsys.readouterr().out
    assert code == 1
    assert "用了不该用的工具：terminal" in out
    assert "超过上限 1" in out
