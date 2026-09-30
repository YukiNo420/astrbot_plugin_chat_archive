# ⚡ AstrBot Chat Archive Plugin (聊天消息存档插件)

<p align="center">
  <img src="logo.png" width="130" height="130" alt="Logo" style="border-radius: 20px; box-shadow: 0 4px 16px rgba(0,0,0,0.12);" />
</p>

<p align="center">
  <strong>为 <a href="https://docs.astrbot.app/">AstrBot</a> 打造的高性能聊天消息存档与暗黑风 Web 可视化面板插件。</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-AGPL_3.0-blue.svg" alt="License: AGPL-3.0">
  <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/AstrBot-v4.8.0+-orange.svg" alt="AstrBot v4.8.0+">
</p>

> 本插件是一款面向 [AstrBot](https://docs.astrbot.app/) 的聊天记录存档工具。它在后台静默接收并持久化群组与私聊消息，同时提供一个内置的 Web 管理面板，用于历史消息的浏览、检索与数据分析。所有数据均存储于本地 SQLite 数据库，无需依赖外部服务。

> [!WARNING]
> **⚠️ 安全与隐私重要提示**：本插件涉及敏感聊天记录的本地持久化存储。为了保障您的数据隐私安全，**请务必在插件配置中为内置 Web 服务设置一个高度随机的强密码 (`api_key`)**。切勿暴露在公网且不设防，以防聊天隐私数据泄露。

## ✨ 功能特性

* 🚀 **异步无感知消息存档**：采用独立的异步消息队列，消息由后台批量写入数据库，对机器人主流程零阻塞、零影响。
* 📊 **全局可视化总览看板 (Dashboard)**：全新设计的全局可视化数据大屏，实时展现多维指标、SVG 发言活跃趋势折线图、消息类型分布及群聊活跃排行。
* 🔍 **全站路由与全局搜索**：支持浏览器前进后退与 `?session_id` 状态深度同步。未选择特定会话时，可在总览面板直接执行跨会话的全局搜索。
* 🖼️ **媒体文件本地缓存**：支持将图片、视频等媒体文件下载并缓存至本地，彻底解决 QQ 原图过期失效与防盗链问题。
* 🧠 **为大模型提供长期记忆**：向 LLM Agent 注册数据库检索工具，使模型能够自主查询任意时间段的历史消息，有效突破上下文长度限制。
* 🔌 **插件扩展友好**：提供开放的 Web 路由挂载接口，其他插件可便捷地在本插件的 Web 服务上注册自定义 API 端点。


---

## 🛠️ 安装方法

1. 进入 AstrBot 插件目录并克隆本仓库：
   ```bash
   cd /path/to/AstrBot/data/plugins
   git clone https://github.com/YukiNo420/astrbot_plugin_chat_archive.git
   cd astrbot_plugin_chat_archive
   ```
2. 安装 WebUI 依赖：
   ```bash
   python3 -m pip install -r requirements.txt
   ```
3. 在 AstrBot 后台配置安全的 `api_key`，然后重启 AstrBot 完成初始化。

---

## ⚙️ 配置说明

### 基础设置（`basic`）

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `enable_archive` | `true` | 是否实时记录聊天消息到数据库中。 |
| `ignored_users` | `[]` | 不希望被记录的用户 ID 列表（如机器人自身的 QQ 号）。 |
| `cache_media` | `false` | 是否开启媒体本地缓存。 |
| `allowed_media_domains` | QQ 媒体域名白名单 | 允许缓存/代理的媒体域名及其子域名，防止 SSRF 访问内网。 |
| `allow_fake_ip` | `true` | 是否放行由 Clash/Mihomo 等代理软件 Fake-IP 模式解析出的保留网段 (`198.18.0.0/15`)，防止局域网安全策略的意外拦截。 |
| `media_max_mb` | `50` | 单个媒体缓存/代理的最大体积，范围 1–200 MB。 |
| `enable_clean` | `false` | 是否定期自动清理过期的媒体缓存文件。 |
| `clean_days` | `30` | 缓存文件保留的最大天数，超期文件将被自动删除。 |
| `db_path` | `""` | 自定义数据库路径，支持环境变量与 `~` 展开，留空使用默认位置。 |
| `sqlite_journal_mode` | `WAL` | SQLite 日志模式。NAS/NFS/SMB 等网络盘可尝试 `DELETE`。 |
| `sqlite_max_connections` | `10` | SQLite 连接池最大连接数，范围 2-64。 |

### WebUI 面板设置（`web_server`）

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `enable` | `true` | 是否启用内置 Web 服务。独立部署时应设为 `false` 以避免端口冲突。 |
| `host` | `127.0.0.1` | Web 监听地址。公网访问请配合强随机 `api_key` 与防火墙使用。 |
| `port` | `8090` | Web 服务端口。 |
| `api_key` | `""` | 访问密码。留空则每次启动生成随机密码并打印在日志。 |

### 环境变量覆盖

| 环境变量 | 说明 |
| :--- | :--- |
| `ARCHIVE_API_KEY` | 覆盖 WebUI API Key。 |
| `ARCHIVE_HOST` / `ARCHIVE_PORT` | 覆盖 WebUI 监听地址与端口。 |
| `ARCHIVE_DB_PATH` | 覆盖 SQLite 数据库路径。 |
| `ARCHIVE_DATA_DIR` | 覆盖插件数据目录。 |
| `ARCHIVE_CONFIG_PATH` | 覆盖 AstrBot 插件配置 JSON 路径。 |
| `ARCHIVE_ALLOWED_MEDIA_DOMAINS` | 逗号分隔的媒体域名白名单。 |
| `ARCHIVE_ALLOW_FAKE_IP` | 覆盖是否放行 Fake-IP 保留网段。 |
| `ARCHIVE_MEDIA_MAX_MB` | 覆盖单个媒体最大体积。 |
| `ARCHIVE_SQLITE_JOURNAL_MODE` | 覆盖 SQLite 日志模式。 |
| `ARCHIVE_SQLITE_MAX_CONNECTIONS` | 覆盖 SQLite 连接池最大连接数。 |
| `ARCHIVE_CORS_ORIGINS` | 逗号分隔的允许跨域来源。 |

---

## 合并转发中的嵌套和文件

### 媒体显示与 Web 启动排查

会话列表中的 `[图片]` / `[视频]` 是摘要；打开会话后才加载媒体。详情中的媒体加载失败时也会退回标签。带查询参数的媒体链接会按 HTML 与 CQ 两层分别解码，保留原始签名参数；升级后前端脚本版本号会更新。

开启缓存时，新归档的图片/视频链接应为 `/static/cache/...`。该路径需要登录后的会话 Cookie 或 `X-API-Key`，未登录返回 401，缓存文件缺失返回 404。只看缓存目录有文件无法判断某条记录是否引用它；旧记录不会自动重新下载或回填。

Docker 更新后 Web 拒绝连接时，请先查看启动日志：缺少 Web 依赖、端口冲突和应用启动失败现在会明确记录，`startup requested` 仅表示已请求启动。确认在新镜像的 AstrBot Python 环境中安装了 `requirements.txt`；需要容器外访问时，容器内监听配置应为 `0.0.0.0`，并发布对应端口。数据库自定义路径须是容器内可写路径，父目录会自动建立；宿主机路径需先挂载到容器。

这些检查不能保证特定 Docker 镜像升级已通过，也不能恢复已被覆盖的旧归档。升级前仍应备份完整数据目录，避免通过重装插件排查而丢失插件目录内的旧数据。

新归档为合并转发添加缩进和结束边界，Web 面板可分别展开嵌套节点，并继续读取旧版归档。仅有转发 ID 时会尝试递归获取内容，最多 8 层、32 次子请求；单次子请求限时 5 秒，总展开限时 15 秒，失败时保留已获取的原始内容。

文件已有 HTTP(S) URL 时显示可点击的文件名。只有文件 ID 时，尝试适配器的 `get_private_file_url`，或在文件明确提供 `group_id` 时使用 `get_group_file_url`；不猜测文件所属群、不调用下载文件的 `get_file`，也不公开适配器本机路径。适配器不支持、文件过期或缺少来源信息时仍保留文件名，不能保证所有转发文件均能取得下载链接。链接不做永久缓存，旧记录缺失的内容不会自动回填。

回归验证：`python -m unittest discover -s tests -v` 和 `node tests/forward_render.test.cjs`。

## 🏗️ 系统架构

```mermaid
flowchart TD
    A1["AstrBot 消息流"] --> A2["ChatArchive 拦截器"]
    A2 --> B1["异步队列"] --> B2["批处理器"] --> B3[("SQLite 数据库")]
    B3 --> C1["FastAPI 服务网关"] --> C2{"鉴权"}
    C2 -->|OK| C3["数据 API"]
    C2 -->|OK| C4["媒体代理"]
    B3 --> E1["LLM 数据库工具"] --> E2["大模型 (Agent)"]
```

---

## 🚀 高级部署与二次开发

如果您对内置前端不满意，或者希望实现前后端解耦部署（如使用 systemd 独立管理 Web 服务），我们在 `contrib/` 目录下提供了一个基础的 `systemd` 服务模板供您参考和修改。

启用独立服务前，请务必在插件配置中将 `web_server.enable` 设置为 `false`，以避免端口冲突。

独立运行 WebUI 时必须设置 `api_key`，可以写入 AstrBot 插件配置，也可以通过环境变量传入：

```bash
export ARCHIVE_API_KEY='change-me-to-a-long-random-secret'
export ARCHIVE_HOST='127.0.0.1'
export ARCHIVE_PORT='8090'
python3 -m astrbot_plugin_chat_archive.web.server
```

如果需要局域网访问，请将 `host` 或 `ARCHIVE_HOST` 改为 `0.0.0.0`，并同时配置防火墙与强随机 `api_key`。

---

## 📄 开源许可证

本项目基于 **[AGPL-3.0](LICENSE)** 协议发布。

---

## ⚠️ 免责声明

本插件仅供学习、研究与个人合法用途使用。使用者应自行确保其使用行为符合所在地区的法律法规及相关平台的服务条款，包括但不限于个人信息保护、数据存储与隐私相关规定。

- **请勿** 将本插件用于未经授权地收集、存储或传播他人的聊天内容。
- **请勿** 将存档数据用于任何商业目的或侵犯他人隐私的行为。
- 在群组中部署本插件前，建议提前告知群成员消息将被记录。

本项目作者不对因使用本插件产生的任何直接或间接损失、法律纠纷或数据泄露承担责任。**使用即视为您已理解并同意上述条款。**
