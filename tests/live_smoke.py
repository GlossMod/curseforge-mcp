"""Manual smoke against CurseForge via the installed stdio executable/module."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def tool(session: ClientSession, name: str, /, **args):
    result = await session.call_tool(name, args)
    if result.isError:
        message = " ".join(item.text for item in result.content if item.type == "text")
        raise RuntimeError(f"{name}: {message}")
    if result.structuredContent is None:
        raise RuntimeError(f"{name}: missing structured content")
    return result.structuredContent


async def verify(args: argparse.Namespace) -> None:
    env = {"CURSEFORGE_API_KEY": os.environ.get("CURSEFORGE_API_KEY", "")}
    if args.download_dir:
        env["CURSEFORGE_DOWNLOAD_DIR"] = str(Path(args.download_dir).resolve())
    executable = shutil.which("curseforge-mcp")
    params = StdioServerParameters(
        command=executable or sys.executable, args=[] if executable else ["-m", "curseforge_mcp"], env=env,
    )
    async with asyncio.timeout(180):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                protocol = (await session.initialize()).protocolVersion
                tools = (await session.list_tools()).tools
                print(f"stdio: protocol={protocol}; {len(tools)} tools", flush=True)
                index, page_size = 0, 50
                while True:
                    games = await tool(session, "list_games", slug="minecraft", index=index, page_size=page_size)
                    if games["data"]:
                        game_id = games["data"][0]["id"]
                        break
                    if "nextIndex" not in games:
                        raise RuntimeError("The key could not access Minecraft in the game catalog.")
                    index, page_size = games["nextIndex"], games["nextPageSize"]
                mods = await tool(session, "search_mods", game_id=game_id, class_id=6, slug=args.mod_slug, page_size=5)
                if not mods["data"]:
                    raise RuntimeError("CurseForge did not return the requested Mod slug.")
                mod_id = mods["data"][0]["id"]
                details = await tool(session, "get_mod", mod_id=mod_id)
                if "descriptionError" in details:
                    raise RuntimeError(details["descriptionError"])
                print(f"metadata: game_id={game_id}; mod_id={mod_id}; homepage={details['data']['links']['websiteUrl']}", flush=True)
                files = await tool(session, "list_mod_files", mod_id=mod_id, page_size=5)
                if not files["data"]:
                    raise RuntimeError("CurseForge did not return any files.")
                file_id = args.file_id or files["data"][0]["id"]
                metadata = (await tool(session, "get_mod_file", mod_id=mod_id, file_id=file_id))["data"]
                relations = await tool(session, "get_file_dependencies", file_id=file_id)
                print(f"file: file_id={file_id}; relations={len(relations['data'])}; filename={metadata['fileName']}", flush=True)
                if not args.download:
                    return
                result = await tool(session, "download_file", mod_id=mod_id, file_id=file_id)
                path = Path(result["path"])
                if path.stat().st_size != result["bytes"] or not path.is_absolute():
                    raise RuntimeError("Downloaded file is missing or does not match the reported size.")
                if result["sizeVerified"] and path.stat().st_size != metadata["fileLength"]:
                    raise RuntimeError("Downloaded file size differs from the metadata size.")
                for algo, digest in result["verifiedHashes"].items():
                    with path.open("rb") as stream:
                        actual = hashlib.file_digest(stream, lambda: hashlib.new(algo, usedforsecurity=False)).hexdigest()
                    if actual != digest:
                        raise RuntimeError(f"Downloaded file failed a second {algo} check.")
                print(f"downloaded: path={path}; bytes={result['bytes']}; upstream size verified={result['sizeVerified']}; hashes={sorted(result['verifiedHashes'])}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod-slug", default="sodium")
    parser.add_argument("--file-id", type=int)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--download-dir")
    arguments = parser.parse_args()
    if arguments.download and not arguments.download_dir:
        parser.error("--download requires an explicit --download-dir")
    try:
        asyncio.run(verify(arguments))
    except* RuntimeError as failures:
        failure = failures
        while isinstance(failure, BaseExceptionGroup):
            failure = failure.exceptions[0]
        sys.exit(str(failure))
