from __future__ import annotations

import asyncio
import hashlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mock_server import ARTIFACT, MOD, TEST_KEY


@asynccontextmanager
async def connect(tmp_path: Path, *, live_entrypoint: bool = False):
    assert sys.version_info >= (3, 11)
    args = ["-m", "curseforge_mcp"] if live_entrypoint else [str(Path(__file__).with_name("mock_server.py")), str(tmp_path)]
    params = StdioServerParameters(
        command=sys.executable, args=args,
        env={"CURSEFORGE_API_KEY": "", "CURSEFORGE_DOWNLOAD_DIR": str(tmp_path)},
    )
    async with asyncio.timeout(20):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "CurseForge"
                yield session


async def invoke(session, name, /, **params):
    reply = await session.call_tool(name, params)
    assert not reply.isError, reply.content
    assert isinstance(reply.structuredContent, dict), reply
    return reply.structuredContent


async def error(session, name, contains, /, **params):
    reply = await session.call_tool(name, params)
    assert reply.isError
    msg = " ".join(item.text for item in reply.content if item.type == "text")
    assert contains in msg, msg
    assert TEST_KEY not in msg and "do-not-disclose" not in msg, msg


def test_installed_entrypoint_initializes_without_key(tmp_path):
    async def test():
        async with connect(tmp_path, live_entrypoint=True) as session:
            tools = (await session.list_tools()).tools
            assert {tool.name for tool in tools} == {
                "list_games", "get_game", "list_categories", "get_game_versions", "search_mods", "get_mod",
                "get_mod_description", "list_mod_files", "get_mod_file", "get_file_dependencies",
                "get_file_changelog", "get_file_download_url", "download_file",
            }
            await error(session, "list_games", "CURSEFORGE_API_KEY")
    asyncio.run(test())


def test_game_filter_pagination_search_and_raw_detail(tmp_path):
    async def test():
        async with connect(tmp_path) as s:
            first = await invoke(s, "list_games", slug="MINECRAFT", page_size=1)
            assert first["data"] == [] and first["nextIndex"] == 1
            assert first["pagination"]["totalCount"] == 3
            second = await invoke(s, "list_games", name="CRAFT", slug="MINECRAFT", index=first["nextIndex"], page_size=first["nextPageSize"])
            assert second["data"][0]["id"] == 432 and second["nextIndex"] == 2
            assert "nextIndex" not in await invoke(s, "list_games", slug="minecraft", index=2, page_size=1)
            assert (await invoke(s, "get_game", game_id=432))["data"]["assets"]["iconUrl"]
            assert (await invoke(s, "list_categories", game_id=432, classes_only=True))["data"][0]["id"] == 6
            assert (await invoke(s, "get_game_versions", game_id=432))["data"][0]["versions"] == ["1.21.1"]
            mods = await invoke(s, "search_mods", game_id=432, class_id=6, category_id=8, keyword="Example",
                                game_version="1.21.1", mod_loader_type=4, page_size=1, sort_field=3, sort_order="desc")
            assert mods["data"] == [MOD] and mods["nextIndex"] == 1
            mod = await invoke(s, "get_mod", mod_id=70)
            assert mod["data"] == MOD and mod["descriptionHtml"] == "<p>Example &amp; details</p>"
            assert (await invoke(s, "get_mod_description", mod_id=70))["data"] == mod["descriptionHtml"]
            await error(s, "search_mods", "game_version", game_id=432, mod_loader_type=4)
            await error(s, "list_games", "10000", index=9999, page_size=2)
    asyncio.run(test())


def test_permissions_missing_fields_and_api_redirect(tmp_path):
    async def test():
        async with connect(tmp_path) as s:
            sparse = await invoke(s, "get_mod", mod_id=71)
            assert sparse["data"] == {"id": 71, "name": "Sparse project"}
            assert "descriptionHtml" not in sparse and "403" in sparse["descriptionError"]
            assert TEST_KEY not in sparse["descriptionError"]
            await error(s, "get_mod_description", "HTTP 403", mod_id=71)
            await error(s, "get_mod", "HTTP 403", mod_id=72)
            await error(s, "get_mod", "HTTP 302", mod_id=73)
    asyncio.run(test())


