const mineflayer = require('mineflayer')
const { pathfinder, Movements } = require('mineflayer-pathfinder')
const { plugin: collectblock } = require('mineflayer-collectblock')
const { plugin: pvp } = require('mineflayer-pvp')
const { loader: autoEat } = require('mineflayer-auto-eat')
const armorManager = require('mineflayer-armor-manager')
const express = require('express')
const { WebSocketServer } = require('ws')
const { SKILLS } = require('./skills/index.js')

// ─────────────────────────────────────────────
// 配置
// ─────────────────────────────────────────────
// mcData 模块级缓存，避免 buildState() 每 500ms 重复创建
let mcData = null
const MC_HOST = '127.0.0.1'
const MC_PORT = 25565
const BOT_USERNAME = 'AIBot'
const API_PORT = 3000   // Python 通过这个端口调用技能 + 接收状态

// ─────────────────────────────────────────────
// 创建 Bot
// ─────────────────────────────────────────────
const bot = mineflayer.createBot({
  host: MC_HOST,
  port: MC_PORT,
  username: BOT_USERNAME,
  auth: 'offline',  // 离线模式（自建服务器）
})

// 加载插件
bot.loadPlugin(pathfinder)
bot.loadPlugin(collectblock)
bot.loadPlugin(pvp)
bot.loadPlugin(autoEat)
bot.loadPlugin(armorManager)

// ─────────────────────────────────────────────
// 后台守护插件配置
// ─────────────────────────────────────────────
bot.once('spawn', () => {
  // 寻路默认移动参数
  mcData = require('minecraft-data')(bot.version)
  const movements = new Movements(bot, mcData)
  movements.allowSprinting = true
  bot.pathfinder.setMovements(movements)

  // 自动吃饭：饥饿低于15时吃，优先吃高饱食度食物
  bot.autoEat.setOpts({ priority: 'foodPoints', minHunger: 15 })
  bot.autoEat.enableAuto()

  console.log(`[Bot] 已生成于 ${bot.entity.position}`)
  broadcastState()
})

// ─────────────────────────────────────────────
// 事件监听：关键事件推送给 Python
// ─────────────────────────────────────────────
const HOSTILE_MOBS = ['zombie', 'skeleton', 'creeper', 'spider', 'witch', 'enderman',
  'blaze', 'ghast', 'slime', 'phantom', 'drowned', 'husk', 'stray', 'pillager']

const eventQueue = []  // 待推送的事件

function pushEvent(type, data = {}) {
  eventQueue.push({ type, data, timestamp: Date.now() })
}

bot.on('health', () => {
  if (bot.health <= 6) pushEvent('health_critical', { health: bot.health })
  if (bot.health <= 0) pushEvent('death', {})
})

bot.on('death', () => pushEvent('death', {}))

bot.on('entitySpawn', (entity) => {
  if (!entity || !entity.position) return
  const dist = entity.position.distanceTo(bot.entity.position)
  if (entity.name === 'creeper' && dist < 12) {
    pushEvent('creeper_nearby', { distance: Math.round(dist) })
  }
  if (HOSTILE_MOBS.includes(entity.name) && dist < 8) {
    pushEvent('mob_nearby', { type: entity.name, distance: Math.round(dist) })
  }
})

bot.on('playerCollect', (collector, collected) => {
  if (collector.username !== bot.username) return
  const item = collected.metadata?.[8]
  if (!item || !mcData) return
  const itemData = mcData.items[item.itemId]
  if (itemData?.name === 'diamond') pushEvent('found_diamond', { count: item.itemCount })
  if (itemData?.name?.includes('emerald')) pushEvent('found_emerald', {})
})

bot.on('time', () => {
  // 天黑提醒（time 18000 = 傍晚，23000 开始变黑）
  if (bot.time.timeOfDay === 13000) pushEvent('dusk', {})
})

// 玩家聊天 → 推送给 Python 情绪脑处理
bot.on('chat', (username, message) => {
  if (username === bot.username) return  // 忽略自己说的话
  console.log(`[Chat] ${username}: ${message}`)
  pushEvent('player_chat', { username, message })
})

