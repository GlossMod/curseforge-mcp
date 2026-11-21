from __future__ import annotations

from typing import Any

import httpx

API_ORIGIN = "https://api.curseforge.com"
PAGE_LIMIT = 10_000
MAX_PAGE_SIZE = 50
JsonObject = dict[str, Any]


class CurseForgeError(Exception):
    """A user-facing error that must not contain credentials or signed CDN URLs."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def page_params(index: int, page_size: int) -> dict[str, int]:
    if not 1 <= page_size <= MAX_PAGE_SIZE or index < 0 or index + page_size > PAGE_LIMIT:
        raise CurseForgeError("分页要求 1 <= page_size <= 50、index >= 0、index + page_size <= 10000。")
    return {"index": index, "pageSize": page_size}


def paginated(payload: JsonObject, index: int, page_size: int) -> JsonObject:
    data = payload["data"]
    if not isinstance(data, list):
        raise CurseForgeError("CurseForge 分页响应的 data 不是列表。")
    pagination = payload.get("pagination")
    total = pagination.get("totalCount") if isinstance(pagination, dict) else None
    next_index = index + len(data)
    more = next_index < total if type(total) is int else len(data) == page_size
    continuation = {
        "nextIndex": next_index,
        "nextPageSize": min(page_size, PAGE_LIMIT - next_index),
    } if data and more and next_index < PAGE_LIMIT else {}
    return {**payload, **continuation}


class CurseForgeClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str | None) -> None:
        self.http = http
        self._api_key = (api_key or "").strip()

    def auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise CurseForgeError("未设置 CURSEFORGE_API_KEY；请在 MCP 服务进程环境中配置获批的 Core API Key。")
        if not self._api_key.isascii() or not all(33 <= ord(ch) <= 126 for ch in self._api_key):
            raise CurseForgeError("CURSEFORGE_API_KEY 包含无效的字符。")
        return {"x-api-key": self._api_key}

    async def request(
        self, path: str, *, params: dict[str, Any] | None = None, body: JsonObject | None = None
    ) -> JsonObject:
        try:
            response = await self.http.request(
                "GET" if body is None else "POST",
                API_ORIGIN + path,
                params={key: value for key, value in (params or {}).items() if value is not None},
                json=body,
                headers={"Accept": "application/json", **self.auth_headers()},
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise CurseForgeError(f"请求 CurseForge API {path} 超时或连接失败。") from None
        status = response.status_code
        if status != 200:
            hint = {
                401: "请检查 CURSEFORGE_API_KEY 是否有效。",
                403: "API Key 无权访问，请核对项目、文件或游戏的访问权限。",
                404: "资源不存在或当前凭据不可见。",
                429: "达到请求限额，请稍后重试。",
            }.get(status, "请核对请求参数或稍后重试。")
            raise CurseForgeError(f"CurseForge API {path} 返回 HTTP {status}。{hint}", status)
        try:
            payload = response.json()
        except ValueError:
            raise CurseForgeError(f"CurseForge API {path} 没有返回有效 JSON。") from None
        if not isinstance(payload, dict) or "data" not in payload:
            raise CurseForgeError(f"CurseForge API {path} 响应缺少 data 字段。")
        return payload

    async def page(
        self, path: str, index: int, page_size: int, **params: Any
    ) -> JsonObject:
        payload = await self.request(path, params={**page_params(index, page_size), **params})
        return paginated(payload, index, page_size)

    async def list_games(
        self, index: int, page_size: int, name: str | None, slug: str | None
    ) -> JsonObject:
        page = await self.page("/v1/games", index, page_size)
        if name is None and slug is None:
            return page
        name_needle = name.strip().casefold() if name is not None else None
        slug_needle = slug.strip().casefold() if slug is not None else None

        def matches(game: JsonObject, field: str, needle: str | None, *, exact: bool) -> bool:
            if needle is None:
                return True
            value = game.get(field)
            if not isinstance(value, str):
                return False
            return value.casefold() == needle if exact else needle in value.casefold()

        return {
            **page,
            "data": [
                game for game in page["data"] if isinstance(game, dict)
                and matches(game, "name", name_needle, exact=False)
                and matches(game, "slug", slug_needle, exact=True)
            ],
        }

    async def get_mod_file(self, file_id: int, mod_id: int | None) -> JsonObject:
        if mod_id is not None:
            payload = await self.request(f"/v1/mods/{mod_id}/files/{file_id}")
        else:
            batch = await self.request("/v1/mods/files", body={"fileIds": [file_id]})
            if not isinstance(batch["data"], list):
                raise CurseForgeError("CurseForge 批量文件响应的 data 不是列表。")
            selected = [
                item for item in batch["data"]
                if isinstance(item, dict) and type(item.get("id")) is int and item["id"] == file_id
            ]
            if not selected:
                raise CurseForgeError(f"未找到 file_id={file_id}，或当前凭据不可见。", 404)
            if len(selected) != 1:
                raise CurseForgeError(f"CurseForge 返回了重复的 file_id={file_id}。")
            payload = {**batch, "data": selected[0]}
        file = payload["data"]
        if not isinstance(file, dict) or type(file.get("id")) is not int or file["id"] != file_id:
            raise CurseForgeError("CurseForge 返回的文件与请求的 file_id 不匹配。")
        if mod_id is not None and "modId" in file and file["modId"] != mod_id:
            raise CurseForgeError("CurseForge 返回的文件不属于请求的 mod_id。")
        return payload

    async def get_download_url(self, mod_id: int, file_id: int) -> JsonObject:
        path = f"/v1/mods/{mod_id}/files/{file_id}/download-url"
        try:
            payload = await self.request(path)
        except CurseForgeError as exc:
            if exc.status in (403, 404):
                raise CurseForgeError(
                    f"{exc} 官方 API 未提供第三方下载地址；如可用，请前往 CurseForge 项目文件页。",
                    exc.status,
                ) from None
            raise
        if not isinstance(payload["data"], str) or not payload["data"].strip():
            raise CurseForgeError("官方 API 未提供第三方下载地址；如可用，请前往 CurseForge 项目文件页。")
        return payload
