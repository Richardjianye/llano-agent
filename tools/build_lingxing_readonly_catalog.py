#!/usr/bin/env python3
"""Build an auditable read-only Lingxing OpenAPI catalog from its docs site."""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path


BASE_URL = "https://apidoc.lingxing.com/"
SIDEBAR_URL = BASE_URL + "_sidebar.md"
OUTPUT_DIR = Path("artifacts/lingxing-readonly-api")


MUTATING_TERMS = (
    "创建", "新增", "添加", "编辑", "修改", "更新", "删除", "作废", "取消", "提交",
    "上传", "导入", "同步", "生成", "保存", "确认", "分配", "释放", "锁库存", "发货",
    "下单", "设置", "启用", "禁用", "标记", "解除", "配对", "审核", "暂存", "续期",
    "续约", "调整", "合并", "拆分", "称重", "出库", "拣货", "解密", "请求标签",
    "撤销", "收货", "入库", "到货", "结束", "归档",
)
READ_TERMS = (
    "查询", "获取", "列表", "详情", "报表", "统计", "分析", "日志", "信息", "库存",
    "订单", "费用", "账单", "利润", "销量", "流量", "排名", "结果", "状态", "下载附件",
)
TASK_SIDE_EFFECT_TERMS = ("创建导出任务", "生成报告", "手动同步", "同步", "刷新", "抓取")
NON_TOOL_SECTIONS = {"接入指引", "常见场景", "授权"}


@dataclass
class Endpoint:
    section: str
    name: str
    doc_path: str
    doc_url: str
    api_path: str
    method: str
    token_bucket: str
    disposition: str
    confidence: str
    reason: str


def fetch(url: str) -> str:
    # Use the system trust store through curl. Some python.org macOS builds do
    # not inherit it, while curl validates the same HTTPS endpoint correctly.
    result = subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--max-time", "30", url],
        check=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.decode("utf-8", errors="replace")


def parse_sidebar(text: str) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    section = "未分类"
    link_re = re.compile(r"^\s*\*\s+\[([^]]+)\]\(([^)]+)\)\s*$")
    top_re = re.compile(r"^\*\s+([^\[].*)$")
    for raw in text.splitlines():
        top = top_re.match(raw)
        if top:
            section = top.group(1).strip()
            continue
        match = link_re.match(raw)
        if not match:
            continue
        name, path = match.groups()
        normalized = path.strip().replace("\\", "/").lstrip("/")
        if not normalized.startswith("docs/"):
            continue
        if not normalized.lower().endswith(".md"):
            normalized += ".md"
        entries.append((section, name.strip(), normalized))
    return entries


def is_non_endpoint_page(doc_path: str, name: str) -> bool:
    path = doc_path.lower()
    return any(token in path for token in (
        "apiupdatelog", "testtoken", "testsign", "testrequest", "/guidance/", "/case/",
        "/authorization/",
    )) or name.endswith("流程说明")


def parse_api_info(markdown: str) -> tuple[str, str, str]:
    rows = []
    in_info = False
    for line in markdown.splitlines():
        if re.match(r"^##\s+接口信息", line):
            in_info = True
            continue
        if in_info and line.startswith("## "):
            break
        if in_info and line.lstrip().startswith("|"):
            cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
            if cells and not all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells):
                rows.append(cells)
    if len(rows) < 2:
        # A few pages omit the "接口信息" heading but keep the canonical table.
        table_match = re.search(
            r"\|\s*API Path\s*\|[^\n]*请求方式[^\n]*\|\s*\n"
            r"\|[^\n]*\|\s*\n"
            r"\|\s*`?([^|`]+)`?\s*\|[^|]*\|\s*(GET|POST|PUT|DELETE|PATCH)\s*\|([^\n|]*)",
            markdown,
            re.I,
        )
        if table_match:
            return table_match.group(1).strip(), table_match.group(2).upper(), table_match.group(3).strip()
        return "", "", ""
    headers = rows[0]
    data = rows[1]
    mapping = dict(zip(headers, data))
    path = next((v for k, v in mapping.items() if "API Path" in k or "请求地址" in k or "接口地址" in k), "")
    method = next((v for k, v in mapping.items() if "请求方式" in k), "")
    bucket = next((v for k, v in mapping.items() if "令牌桶" in k), "")
    if not path:
        match = re.search(r"`(/(?:erp|pb|bd|api)/[^`\s|]+)`", markdown)
        path = match.group(1) if match else ""
    if not method:
        match = re.search(r"请求方式[^\n]*(GET|POST|PUT|DELETE|PATCH)", markdown, re.I)
        method = match.group(1).upper() if match else ""
    return path, method.upper(), bucket