// ─────────────────────────────────────────────
// 状态序列化（推送给 Python）
// ─────────────────────────────────────────────
function buildState() {
  if (!bot.entity || !mcData) return null

  // 背包：name → count
  const inventory = {}
  for (const item of bot.inventory.items()) {
    inventory[item.name] = (inventory[item.name] || 0) + item.count
  }

  // 附近实体（32格内）
  const nearby_entities = Object.values(bot.entities)
    .filter(e => e !== bot.entity && e.position)
    .filter(e => e.position.distanceTo(bot.entity.position) < 32)
    .map(e => ({
      type: e.name,
      display: e.displayName,
      distance: Math.round(e.position.distanceTo(bot.entity.position)),
      health: e.health,
    }))
    .sort((a, b) => a.distance - b.distance)
    .slice(0, 10)

  // 附近资源方块（16格内）
  const RESOURCE_BLOCKS = ['oak_log', 'birch_log', 'spruce_log', 'coal_ore',
    'iron_ore', 'gold_ore', 'diamond_ore', 'crafting_table', 'furnace', 'chest']
  const nearby_blocks = []
  for (const blockName of RESOURCE_BLOCKS) {
    const blockData = mcData.blocksByName[blockName]
    if (!blockData) continue
    const pos = bot.findBlock({ matching: blockData.id, maxDistance: 16 })
    if (pos) nearby_blocks.push({ type: blockName, distance: Math.round(pos.position.distanceTo(bot.entity.position)) })
  }

  // 消费并清空事件队列
  const events = eventQueue.splice(0)

  return {
    health: bot.health,
    food: bot.food,
    position: {
      x: Math.round(bot.entity.position.x),
      y: Math.round(bot.entity.position.y),
      z: Math.round(bot.entity.position.z),
    },
    time_of_day: bot.time?.timeOfDay,
    dimension: bot.game?.dimension,
    inventory,
    nearby_entities,
    nearby_blocks,
    events,  // Python 消费后处理情绪
  }
}

// ─────────────────────────────────────────────
// HTTP Server（技能调用）
// ─────────────────────────────────────────────
const app = express()
app.use(express.json())

// 技能调用接口：Python POST /skill { name, args }
app.post('/skill', async (req, res) => {
  const { name, args = {} } = req.body
  console.log(`[Skill] 调用: ${name}`, args)

  const skillFn = SKILLS[name]
  if (!skillFn) {
    return res.json({ status: 'failed', reason: `unknown_skill:${name}` })
  }

  try {
    const output = await skillFn(bot, args)
    console.log(`[Skill] 完成: ${name}`, output)
    pushEvent('task_step_done', { skill: name, output })
    res.json({ status: 'success', output })
  } catch (err) {
    console.error(`[Skill] 失败: ${name}`, err.message)
    res.json({ status: 'failed', reason: err.message })
  }
})

// 配方查询：根据 minecraft-data 返回物品合成所需材料
app.get('/recipe/:item', (req, res) => {
  const mcData = require('minecraft-data')(bot.version)
  const itemName = req.params.item
  const itemData = mcData.itemsByName[itemName]
  if (!itemData) return res.json({ found: false, error: `unknown_item:${itemName}` })

  const rawRecipes = mcData.recipes[itemData.id]
  if (!rawRecipes || rawRecipes.length === 0) {
    return res.json({ found: false, error: `no_recipe_for:${itemName}` })
  }

  const recipe = rawRecipes[0]
  const ingredients = {}

  // cell 可能是数字（item id）或 {id, count} 对象
  function cellId(cell) {
    if (typeof cell === 'number') return cell
    if (cell && typeof cell === 'object') return cell.id ?? cell
    return null
  }

  if (recipe.inShape) {
    for (const row of recipe.inShape) {
      for (const cell of row) {
        if (cell == null) continue
        const id = cellId(cell)
        if (id == null) continue
        const name = mcData.items[id]?.name || `id:${id}`
        ingredients[name] = (ingredients[name] || 0) + 1
      }
    }
  } else if (recipe.ingredients) {
    for (const cell of recipe.ingredients) {
      if (cell == null) continue
      const id = cellId(cell)
      if (id == null) continue
      const name = mcData.items[id]?.name || `id:${id}`
      ingredients[name] = (ingredients[name] || 0) + 1
    }
  }

  // 3x3 配方（行数>2 或列数>2）需要合成台
  const needsTable = recipe.inShape &&
    (recipe.inShape.length > 2 || recipe.inShape.some(r => r.length > 2))

  res.json({
    found: true,
    item: itemName,
    result_count: recipe.result?.count || 1,
    needs_table: needsTable || false,
    ingredients,
  })
})

// 健康检查
app.get('/ping', (_req, res) => res.json({ ok: true, username: bot.username }))

const httpServer = app.listen(API_PORT, () => {
  console.log(`[HTTP] 监听端口 ${API_PORT}`)
})

// ─────────────────────────────────────────────
// WebSocket Server（状态推送）
// ─────────────────────────────────────────────
const wss = new WebSocketServer({ server: httpServer, path: '/state' })
const wsClients = new Set()

wss.on('connection', (ws) => {
  console.log('[WS] Python 已连接')
  wsClients.add(ws)
  ws.on('close', () => wsClients.delete(ws))
})

function broadcastState() {
  if (wsClients.size === 0) return
  const state = buildState()
  if (!state) return
  const msg = JSON.stringify(state)
  for (const ws of wsClients) {
    if (ws.readyState === ws.OPEN) ws.send(msg)
  }
}

// 每 500ms 推送一次状态
setInterval(broadcastState, 500)

// ─────────────────────────────────────────────
// 错误处理
// ─────────────────────────────────────────────
bot.on('error', (err) => console.error('[Bot] 错误:', err))
bot.on('kicked', (reason) => console.error('[Bot] 被踢出:', reason))
bot.on('end', () => {
  console.log('[Bot] 连接断开，5秒后重连...')
  setTimeout(() => process.exit(1), 5000)  // 让 pm2/supervisor 重启
})
