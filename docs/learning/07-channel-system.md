# IM 渠道系统：多入口统一接入

> 源码目录：[backend/modules/channels/](backend/modules/channels/)
> 核心文件：`manager.py` (410行), `handler.py` (2270行), `base.py`
> 难度：⭐ 工程量大但概念直观
> 预计学习时间：1 天

---

## 📋 前置知识

- HTTP Webhook 机制（IM 平台把消息通过 HTTP 请求推给你的服务器）
- WebSocket 长连接（部分渠道需要主动建立连接）
- Supervisor 模式（监控子进程/任务，崩溃自动重启）
- 消息队列（生产者-消费者模式）

---

## 🎯 核心概念

### 这个模块解决什么问题？

不同 IM 平台的消息格式、认证方式、投递机制完全不同。Channel 层用**适配器模式**把它们统一为 `InboundMessage` 和 `OutboundMessage`，上层代码不需要关心消息是从微信还是 Telegram 来的。

```
八种渠道 → 统一模型

微信    ─┐
QQ      ─┤
Telegram─┤
钉钉    ─┼──→ InboundMessage ──→ AgentLoop ──→ OutboundMessage ──→ 对应渠道发出
飞书    ─┤    (统一格式)                    (统一格式)
企微    ─┤
微博    ─┤
小智AI  ─┘
```

### 核心设计

```
ChannelManager
├── 初始化：根据配置动态加载渠道类（注册表 + __import__）
├── 启动：每个渠道在独立的 asyncio Task 中运行
├── 监督：渠道崩溃后自动重启（指数退避 5s → 300s）
├── 路由：出站消息按 channel + account_id 找到正确的渠道实例
└── 多账号：同一渠道可运行多个机器人账号
```

---

## 🔍 关键代码路径

### 路径 1：动态加载 [manager.py:50-85]

```python
_CHANNEL_REGISTRY = {
    "telegram": ("backend.modules.channels.telegram", "TelegramChannel"),
    "qq":       ("backend.modules.channels.qq", "QQChannel"),
    "wechat":   ("backend.modules.channels.wechat", "WeChatChannel"),
    ...
}

def _init_channels(self):
    for name, (module_path, class_name) in _CHANNEL_REGISTRY.items():
        channel_cfg = getattr(channels_config, name, None)
        if not channel_cfg or not channel_cfg.enabled:
            continue  # 未启用的渠道直接跳过

        # 动态导入渠道类
        module = __import__(module_path, fromlist=[class_name])
        cls = getattr(module, class_name)

        # 为每个启用的 account 创建独立实例
        for instance_key, instance_cfg, account_id in instances:
            channel = cls(instance_cfg)
            self.channels[instance_key] = channel
```

**设计要点**：渠道是通过字符串注册表动态加载的，不是硬编码的 import。这样渠道的依赖缺失（如 `wechatpy` 没装）不会影响其他渠道。

### 路径 2：Supervisor 重启 [manager.py:223-256]

```python
async def _start_channel_supervised(self, name, channel):
    initial_backoff = 5    # 第一次重连等 5 秒
    max_backoff = 300      # 最长等 5 分钟
    backoff = initial_backoff

    while self._running:
        start_time = asyncio.get_event_loop().time()
        try:
            await channel.start()
            # 渠道正常运行...（阻塞在这里直到 crash）
        except Exception as e:
            logger.error(f"Channel {name} error: {e}")

        # 如果成功运行超过 60 秒，重置退避时间
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > 60:
            backoff = initial_backoff

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)  # 指数增长
```

**为什么 60 秒是分界线**：如果渠道稳定运行超过 60 秒后 crash，说明不是配置问题而是偶发网络问题，重置退避时间可以快速重试。反之，如果启动后立即 crash，说明可能是配置错误，逐步拉长重试间隔避免刷日志。

### 路径 3：多账号支持 [manager.py:91-190]

同一渠道可以配置多个账号（比如公司有两个飞书机器人：客服机器人和内部通知机器人）。每个账号有独立的 `instance_key = "{channel_name}:{account_id}"`，消息路由时根据 `account_id` 找到正确的实例。

**去重检查**：通过 `unique_field_map`（如 Telegram 的 token、微信的 login_bot_id）检测同一个物理机器人是否被配置了多次。

### 路径 4：消息总线 [manager.py:262-287]

```
入站: Channel._on_message → ChannelManager._on_inbound_message → bus.publish_inbound(msg)
出站: bus.consume_outbound() → ChannelManager._dispatch_outbound → channel.send(msg)
```

使用企业消息队列（`EnterpriseMessageQueue`）解耦渠道层和 Agent 层。渠道不需要知道 Agent 怎么处理消息，Agent 不需要知道消息发到哪个渠道。

---

## 💡 可深挖的逻辑

### 深度点 1：handler.py 的 2270 行

这是整个项目中**最大的单文件**。它处理：
- 命令识别（`/new`, `/m`, `/team` 等）
- 消息预处理（去掉 @mention、清理格式）
- 会话管理（获取/创建 Session）
- Agent 调度（创建 AgentLoop → 流式输出 → 回传渠道）

**阅读建议**：从 `handle_message()` 入口开始追踪，理解一次完整的消息处理流程。不用逐行读，理解主要分支即可。

### 深度点 2：渠道实例的去重签名

`build_signature()` 方法为每个渠道配置生成唯一的物理签名（如 telegram 用 token、wechat 用 login_bot_id）。同一个物理机器人被配置了两次时，第二次会被跳过并打 warning 日志。

---

## 🎤 面试话术

### 1 分钟版

> CountBot 支持 8 种 IM 渠道的统一接入。核心是适配器模式——每个渠道实现统一的 `BaseChannel` 接口，ChannelManager 通过注册表动态加载。渠道在独立的 supervisor 任务中运行，崩溃后指数退避自动重连。支持同一渠道运行多个机器人账号，通过消息总线解耦渠道层和 Agent 层。

---

## ✏️ 练习建议

### 练习 1：创建一个 Telegram Bot（1 小时）
1. 在 Telegram 创建 Bot（通过 @BotFather 获取 token）
2. 在 CountBot 配置中启用 Telegram 渠道
3. 给 Bot 发消息，观察整个流程

### 练习 2：追踪消息处理全流程（1 小时）
从 `_on_inbound_message` 开始，加日志追踪一条消息从进来到 AgentLoop 处理完毕返回给渠道的完整路径。画出时序图。

### 练习 3：测试 Supervisor 重启（30 分钟）
故意让一个渠道 crash（比如提供无效的 API token），观察日志中的重启行为和退避时间变化。
