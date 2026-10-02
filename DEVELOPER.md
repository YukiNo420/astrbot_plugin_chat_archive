# Chat Archive 开发者 API 文档

`astrbot_plugin_chat_archive` 为有需要访问 AstrBot 内部聊天记录的第三方插件提供了**内部 Python API** 以及**热插拔的 Web API 扩展机制**。

通过这些接口，您可以非常轻松地实现诸如 **AI 的长期/短期上下文记忆**、**用户发言画像**、**活跃度图表统计**等功能。API 返回的所有消息均为**清洗过的干净字符串**（其中媒体文件如图片、视频、语音已自动转换为标准的 CQ 码格式）。

---

## 🚀 快速入门

在您编写的插件（如 `main.py`）中，您可以通过 `self.context` 方便地获取当前已注册的存档插件类，并直接调用其暴露的静态类方法：

```python
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.api.event import filter

@register("my_plugin", "demo", "调用存档插件演示", "1.0")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        
    @filter.command("查询上下文")
    async def get_context(self, event: AstrMessageEvent):
        # 1. 获取 ChatArchive 插件的 Star 元数据
        star_meta = self.context.get_registered_star("astrbot_plugin_chat_archive")
        if not star_meta:
            yield event.plain_result("未安装聊天记录存档插件 astrbot_plugin_chat_archive")
            return
        # 获取插件的类类型
        archive = star_meta.star_cls_type
            
        # 2. 调用 API 查询最近 10 条聊天记录（默认排除已撤回的消息）
        history = archive.get_history(
            session_id=event.unified_msg_origin, 
            limit=10, 
            asc=True
        )
        
        # 3. 格式化为大模型的 Prompt 上下文
        context_str = "\n".join([
            f"{msg['sender_name']}: {msg['message']}" for msg in history
        ])
        yield event.plain_result(f"最近 10 条上下文：\n{context_str}")
```

> [!TIP]
> 如果您是在做 AI 人格、MBTI 分析等大模型对话分析场景，推荐直接使用快捷接口 `get_context_messages()`，它将直接返回格式化好的时间、昵称与内容元组列表，无需手动解析时间戳。

---

## 📊 内部 Python API 接口参考

所有 Python API 均定义在插件的 `main.py` 中，均为 `@classmethod` 类静态方法。您只需通过前面获取的 `star_meta.star_cls_type` 直接调用它们即可。

> [!IMPORTANT]
> 所有的数据库查询接口均为**同步 (Synchronous) 阻塞**的 I/O 操作。如果您的查询范围非常大（例如 `limit` 设为了数千条），在 `async` 异步上下文中调用时，请务必使用 `asyncio.to_thread(archive.get_history, ...)` 将其包裹执行，以避免阻塞 AstrBot 主事件循环。

### 1. `get_history(...)`

高健壮性的通用聊天历史多维筛选接口。

内部 Python API 的 `keyword` 继续采用原有的连续子串匹配。HTTP `/api/history` 新增可选参数 `search_mode=terms`，启用空白分词、双引号短语和 Unicode 大小写复核；省略参数或传 `search_mode=literal` 保持原有行为。网页消息搜索显式使用 `terms`，其他调用方无需修改。搜索模式不改变会话 / 用户 / 时间过滤、计数、排序和游标分页，也不执行数据库迁移。

**参数列表：**
| 参数名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `user_id` | `str` | `None` | 按发送者的用户 ID (QQ号) 进行过滤 |
| `session_id` | `str` | `None` | 按会话 ID (如 `event.unified_msg_origin`) 进行过滤 |
| `keyword` | `str` | `None` | 模糊搜索聊天消息内容 |
| `since_ts` | `int` | `None` | 仅返回大于或等于此 Unix 时间戳的消息 |
| `until_ts` | `int` | `None` | 仅返回小于或等于此 Unix 时间戳的消息 |
| `limit` | `int` | `50` | 最大返回的记录数量（建议不超过 1000） |
| `offset` | `int` | `0` | 分页查询的偏移量 |
| `asc` | `bool` | `True` | 排序规则：`True` 为时间正序（最旧的在最前），`False` 为时间倒序（最新的在最前） |
| `exclude_recalled` | `bool` | `True` | 是否排除已撤回的消息 |

