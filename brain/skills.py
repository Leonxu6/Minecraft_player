"""
技能工具库：每个 @tool 对应一个 Mineflayer 技能调用。

设计原则：
- 工具要通用（collect_block 靠 block_type 参数区分砍树/挖矿/挖石头）
- 工具返回描述性字符串，失败信息直接告诉 LLM 原因
- LLM 通过 function calling 选择调用哪个工具及参数
"""
from langchain_core.tools import tool
import aiohttp
from bot_client import call_skill, BOT_API


# ── 资源采集 ────────────────────────────────────

@tool
async def collect_block(block_type: str, count: int) -> str:
    """
    采集指定类型的方块（自动寻路 + 挖掘 + 拾取掉落物）。

    适用于所有需要"去拿某种方块"的场景：
    - 砍木头：block_type="oak_log" 或 "birch_log" / "spruce_log"
    - 挖石头：block_type="stone" 或 "cobblestone"
    - 挖煤矿：block_type="coal_ore" 或 "deepslate_coal_ore"
    - 挖铁矿：block_type="iron_ore" 或 "deepslate_iron_ore"
    - 挖金矿：block_type="gold_ore"
    - 挖钻石：block_type="diamond_ore" 或 "deepslate_diamond_ore"
    - 挖泥土/沙子/圆石等任意可挖掘方块

    block_type: Minecraft 方块 id（英文 snake_case）
    count: 需要采集的数量
    """
    result = await call_skill("collectBlock", {"type": block_type, "count": count})
    if result["status"] == "failed":
        return f"采集失败: {result['reason']}（提示：如果是 no_X_nearby，说明附近没有这种方块，需要先移动到合适位置）"
    out = result.get("output", {})
    return f"成功采集 {block_type} x{count}，背包中现有 {out.get('inventory_count', '?')} 个"


# ── 合成与制作 ────────────────────────────────────

@tool
async def place_crafting_table() -> str:
    """
    将背包中的合成台放置在脚下地面上。
    在调用 craft_item 之前，如果附近没有合成台，必须先调用此工具。
    """
    result = await call_skill("placeCraftingTable", {})
    if result["status"] == "failed":
        return f"放置失败: {result['reason']}（如果是 no_crafting_table_in_inventory，需要先 craft_item crafting_table）"
    return "合成台已放置在地面上，现在可以合成物品了"


@tool
async def craft_item(item: str, count: int = 1) -> str:
    """
    合成指定物品（需要合成台在附近 4 格内，且背包内有足够原材料）。

    常用 item 值：
    - 工具类：wooden_pickaxe, stone_pickaxe, iron_pickaxe, wooden_axe, wooden_sword, stone_sword, iron_sword
    - 基础物品：crafting_table, stick, torch, chest, furnace, wooden_planks
    - 盔甲类：leather_helmet, iron_chestplate 等
    - 食物类：bread（需要小麦）

    item: Minecraft 物品 id
    count: 合成数量
    """
    result = await call_skill("craft", {"item": item, "count": count})
    if result["status"] == "failed":
        return f"合成失败: {result['reason']}（常见原因：缺少材料、附近没有合成台、配方不存在）"
    return f"成功合成 {item} x{count}"


@tool
async def smelt_item(input_item: str, fuel: str, count: int = 1) -> str:
    """
    使用熔炉冶炼物品（需要熔炉在附近 4 格内）。

    常用组合：
    - 冶铁：input_item="raw_iron", fuel="coal"
    - 冶金：input_item="raw_gold", fuel="coal"
    - 烤食物：input_item="porkchop"/"beef"/"chicken", fuel="coal"
    - 烧沙子：input_item="sand", fuel="coal" → 得到玻璃
    - 烧圆石：input_item="cobblestone", fuel="coal" → 得到石头

    input_item: 原料 id
    fuel: 燃料 id（coal / charcoal / wooden_log 等）
    count: 冶炼数量
    """
    result = await call_skill("smelt", {"input": input_item, "fuel": fuel, "count": count})
    if result["status"] == "failed":
        return f"冶炼失败: {result['reason']}"
    return f"成功冶炼 {input_item} x{count}，燃料使用 {fuel}"


# ── 导航 ────────────────────────────────────

@tool
async def navigate_to_coords(x: int, y: int, z: int) -> str:
    """
    移动到指定世界坐标（x, y, z）。
    适合已知目标坐标时使用（如返回基地、去已知地点）。
    """
    result = await call_skill("navigateTo", {"x": x, "y": y, "z": z})
    if result["status"] == "failed":
        return f"导航失败: {result['reason']}"
    return f"已到达坐标 ({x}, {y}, {z})"


@tool
async def navigate_to_block(block_type: str) -> str:
    """
    寻找附近 64 格内的指定方块并移动过去（2 格内）。
    适合"去找合成台/熔炉/箱子"等需要靠近某个功能方块的场景。

    block_type: 方块 id，如 crafting_table / furnace / chest / bed
    """
    result = await call_skill("navigateToBlock", {"type": block_type})
    if result["status"] == "failed":
        return f"导航失败: {result['reason']}（如果找不到，说明附近没有这种方块）"
    out = result.get("output", {})
    return f"已靠近 {block_type}，位置 {out.get('position', '?')}"


