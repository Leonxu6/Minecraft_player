# Minecraft 直播智能体架构设计

## 核心设计原则

- LLM 只做"选什么"，不做"怎么做"
- 确定性的执行顺序交给队列，不重复问 LLM
- 能用现有开源项目解决的不自己造轮子
- 能查表解决的不走 LLM

---

## 确认的技术选型

| 模块 | 方案 | 原因 |
|------|------|------|
| 游戏控制 | Node.js + Mineflayer 插件 | 唯一选择，插件生态完整 |
| 虚拟角色 + 语音 + 表情 | Open-LLM-VTuber | 开源，含 Live2D/TTS/LLM/情绪全套 |
| 互动大脑 LLM | Open-LLM-VTuber 内置 | 直接复用，支持 Claude |
| 游戏大脑 LLM | Claude API (Python 直调) | 规划任务用 Sonnet，快速响应用 Haiku |
| 弹幕接收 | bilibili-api / blivedm (Python) | B站官方协议的 WebSocket 封装 |
| 游戏服务器 | 自建 Spigot/Paper（关反作弊） | 自主可控，Mineflayer 无限制 |

---

## 总体架构（三个进程）

```
┌──────────────────────────────────────────────────────────────────┐
│                        [Minecraft Server]                         │
│                      自建 Spigot/Paper                            │
└─────────────────────────┬────────────────────────────────────────┘
                          │ MC 协议
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│              进程 1：Node.js — Mineflayer Bot                     │
│                                                                   │
│  后台守护（启动即运行）                                             │
│  ├─ mineflayer-auto-eat        饥饿自动吃，不进任务队列             │
│  ├─ mineflayer-armor-manager   有更好盔甲自动换，不进任务队列        │
│  └─ mineflayer-tool            挖矿自动选最佳工具                   │
│                                                                   │
│  技能执行层（接收 Python 指令）                                     │
│  ├─ collectBlock  →  mineflayer-collectblock（找+走+挖+收一体）    │
│  ├─ attack        →  mineflayer-pvp                               │
│  ├─ navigate      →  mineflayer-pathfinder                        │
│  └─ craft/smelt/place  →  mineflayer 原生 API                     │
│                                                                   │
│  对外接口                                                          │
│  ├─ POST /skill  接收技能调用，同步返回结果                         │
│  └─ WebSocket /state  每 500ms 推送游戏状态                        │
└──────────────────────┬───────────────────────────────────────────┘
                       │ REST + WebSocket
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│              进程 2：Python — 游戏大脑 + 协调层                    │
│                                                                   │
│  Game Brain                                                       │
│  ├─ 接收 Mineflayer 状态 → 维护 Shared State                      │
│  ├─ 监听关键事件 → 推送给 Open-LLM-VTuber（情绪触发）              │
│  ├─ LLM Planner：goal → Task Queue（一次性规划）                   │
│  ├─ Task Queue：顺序弹出执行，不重复问 LLM                         │
│  └─ Skill Caller：POST /skill → Node.js                          │
│                                                                   │
│  协调层                                                            │
│  ├─ bilibili-api 接收弹幕                                          │
│  ├─ 弹幕分类：游戏命令 → pending_command；闲聊 → 转发给 VTuber     │
│  └─ MCP Server：暴露 get_game_state() 给 Open-LLM-VTuber         │
└──────────────────────┬───────────────────────────────────────────┘
                       │ WebSocket（发消息）+ MCP（提供工具）
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│         进程 3：Open-LLM-VTuber（互动大脑 + 虚拟角色）             │
│                                                                   │
│  输入                                                              │
│  ├─ 弹幕消息（Python 转发过来）                                    │
│  ├─ 游戏事件通知（Python 推送，如"发现钻石"）                       │
│  └─ MCP 工具：get_game_state() → 返回当前血量/背包/正在做什么      │
│                                                                   │
│  处理                                                              │
│  ├─ LLM（Claude）生成回复，内嵌情绪标签 [joy] [scared] 等          │
│  └─ 情绪标签自动驱动 Live2D 表情                                   │
│                                                                   │
│  输出                                                              │
│  ├─ TTS → 语音播报                                                 │
│  └─ Live2D → 虚拟角色表情/动作                                     │
└──────────────────────────────────────────────────────────────────┘
```

