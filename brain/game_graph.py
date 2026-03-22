"""
Game Brain：LangGraph ReAct 架构
- LLM 通过 function calling 直接调用技能
- 每次 LLM 节点执行时注入最新游戏状态（动态，不是快照）
- 图执行直到 LLM 不再调用工具（目标完成）
"""
import asyncio
import os
from typing import Annotated

from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from typing_extensions import TypedDict

from shared_state import state as shared_state
from skills import ALL_TOOLS

# ─────────────────────────────────────────────
# LLM：DeepSeek（OpenAI 兼容）
# ─────────────────────────────────────────────
llm = ChatOpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com/v1",
    model="deepseek-chat",
    temperature=0.1,
    max_retries=2,
)

# parallel_tool_calls=False：游戏动作必须串行（挖矿完才能合成），禁止并行
llm_with_tools = llm.bind_tools(ALL_TOOLS, parallel_tool_calls=False)

# ─────────────────────────────────────────────
# LangGraph State
# ─────────────────────────────────────────────
class GameBrainState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    goal: str


# ─────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """\
你是一个 Minecraft 游戏 AI，负责自主完成给定的游戏目标。

行动原则：
1. 需要合成某样东西前，先用 check_recipe 查清楚需要什么材料
2. 确认背包里有足够材料后再合成，没有就先 collect_block
3. 工具失败时分析错误原因，换方式重试，不要重复相同的失败操作
4. 目标完成后回复"目标完成：[说明]"；多次尝试仍无法完成才回复"无法完成：[原因]"

资源不在附近时：
- 错误含 no_X_nearby 说明 32 格内没有，需要先探索
- 用 navigate_to_coords 从当前 position 向 x 或 z 偏移 100~200 格，探索后再试
- 同类资源可以换种类，例如找不到 oak_log 就试 birch_log / spruce_log

背包与合成台：
- 需要合成台的配方：先 craft_item(crafting_table) → place_crafting_table() → 再合成
- 背包满了先 toss_item 丢低价值物品（cobblestone / dirt / gravel）
"""


# ─────────────────────────────────────────────
# 图节点
# ─────────────────────────────────────────────

# 最多保留的历史消息条数（防止上下文膨胀超出 token 限制）
MAX_HISTORY = 20

async def llm_node(state: GameBrainState) -> dict:
    """
    调用 LLM。每次都注入最新的游戏状态（动态，不用快照）。
    System Message 包含角色设定 + 实时游戏状态。
    消息历史超过 MAX_HISTORY 条时只保留最近的，防止 token 溢出。
    """
    game_ctx = shared_state.to_llm_context()
    system = SystemMessage(content=f"{SYSTEM_PROMPT}\n\n【当前游戏状态】\n{game_ctx}")

    # 只保留最近 MAX_HISTORY 条，截断旧消息
    messages = state["messages"][-MAX_HISTORY:]
    response = await llm_with_tools.ainvoke([system] + messages)

    # 打印 LLM 的每步决策
    if response.tool_calls:
        for tc in response.tool_calls:
            print(f"[LLM→] 调用 {tc['name']}({tc['args']})")
    elif response.content:
        print(f"[LLM→] {response.content}")

    return {"messages": [response]}


# handle_tool_errors=True：工具抛出异常时返回错误信息给 LLM，而不是崩溃整个图
tool_node = ToolNode(ALL_TOOLS, handle_tool_errors=True)


# ─────────────────────────────────────────────
# 构建图
# ─────────────────────────────────────────────

def build_graph():
    graph = StateGraph(GameBrainState)
    graph.add_node("llm", llm_node)
    graph.add_node("tools", tool_node)

    graph.add_edge(START, "llm")
    # LLM 有 tool_call → 去 tools 节点；没有 → 结束
    graph.add_conditional_edges("llm", tools_condition)
    # 工具执行完 → 回到 LLM 决定下一步
    graph.add_edge("tools", "llm")

    return graph.compile()


GRAPH = build_graph()


# ─────────────────────────────────────────────
# Game Brain 主循环
# ─────────────────────────────────────────────

class GameBrain:
    async def run(self):
        while True:
            # 检查是否有新命令
            if shared_state.pending_command:
                shared_state.current_goal = shared_state.pending_command
                shared_state.pending_command = None
                shared_state.last_failure = None
                print(f"\n[GameBrain] 切换目标: {shared_state.current_goal}")

            goal = shared_state.current_goal
            print(f"\n[GameBrain] 开始执行目标: {goal}")

            try:
                await self._run_goal(goal)
            except Exception as e:
                print(f"[GameBrain] 图执行异常: {type(e).__name__}: {e}")
                shared_state.last_failure = str(e)
                await asyncio.sleep(3)

    async def _run_goal(self, goal: str):
        """运行一个完整的目标，直到 LLM 停止调用工具"""
        initial_state: GameBrainState = {
            "messages": [HumanMessage(content=f"请完成目标：{goal}")],
            "goal": goal,
        }

        final_message = ""
        async for event in GRAPH.astream(initial_state, config={"recursion_limit": 25}):
            for node_name, node_output in event.items():
                if node_name == "tools":
                    for msg in node_output.get("messages", []):
                        tool_name = getattr(msg, "name", "")
                        content = getattr(msg, "content", "")
                        print(f"[Tool:{tool_name}] {content}")

                elif node_name == "llm":
                    for msg in node_output.get("messages", []):
                        content = getattr(msg, "content", "")
                        if content and not getattr(msg, "tool_calls", None):
                            final_message = content
                            print(f"[GameBrain] LLM 总结: {content}")

        # 目标结束
        if final_message.startswith("目标完成"):
            print(f"[GameBrain] 完成: {final_message}")
        elif final_message.startswith("无法完成"):
            shared_state.last_failure = final_message
            print(f"[GameBrain] 失败: {final_message}")
