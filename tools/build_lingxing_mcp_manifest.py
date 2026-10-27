#!/usr/bin/env python3
"""把审计过的领星只读目录编译成运行时受信任清单。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "artifacts/lingxing-readonly-api/included.json"
DOCS = ROOT / "artifacts/lingxing-readonly-api/source_docs"
TARGET = ROOT / "audit-gateway/server/mcp_adapters/data/lingxing.json"

DOMAIN_CODES = {
    "BasicData": "basic", "Sale": "sale", "Product": "product",
    "Warehouse": "warehouse", "Finance": "finance", "Statistics": "statistics",
    "FBA": "fba", "Purchase": "purchase", "Advertising": "ads",
    "VC": "vc", "亚马逊源表数据": "amazon_report", "产品": "product",
    "仓库": "warehouse", "基础数据": "basic", "多平台": "multichannel",
    "客服": "customer_service", "工具": "tool", "广告": "ads",
    "物流": "logistics", "目标管理": "target", "统计": "statistics",
    "补货建议": "replenishment", "补货限制": "replenishment_limit",
    "财务": "finance", "采购": "purchase", "销售": "sale",
}

CURATED = {
    "docs/BasicData/SellerLists.md": "lx.basic.seller.list",
    "docs/Sale/Orderlists.md": "lx.sale.order.list",
    "docs/Sale/OrderDetail.md": "lx.sale.order.detail",
    "docs/Product/ProductLists.md": "lx.product.list",
    "docs/Product/ProductDetails.md": "lx.product.detail",
    "docs/Warehouse/FBAStock_v2.md": "lx.warehouse.fba_stock.list",
    "docs/FBA/GetInboundShipmentList.md": "lx.fba.shipment.list",
    "docs/Purchase/PurchaseOrderList.md": "lx.purchase.order.list",
}


def snake(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def schema_for(doc_path: str) -> dict:
    path = DOCS / doc_path.removeprefix("docs/")
    if not path.exists():
        return {"type": "object", "additionalProperties": True}
    text = path.read_text(encoding="utf-8", errors="replace")
    section = re.search(r"## 请求参数(.*?)(?:\n## |\Z)", text, re.S)
    properties: dict[str, dict] = {}
    required: list[str] = []
    if section:
        type_map = {"int": "integer", "number": "number", "array": "array", "object": "object", "bool": "boolean"}
        for raw in section.group(1).splitlines():
            if not raw.startswith("|") or "---" in raw or "参数名" in raw:
                continue
            cells = [re.sub(r"<.*?>", " ", c).strip() for c in raw.strip("|").split("|")]
            if len(cells) < 4 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cells[0]):
                continue
            typ = re.sub(r"[\[\]]", "", cells[3]).lower()
            item: dict = {"type": type_map.get(typ, "string")}
            if cells[1]:
                item["description"] = re.sub(r"\s+", " ", cells[1])[:500]
            properties[cells[0]] = item
            if cells[2] == "是":
                required.append(cells[0])
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def main() -> None:
    records = json.loads(SOURCE.read_text(encoding="utf-8"))
    used: set[str] = set()
    capabilities = []
    for row in records:
        doc_path = row["doc_path"]
        domain = DOMAIN_CODES.get(row["section"], snake(row["section"]))
        if not domain:
            raise ValueError(f"分类缺少稳定英文代号：{row['section']!r}")
        stem = snake(Path(doc_path).stem)
        code = CURATED.get(doc_path, f"lx.{domain}.{stem}")
        if code in used:
            suffix = hashlib.sha256(doc_path.encode()).hexdigest()[:6]
            code = f"{code}.{suffix}"
        used.add(code)
        method_match = re.match(r"(GET|POST)\b", row["method"].upper())
        if not method_match:
            raise ValueError(f"不支持的请求方法 {row['method']!r}: {doc_path}")
        payload = {
            "code": code,
            "name": row["name"],
            "domain": row["section"],
            "method": method_match.group(1),
            "path": row["api_path"],
            "doc_url": row["doc_url"],
            "token_bucket": row["token_bucket"],
            "input_schema": schema_for(doc_path),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["source_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        capabilities.append(payload)
    output = {"version": 1, "adapter": "lingxing", "capabilities": capabilities}
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(capabilities)} capabilities to {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