---

## 关键数据流

### 弹幕流

```
B站直播间弹幕
    ↓ bilibili-api WebSocket
Python 弹幕分类器（本地规则）
    ├─ 闲聊类  →  直接转发到 Open-LLM-VTuber WebSocket
    └─ 命令类  →  写入 shared_state.pending_command
                  + 发通知给 VTuber："好的我去做！"
```

### 游戏事件流

```
Mineflayer 事件（血量低/发现钻石/苦力怕近身/死亡）
    ↓ Node.js 监听，WebSocket 推给 Python
Python 事件处理器
    └─ 发消息给 Open-LLM-VTuber WebSocket
       例："[游戏事件] 发现了钻石！"
           "[游戏事件] 苦力怕距离 6 米，正在逃跑"
    → VTuber LLM 自然反应，带上 [joy] [scared] 等标签
    → Live2D 自动切换表情
```

### 游戏上下文注入

```
Open-LLM-VTuber 的 LLM 调用 MCP 工具 get_game_state()
    ↓ Python MCP Server 返回
{
  "doing": "正在砍树，还需要 12 根木头",
  "health": "16/20",
  "inventory": "oak_log x8, stone_sword x1",
  "goal": "build a house"
}
    → LLM 在回答弹幕时自然融入游戏上下文
      "我现在在砍树，砍完木头就去盖房子！"
```

### 游戏规划流

```
当前目标（初始 or 来自弹幕命令）
    ↓
Game Brain LLM Planner（Claude Sonnet）
输入: goal + inventory + nearby + last_failure
输出: ["collectBlock(oak_log,20)", "craft(crafting_table)", ...]
    ↓
Task Queue 顺序弹出
    ↓
POST /skill → Node.js 执行
    ↓
返回 {status, reason, output}
    ↓
success → 继续队列下一个
failed  → 清空队列，下次循环触发重新规划（带失败原因）
```

---

## Game Brain 核心逻辑

```python
class GameBrain:
    def __init__(self):
        self.queue: list = []
        self.goal = "survive and explore"
        self.last_failure = None

    def run(self):
        while True:
            # 优先检查观众命令
            if shared_state.pending_command:
                self.goal = shared_state.pending_command
                shared_state.pending_command = None
                self.queue.clear()

            # 队列空则重新规划
            if not self.queue:
                self.replan()

            skill = self.queue.pop(0)
            result = bot_client.call_skill(skill)  # 同步，等完成

            if result["status"] == "failed":
                self.last_failure = result["reason"]
                self.queue.clear()
                # 推送事件给 VTuber
                vtuber_client.send_event(f"任务失败：{result['reason']}")

    def replan(self):
        context = build_context(shared_state, self.goal, self.last_failure)
        # LLM 输出固定格式：技能调用列表
        self.queue = llm_plan(context)
        self.last_failure = None
        # 告知 VTuber 新计划
        vtuber_client.send_event(f"新计划：{self.goal}，步骤 {len(self.queue)} 步")
```

---

## Node.js 技能库（薄封装，核心靠插件）

```javascript
const SKILLS = {
  collectBlock: async (bot, { type, count }) => {
    const blocks = bot.findBlocks({ matching: mcData.blocksByName[type].id, count })
    await bot.collectBlock.collect(blocks)
    return { collected: count }
  },
  navigateTo: async (bot, { x, y, z }) => {
    await bot.pathfinder.goto(new GoalBlock(x, y, z))
  },
  attackMob: async (bot, { type }) => {
    const entity = Object.values(bot.entities).find(e => e.name === type)
    if (!entity) throw new Error(`no_${type}_nearby`)
    bot.pvp.attack(entity)
    await once(bot, 'stoppedAttacking')
  },
  craft: async (bot, { item, count = 1 }) => {
    const recipe = bot.recipesFor(mcData.itemsByName[item].id)[0]
    if (!recipe) throw new Error(`no_recipe_for_${item}`)
    await bot.craft(recipe, count, bot.findBlock({ matching: mcData.blocksByName['crafting_table'].id }))
  },
  // ... 其余技能同样是薄封装
}

// 统一路由 + 错误处理
app.post('/skill', async (req, res) => {
  const { name, args } = req.body
  try {
    const output = await SKILLS[name](bot, args)
    res.json({ status: 'success', output })
  } catch (e) {
    res.json({ status: 'failed', reason: e.message })
  }
})
```