**返回值 (`list[dict]`):**
```python
[
    {
        "id": 1,
        "user_id": "example-user",
        "sender_name": "张三",
        "message": "大家好！",
        "timestamp": 1775151796,
        "session_id": "group:example-group",
        "message_type": "group",
        "session_name": "测试群",
        "msg_id": "example-message",
        "is_recalled": 0
    },
    ...
]
```

---

### 2. `get_sessions()`

获取当前数据库中所有已经产生了存档记录的会话列表。

**返回值 (`list[dict]`):**
```python
[
    {
        "session_id": "group:example-group",
        "message_type": "group",
        "count": 1054,
        "last_time": 1775151796
    }
]
```

---

### 3. `get_member_rank(session_id, limit=10, since_ts=None, until_ts=None)`

获取某个指定会话（群聊）中发言最活跃的成员排行。

**参数列表：**
| 参数名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `session_id` | `str` | *必填* | 会话 ID |
| `limit` | `int` | `10` | 排行榜名额数量 |
| `since_ts` | `int` | `None` | 查询的起始时间戳 |
| `until_ts` | `int` | `None` | 查询的截止时间戳 |

**返回值 (`list[dict]`):**
```python
[
    {
        "user_id": "example-user",
        "sender_name": "张三",
        "count": 420
    }
]
```

---

### 4. `get_user_summary(user_id, session_id=None)`

获取某个特定用户的历史发言统计画像（支持指定单个会话范围过滤）。

**参数列表：**
| 参数名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `user_id` | `str` | *必填* | 用户的 ID (QQ号) |
| `session_id` | `str` | `None` | 可选。如果传入，将仅统计该用户在此会话中的发言情况 |

**返回值 (`dict`):**
```python
{
    "user_id": "example-user",
    "total_messages": 420,
    "first_seen": 1775000000,
    "last_seen": 1775151796,
    "last_nickname": "张三"
}
```

---

### 5. `get_message_count(...)`

轻量级的消息总数统计接口。不需要读取和解析消息具体内容，非常适合做逻辑阈值校验。

**参数列表：**
| 参数名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `user_id` | `str` | `None` | 按发送者过滤 |
| `session_id` | `str` | `None` | 按会话过滤 |
| `since_ts` | `int` | `None` | 查询的起始时间戳 |
| `until_ts` | `int` | `None` | 查询的截止时间戳 |
| `exclude_recalled` | `bool` | `True` | 是否排除已撤回的消息 |

**返回值 (`int`):** 符合条件的消息总数量。

```python
# 示例：检查某个用户是否在当前群聊中发过足够多的消息，才触发大模型的人物侧写分析
count = archive.get_message_count(
    user_id="example-user",
    session_id=event.unified_msg_origin
)
if count >= 50:
    # 触发分析流程...
```

---

### 6. `get_context_messages(...)`

专为 AI 大模型上下文记忆优化的快捷接口。直接返回由干净字段构成的三元组，调用者无需手动转换 Unix 时间戳。

**参数列表：**
| 参数名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `session_id` | `str` | *必填* | 会话 ID |
| `user_id` | `str` | `None` | 可选。若传入则仅统计特定用户的消息 |
| `limit` | `int` | `50` | 最多获取的消息条数 |
| `exclude_recalled` | `bool` | `True` | 是否排除已撤回的消息 |

**返回值 (`list[tuple[str, str, str]]`):** 返回由 `(格式化时间, 发送者昵称, 消息文本)` 组成的三元组列表，按时间正序排列（最旧的在最前，最适合直接喂给大模型）。

