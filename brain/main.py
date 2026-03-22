"""
入口：启动游戏 AI + 情绪脑
"""
import asyncio
from dotenv import load_dotenv
load_dotenv()  # 读取 brain/.env
from bot_client import state_listener, ping
from game_graph import GameBrain
from interaction_brain import InteractionBrain


async def wait_for_bot(retries: int = 10):
    for i in range(retries):
        if await ping():
            print("[Main] Bot Server 已就绪")
            return
        print(f"[Main] 等待 Bot Server... ({i+1}/{retries})")
        await asyncio.sleep(2)
    raise RuntimeError("Bot Server 未响应，请先启动 node bot_server.js")


async def main():
    await wait_for_bot()
    await asyncio.gather(
        state_listener(),
        GameBrain().run(),
        InteractionBrain().run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
