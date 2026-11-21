from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

import httpx

from .client import CurseForgeClient, CurseForgeError, JsonObject

EDGE_HOST = "edge.forgecdn.net"
REDIRECT_CODES = (301, 302, 303, 307, 308)
REFRESH_CODES = (403, 404, 410)
HASH_NAMES = {1: "sha1", 2: "md5"}


def file_name(file: JsonObject) -> str:
    value = file.get("fileName")
    if (
        not isinstance(value, str) or not value or value in (".", "..")
        or value.endswith((" ", ".")) or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', value)
        or PureWindowsPath(value).is_reserved()
    ):
        raise CurseForgeError("文件元数据没有安全的 fileName，未保存文件。")
    return value


def cdn_url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
        allowed = (
            url.scheme == "https" and url.port in (None, 443) and not url.userinfo
            and (url.host == "forgecdn.net" or url.host.endswith(".forgecdn.net"))
            and not any(key.casefold() in ("api-key", "x-api-key") for key in url.params)
        )
    except (httpx.InvalidURL, ValueError):
        allowed = False
    if not allowed:
        raise CurseForgeError("下载 URL 或重定向不符合 CurseForge CDN 安全规则（HTTPS、官方 forgecdn.net、无内嵌凭据）。")
    return url


def hash_expectations(file: JsonObject) -> tuple[dict[str, str], list[int]]:
    hashes = file.get("hashes")
    if hashes is None:
        return {}, []
    if not isinstance(hashes, list):
        raise CurseForgeError("CurseForge 文件哈希列表格式无效。")
    expected: dict[str, str] = {}
    unsupported: list[int] = []
    for entry in hashes:
        if not isinstance(entry, dict) or type(entry.get("algo")) is not int:
            raise CurseForgeError("CurseForge 文件哈希条目格式无效。")
        name = HASH_NAMES.get(entry["algo"])
        if name is None:
            if entry["algo"] not in unsupported:
                unsupported.append(entry["algo"])
            continue
        value = entry.get("value")
        length = 40 if name == "sha1" else 32
        if not isinstance(value, str) or not re.fullmatch(rf"[a-fA-F0-9]{{{length}}}", value):
            raise CurseForgeError(f"CurseForge 文件的 {name.upper()} 校验值格式无效。")
        value = value.lower()
        if name in expected and expected[name] != value:
            raise CurseForgeError(f"CurseForge 文件的 {name.upper()} 校验值相互冲突。")
        expected[name] = value
    return expected, unsupported


class Downloader:
    def __init__(self, client: CurseForgeClient, directory: Path) -> None:
        self.client = client
        self.directory = directory

    async def _transfer(
        self, url: httpx.URL, output: BinaryIO, hashers: dict, expected_size: int | None
    ) -> int:
        try:
            for hop in range(6):  # one initial request and at most five redirects
                headers = {"Accept-Encoding": "identity"}
                if url.host == EDGE_HOST:
                    # httpx can forward custom headers during redirects. Build each hop ourselves.
                    headers.update(self.client.auth_headers())
                async with self.client.http.stream(
                    "GET", url, headers=headers, follow_redirects=False
                ) as response:
                    status = response.status_code
                    if status in REDIRECT_CODES:
                        location = response.headers.get("Location")
                        if not location or hop == 5:
                            raise CurseForgeError("CDN 重定向缺少目标或超过五次。")
                        try:
                            url = cdn_url(str(url.join(location)))
                        except (httpx.InvalidURL, ValueError):
                            raise CurseForgeError("CDN 重定向 URL 格式无效。") from None
                        continue
                    if status != 200:
                        hint = {
                            401: "请核对 CURSEFORGE_API_KEY 是否有效且有下载权限。",
                            403: "无下载权限或链接已过期。",
                            404: "文件不可用或链接已过期。",
                            410: "该链接已过期或文件已移除。",
                            429: "下载达到请求限额，请稍后重试。",
                        }.get(status, "未保存该文件。")
                        raise CurseForgeError(f"CurseForge CDN 返回 HTTP {status}。{hint}", status)
                    encoding = response.headers.get("Content-Encoding", "identity").lower()
                    if encoding != "identity":
                        raise CurseForgeError("CDN 返回了非 identity 编码，不能安全校验原始文件。")
                    size = 0
                    async for chunk in response.aiter_raw():
                        size += len(chunk)
                        if expected_size is not None and size > expected_size:
                            raise CurseForgeError("下载内容字节数超过上游 fileLength。")
                        output.write(chunk)
                        for hasher in hashers.values():
                            hasher.update(chunk)
                    return size
        except httpx.RequestError:
            raise CurseForgeError("CDN 下载超时、连接失败或传输中断；未保留未完成文件。") from None
        raise AssertionError("unreachable redirect state")

    async def download(self, file_id: int, mod_id: int | None) -> JsonObject:
        file = (await self.client.get_mod_file(file_id, mod_id))["data"]
        if file.get("isAvailable") is False:
            raise CurseForgeError("CurseForge 标记此文件不可用，未尝试下载。")
        if mod_id is None:
            mod_id = file.get("modId")
        if type(mod_id) is not int or mod_id <= 0:
            raise CurseForgeError("文件元数据缺少有效的 modId；请显式提供 mod_id。")
        name = file_name(file)
        expected, unsupported = hash_expectations(file)
        hashers = {algo: hashlib.new(algo, usedforsecurity=False) for algo in expected}
        expected_size = file.get("fileLength")
        if expected_size is not None and (type(expected_size) is not int or expected_size < 0):
            raise CurseForgeError("CurseForge 文件的 fileLength 格式无效。")

        tmp: Path | None = None
        try:
            directory = self.directory.resolve()
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / name
            if os.path.lexists(destination):
                raise CurseForgeError("下载目录下已存在同名文件，未覆盖。")
            url = cdn_url((await self.client.get_download_url(mod_id, file_id))["data"])
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=directory, prefix=".curseforge-", suffix=".part", delete=False
            ) as output:
                tmp = Path(output.name)
                try:
                    size = await self._transfer(url, output, hashers, expected_size)
                except CurseForgeError as exc:
                    if exc.status not in REFRESH_CODES:
                        raise
                    fresh = cdn_url((await self.client.get_download_url(mod_id, file_id))["data"])
                    size = await self._transfer(fresh, output, hashers, expected_size)

            if expected_size is not None and size != expected_size:
                raise CurseForgeError(f"下载大小不匹配：上游 fileLength 为 {expected_size}，实际为 {size} 字节。")
            for algo, digest in expected.items():
                if hashers[algo].hexdigest() != digest:
                    raise CurseForgeError(f"下载文件的 {algo.upper()} 与上游校验值不一致。")

            # No overwrite even when another download finishes after our initial existence check.
            if os.name == "nt":
                tmp.rename(destination)
            else:
                os.link(tmp, destination)
        except FileExistsError:
            raise CurseForgeError("目标路径已存在，未覆盖任何文件。") from None
        except OSError as exc:
            raise CurseForgeError(f"无法保存本地下载文件（{exc.strerror or type(exc).__name__}）。") from None
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

        result = {
            "path": str(destination), "bytes": size, "fileId": file_id, "modId": mod_id,
            "sizeVerified": expected_size is not None, "verifiedHashes": expected,
        }
        if unsupported:
            result["unverifiedHashAlgorithms"] = unsupported
        return result
