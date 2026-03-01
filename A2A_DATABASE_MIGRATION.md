# A2A 数据库存储迁移文档

## 概述

已将 A2A routes 中的内存存储（InMemory）迁移到数据库存储（Database），使用 EvoAI 的 PostgreSQL 数据库。

---

## 主要变化

### 1. 存储类型变更

| 组件 | 之前 | 现在 | 说明 |
|------|------|------|------|
| Task Store | `InMemoryTaskStore` | `DatabaseTaskStore` | 任务持久化到数据库 |
| Push Config Store | `InMemoryPushNotificationConfigStore` | `DatabasePushNotificationConfigStore` | Push配置持久化到数据库 |

### 2. 数据库连接

```python
# 创建异步引擎
async_connection_string = settings.POSTGRES_CONNECTION_STRING.replace(
    "postgresql://", "postgresql+asyncpg://"
)

async_engine = create_async_engine(
    async_connection_string,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
```

### 3. 数据库表

创建了两个新表：

#### a2a_tasks
存储 A2A 任务信息：
- task_id (主键)
- context_id
- status (working/completed/failed)
- artifacts (JSON)
- history (JSON)
- created_at
- updated_at

#### a2a_push_configs
存储 Push Notification 配置：
- task_id (主键)
- url (webhook URL)
- token
- authentication (加密存储)
- created_at
- updated_at

---

## 代码变更

### src/api/a2a_routes.py

#### 新增导入
```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from a2a.server.tasks import (
    DatabaseTaskStore,  # 替换 InMemoryTaskStore
    DatabasePushNotificationConfigStore,  # 替换 InMemoryPushNotificationConfigStore
)
```

#### 创建异步引擎
```python
# 在模块级别创建共享的异步引擎
async_connection_string = settings.POSTGRES_CONNECTION_STRING.replace(
    "postgresql://", "postgresql+asyncpg://"
)
async_engine = create_async_engine(
    async_connection_string,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
```

#### A2AServerManager 更新
```python
class A2AServerManager:
    def __init__(self):
        self.servers: Dict[str, A2AStarletteApplication] = {}
        self.apps: Dict[str, Any] = {}
        self.task_stores: Dict[str, TaskStore] = {}
        self.push_config_stores: Dict[str, DatabasePushNotificationConfigStore] = {}
        self.push_senders: Dict[str, BasePushNotificationSender] = {}
        self.httpx_client = httpx.AsyncClient(timeout=30.0)
        self.async_engine = async_engine  # 使用共享异步引擎
```

#### 创建数据库存储
```python
# 创建数据库 task store（共享表）
task_store = DatabaseTaskStore(
    engine=self.async_engine,
    create_table=True,  # 自动创建表
    table_name="a2a_tasks",  # 共享表
)

# 创建数据库 push config store（共享表）
push_config_store = DatabasePushNotificationConfigStore(
    engine=self.async_engine,
    create_table=True,  # 自动创建表
    table_name="a2a_push_configs",  # 共享表
    encryption_key=getattr(settings, 'A2A_ENCRYPTION_KEY', None),
)
```

---

## 优势

### 1. 数据持久化 ✅
- 任务信息不会因服务重启而丢失
- Push notification 配置持久化
- 支持任务历史查询

### 2. 可扩展性 ✅
- 支持多实例部署
- 共享任务状态
- 统一的数据存储

### 3. 可靠性 ✅
- 数据库事务保证
- 自动重连机制
- 连接池管理

### 4. 安全性 ✅
- Push配置支持加密存储
- 数据库级别的访问控制
- 审计日志支持

---

## 初始化

### 方法1: 自动初始化（推荐）
表会在首次使用时自动创建（`create_table=True`）。无需手动初始化。

### 方法2: 手动初始化
运行初始化脚本：

```bash
python init_a2a_tables_simple.py
```