```python
# 示例：将近期的聊天数据格式化为 AI 对话上下文
messages = archive.get_context_messages(
    session_id=event.unified_msg_origin,
    user_id=sender_id,
    limit=100
)

chat_log = "\n".join([
    f"[{ts}] {name}: {msg}" for ts, name, msg in messages
])
# 将得到的 chat_log 直接塞入大模型 Prompt 模板中...
```

---

> [!NOTE]
> 在为大模型构建上下文记忆时，推荐使用 `since_ts` 限制一个固定的时间窗口（如最近 3 天），而不要进行无上限的大数量查询，以防大模型超出 Token 上下文大小限制。

---

## 🧩 免侵入式自定义 Web API 动态挂载 (热插拔)

`astrbot_plugin_chat_archive` 内置了一个极具扩展性的**热插拔 API 加载机制**。允许用户直接将自定义的 **HTTP REST API 接口** 动态挂载到插件的内置 FastAPI 网页服务器上，而**完全不需要更改插件的核心代码文件**。

这保证了当插件在主分支发生更新升级时，您本地自定义编写的所有接口**绝对不会被 git pull 覆盖或丢失**。

### ⚙️ 实现步骤

1. 在您的 AstrBot 根目录中，进入 `data/` 目录（位于 `data/plugins` 的上一级）。
2. 在 `data/` 目录下新建一个名为 `chat_archive_ext` 的文件夹。
3. 将您使用 Python 编写的接口文件（例如 `my_api.py`，不要以双下划线 `_` 开头）直接放置于该文件夹下。
4. **重新启动 AstrBot**。插件会在启动时自动扫描该目录，动态导入所有扩展脚本，并将其注册到内置 FastAPI WebUI 服务中。

### 📄 标准扩展代码模板

在 `data/chat_archive_ext/` 下创建一个名为 `my_custom_api.py` 的文件，写入如下的标准格式：

```python
from fastapi import APIRouter, Request, HTTPException

def register(app, get_db_connection):
    """
    当插件服务启动时，该函数会被自动调用并挂载。
    
    参数说明:
        app: 全局内置的 FastAPI() 应用实例。
        get_db_connection: 数据库连接工厂，调用 db = get_db_connection() 获取连接。
    """
    
    # 1. 建议使用 APIRouter 进行子路由划分，并定义专属的接口前缀 (Prefix)
    router = APIRouter(prefix="/api/custom", tags=["自定义扩展模块"])

    # 2. 定义您所需要的 API 端点
    @router.get("/user_active_hours")
    def get_user_active_hours(user_id: str):
        # 直接使用主程序暴露的连接工厂获取 SQLite 数据库连接
        db = get_db_connection()
        try:
            sql = """
                SELECT CAST(((timestamp + 28800) / 3600) % 24 AS INTEGER) as hour, COUNT(*) as cnt
                FROM chat_history
                WHERE user_id = ?
                GROUP BY hour
                ORDER BY hour
            """
            rows = db.execute(sql, (str(user_id),)).fetchall()
            return {"success": True, "data": rows}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            db.close() # 【非常重要】在 finally 块中必须手动关闭数据库连接，防止连接泄露锁死数据库

    # 3. 将您的路由注册到主应用的 FastAPI Web 服务器中
    app.include_router(router)
```

### 🔒 接口安全与鉴权保护

根据内置 Web 服务中间件的设计，除非您显式注册在白名单路径中（如 `/`、`/static`、`/api/auth/verify` 等公开页面），**您挂载的所有自定义接口都将自动受到主程序 API-Key 安全门禁的保护**。
- 您在 `/api/custom/...` 下挂载的接口在被请求时，客户端必须在 HTTP 请求头中携带正确的 WebUI 登录凭证（Header 中添加 `X-API-Key` 字段）：
  ```http
  GET /api/custom/user_active_hours?user_id=example-user HTTP/1.1
  Host: 127.0.0.1:8090
  X-API-Key: <您在后台配置的 api_key 密码>
  ```
- 如果请求未携带此 Header 或凭证错误，接口将自动返回 `401 Unauthorized` 拒绝访问，这极大地保障了您自建数据库接口的数据安全性。
