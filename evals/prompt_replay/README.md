# 提示词回放集

本目录提供人工构造的提示词和指标计算工具，不包含生产使用统计或真实会话。

- `prompts.jsonl`：合成场景及预期行为。
- `metrics.py`：首字等待时间、工具调用数、进展说明比例和沉默段计算。
- `score.py`：读取测试指标 TSV 与会话映射 JSON，生成检查结果。

使用独立测试账号执行场景，再按 `metrics.py` 文档整理指标行。模型调用可能产生费用，费用取决于配置的服务商。

```bash
python -m evals.prompt_replay.score rows.tsv --map session_map.json
```

可通过 `--baseline baseline.json` 展示自己生成的测试基线。不要把运行结果、会话映射或真实对话提交到源码仓库。答案质量仍需对照 `expect` 人工判断。