def classify(section: str, name: str, api_path: str, markdown: str) -> tuple[str, str, str]:
    compact = re.sub(r"\s+", "", name)
    if section in NON_TOOL_SECTIONS:
        return "excluded", "high", "文档/鉴权基础设施，不作为业务 MCP 工具"
    if any(term in compact for term in TASK_SIDE_EFFECT_TERMS):
        return "excluded", "high", "会创建任务、同步或刷新外部/ERP状态"
    if any(term in compact for term in MUTATING_TERMS):
        # Explicitly retain genuine read-only result/status lookups whose names mention a prior action.
        if compact.startswith(("查询", "获取")) and any(term in compact for term in ("结果", "状态", "列表", "详情")):
            return "included", "high", "查询既有任务/对象，不触发状态变更"
        return "excluded", "high", "名称包含明确写操作语义"
    if compact.startswith(("查询", "获取")):
        return "included", "high", "明确查询/获取语义"
    if section == "广告" and "/newad/" in api_path.lower():
        return "included", "medium", "广告基础数据/报表读取接口，且无写操作语义"
    if section in {"多平台", "财务"} and any(term in compact for term in ("报告", "明细", "账务", "回款", "结算", "服务费", "面单费")):
        return "included", "medium", "财务/报告明细读取语义"
    if compact in {"下载附件", "下载定制化附件"}:
        return "included", "high", "下载既有附件"
    if any(term in compact for term in READ_TERMS):
        return "included", "medium", "名称呈现读取语义，无明确写操作词"
    lower_path = api_path.lower()
    if any(token in lower_path for token in ("/list", "/page", "/query", "/search", "/detail", "/get")):
        return "review", "low", "路径疑似查询，但名称缺少明确只读语义"
    # HTTP method cannot settle this API: many reads are POST and some GETs may trigger work.
    return "review", "low", "文档名称与路径均不足以证明无副作用"


def build_one(item: tuple[str, str, str]) -> tuple[Endpoint, str]:
    section, name, doc_path = item
    url = BASE_URL + doc_path
    markdown = fetch(url)
    api_path, method, bucket = parse_api_info(markdown)
    if is_non_endpoint_page(doc_path, name):
        disposition, confidence, reason = "excluded", "high", "说明/测试/鉴权页面，不作为业务 MCP 工具"
    else:
        disposition, confidence, reason = classify(section, name, api_path, markdown)
    if not api_path or not method:
        if disposition == "included":
            disposition, confidence, reason = "review", "low", "未能从文档解析出完整 API Path/请求方式"
    return Endpoint(section, name, doc_path, url, api_path, method, bucket,
                    disposition, confidence, reason), markdown


