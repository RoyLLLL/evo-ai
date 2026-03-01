# A2A Agent 架构说明

## 当前架构

### 服务器端（已使用 SDK 0.3.24）✅

**文件**: `src/api/a2a_routes.py`

- 使用 `a2a.server.apps.A2AStarletteApplication`
- 使用 `a2a.server.request_handlers.DefaultRequestHandler`
- 使用 `a2a.server.agent_execution.AgentExecutor`
- 使用 `DatabaseTaskStore` 和 `DatabasePushNotificationConfigStore`
- **完全基于 SDK 0.3.24**

### 客户端（HTTP 客户端）

**文件**:
- `src/services/adk/custom_agents/a2a_agent.py` - ADK 自定义 agent
- `src/utils/a2a_enhanced_client.py` - 增强型 A2A 客户端

**当前实现**:
- 使用 `httpx.AsyncClient` 直接发送 HTTP 请求
- 支持两种实现：custom 和 SDK
- 自动检测并选择最佳实现
- 发送 JSON-RPC 2.0 格式的请求

**为什么不直接使用 SDK 的 A2AClient**:
1. SDK 的 `A2AClient` 在 0.3.0 版本中 API 发生了变化
2. `EnhancedA2AClient` 需要支持自定义实现和 SDK 实现的切换
3. 直接使用 HTTP 更灵活，可以处理两种实现
4. 服务器端已经使用 SDK，客户端只需要发送正确格式的请求

---

## 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    ADK Custom Agent                         │
│              (a2a_agent.py)                                 │
│                                                             │
│  - 使用 EnhancedA2AClient                                   │
│  - 自动检测实现类型                                          │
│  - 支持流式和非流式                                          │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   │ HTTP Request (JSON-RPC 2.0)
                   │
                   ▼
┌─────────────────────────────────────────────────────────────┐
│              EnhancedA2AClient                              │
│           (a2a_enhanced_client.py)                          │
│                                                             │
│  - 检测可用实现 (custom/SDK)                                │
│  - 选择最佳实现                                              │
│  - 发送 HTTP 请求                                           │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   │ POST /api/v1/a2a/{agent_id}
                   │ (JSON-RPC 2.0: message/send, message/stream)
                   │
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                  A2A Routes (FastAPI)                       │
│                  (a2a_routes.py)                            │
│                                                             │
│  ✅ 使用 a2a-sdk 0.3.24                                     │
│  ✅ A2AStarletteApplication                                 │
│  ✅ DefaultRequestHandler                                   │
│  ✅ DatabaseTaskStore                                       │
│  ✅ DatabasePushNotificationConfigStore                     │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   │ 调用 EvoAIAgentExecutor
                   │
                   ▼
┌─────────────────────────────────────────────────────────────┐
│            EvoAIAgentExecutor                               │
│         (实现 AgentExecutor 接口)                            │
│                                                             │
│  - 提取消息和文件                                            │
│  - 调用 agent_runner                                        │
│  - 处理流式响应                                              │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   │ 调用 run_agent / run_agent_stream
                   │
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                  Agent Runner                               │
│            (agent_runner.py)                                │
│                                                             │
│  - 执行实际的 agent 逻辑                                     │
│  - 集成 session_service                                     │
│  - 集成 artifacts_service                                   │
│  - 集成 memory_service                                      │
└─────────────────────────────────────────────────────────────┘
```

---

## 数据流

### 1. 客户端发送消息

```python
# a2a_agent.py
async with EnhancedA2AClient(config) as client:
    response = await client.send_message(
        agent_id=agent_id,
        message="Hello",
        session_id=session_id
    )
```

### 2. EnhancedA2AClient 发送 HTTP 请求

```python
# a2a_enhanced_client.py
request_data = {
    "jsonrpc": "2.0",
    "id": str(uuid4()),
    "method": "message/send",
    "params": {
        "message": {
            "role": "user",
            "parts": [{"type": "text", "text": message}],
            "messageId": message_id
        }
    }
}

response = await self.httpx_client.post(url, json=request_data)
```

### 3. A2A Routes 处理请求（使用 SDK）

```python
# a2a_routes.py
# SDK 自动处理 JSON-RPC 请求
# DefaultRequestHandler 调用 EvoAIAgentExecutor
```

### 4. EvoAIAgentExecutor 执行

```python
# EvoAIAgentExecutor.execute()
result = await run_agent(
    agent_id=str(self.agent_id),
    external_id=context_id,
    message=text,
    session_service=session_service,
    artifacts_service=artifacts_service,
    memory_service=memory_service,
    db=self.db,
    files=files
)
```

### 5. 返回响应

```
Agent Runner → EvoAIAgentExecutor → SDK → HTTP Response → EnhancedA2AClient → a2a_agent.py
```

---

## 为什么这个架构是最优的

### ✅ 优点

1. **服务器端完全使用 SDK**
   - 标准化的实现
   - 自动获得 SDK 更新
   - 完整的 A2A 协议支持

2. **客户端灵活性**
   - 支持多种实现
   - 自动检测和切换
   - 向后兼容

3. **清晰的职责分离**
   - 服务器端：协议处理（SDK）
   - 客户端：HTTP 通信（httpx）
   - Agent：业务逻辑（agent_runner）

4. **易于维护**
   - 服务器端升级 SDK 不影响客户端
   - 客户端可以连接到任何 A2A 服务器
   - 解耦的架构

---

## 是否需要更新客户端？

### 当前状态：✅ 不需要

**原因**:
1. 客户端使用 HTTP 直接通信，这是标准的 A2A 协议方式
2. 服务器端已经使用 SDK 0.3.24，完全符合规范
3. 客户端发送的请求格式正确（JSON-RPC 2.0）
4. `EnhancedA2AClient` 提供了很好的抽象和错误处理

### 如果要使用 SDK 的 A2AClient

如果你坚持要在客户端也使用 SDK 的 `A2AClient` 类，需要：

1. **更新 EnhancedA2AClient**
   ```python
   from a2a.client import A2AClient

   # 创建 SDK 客户端
   sdk_client = A2AClient(
       httpx_client=self.httpx_client,
       agent_card=agent_card,
       url=agent_url
   )

   # 使用 SDK 客户端发送消息
   response = await sdk_client.send_message(...)
   ```

2. **问题**:
   - SDK 0.3.0 的 `A2AClient` API 可能不稳定
   - 需要处理 agent card 的获取
   - 失去了自定义实现的支持
   - 更复杂的错误处理

---

## 建议

### ✅ 保持当前实现

**推荐保持当前的架构**，因为：

1. 服务器端已经完全使用 SDK 0.3.24 ✅
2. 客户端使用 HTTP 是标准做法 ✅
3. 架构清晰，职责分离 ✅
4. 易于维护和扩展 ✅

### 如果需要更新

只有在以下情况下才需要更新客户端：

1. SDK 的 `A2AClient` 提供了重要的新功能
2. 需要使用 SDK 的高级特性（如拦截器、中间件）
3. 官方推荐使用 SDK 客户端而不是直接 HTTP

---

## 总结

✅ **服务器端**: 完全使用 a2a-sdk 0.3.24
✅ **客户端**: 使用 HTTP (httpx) 发送标准 A2A 请求
✅ **架构**: 清晰、解耦、易维护
✅ **建议**: 保持当前实现，无需更新客户端

当前的实现已经是最佳实践！🎉
