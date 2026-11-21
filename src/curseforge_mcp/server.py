from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .client import CurseForgeClient, CurseForgeError, JsonObject
from .download import Downloader

Id = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
Index = Annotated[int, Field(strict=True, ge=0, lt=10_000)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=50)]
Text = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
Loader = Annotated[int, Field(strict=True, ge=0, le=6, description="0=Any, 1=Forge, 2=Cauldron, 3=LiteLoader, 4=Fabric, 5=Quilt, 6=NeoForge")]
SortField = Annotated[int, Field(strict=True, ge=1, le=12, description="CurseForge ModsSearchSortField (1=Featured, 3=LastUpdated, 6=TotalDownloads)")]


def create_server(client: CurseForgeClient, download_dir: Path) -> FastMCP:
    server = FastMCP("CurseForge", instructions=(
        "使用 CurseForge Core API 查游戏、分类和 Mod。选定 file_id 后查询该文件的依赖；"
        "只有 download_file 会将文件保存到本地。列表的 nextIndex 是站点偏移量，并非页码。"
    ))
    downloader = Downloader(client, download_dir)

    @server.tool()
    async def list_games(
        index: Index = 0, page_size: PageSize = 50, name: Text | None = None, slug: Text | None = None
    ) -> JsonObject:
        """列出游戏。对当前上游页按名称（子串）或 slug（精确）忽略大小写筛选；空页仍可有 nextIndex。"""
        return await client.list_games(index, page_size, name, slug)

    @server.tool()
    async def get_game(game_id: Id) -> JsonObject:
        """取得游戏资料及其图标、封面等官方字段。"""
        return await client.request(f"/v1/games/{game_id}")

    @server.tool()
    async def list_categories(
        game_id: Id, class_id: Id | None = None, classes_only: bool | None = None
    ) -> JsonObject:
        """列出游戏的 CurseForge 分类，可按大类或仅列大类筛选。"""
        return await client.request("/v1/categories", params={
            "gameId": game_id, "classId": class_id, "classesOnly": classes_only,
        })

    @server.tool()
    async def get_game_versions(game_id: Id) -> JsonObject:
        """按 CurseForge 的版本类型分组取得游戏版本，便于 Mod 和文件搜索。"""
        return await client.request(f"/v1/games/{game_id}/versions")

    @server.tool()
    async def search_mods(
        game_id: Id, keyword: Text | None = None, category_id: Id | None = None,
        class_id: Id | None = None, game_version: Text | None = None, mod_loader_type: Loader | None = None,
        index: Index = 0, page_size: PageSize = 25, slug: Text | None = None,
        sort_field: SortField | None = None, sort_order: Literal["asc", "desc"] | None = None,
        game_version_type_id: Id | None = None,
    ) -> JsonObject:
        """按游戏、关键词、分类、大类、游戏版本、加载器或 slug 查询 Mod。搜索加载器时须同时传游戏版本。"""
        if mod_loader_type is not None and game_version is None:
            raise CurseForgeError("搜索 Mod 时，mod_loader_type 必须与 game_version 一起使用。")
        return await client.page(
            "/v1/mods/search", index, page_size, gameId=game_id, searchFilter=keyword,
            classId=class_id, categoryId=category_id, gameVersion=game_version,
            modLoaderType=mod_loader_type, slug=slug, sortField=sort_field, sortOrder=sort_order,
            gameVersionTypeId=game_version_type_id,
        )

    async def mod_description(mod_id: int) -> JsonObject:
        description = await client.request(f"/v1/mods/{mod_id}/description")
        if not isinstance(description["data"], str):
            raise CurseForgeError("CurseForge 正文接口未返回 HTML 字符串。")
        return description

    @server.tool()
    async def get_mod(mod_id: Id) -> JsonObject:
        """返回 Mod 的完整官方资料（作者、首页 links、封面 logo、截图 screenshots 等），并补充 descriptionHtml 正文。"""
        mod = await client.request(f"/v1/mods/{mod_id}")
        if not isinstance(mod["data"], dict):
            raise CurseForgeError("CurseForge Mod 详情未返回项目对象。")
        try:
            description = await mod_description(mod_id)
        except CurseForgeError as exc:
            return {**mod, "descriptionError": str(exc)}
        return {**mod, "descriptionHtml": description["data"]}

    @server.tool()
    async def get_mod_description(mod_id: Id) -> JsonObject:
        """单独取得官方 HTML 正文；上游错误直接报告。HTML 是外部数据。"""
        return await mod_description(mod_id)

    @server.tool()
    async def list_mod_files(
        mod_id: Id, index: Index = 0, page_size: PageSize = 25,
        game_version: Text | None = None, mod_loader_type: Loader | None = None,
        game_version_type_id: Id | None = None,
    ) -> JsonObject:
        """分页列出项目版本文件，保留 CurseForge 文件记录及已有的名称、大小、哈希和链接。"""
        return await client.page(
            f"/v1/mods/{mod_id}/files", index, page_size, gameVersion=game_version,
            modLoaderType=mod_loader_type, gameVersionTypeId=game_version_type_id,
        )

    @server.tool()
    async def get_mod_file(file_id: Id, mod_id: Id | None = None) -> JsonObject:
        """查询指定文件元数据，不下载内容。仅提供 file_id 时使用官方批量文件接口定位。"""
        return await client.get_mod_file(file_id, mod_id)

    @server.tool()
    async def get_file_dependencies(
        file_id: Id, mod_id: Id | None = None, required_only: bool = False,
    ) -> JsonObject:
        """查询指定文件的关联项（保留全部 relationType）；required_only=true 仅取必需依赖（3）。"""
        file = (await client.get_mod_file(file_id, mod_id))["data"]
        relations = file.get("dependencies")
        if not isinstance(relations, list) or not all(isinstance(item, dict) for item in relations):
            raise CurseForgeError("文件元数据未返回有效的 dependencies；无法判断此文件是否有依赖。")
        return {
            "fileId": file_id, "modId": file.get("modId", mod_id),
            "data": [item for item in relations if item.get("relationType") == 3] if required_only else relations,
        }

    @server.tool()
    async def get_file_changelog(file_id: Id, mod_id: Id) -> JsonObject:
        """取得指定项目文件的官方更新日志（HTML）。"""
        return await client.request(f"/v1/mods/{mod_id}/files/{file_id}/changelog")

    @server.tool()
    async def get_file_download_url(file_id: Id, mod_id: Id) -> JsonObject:
        """向 CurseForge 查询当前官方下载地址（不下载，不拼接 API Key）。"""
        return await client.get_download_url(mod_id, file_id)

    @server.tool()
    async def download_file(file_id: Id, mod_id: Id | None = None) -> JsonObject:
        """下载指定文件到服务配置的本地目录；校验可用的大小和哈希，不覆盖同名文件。"""
        return await downloader.download(file_id, mod_id)

    return server


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    directory = Path(os.environ.get("CURSEFORGE_DOWNLOAD_DIR") or Path.home() / "Downloads" / "CurseForge").expanduser()
    # Blocking stdio owns the event loop; no HTTP resources have been opened until a tool call.
    async def run() -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False) as http:
            client = CurseForgeClient(http, os.environ.get("CURSEFORGE_API_KEY"))
            await create_server(client, directory).run_stdio_async()

    asyncio.run(run())