def write_csv(path: Path, endpoints: list[Endpoint]) -> None:
    fields = list(Endpoint.__annotations__)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(endpoint) for endpoint in endpoints)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sidebar = fetch(SIDEBAR_URL)
    entries = parse_sidebar(sidebar)
    endpoints: list[Endpoint] = []
    failures: list[dict[str, str]] = []
    docs_dir = OUTPUT_DIR / "source_docs"
    docs_dir.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(build_one, item): item for item in entries}
        for future in as_completed(futures):
            section, name, doc_path = futures[future]
            try:
                endpoint, markdown = future.result()
                endpoints.append(endpoint)
                local = docs_dir / doc_path.removeprefix("docs/")
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_text(markdown, encoding="utf-8")
            except (OSError, subprocess.CalledProcessError) as exc:
                failures.append({"section": section, "name": name, "doc_path": doc_path, "error": str(exc)})
    endpoints.sort(key=lambda x: (x.section, x.name, x.doc_path))
    for disposition in ("included", "excluded", "review"):
        subset = [e for e in endpoints if e.disposition == disposition]
        write_csv(OUTPUT_DIR / f"{disposition}.csv", subset)
        (OUTPUT_DIR / f"{disposition}.json").write_text(
            json.dumps([asdict(e) for e in subset], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (OUTPUT_DIR / "all_endpoints.json").write_text(
        json.dumps([asdict(e) for e in endpoints], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    whitelist = {
        "version": 1,
        "default_action": "deny",
        "matching_rule": "exact_method_and_path",
        "endpoints": [
            {
                "method": e.method,
                "path": e.api_path,
                "name": e.name,
                "domain": e.section,
                "doc_url": e.doc_url,
                "token_bucket": e.token_bucket,
            }
            for e in endpoints if e.disposition == "included"
        ],
    }
    (OUTPUT_DIR / "mcp_whitelist.json").write_text(
        json.dumps(whitelist, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUTPUT_DIR / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "source": SIDEBAR_URL,
        "total_sidebar_entries": len(entries),
        "parsed": len(endpoints),
        "included": sum(e.disposition == "included" for e in endpoints),
        "excluded": sum(e.disposition == "excluded" for e in endpoints),
        "review": sum(e.disposition == "review" for e in endpoints),
        "failures": len(failures),
        "classification_note": "Conservative semantic classification; HTTP method alone is not treated as proof of read-only behavior.",
    }
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    readme = f"""# 领星 OpenAPI 只读接口目录

生成日期：2026-08-12

## 结论

- 文档目录共 {summary['total_sidebar_entries']} 项。
- 可纳入只读 MCP 白名单的业务接口：{summary['included']} 个。
- 明确排除的写操作、鉴权、说明与测试页面：{summary['excluded']} 项。
- 待人工复核：{summary['review']} 项。
- 文档抓取失败：{summary['failures']} 项。

本目录采用保守的“默认拒绝”口径。领星大量查询接口使用 `POST`，因此 HTTP
方法不能证明接口是否只读；分类以接口名称、业务语义、路径和文档详情共同判断。

## 文件

- `mcp_whitelist.json`：MCP/API 网关可消费的精确 `method + path` 白名单。
- `included.csv` / `included.json`：纳入接口及分类依据。
- `excluded.csv` / `excluded.json`：排除接口及原因，供审计。
- `review.csv` / `review.json`：需要人工复核的边界项。
- `all_endpoints.json`：全部文档项。
- `summary.json`：数量摘要。
- `failures.json`：文档获取失败项。
- `source_docs/`：本次判定所用的原始 Markdown 快照。

## 网关实施要求

1. 网关必须 `default deny`，只允许 `mcp_whitelist.json` 中完全匹配的请求方法和路径。
2. 不允许 MCP 客户端传入任意 URL、Path、HTTP Method 或自定义 Host。
3. App Secret 只保存在网关的密钥存储中；模型和 MCP Tool 参数均不得看到它。
4. Access Token 的获取/续约属于网关内部鉴权，不注册成模型可调用的 Tool。
5. 对每个角色再做领域、店铺 `sid`、数据时间范围和敏感字段裁剪；“接口只读”不等于“数据可见”。
6. 对订单、财务、客服等接口执行字段级脱敏，设置分页上限、时间窗上限、速率限制和审计日志。
7. 每次领星文档更新后重新生成并审阅差异；新增接口保持拒绝，直到明确批准。

## 特别排除的边界类型

- 创建导出任务、下载链接续期、手动同步、刷新数据。
- 生成/打印标签、上传附件、请求标签。
- 归档广告对象、修改价格/库存/备注、配对、发货、收货、入库和作废。
- Token 获取/续约、在线请求测试及接入说明页面。

`下载附件`与`下载定制化附件`只读取既有文件，纳入只读清单；部署时仍应限制文件大小、内容类型和访问日志。
"""
    (OUTPUT_DIR / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
