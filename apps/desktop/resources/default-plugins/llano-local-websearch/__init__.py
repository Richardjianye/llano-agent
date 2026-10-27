from agent.web_search_provider import WebSearchProvider


class LocalWebSearchProvider(WebSearchProvider):
    @property
    def name(self):
        return "local-open-websearch"

    @property
    def display_name(self):
        return "本地网页搜索"

    def is_available(self):
        from hermes_cli.config import load_config
        disabled = (load_config().get("plugins") or {}).get("disabled") or []
        if "llano-local-websearch" in disabled:
            return False
        from tools.mcp_tool import _load_mcp_config, _parse_boolish
        cfg = _load_mcp_config().get("llano_websearch", {})
        return bool(cfg.get("command") and not cfg.get("url") and
                    _parse_boolish(cfg.get("enabled", True), default=True))

    def supports_extract(self):
        return True

    def search(self, query, limit=5):
        return _search(self, query, limit)

    def extract(self, urls, **kwargs):
        return _extract(self, urls, **kwargs)


def register(ctx):
    ctx.register_web_search_provider(LocalWebSearchProvider())


def _call(tool, arguments):
    import json
    from tools.mcp_tool import call_provider_mcp_tool, _load_mcp_config
    config = _load_mcp_config().get("llano_websearch") or {}
    env = config.get("env") or {}
    if config.get("url") or not config.get("command") or env.get("SEARCH_MODE") != "request" or env.get("MODE") != "stdio":
        raise RuntimeError("本地网页服务需要 stdio 和纯 HTTP 请求模式，请检查服务器配置")
    raw = call_provider_mcp_tool("llano_websearch", tool, arguments)
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("本地网页服务返回了无效结果") from exc
    if not isinstance(value, dict) or value.get("error"):
        raise RuntimeError(str(value.get("error", "本地网页服务返回了无效结果")) if isinstance(value, dict) else "本地网页服务返回了无效结果")
    payload = value.get("result", value)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise RuntimeError("本地网页服务返回了无效结果") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("本地网页服务返回了无效结果")
    return payload


def _allowed(url):
    from tools.url_safety import is_safe_url
    from tools.website_policy import check_website_access
    if not is_safe_url(url):
        return "已拒绝访问非公开或不安全的网址"
    blocked = check_website_access(url)
    return blocked["message"] if blocked else None


def _search(self, query, limit=5):
    try:
        if not self.is_available():
            raise RuntimeError("本地网页服务未配置或已关闭")
        limit = max(1, min(5, int(limit)))
        error = _allowed("https://www.bing.com/search")
        if error:
            raise RuntimeError(error)
        data = _call("search", {"query": query, "limit": limit, "engines": ["bing"]})
        if not isinstance(data.get("results"), list):
            raise RuntimeError("本地搜索服务未返回结果列表")
        if data.get("partialFailures") and not data["results"]:
            raise RuntimeError("；".join(str(x.get("message", "搜索失败")) for x in data["partialFailures"]))
        results = []
        for entry in data["results"][:limit]:
            url = entry.get("url", "")
            if not url or _allowed(url):
                continue
            results.append({"title": str(entry.get("title", "")), "url": url,
                            "description": str(entry.get("description", "")), "position": len(results) + 1})
        return {"success": True, "data": {"web": results}}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _extract(self, urls, **kwargs):
    results = []
    for url in urls:
        try:
            error = _allowed(url)
            if error:
                raise RuntimeError(error)
            if not self.is_available():
                raise RuntimeError("本地网页服务未配置或已关闭")
            data = _call("fetchWebContent", {"url": url, "renderMode": "request", "readability": True,
                         "includeLinks": False, "maxChars": max(1000, min(30000, int(kwargs.get("char_limit") or 15000)))})
            final_url = data.get("finalUrl") or url
            error = _allowed(final_url)
            if error:
                raise RuntimeError(error)
            if not isinstance(data.get("content"), str) or not data["content"].strip():
                raise RuntimeError("网页没有可读取的正文")
            results.append({"url": url, "title": data.get("title", ""), "content": data["content"],
                            "raw_content": data["content"], "metadata": {"source": self.name, "final_url": final_url,
                            "retrieval_method": data.get("retrievalMethod"), "truncated": data.get("truncated", False)}})
        except Exception as exc:
            results.append({"url": url, "title": "", "content": "", "error": str(exc)})
    return results

