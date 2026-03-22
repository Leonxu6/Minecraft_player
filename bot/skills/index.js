const { goals: { GoalBlock, GoalNear, GoalFollow } } = require('mineflayer-pathfinder')

// mcData 每个 bot 实例缓存一次，避免反复创建大对象
const mcDataCache = {}
function getMcData(bot) {
  if (!mcDataCache[bot.version]) {
    mcDataCache[bot.version] = require('minecraft-data')(bot.version)
  }
  return mcDataCache[bot.version]
}

// ─────────────────────────────────────────────
// 技能路由：LLM 只能调用这里列出的技能
// 每个技能接收 (bot, args)，返回 output 或 throw Error
// ─────────────────────────────────────────────

const SKILLS = {

  // 收集方块（寻路+挖掘+捡起，collectblock 插件一体处理）
  // args: { type: "oak_log", count: 20 }
  collectBlock: async (bot, { type, count = 1 }) => {
    const mcData = getMcData(bot)
    const block = mcData.blocksByName[type]
    if (!block) throw new Error(`unknown_block_type:${type}`)

    // maxDistance 32 而非 64：减少 pathfinder A* 节点数，避免 OOM
    const positions = bot.findBlocks({
      matching: block.id,
      maxDistance: 32,
      count: count + 2
    })
    if (positions.length === 0) throw new Error(`no_${type}_nearby`)

    // 逐个收集，避免 pathfinder 同时规划多条路径占用大量内存
    let done = 0
    for (const pos of positions.slice(0, count)) {
      const target = bot.blockAt(pos)
      if (!target) continue
      try {
        await bot.collectBlock.collect(target, { ignoreNoPath: true })
        done++
        if (done >= count) break
      } catch (e) {
        // 单个方块失败不中止，继续下一个
      }
    }

    const invCount = bot.inventory.items()
      .filter(i => i.name === type || i.name === type.replace('_ore', '') || i.name === 'cobblestone' && type === 'stone')
      .reduce((sum, i) => sum + i.count, 0)
    return { collected_type: type, inventory_count: invCount }
  },

  // 移动到指定坐标
  // args: { x: 100, y: 64, z: -200 }
  // y 只作参考，GoalNear 允许 ±3 格误差，避免精确坐标找不到路径
  navigateTo: async (bot, { x, y, z }) => {
    await bot.pathfinder.goto(new GoalNear(x, y, z, 3))
    const p = bot.entity.position
    return { arrived_at: { x: Math.round(p.x), y: Math.round(p.y), z: Math.round(p.z) } }
  },

  // 移动到目标方块附近（2格内）
  // args: { type: "crafting_table" }
  navigateToBlock: async (bot, { type }) => {
    const mcData = getMcData(bot)
    const block = mcData.blocksByName[type]
    if (!block) throw new Error(`unknown_block_type:${type}`)

    const pos = bot.findBlock({ matching: block.id, maxDistance: 64 })
    if (!pos) throw new Error(`no_${type}_nearby`)

    await bot.pathfinder.goto(new GoalNear(pos.position.x, pos.position.y, pos.position.z, 2))
    return { navigated_to: type, position: pos.position }
  },

  // 攻击指定类型的生物（pvp 插件）
  // args: { type: "zombie" }
  attackMob: async (bot, { type }) => {
    const entity = Object.values(bot.entities).find(
      e => e.name === type && e.position.distanceTo(bot.entity.position) < 16
    )
    if (!entity) throw new Error(`no_${type}_nearby`)

    bot.pvp.attack(entity)
    // 等待 pvp 插件停止攻击（目标死亡或超时）
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        bot.pvp.stop()
        reject(new Error(`attack_timeout:${type}`))
      }, 30000)
      bot.once('stoppedAttacking', () => {
        clearTimeout(timeout)
        resolve()
      })
    })
    return { killed: type }
  },

  // 合成物品
  // args: { item: "crafting_table", count: 1 }
  craft: async (bot, { item, count = 1 }) => {
    const mcData = getMcData(bot)
    const itemData = mcData.itemsByName[item]
    if (!itemData) throw new Error(`unknown_item:${item}`)

    // 找合成台（如果需要的话）
    const tableBlock = bot.findBlock({
      matching: mcData.blocksByName['crafting_table']?.id,
      maxDistance: 4
    })

    const recipes = bot.recipesFor(itemData.id, null, 1, tableBlock)
    if (!recipes || recipes.length === 0) throw new Error(`no_recipe_or_missing_materials:${item}`)

    await bot.craft(recipes[0], count, tableBlock)
    return { crafted: item, count }
  },

  // 放置合成台（craft 之前用这个）
  // args: {}
  placeCraftingTable: async (bot, _args) => {
    const Vec3 = require('vec3')
    const mcData = getMcData(bot)
    const tableItem = bot.inventory.findInventoryItem(mcData.itemsByName['crafting_table'].id)
    if (!tableItem) throw new Error('no_crafting_table_in_inventory')

    // 扫描周围 4 个方向，找到"脚下实心、头部是空气"的位置
    // 注意：不尝试 pos.offset(0,-1,0)（机器人正下方），因为放置位置和机器人实体重叠会被服务器拒绝
    const pos = bot.entity.position.floored()
    const candidates = [
      pos.offset(1, -1, 0),   // 右
      pos.offset(-1, -1, 0),  // 左
      pos.offset(0, -1, 1),   // 前
      pos.offset(0, -1, -1),  // 后
    ]

    for (const candidatePos of candidates) {
      const refBlock = bot.blockAt(candidatePos)
      if (!refBlock || refBlock.name === 'air' || refBlock.name === 'water' || refBlock.name === 'lava') continue
      const above = bot.blockAt(candidatePos.offset(0, 1, 0))
      if (!above || above.name !== 'air') continue

      await bot.equip(tableItem, 'hand')
      await bot.lookAt(candidatePos.offset(0.5, 1, 0.5))
      await bot.placeBlock(refBlock, new Vec3(0, 1, 0))
      return { placed: 'crafting_table', at: candidatePos }
    }

    throw new Error('no_valid_surface_to_place')
  },

  // 冶炼物品
  // args: { input: "raw_iron", fuel: "coal", count: 8 }
  smelt: async (bot, { input, fuel, count = 1 }) => {
    const mcData = getMcData(bot)
    const furnaceBlock = bot.findBlock({
      matching: mcData.blocksByName['furnace'].id,
      maxDistance: 4
    })
    if (!furnaceBlock) throw new Error('no_furnace_nearby')

    const furnace = await bot.openFurnace(furnaceBlock)
    await furnace.putInput(mcData.itemsByName[input].id, null, count)
    await furnace.putFuel(mcData.itemsByName[fuel].id, null, count)

    // 等待冶炼完成（每个 10s）
    await new Promise(r => setTimeout(r, count * 10000 + 1000))

    await furnace.takeOutput()
    furnace.close()
    return { smelted: input, count }
  },

  // 放置方块
  // args: { type: "dirt", x: 0, y: 64, z: 0 }
  placeBlock: async (bot, { type, x, y, z }) => {
    const mcData = getMcData(bot)
    const item = bot.inventory.findInventoryItem(mcData.itemsByName[type].id)
    if (!item) throw new Error(`no_${type}_in_inventory`)

    const targetPos = new (require('vec3'))(x, y, z)
    // 找参考方块（目标位置下方）
    const refBlock = bot.blockAt(targetPos.offset(0, -1, 0))
    if (!refBlock || refBlock.name === 'air') throw new Error('no_reference_block_below')

    await bot.equip(item, 'hand')
    await bot.placeBlock(refBlock, new (require('vec3'))(0, 1, 0))
    return { placed: type, at: { x, y, z } }
  },

  // 睡觉跳过夜晚
  // args: {}
  sleep: async (bot, _args) => {
    const mcData = getMcData(bot)
    const bedTypes = ['white_bed', 'red_bed', 'black_bed', 'brown_bed', 'blue_bed']
    let bed = null
    for (const bedType of bedTypes) {
      if (!mcData.blocksByName[bedType]) continue
      bed = bot.findBlock({ matching: mcData.blocksByName[bedType].id, maxDistance: 8 })
      if (bed) break
    }
    if (!bed) throw new Error('no_bed_nearby')

    await bot.sleep(bed)
    await new Promise((resolve, reject) => {
      bot.once('wake', resolve)
      setTimeout(() => reject(new Error('sleep_timeout')), 15000)
    })
    return { slept: true }
  },

  // 装备手持物品
  // args: { item: "diamond_sword" }
  equipItem: async (bot, { item }) => {
    const mcData = getMcData(bot)
    const itemData = mcData.itemsByName[item]
    if (!itemData) throw new Error(`unknown_item:${item}`)

    const inv = bot.inventory.findInventoryItem(itemData.id)
    if (!inv) throw new Error(`no_${item}_in_inventory`)

    await bot.equip(inv, 'hand')
    return { equipped: item }
  },

  // 丢弃物品（背包满时清理垃圾）
  // args: { item: "dirt", count: 64 }
  tossItem: async (bot, { item, count = 1 }) => {
    const mcData = getMcData(bot)
    const itemData = mcData.itemsByName[item]
    if (!itemData) throw new Error(`unknown_item:${item}`)
    await bot.toss(itemData.id, null, count)
    return { tossed: item, count }
  },

  // 在附近寻找并靠近某个生物/玩家
  // args: { type: "cow", max_distance: 32 }
  findAndApproach: async (bot, { type, max_distance = 32 }) => {
    const entity = Object.values(bot.entities).find(
      e => e.name === type && e.position.distanceTo(bot.entity.position) < max_distance
    )
    if (!entity) throw new Error(`no_${type}_within_${max_distance}m`)
    await bot.pathfinder.goto(new GoalNear(entity.position.x, entity.position.y, entity.position.z, 2))
    return { approached: type, distance: Math.round(entity.position.distanceTo(bot.entity.position)) }
  },

  // ── 互动技能 ─────────────────────────────────

  // 在游戏聊天框说话
  // args: { text: "你好！" }
  chatMessage: async (bot, { text }) => {
    bot.chat(String(text))
    return { sent: text }
  },

  // 靠近指定玩家（2格内）
  // args: { username: "Steve" }
  approachPlayer: async (bot, { username }) => {
    const player = bot.players[username]
    if (!player?.entity) throw new Error(`player_not_found:${username}`)
    const pos = player.entity.position
    await bot.pathfinder.goto(new GoalNear(pos.x, pos.y, pos.z, 2))
    return { approached: username }
  },

  // 攻击指定玩家（3秒后自动停手）
  // args: { username: "Steve" }
  attackPlayer: async (bot, { username }) => {
    const player = bot.players[username]
    if (!player?.entity) throw new Error(`player_not_found:${username}`)
    bot.pvp.attack(player.entity)
    await new Promise(resolve => setTimeout(resolve, 3000))
    bot.pvp.stop()
    return { attacked: username }
  },

  // 反复蹲起（示好/点头动作），times 次
  // args: { times: 5 }
  crouch: async (bot, { times = 5 }) => {
    for (let i = 0; i < times; i++) {
      bot.setControlState('sneak', true)
      await new Promise(r => setTimeout(r, 350))
      bot.setControlState('sneak', false)
      await new Promise(r => setTimeout(r, 350))
    }
    return { crouched: times }
  },

  // 原地跳跃 count 次
  // args: { count: 4 }
  jump: async (bot, { count = 4 }) => {
    for (let i = 0; i < count; i++) {
      bot.setControlState('jump', true)
      await new Promise(r => setTimeout(r, 200))
      bot.setControlState('jump', false)
      await new Promise(r => setTimeout(r, 400))
    }
    return { jumped: count }
  },

  // 挥手（摆臂）
  swingArm: async (bot, _args) => {
    bot.swingArm()
    return { swung: true }
  },
}