# ── 战斗 ────────────────────────────────────

@tool
async def attack_mob(mob_type: str) -> str:
    """
    攻击附近 16 格内指定类型的生物，直到其死亡。

    常用 mob_type：
    - 敌对生物：zombie, skeleton, creeper, spider, witch, enderman, drowned, husk, stray, pillager
    - 动物（打猎）：cow, pig, chicken, sheep
    - Boss：wither, ender_dragon（不建议在初期尝试）

    mob_type: 生物 id
    """
    result = await call_skill("attackMob", {"type": mob_type})
    if result["status"] == "failed":
        return f"攻击失败: {result['reason']}（如果是 no_X_nearby，说明附近没有这种生物）"
    return f"成功击杀 {mob_type}"


# ── 建造与放置 ────────────────────────────────────

@tool
async def place_block(block_type: str, x: int, y: int, z: int) -> str:
    """
    在指定坐标放置一个方块（背包中需有该方块）。

    block_type: 方块 id，如 cobblestone / dirt / oak_planks / glass
    x, y, z: 放置位置的世界坐标
    """
    result = await call_skill("placeBlock", {"type": block_type, "x": x, "y": y, "z": z})
    if result["status"] == "failed":
        return f"放置失败: {result['reason']}"
    return f"已在 ({x},{y},{z}) 放置 {block_type}"


# ── 生存管理 ────────────────────────────────────

@tool
async def sleep_in_bed() -> str:
    """
    在附近 8 格内的床上睡觉以跳过夜晚。
    仅在夜晚且附近有床时有效。如果没有床，需要先合成床并放置。
    """
    result = await call_skill("sleep", {})
    if result["status"] == "failed":
        return f"睡觉失败: {result['reason']}"
    return "已跳过夜晚，现在是白天"


@tool
async def equip_item(item: str) -> str:
    """
    将背包中的指定物品装备到手上。
    在战斗前装备武器、挖矿前装备镐子时使用。

    item: 物品 id，如 iron_sword / diamond_pickaxe / bow
    """
    result = await call_skill("equipItem", {"item": item})
    if result["status"] == "failed":
        return f"装备失败: {result['reason']}"
    return f"已装备 {item}"


@tool
async def toss_item(item: str, count: int) -> str:
    """
    将背包中的指定物品丢弃（背包满时清理垃圾物品使用）。
    通常丢弃 dirt、gravel、cobblestone 等低价值方块。

    item: 物品 id
    count: 丢弃数量
    """
    result = await call_skill("tossItem", {"item": item, "count": count})
    if result["status"] == "failed":
        return f"丢弃失败: {result['reason']}"
    return f"已丢弃 {item} x{count}"


@tool
async def approach_entity(entity_type: str, max_distance: int = 32) -> str:
    """
    寻找并靠近指定类型的生物（默认 32 格范围内）。
    适合"去找牛/猪/羊"等需要靠近动物的场景，或靠近但不立即攻击的情况。

    entity_type: 生物 id，如 cow / sheep / villager
    max_distance: 搜索半径（格）
    """
    result = await call_skill("findAndApproach", {"type": entity_type, "max_distance": max_distance})
    if result["status"] == "failed":
        return f"靠近失败: {result['reason']}"
    out = result.get("output", {})
    return f"已靠近 {entity_type}，距离 {out.get('distance', '?')} 格"


# ── 配方查询 ────────────────────────────────────

@tool
async def check_recipe(item: str) -> str:
    """
    查询合成某个物品需要哪些材料、数量，以及是否需要合成台。
    在计划采集或合成之前调用，确认材料清单。

    item: 物品 id，如 iron_pickaxe / crafting_table / torch / wooden_planks
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BOT_API}/recipe/{item}") as resp:
            data = await resp.json()

    if not data.get("found"):
        return f"找不到 {item} 的合成配方：{data.get('error', '未知原因')}"

    ingredients = data.get("ingredients", {})
    ing_str = ", ".join(f"{k} x{v}" for k, v in ingredients.items())
    table_str = "需要合成台" if data["needs_table"] else "不需要合成台（手持合成）"
    count_str = f"每次合成产出 {data['result_count']} 个" if data["result_count"] > 1 else ""

    return f"{item}：{ing_str}。{table_str}。{count_str}".strip("。")


# ── 工具列表（供 game_graph.py 导入） ────────────────────────────────────

ALL_TOOLS = [
    check_recipe,
    collect_block,
    place_crafting_table,
    craft_item,
    smelt_item,
    navigate_to_coords,
    navigate_to_block,
    attack_mob,
    place_block,
    sleep_in_bed,
    equip_item,
    toss_item,
    approach_entity,
]