输出示例：
```
======================================================================
A2A Database Tables Initialization
======================================================================
INFO:__main__:Connecting to database...
INFO:__main__:Database: localhost:5432/postgres

📋 Creating A2A database tables...
  - Creating a2a_tasks table...
    ✅ a2a_tasks table ready
  - Creating a2a_push_configs table...
    ✅ a2a_push_configs table ready

🔍 Verifying tables...

✅ A2A tables in database:
   • a2a_tasks (8 columns)
   • a2a_push_configs (6 columns)

🎉 A2A database initialization complete!
```

---

## 配置

### 环境变量

确保 `POSTGRES_CONNECTION_STRING` 已配置：

```env
POSTGRES_CONNECTION_STRING=postgresql://user:password@localhost:5432/dbname
```

### 可选配置

#### 加密密钥（用于 Push Config）
```env
A2A_ENCRYPTION_KEY=your-32-byte-encryption-key
```

如果不设置，Push配置将以明文存储（不推荐生产环境）。

---

## 数据库架构

### a2a_tasks 表结构
```sql
CREATE TABLE a2a_tasks (
    id VARCHAR PRIMARY KEY,           -- Task ID
    context_id VARCHAR,               -- Context ID
    status VARCHAR,                   -- Task status
    artifacts JSON,                   -- Task artifacts
    history JSON,                     -- Conversation history
    metadata JSON,                    -- Additional metadata
    created_at TIMESTAMP,             -- Creation time
    updated_at TIMESTAMP              -- Last update time
);

CREATE INDEX idx_a2a_tasks_context_id ON a2a_tasks(context_id);
CREATE INDEX idx_a2a_tasks_status ON a2a_tasks(status);
```

### a2a_push_configs 表结构
```sql
CREATE TABLE a2a_push_configs (
    task_id VARCHAR PRIMARY KEY,      -- Task ID
    url VARCHAR NOT NULL,             -- Webhook URL
    token VARCHAR,                    -- Optional token
    authentication BYTEA,             -- Encrypted auth info
    created_at TIMESTAMP,             -- Creation time
    updated_at TIMESTAMP              -- Last update time
);
```

---

## 性能考虑

### 连接池配置
```python
async_engine = create_async_engine(
    async_connection_string,
    pool_size=10,        # 基础连接数
    max_overflow=20,     # 最大溢出连接数
    pool_pre_ping=True,  # 连接健康检查
)
```

### 索引优化
- `context_id` 索引：快速查询特定会话的任务
- `status` 索引：快速查询特定状态的任务

### 缓存策略
- 服务器实例缓存（内存）
- ASGI app 缓存（内存）
- 数据库连接池（复用连接）

---

## 迁移检查清单

- [x] 替换 InMemoryTaskStore → DatabaseTaskStore
- [x] 替换 InMemoryPushNotificationConfigStore → DatabasePushNotificationConfigStore
- [x] 创建异步数据库引擎
- [x] 配置连接池
- [x] 添加表自动创建逻辑
- [x] 创建初始化脚本
- [x] 测试数据库连接
- [x] 验证表创建

---

## 测试验证

```bash
# 测试导入
python -c "from src.api.a2a_routes import async_engine, server_manager; print('OK')"

# 测试数据库连接
python -c "
from src.api.a2a_routes import async_engine
print('Engine:', async_engine.url)
"

# 初始化表
python init_a2a_tables_simple.py
```

---

## 回滚方案

如需回滚到内存存储：

```python
# 在 a2a_routes.py 中
from a2a.server.tasks import InMemoryTaskStore, InMemoryPushNotificationConfigStore

# 替换
task_store = InMemoryTaskStore()
push_config_store = InMemoryPushNotificationConfigStore()
```

---

## 依赖

### 新增依赖
- `asyncpg>=0.30.0` ✅ (已安装)
- `sqlalchemy[asyncio]>=2.0` ✅ (已安装)

### 现有依赖
- `a2a-sdk>=0.3.0` ✅
- `postgresql` 数据库 ✅

---

## 总结

✅ **完成**: 所有 InMemory 存储已迁移到数据库存储
✅ **持久化**: 任务和配置数据持久化到 PostgreSQL
✅ **性能**: 连接池和索引优化
✅ **安全**: 支持加密存储
✅ **可靠**: 自动重连和健康检查

迁移完成！🎉
