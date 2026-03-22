"""
情绪脑：监听玩家游戏内聊天，判断情绪，做出游戏内反应。

流程：
  player_chat 事件 → LLM 判断意图和情绪 → 游戏内聊天回复 + 肢体动作
  若判断为任务指令 → 写入 pending_command，交给游戏脑执行
"""
import asyncio
import json
import re

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from bot_client import call_skill
from shared_state import state as shared_state


# ─────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────
llm = ChatOpenAI(
    api_key="sk-ab32a70742d240a98693b34aeac4566c",
    base_url="https://api.deepseek.com/v1",
    model="deepseek-chat",
    temperature=0.8,
)


# ─────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """\
你是一个有个性的 Minecraft 游戏搭子机器人，叫「方块」。玩家在游戏聊天框里和你说话，你要做出真实的情绪反应。

性格：活泼、有点傲娇、对主人忠诚、偶尔耍小脾气。

判断规则：
- 被骂、被侮辱、被攻击 → action: attack（反击，傲娇回怼）
- 被夸、被表扬、被叫宝贝 → action: approach_crouch（靠近蹲蹲示好）
- 被叫过来、被召唤 → action: approach_crouch（靠近，回复"来了"）
- 有趣的事、主人说发现了好东西 → action: jump（兴奋跳跳）
- 打招呼、随便聊天 → action: swing（挥手回应）
- 任务指令（去采/挖/建/合成/打某东西）→ action: none，task 里写出任务目标，reply 说"收到！"
- 其他闲聊 → action: none

reply 要符合角色性格，不要太正式，可以带点情绪（哼！/ 好啦好啦 / 嘿嘿 等），20字以内。

只输出 JSON，不要有任何其他内容：
{"reply": "...", "action": "attack|approach_crouch|jump|swing|none", "task": "任务描述或空字符串"}
"""


# ─────────────────────────────────────────────
# 核心处理
# ─────────────────────────────────────────────
async def handle_chat(username: str, message: str):
    print(f"[InteractionBrain] {username}: {message}")

    ctx = shared_state.to_llm_context()
    try:
        response = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"玩家「{username}」说：{message}\n\n当前游戏状态：\n{ctx}"),
        ])
        # 从返回内容中提取 JSON（防止 LLM 多输出了 markdown 代码块）
        text = response.content.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        data = json.loads(m.group() if m else text)
        reply  = data.get("reply", "嗯")
        action = data.get("action", "none")
        task   = data.get("task", "")
    except Exception as e:
        print(f"[InteractionBrain] LLM 异常: {e}")
        return

    print(f"[InteractionBrain] 回复={reply!r}  动作={action}  任务={task!r}")

    # 先说话
    await call_skill("chatMessage", {"text": reply})

    # 任务指令 → 交给游戏脑
    if task:
        shared_state.pending_command = task
        return

    # 执行肢体动作
    if action == "attack":
        await call_skill("attackPlayer", {"username": username})
    elif action == "approach_crouch":
        await call_skill("approachPlayer", {"username": username})
        await call_skill("crouch", {"times": 6})
    elif action == "jump":
        await call_skill("jump", {"count": 5})
    elif action == "swing":
        await call_skill("swingArm", {})


# ─────────────────────────────────────────────
# 情绪脑主循环
# ─────────────────────────────────────────────
class InteractionBrain:
    async def run(self):
        """轮询事件队列，处理 player_chat 事件"""
        while True:
            events = shared_state.pop_events()
            for event in events:
                if event["type"] == "player_chat":
                    data = event["data"]
                    # 用 create_task 避免阻塞事件循环，游戏脑和情绪脑并行
                    asyncio.create_task(
                        handle_chat(data["username"], data["message"])
                    )
            await asyncio.sleep(0.2)