def test_file_selection_and_dependencies_are_file_specific(tmp_path):
    async def test():
        async with connect(tmp_path) as s:
            first = await invoke(s, "list_mod_files", mod_id=70, page_size=1)
            second = await invoke(s, "list_mod_files", mod_id=70, page_size=1, index=first["nextIndex"])
            assert [first["data"][0]["id"], second["data"][0]["id"]] == [100, 101]
            assert "nextIndex" not in second
            metadata = await invoke(s, "get_mod_file", file_id=100, mod_id=70)
            assert metadata["data"] == first["data"][0]
            deps = await invoke(s, "get_file_dependencies", file_id=100)
            assert deps["data"] == metadata["data"]["dependencies"]
            assert [item["relationType"] for item in deps["data"]] == [3, 2, 5, 94]
            other_deps = await invoke(s, "get_file_dependencies", file_id=101)
            assert [item["relationType"] for item in other_deps["data"]] == [3, 1, 4, 6]
            required = await invoke(s, "get_file_dependencies", file_id=101, required_only=True)
            assert required["data"] == [{"modId": 60, "relationType": 3}]
            assert (await invoke(s, "get_file_dependencies", file_id=109))["data"] == []
            await error(s, "get_file_dependencies", "无法判断", file_id=102)
            await error(s, "get_mod_file", "未找到", file_id=999)
            assert (await invoke(s, "get_file_changelog", file_id=100, mod_id=70))["data"] == "<p>Changes</p>"
            assert (await invoke(s, "get_file_download_url", file_id=100, mod_id=70))["data"].endswith("100.jar")
    asyncio.run(test())


def test_download_integrity_cross_cdn_no_secret_and_no_overwrite(tmp_path):
    async def test():
        async with connect(tmp_path) as s:
            result = await invoke(s, "download_file", file_id=100)
            path = Path(result["path"])
            assert path == tmp_path / "example-100.jar" and path.read_bytes() == ARTIFACT
            assert result["bytes"] == len(ARTIFACT) and result["sizeVerified"] is True
            assert result["verifiedHashes"] == {"sha1": hashlib.sha1(ARTIFACT).hexdigest(), "md5": hashlib.md5(ARTIFACT).hexdigest()}
            path.write_bytes(b"existing")
            await error(s, "download_file", "未覆盖", file_id=100)
            assert path.read_bytes() == b"existing"
            assert list(tmp_path.iterdir()) == [path]
    asyncio.run(test())


def test_download_expired_url_refresh_missing_hash_and_size(tmp_path):
    async def test():
        async with connect(tmp_path) as s:
            refreshed = await invoke(s, "download_file", file_id=105)
            assert Path(refreshed["path"]).read_bytes() == ARTIFACT
            sparse = await invoke(s, "download_file", file_id=109)
            assert Path(sparse["path"]).read_bytes() == ARTIFACT
            assert sparse["sizeVerified"] is False and sparse["verifiedHashes"] == {} and sparse["unverifiedHashAlgorithms"] == [90]
            assert not list(tmp_path.glob("*.part"))
    asyncio.run(test())


def test_download_denied_interrupted_bad_hash_size_and_unsafe_metadata(tmp_path):
    async def test():
        async with connect(tmp_path) as s:
            for file_id, message in ((103, "未提供第三方下载地址"), (104, "传输中断"), (106, "安全规则"),
                                     (107, "SHA1"), (108, "HTTP 401"), (110, "安全的 fileName"),
                                     (111, "不可用"), (112, "安全规则"), (113, "超过上游 fileLength")):
                await error(s, "download_file", message, file_id=file_id)
                assert not list(tmp_path.iterdir())
    asyncio.run(test())
