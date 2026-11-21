"""Offline, schema-shaped CurseForge Core API and CDN via httpx.MockTransport."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import httpx

from curseforge_mcp.client import CurseForgeClient
from curseforge_mcp.server import create_server

TEST_KEY = "$2a$fixture-only-credential"
ARTIFACT = b"curseforge binary\x00\xff\x84"
MOD = {
    "id": 70, "gameId": 432, "name": "Example",
    "links": {"websiteUrl": "https://www.curseforge.com/minecraft/mc-mods/example", "sourceUrl": "https://source.invalid/example"},
    "authors": [{"id": 7, "name": "Author", "url": "https://www.curseforge.com/members/example"}],
    "logo": {"url": "https://media.forgecdn.net/example.png"},
    "screenshots": [{"url": "https://media.forgecdn.net/example2.png", "thumbnailUrl": "https://media.forgecdn.net/example2-small.png"}],
    "upstreamUnknownField": {"preserve": [False, {"nullable": None}]},
}
FILE_IDS = set(range(100, 114))


def file_data(file_id: int) -> dict:
    deps = ({"modId": 50, "relationType": 3, "extra": "keep"}, {"modId": 51, "relationType": 2},
            {"modId": 52, "relationType": 5}, {"modId": 53, "relationType": 94})
    file = {
        "id": file_id, "modId": 70, "fileName": f"example-{file_id}.jar", "isAvailable": True,
        "hashes": [{"algo": 1, "value": hashlib.sha1(ARTIFACT).hexdigest().upper()},
                   {"algo": 2, "value": hashlib.md5(ARTIFACT).hexdigest()}],
        "fileLength": len(ARTIFACT), "downloadUrl": "https://edge.forgecdn.net/files/unused/stale.jar",
        "gameVersions": ["1.21.1", "Fabric"], "dependencies": list(deps), "releaseType": 1,
    }
    if file_id == 101:
        file["dependencies"] = [{"modId": 60, "relationType": 3}, {"modId": 50, "relationType": 1},
                                {"modId": 61, "relationType": 4}, {"modId": 62, "relationType": 6}]
    elif file_id == 102:
        del file["dependencies"]
    elif file_id == 107:
        file["hashes"][0]["value"] = "9" * 40
    elif file_id == 109:
        del file["fileLength"]
        file["hashes"] = [{"algo": 90, "value": "future"}]
        file["dependencies"] = []
    elif file_id == 110:
        file["fileName"] = "../escape.jar"
    elif file_id == 111:
        file["isAvailable"] = False
    elif file_id == 113:
        file["fileLength"] -= 1
    return file


class BinaryStream(httpx.AsyncByteStream):
    def __init__(self, url: httpx.URL, interrupted: bool = False) -> None:
        self.url = url
        self.interrupted = interrupted

    async def __aiter__(self):
        yield ARTIFACT[:8]
        if self.interrupted:
            raise httpx.RemoteProtocolError(f"untrusted URL: {self.url}")
        yield ARTIFACT[8:]


def data(payload) -> httpx.Response:
    return httpx.Response(200, json={"data": payload})


def page(request: httpx.Request, rows: list) -> httpx.Response:
    index = int(request.url.params["index"])
    page_size = int(request.url.params["pageSize"])
    selected = rows[index:index + page_size]
    return httpx.Response(200, json={
        "data": selected, "pagination": {"index": index, "pageSize": page_size, "resultCount": len(selected), "totalCount": len(rows)},
    })


class MockSite:
    def __init__(self) -> None:
        self.requests: Counter[str] = Counter()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        key = request.headers.get("x-api-key")
        assert request.url.scheme == "https" and request.url.port in (None, 443)
        assert "api-key" not in request.url.params
        self.requests[path] += 1
        if host == "mediafilez.forgecdn.net":
            assert key is None and path == "/files/served/100.jar"
            return httpx.Response(200, stream=BinaryStream(request.url))
        if host == "edge.forgecdn.net":
            assert key == TEST_KEY
            if path == "/files/example/100.jar":
                return httpx.Response(302, headers={"Location": "https://mediafilez.forgecdn.net/files/served/100.jar"})
            if path == "/files/example/106.jar":
                return httpx.Response(307, headers={"Location": "https://forgecdn.net.external.invalid/receive"})
            if path == "/files/example/112.jar":
                return httpx.Response(302, headers={"Location": "http://edge.forgecdn.net/receive"})
            if path == "/files/example/105.jar" and request.url.params["signature"] == "expired":
                return httpx.Response(410)
            if path == "/files/example/108.jar":
                return httpx.Response(401, text=f"super-secret error {TEST_KEY}")
            if path in {f"/files/example/{number}.jar" for number in (104, 105, 107, 109, 113)}:
                return httpx.Response(200, stream=BinaryStream(request.url, path.endswith("/104.jar")))
            raise AssertionError(f"unexpected CDN request path: {path}")
        assert host == "api.curseforge.com" and key == TEST_KEY
        games = [
            {"id": 1, "name": "Other Game"}, {"id": 432, "name": "Minecraft", "slug": "minecraft", "assets": {"iconUrl": "https://media.forgecdn.net/game.png"}},
            {"id": 22, "name": "Minecraft Dungeons", "slug": "minecraft-dungeons"},
        ]
        if path == "/v1/games":
            assert set(request.url.params.keys()) == {"index", "pageSize"}
            return page(request, games)
        if path == "/v1/games/432":
            return data(games[1])
        if path == "/v1/games/432/versions":
            return data([{"type": 1, "versions": ["1.21.1"]}])
        if path == "/v1/categories":
            assert dict(request.url.params) == {"gameId": "432", "classesOnly": "true"}
            return data([{"id": 6, "isClass": True}])
        if path == "/v1/mods/search":
            assert dict(request.url.params) == {
                "gameId": "432", "index": "0", "pageSize": "1", "classId": "6", "categoryId": "8",
                "searchFilter": "Example", "gameVersion": "1.21.1", "modLoaderType": "4", "sortField": "3", "sortOrder": "desc",
            }
            return page(request, [MOD, {"id": 71}])
        if path == "/v1/mods/70":
            return data(MOD)
        if path == "/v1/mods/70/description":
            return data("<p>Example &amp; details</p>")
        if path == "/v1/mods/71":
            return data({"id": 71, "name": "Sparse project"})
        if path in ("/v1/mods/71/description", "/v1/mods/72"):
            return httpx.Response(403, text=f"untrusted body {TEST_KEY}")
        if path == "/v1/mods/73":
            return httpx.Response(302, headers={"Location": "https://external.invalid/receive"})
        if path == "/v1/mods/70/files":
            return page(request, [file_data(100), file_data(101)])
        if path == "/v1/mods/files":
            assert request.method == "POST"
            file_ids = json.loads(request.content)["fileIds"]
            return data([file_data(number) for number in file_ids if number in FILE_IDS])
        if path == "/v1/mods/70/files/100/changelog":
            return data("<p>Changes</p>")
        if path.startswith("/v1/mods/70/files/"):
            pieces = path.split("/")
            file_id = int(pieces[5])
            if len(pieces) == 6:
                return data(file_data(file_id))
            assert pieces[6] == "download-url" and file_id not in (110, 111)
            if file_id == 103:
                return data(None)
            suffix = ""
            if file_id == 104:
                suffix = "?signature=do-not-disclose"
            if file_id == 105:
                suffix = "?signature=" + ("expired" if self.requests[path] == 1 else "fresh")
            return data(f"https://edge.forgecdn.net/files/example/{file_id}.jar{suffix}")
        raise AssertionError(f"unexpected API request: {path}")


async def start(download_dir: Path) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(MockSite()), follow_redirects=False) as http:
        await create_server(CurseForgeClient(http, TEST_KEY), download_dir).run_stdio_async()


if __name__ == "__main__":
    asyncio.run(start(Path(sys.argv[1])))
