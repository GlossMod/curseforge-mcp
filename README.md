# CurseForge MCP 

<div align="center">

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/) [![MCP 1.x](https://img.shields.io/badge/MCP-1.x-green.svg)](https://modelcontextprotocol.io/)

**按游戏版本和加载器查找 Mod，查看每个文件的前置并下载。**

</div>

---

## ✨ 项目简介

这个非官方本地 stdio 服务把 CurseForge 的游戏目录、项目资料和版本文件接入 MCP 客户端。一个 Mod 的不同文件可以有不同的前置，因此选定游戏版本和加载器后，再按 `file_id` 查询依赖。

## ⚡ 快速开始

### 前置条件

- Python 3.11 或更高版本，以及 [uv](https://docs.astral.sh/uv/)。
- 一个获批的 CurseForge Core API Key，可从 [CurseForge for Studios](https://console.curseforge.com/) 申请。

### 安装与更新

在 `curseforge-mcp` 仓库根目录执行：

```sh
uv sync --python 3.11
```

更新仓库源码后，重新执行上面的命令并重启客户端中的服务。若使用已有的 Python 虚拟环境，也可以用 `python -m pip install --upgrade .` 安装或更新，启动命令为 `curseforge-mcp`。

### 配置客户端

在仓库根目录创建 `.env`。已有配置可直接复用。将密钥放在单引号内，以保留可能出现的 `$` 等字符。

```dotenv
CURSEFORGE_API_KEY='替换为你的 Core API Key'
# 可选；默认目录为 ~/Downloads/CurseForge
# CURSEFORGE_DOWNLOAD_DIR=E:/Downloads/CurseForge
```

客户端可以在没有 Key 时列出工具；读取站点数据需要 Key。

将以下条目加入使用 `mcpServers` 格式的客户端，把 `--directory` 后的示例路径替换为本仓库的绝对路径。使用 VS Code 的 `.vscode/mcp.json` 时，顶层键使用 `servers`。

```json
{
  "mcpServers": {
    "curseforge-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "E:/GitHub/mcp/curseforge-mcp", "run", "--no-sync", "--env-file", ".env", "curseforge-mcp"]
    }
  }
}
```

### 第一次调用

让客户端执行 `search_mods(game_id=432, slug="sodium")` 查找 Minecraft 的 Sodium。将结果中的 `id` 传给 `list_mod_files`，选择合适的文件，再用 `get_file_dependencies(file_id=…, required_only=true)` 查看必需前置。

## 🧰 工具列表

| 工具                    | 主要参数                                                                 | 用途                                                                                                            |
| ----------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `list_games`            | `name`、`slug`、`index`、`page_size` 可选                                | 分页列游戏。名称匹配子串，slug 精确匹配，两者都忽略大小写。                                                     |
| `get_game`              | `game_id`                                                                | 取得游戏详情及图片。                                                                                            |
| `list_categories`       | `game_id`；可选 `class_id`、`classes_only`                               | 取得指定游戏的分类或大类 ID。                                                                                   |
| `get_game_versions`     | `game_id`                                                                | 取得站点公布的游戏版本。                                                                                        |
| `search_mods`           | `game_id`；可选 `keyword`、`slug`、`category_id`、`class_id` 等          | 按关键词、分类、版本、加载器筛选。使用 `mod_loader_type` 时同时指定 `game_version`；支持排序与分页。            |
| `get_mod`               | `mod_id`                                                                 | 返回完整项目资料，包括作者、主页、封面和截图，并补充 `descriptionHtml`；正文请求失败时返回 `descriptionError`。 |
| `get_mod_description`   | `mod_id`                                                                 | 单独读取项目的 HTML 介绍。                                                                                      |
| `list_mod_files`        | `mod_id`；可选 `game_version`、`mod_loader_type`、`game_version_type_id` | 按兼容条件分页列出文件及已有的哈希、大小和下载地址。                                                            |
| `get_mod_file`          | `file_id`；可选 `mod_id`                                                 | 获取指定文件的原始元数据。                                                                                      |
| `get_file_dependencies` | `file_id`；可选 `mod_id`、`required_only`                                | 保留原始依赖关系。`required_only=true` 只返回必需依赖（`relationType=3`）。                                     |
| `get_file_changelog`    | `file_id`、`mod_id`                                                      | 取得所选文件的 HTML 更新日志。                                                                                  |
| `get_file_download_url` | `file_id`、`mod_id`                                                      | 查询官方为此文件签发的下载地址。                                                                                |
| `download_file`         | `file_id`；可选 `mod_id`                                                 | 下载选定文件到本地，返回绝对路径、字节数及已验证的哈希。                                                        |

**分页。** `index` 是从 0 开始的上游偏移量，`page_size` 最大为 50，两者之和最多为 10,000。`list_games` 在每个上游页面内筛选，即使本页没有匹配项，也应按返回的 `nextIndex`、`nextPageSize` 继续。

**依赖关系。** `relationType` 的 1–6 依次表示内嵌库、可选依赖、必需依赖、工具、不兼容和包含内容。关系指向目标 Mod，具体兼容文件须从该项目的文件列表中选择。

**下载权限与校验。** 工具使用官方 `/download-url`。若站点拒绝第三方下载，可从 `get_mod` 返回的 `data.links.websiteUrl` 打开项目文件页。仅向官方 API 和 `edge.forgecdn.net` 发送密钥；其他允许的 CDN 跳转不携带密钥。下载器核对可用的大小、SHA-1 和 MD5，成功后保存文件，遇到同名文件时保留原件。

## 测试

```sh
uv run --extra test pytest -q
uv run --env-file .env python tests/live_smoke.py
```

第一条使用模拟 API 和 CDN。第二条使用当前 Key 读取实站资料；如需验证真实下载，追加 `--download --download-dir ./live-downloads`。

## 构建与发布到 PyPI

本地可运行 `uv build`，在 `dist/` 检查 wheel 和源码包。

在 PyPI 为 `curseforge-mcp` 配置 GitHub Actions Trusted Publisher：仓库为 `GlossMod/curseforge-mcp`（Owner：`GlossMod`，Repository：`curseforge-mcp`），Workflow：`publish.yml`，Environment：`pypi`。

在 GitHub 发布带版本标签的 Release（例如 `v0.1.1`）后，`.github/workflows/publish.yml` 会使用标签版本构建并检查 wheel 和源码包，通过后自动发布到 PyPI。版本只在 CI 中替换，不会回写仓库。

参考：[CurseForge REST API](https://docs.curseforge.com/rest-api/) · [CDN 下载鉴权](https://blog.curseforge.com/introducing-api-key-authentication-for-curseforge-file-downloads/)