// 所有可用技能的描述（用于生成 LLM 的 Planner Prompt）
const SKILL_DESCRIPTIONS = `
可用技能列表（只能使用以下技能，不能使用列表外的任何名称）：

- collectBlock(type, count)
    收集指定方块，自动寻路+挖掘+拾取。
    type: 方块 id，如 oak_log / stone / iron_ore / coal_ore / dirt
    count: 需要数量

- navigateTo(x, y, z)
    移动到世界坐标。适合已知坐标时使用。

- navigateToBlock(type)
    寻找附近的指定方块并走过去（64格内）。
    type: 方块 id，如 crafting_table / furnace / chest

- attackMob(type)
    攻击附近指定类型的生物（16格内）。
    type: 生物 id，如 zombie / skeleton / creeper / cow / pig

- craft(item, count)
    合成物品。需要已有合成台在附近（4格内），且背包内有足够材料。
    item: 物品 id，如 wooden_pickaxe / crafting_table / stick / torch

- placeCraftingTable()
    将背包中的合成台放置在地上。在 craft 之前调用。

- smelt(input, fuel, count)
    使用熔炉冶炼。需要熔炉在附近（4格内）。
    input: 原料 id，如 raw_iron / raw_gold / sand
    fuel: 燃料 id，如 coal / wooden_log

- placeBlock(type, x, y, z)
    在指定坐标放置方块（需背包有该方块）。

- sleep()
    在附近的床上睡觉以跳过夜晚（8格内需有床）。

- equipItem(item)
    将背包中的物品装备到手上。
    item: 物品 id，如 diamond_sword / iron_pickaxe

- tossItem(item, count)
    丢弃背包中的物品（背包满时清理垃圾用）。

- findAndApproach(type, max_distance)
    寻找并靠近指定生物（默认32格内）。
`

module.exports = { SKILLS, SKILL_DESCRIPTIONS }