---

## Open-LLM-VTuber 配置要点

```yaml
# conf.yaml 关键配置
persona_prompt: |
  你是一个正在直播玩 Minecraft 的可爱虚拟主播。
  你有自己的性格和情绪，会对游戏事件做出真实反应。
  当观众发弹幕时你会回应，同时继续关注游戏。
  你可以通过 get_game_state 工具了解当前游戏状态。

# MCP 工具配置（接入我们的 Python MCP Server）
mcp_servers:
  - name: minecraft_state
    url: "http://localhost:8765"

# 情绪映射（Live2D 表情 ID）
emotion_map:
  joy: 0
  scared: 1
  sad: 2
  surprised: 3
  neutral: 4
  angry: 5
```

---

## Mineflayer 能力边界总结

### 插件直接覆盖（无需自定义）

| 需求 | 插件 |
|------|------|
| 寻路 | mineflayer-pathfinder（A*，绕障、游泳、跳跃） |
| 挖矿+收集（一体） | mineflayer-collectblock |
| 战斗 | mineflayer-pvp |
| 自动吃饭 | mineflayer-auto-eat（后台守护） |
| 自动换盔甲 | mineflayer-armor-manager（后台守护） |
| 自动选工具 | mineflayer-tool（collectblock 内部调用） |

### 可读取的游戏状态

```
bot.health / bot.food / bot.oxygenLevel
bot.inventory          → 完整背包
bot.entities           → 视野内所有实体（类型/位置/血量）
bot.findBlocks(opts)   → 范围内查找指定方块
bot.time.timeOfDay     → 游戏时间（判断白天/夜晚）
bot.game.dimension     → 当前维度
```

### 硬限制

- 反作弊服务器（Grim/Vulcan）不兼容 → 已解决：自建服务器
- 无法获取渲染画面 → 用 `findBlocks` + `entities` 代替视觉感知

---

## 能实现的最终效果

1. **自主玩游戏**：Bot 根据 LLM 规划自动完成生存任务（砍树、挖矿、盖房、战斗），无需人工干预
2. **响应弹幕**：观众发弹幕，VTuber 实时语音回应，Live2D 有表情变化
3. **弹幕影响游戏**：观众说"去找钻石"，Bot 完成当前任务后切换目标，VTuber 播报计划
4. **游戏事件驱动情绪**：找到钻石 → 惊喜表情+欢呼；苦力怕近身 → 害怕表情+喊叫；死亡 → 悲伤
5. **游戏状态融入对话**：观众问"你在干嘛"，VTuber 通过 MCP 获取状态后自然回答
6. **全自动存活**：吃饭、换盔甲、选工具全部自动，不需要规划介入

---

## 待实现的模块清单

### 进程 1：Node.js Bot Server
- [ ] bot_server.js：Mineflayer + 插件加载 + Express HTTP
- [ ] skill_router.js：技能路由（collectBlock/attack/navigate/craft/smelt/place）
- [ ] state_pusher.js：WebSocket 推送游戏状态 + 事件

### 进程 2：Python 协调层
- [ ] bot_client.py：HTTP client → Node.js
- [ ] state_manager.py：Shared State 维护 + 序列化
- [ ] game_brain.py：Task Queue + LLM Planner
- [ ] danmaku_listener.py：bilibili-api 接收弹幕 + 分类
- [ ] vtuber_client.py：向 Open-LLM-VTuber WebSocket 发消息
- [ ] mcp_server.py：MCP Server 暴露 get_game_state()

### 进程 3：Open-LLM-VTuber
- [ ] conf.yaml：角色配置 + MCP 工具配置
- [ ] 调试验证情绪标签和 Live2D 表情对应关系
