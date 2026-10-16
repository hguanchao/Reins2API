# Reins2API

多上游网关：把 **OpenCode Zen**、**WorkBuddy（腾讯 CodeBuddy 号池）** 与 **Codex（ChatGPT 后端 Codex 服务）** 包装成三种业界标准 API 端点，客户端任选一种协议即可调用，网关负责模型路由、协议转换(3×3 桥)与流式转码。

- **Python 3.13 零框架**(标准库 `ThreadingHTTPServer` + `curl_cffi`)+ **React 19** 管理台
- **协议桥**:OpenAI Chat / OpenAI Responses / Anthropic Messages 三协议两两互转(9 个方向一条转换路径),同协议字节级透传、未知字段原样保留
- **流转码**:同协议 SSE 透传;跨协议 parser → BridgeStreamEvent → emitter 转码;客户端要 JSON 而上游被强制流式时自动折叠
- **免费档自动塑形**:上游对免费档强制"流式 + agent 工具集",网关补齐后把强制流折叠回客户端要的形态,客户端完全无感
- **分库遥测**:每个上游一个 SQLite 文件(`server/data/opencode.db`、`server/data/workbuddy.db`、`server/data/codex.db`),用量与请求日志同源;另有 `server/data/models.json` 保存各上游模型目录快照——三家**统一核心字段**(`id`/`name`/`description`/`context_window`/`max_output_tokens`/`input_modalities`/`output_modalities`/`supports_images`/`supports_reasoning`/`supports_tool_call`/`reasoning_efforts`/`default_effort`/`protocol`/`free`/`extras`,上游没声明的字段省略),形状 `{"opencode":[],"workbuddy":{"cn":[],"glb":[]},"codex":[],"meta":{"<provider>":{"updated_at_ms":…}}}`,每次刷新覆盖对应键,供排查/对照。**模型目录只在页面点「获取模型」时刷新**(`POST /api/{provider}/refresh`),启动只读这份快照(缓存为空才兜底拉一次);内容有变才写盘,写前把现文件轮转成 `models.json.bak`
- **WorkBuddy 号池**:多账号加权选号(快过期积分加权 + 成本分层 + Top-5 候选 + 防惊群)、冷却/熔断、会话粘性、token 自动刷新、OAuth 设备码登录、DSML 工具调用标记还原;四类定时任务(签到+连登管家 / 活跃上报 / 猫猫旅行 / token 保活)各有独立开关与时点(见 `docs/workbuddy-provider-design.md`)
- **Codex 号池**:ChatGPT 账号 OAuth(PKCE 授权码)接入、多账号加权选号、冷却/熔断、会话粘性、refresh_token 轮换与自动刷新、模型目录按账号下发(见 `docs/codex-provider-design.md`)
- **管理台**:OpenCode 状态页、WorkBuddy 号池页(含定时任务弹窗、暂停选号单列统计;账号表**积分列**在主行「剩余 / 总」下再给一行**快过期提示**——<7 天橙色、<1 天红色,数据全部来自本地库、打开页面不打上游)、Codex 页、系统配置页
- **插件轴**:`providers/` 按上游分包,后期加新上游 = 一个包 + 一行注册

## 接入

```bash
# 起服务(默认 127.0.0.1:8787;四步:自动装 uv/Node → 清理端口 → 后台启动 → 健康检查)
./start.sh                       # bash
.\start.ps1                      # PowerShell(CMD 双击 start.bat)
./start.sh 9000                  # 指定端口
./start.sh --rebuild             # 强制重建前端(默认:产物缺失或 web 源码更新时才构建)
# 再次运行 = 重启(自动结束旧实例);停止 = 结束端口上的进程;日志在 server/logs/
# 启动后窗口保持打开并滚动实时日志,直接关闭窗口不影响后台服务
# 或前台手动运行
cd server && uv sync && uv run python main.py --serve 9000

# 推理(任选协议;模型名用 /v1/models 列出的规范名,或裸名)
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"oc:big-pickle","messages":[{"role":"user","content":"hi"}],"stream":true}'

curl http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"cn:deepseek-v4.1-flash","max_tokens":128,"messages":[{"role":"user","content":"hi"}]}'
```

### 模型命名(别名前缀)

模型名用 `<前缀>:<裸名>` 形式,前缀决定路由到哪个上游、发上游前由 provider 剥掉前缀:

| 前缀 | 上游 | 例 |
|---|---|---|
| `oc:` | OpenCode Zen | `oc:big-pickle` |
| `cn:` | WorkBuddy 国内域 | `cn:deepseek-v4.1-flash` |
| `glb:` | WorkBuddy 国际域(global) | `glb:deepseek-v4.1-flash` |
| `cx:` | Codex(ChatGPT 后端) | `cx:gpt-5-codex` |

裸名(无前缀)归默认 provider(opencode);`cn:`/`glb:` 只在该域账号池内选号——域内无可用账号时返回 429,不会串域借号。`cx:` 走 Codex 号池(所有 codex 模型的上游协议都是 `responses`,chat/anthropic 客户端由桥自动转换)。旧式 `provider/xxx`(如 `workbuddy/xxx`、`opencode/xxx`)仍兼容。

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat 协议 |
| `POST /v1/responses` | OpenAI Responses 协议 |
| `POST /v1/messages` | Anthropic Messages 协议 |
| `GET /v1/models` | 可服务模型清单(全 provider 合并,规范名 `oc:`/`cn:`/`glb:`/`cx:`) |
| `GET /healthz` | 健康检查 |
| `GET /api/{provider}/ops` | 运维聚合快照(状态+目录+用量;opencode / workbuddy / codex 通用) |
| `GET /api/{provider}/requests` | 遥测明细分页 |
| `GET /api/{provider}/usage` | 用量聚合(`1h/24h/7d/30d`,含 by_account) |
| `GET/POST/DELETE /api/workbuddy/accounts[/{uid}]` | 账号列表 / 手动导入 / 删除 |
| `POST /api/workbuddy/accounts/{uid}/action` | pause / resume / disable / revive / refresh |
| `POST /api/workbuddy/accounts/{uid}/checkin` | 立即签到(force 绕过本地台账,上游幂等兜底) |
| `POST /api/workbuddy/checkin/run-all` | 一键全签 = 签到 + 余额刷新(回血解冻硬冷却)+ 连登管家(串行 + 账号间错峰;跳过 global/企业号) |
| `GET /api/workbuddy/schedule` | 定时任务排程快照(四类开关/cron/下次触发/上次结果,并附一句人话说明) |
| `POST /api/workbuddy/schedule` | 改定时任务开关与时点(cron 表达式；非法表达式 400 并给出坏在哪个字段) |
| `POST /api/workbuddy/tasks/{checkin\|activity\|travel\|keepalive}/run` | 立即运行一类定时任务(与自动排程同一条管线;在跑 → 409) |
| `POST /api/workbuddy/login/start`、`GET .../login/poll` | OAuth 设备码登录(浏览器授权 → 自动入池) |
| `GET/POST/DELETE /api/codex/accounts[/{uid}]` | 账号列表 / 手动导入(粘贴 refresh_token 或 access_token+account_id) / 删除 |
| `POST /api/codex/accounts/import` | 批量导入(CPA / codex2api 形状的 JSON:单对象 / 数组 / `{"accounts":[…]}`) |
| `GET /api/codex/accounts/export?format=cpa\|codex2api` | 导出账号为对应形状的 JSON |
| `POST /api/codex/accounts/{uid}/action` | pause / resume / disable / revive / refresh |
| `POST /api/codex/login/start` | 发起 OAuth(PKCE):返回授权 URL 与 session_id |
| `POST /api/codex/login/exchange` | 粘贴回调 URL(或 code)兑换 token 并自动入池 |

Web 管理台:构建前端后访问 `http://127.0.0.1:8787/`(SPA;根路径会 replace 重定向到 `/opencode`,三页各有独立地址 `/opencode`、`/workbuddy`、`/settings`,可直接进、可刷新——服务端对非 API 路径回退 index.html)。开发态 `cd web && npm run dev`(5274,代理到后端)。

## 凭证与安全

- **OpenCode**:凭证体系固定为**单一 `public` 匿名凭证**:只服务免费档模型(路由层拦截非免费模型,上游对它们一律回 "Missing API key")。
- **WorkBuddy**:多账号号池,凭证存 `server/data/workbuddy.db` 的 accounts 表(明文,与 wb2api 同款取舍);通过管理台「添加账号」走 OAuth 设备码登录,或手动粘贴 token;token 过期前 10 分钟自动刷新,保活排程另在全账号范围无条件续期。**定时任务四类各自独立开关与时点**(管理台「定时任务」弹窗,入口在号池面板头「一键全签」左侧;时点是一条 **5 字段 cron**(分 时 日 月 周,时区恒北京时间,如 `7 9,21 * * *` = 每天 9:07 与 21:07),保存即生效、落盘 `server/config.json` 的 `schedule` 段;老配置里的 `hours: [9,21]` 会在首次读取时按 `WB_SCHEDULE_MINUTE` 自动迁成 cron):
  - **签到**(默认 09/21 点):全池签到 → 查余额(余额回血则解冻「积分耗尽」硬冷却)→ 末尾跑**连登管家**(兑换所有已解锁档位 + 抽完所有抽奖次数;档位 7d/14d/28d 按连续登录天数解锁,幂等)
  - **活跃上报**(默认 10 点):每号一条 `chat_request_send` 上报,同时点亮 growth 连登天数并解锁 `first_buddy`(领养前置);上报后回读连登天数自检「200 但被静默丢弃」
  - **猫猫旅行**(默认 09/21 点,独立排程):无猫 → 上报(解锁任务)→ 同意协议 → 领养;有猫 → 按状态机「到站领奖 / 空闲派出 / 在途跳过」,一趟只推进一步
  - **token 保活**(默认 22 点):全账号用 refresh_token 换新 AT(不看剩余时长);失败按连续计数判定,连续 3 次 session 失效才自动禁用(网络抖动不计入,刷新成功即清零复活)

  账号门控:上游的**签到与成长写侧任务**(档位兑换、抽奖、猫猫旅行/领养)只对 CN 个人号开放——global 域与企业号在发请求前跳过(空撞既是日志噪音也是风控特征)。**活跃上报连 global 一起报**(国际版 `/v2/report` 实测可用,不报则国际版的连登永远点不亮),而连登天数回读在国际版同样正常(实测),所以上报与自检对 global 照跑、只跳过那里的写侧任务。global 域走 `www.workbuddy.ai`、CN 走 `copilot.tencent.com`(growth)/`www.codebuddy.cn`(billing),按 realm 自动分流。参数(冷却/熔断/超时/排程/账号间隔等)写死在 `server/core/settings.py` 的 `WB_*` 常量。

  **选号**(实现住共用的 `providers/account_pool.py`,但新机制只在 WorkBuddy 打开;Codex 不覆写任何钩子、行为逐字不变):粘性会话优先 → 过滤(冷却/熔断/在途占满/积分保底)→ **成本分层** → **加权随机** → **Top-5 短名单** → **防惊群** → 软冷却兜底。
  - **加权随机**:`1 + 闲置补偿(0.5/h 封顶 5.0) + credits 项(按候选集最大余额归一,满额 +10)`,未知余额不加 credits 项(按 1.0 计)。**企业额度**走 `get-enterprise-user-usage`(`X-Enterprise-Id`,`credit`=本周期已用四舍五入、`limitNum`=额度;负值=不限量 → 大剩余量 + 总量 -1),所以企业号也有真实余额参与权重与积分保底
  - **快过期积分加权**:余额查询解析**包级到期时间**(`CycleEndTime`),7 天窗口内到期的余额单独落库;带有效快过期批次的号在候选集里按 3 个虚拟实例计权(`WB_EXPIRING_SLOTS`),把快到期的赠送/补偿积分先花掉——实测本机账号 350 分里有 250 分在 7 天内到期
  - **成本分层**:按 (账号, 模型) 的**实测扣费**分三层(响应末帧 `usage.credit` 按每千 token 归一 → 0 实测免费 / 1 无观测 / 2 实测收费),只留最优层;存在"免费层垄断 + 无观测号"时每 30 分钟把一轮真实请求改道给无观测号去实测(**条件探索**,零额外上游请求),成功即毕业。`/api/workbuddy/state` 的 `model_costs` 就是这本台账(默认 6 小时观测有效期,"夜间免费"这类时段优惠过期后不再影响选号)
  - **Top-5 短名单**:按 (单价, 权重) 取前 5 再在名单内加权抽签,打散热点;并列候选在截断前洗牌(否则排序会把并列号里固定的那批钉死在名单里)
  - **防惊群**:同号 100ms 撞号窗口内让位给其它号(短名单全热时按**单调选中序号**在全候选里取最旧——墙钟精度不足,连续选号可能同一毫秒,只有序号能分全序)
  - **积分保底**:`WB_CREDIT_FLOOR`(默认 0 = 关闭)触底的号只拒绝**收费**模型,免费/未知倍率照放行(保底的目的正是"留余额给免费模型用");收费判据实测台账优先、目录倍率兜底
  - **模型级阻塞**:上游 11102("该后端无此模型")按 (账号, 模型) 落**负缓存**(基础 6h、重复命中指数退避封顶 24h,成功请求即清除)——11102 是账号-模型维度的稳定属性,不记的话每次请求都会重撞一遍。当**所有**账号都被该模型挡住时,返回 400 + `model_unavailable`(而不是伪装成"没有可用账号"的 429):换模型才有用,退避重试同一个模型是白等
  - 选号的"最近使用"时刻与选中序号是进程内状态,重启清零(良性:退化为无补偿/无 LRU)
- **Codex**:多账号号池,凭证存 `server/data/codex.db` 的 accounts 表(明文)。通过管理台/API 走 **OAuth PKCE 授权码**登录(浏览器授权 → 把回调 URL 粘贴回 `/api/codex/login/exchange` 自动入池),或手动粘贴 `refresh_token`(会立即刷新换取 AT 并解析账号)。`refresh_token` 会**轮换**(上游可能换发新 RT,没换发时保留旧值);token 刷新有两条路——**请求前按需**(距过期不足 10 分钟)与**后台巡检**(每 2 分钟醒来,只刷同样快过期的号;不做全量刷,免得每个号的 RT 周期性轮换),两条路按账号串行化以防 RT 双花;凭证失效(401)先刷新同号重试,连续失败达阈值则禁用待人工重新授权;**上游明确拒绝这张 RT(invalid_grant 等)或换票成功但落库失败时也直接停用**(前者说明票已经死在别处,后者说明新票只在上游那一侧存在过——两种情况库里剩下的都是废票,再拿去换只会白打一次上游)。**额度**(5h/7d 限流窗口)由每次成功响应的 `x-codex-{primary,secondary}-*` 头回填。参数(冷却/熔断/超时/池等)写死在 `server/core/settings.py` 的 `CODEX_*` 常量。
- **管理 API 与推理端点都无鉴权**,安全姿态是默认只监听 `127.0.0.1`。对外暴露前请自己加反代鉴权(WorkBuddy 与 Codex 号池都有真实凭证,务必先加)。
- 运行时可持久化配置有两块(`server/config.json`):**出站代理三档**与 **WorkBuddy 定时任务**(`schedule` 段的四类开关与时点);其余值写死在 `server/core/settings.py` 与各 provider。

## 架构

```
server/
├── main.py              入口:注册 provider(opencode + workbuddy + codex) → 起服务
├── core/                settings(写死值) / logger(北京时间滚动) / http_body / respond
├── api/server.py        零框架分发层(限流 503/413/408/SPA 托管),路由 /v1 /api /healthz
├── gateway/             ★ 与上游无关的协议核心
│   ├── types.py         Bridge 中间结构 + Usage
│   ├── protocols/       三个线上协议各一个文件:chat / responses / anthropic
│   │   ├── chat.py      Chat 协议:请求/响应编解码 + 流式解析/发射
│   │   ├── responses.py Responses 协议:同上
│   │   ├── anthropic.py Anthropic 协议:同上
│   │   └── _stream.py   共享流式状态机(事件路由 + 累积);协议差异走子类钩子
│   ├── bridge/          协议中立层:共享助手(content/reasoning/stop/usage…)+ 桥入口
│   ├── stream/          SSE 读写(sse/errors) + 流式编排(forward/transcode/collapse/observer)
│   ├── inference.py     编排:前缀路由→转换→塑形→上游→回译→遥测
│   └── base.py          Provider 接口 + registry(插件轴,前缀路由)
├── providers/           共享层:identity(会话标识派生) / models_snapshot(上游目录快照)
│                        / capabilities·parts·profiles(能力档案与出站附件归一)
│                        / common(北京时间·刷新回报·错误体·retry-after)
│                        / account_store·account_pool(两个号池 provider 的公共内核:
│                          SQLite 管道 + 选号/租约/粘性;子类只声明列集与三处钩子)
├── providers/opencode/  OpenCode Zen 接入:routing/discovery/upstream
├── providers/workbuddy/ WorkBuddy 号池接入:pool/store/classify/shape/dsml/streamwrap/upstream/login/checkin/growth/scheduler
├── providers/codex/     Codex 号池接入:pool/store/classify/shape/oauth/upstream/login
├── net/proxy.py         direct/system(环境变量→WinINET 注册表)/manual
└── db/                  分库遥测:每 provider 一个 SQLite(requests 表 + 用量聚合;workbuddy/codex 另有 accounts 表)
web/                     React 管理台(OpenCodePage / WorkBuddyPage / CodexPage / SettingsPage),构建产物落 web/dist/
```

依赖方向:`api → inference → registry → provider → bridge`。`gateway/` 永不 import `providers/*`。

## 已知取舍

- 会话亲和:首条 user 消息哈希成规范 `ses_` 形态,跨轮次稳定,保住上游 prompt cache。
- 遥测只进不出(无保留期清理),涨大后需再加 janitor。
- 首字耗时:遥测记 `requests.first_byte_ms`(上游首个非空字节)。workbuddy 打上游恒流式,该列恒有值;opencode 的 curl 流模式跟随客户端意图——客户端要**非流式**时整包被 curl 缓冲,网关看不到"第一个字节",该列为 0(界面显示「—」)。不拿"整包到齐"冒充首字:那等于总耗时,是条错数据。**吐字速率**同理:分母是扣除首字等待的生成耗时(扣完不足 200ms 或没有首字观测时退回端到端),否则同一模型 TTFB 从 0.3s 涨到 2s 会让速率凭空掉一半,读起来像"模型变慢"。
- 日志/时间戳强制北京时间(与 SQLite 字符串比较一致)。
- 定时任务的 cron 是**有意收窄的子集**(`core/cron.py`):支持 `*` / `a` / `a-b` / `a,b` / `*/n` / `a-b/n` / `a/n`,日与周同时受限时按 Vixie 的「或」语义;不支持 `@daily` 别名、秒/年字段、`JAN`/`MON` 英文名与 `L`/`W`/`#` 扩展——遇到即判非法并回具体字段的错误,**不静默降级**(静默降级会让人以为排程生效了)。"永不触发"是合法配置(如 `0 0 30 2 *`),界面显示"该表达式近期不触发"而不是报错。
- 错误透传:上游 4xx(如限流 429、凭证 401)原样带回,客户端需能区分上游与本地错误(看 `error.type`)。
- workbuddy 出站脱敏:上游内容审核按**逐字精确匹配**拦整单(400,`code 11128`)——会话里只要出现过 `You are Claude Code, Anthropic's official CLI for Claude` 这类模板句(客户端 system prompt 注入的,或工具结果里引用过参考实现源码),之后**每一轮**都被拒(历史每轮重发)。`providers/workbuddy/sanitize.py` 在出站前把这类指纹做最小改写(换一个词/整段删除 header 键值对),命中即改写并打一条 WARNING 日志。客户端自己的历史不受影响;只改发往上游的副本。
- workbuddy 出站清理的另一类脏数据:历史里的 `tool_calls[].function.name` 为空/纯空白(流式响应在 name 分片到达前断掉时,客户端会把这个占位写进会话)。上游对它直接回 400 `11133 ... must be a non-empty string`,**带不带配对结果都会拒**——这类脏条目正好命中"按 id 对称裁剪"的保留条件,所以光靠孤儿清理挡不住,整条会话会每轮 400。`shape.py` 的清理层现在把空 name 的调用一并裁掉(判定收窄到「function 存在且 name 是空白串」,缺字段的占位不判脏)。
- `prompt_cache_key` 缺 `conversation_id` 时回退**会话键**(Anthropic/Claude Code 流量不带对话 id):不回退的话 `conv` 为空串,该账号下所有请求会算出同一个常量键,前缀缓存互相驱逐、每轮都冷启动。

## 多模态与模型能力适配

三个端点都能**收**文本/图片/视频/PDF/音频,但"能收"≠"上游能用"。**厂商差异不在共享代码里**——`providers/profiles.py` 给每个 provider 一份上游档案,`providers/capabilities.py` 是通用机制,`providers/parts.py`(出站附件归一)只按档案给的 `accepted_parts` 决定放行/改写/降级,不认识任何厂商名。加新上游 = 写一份档案,不改共享代码。**每家上游的实测结论(探针、日期、未验证项、复测方法)记在 `server/tests/upstream_evidence.md`**,生产代码里不放实验明细。

**两层能力分开表达**:

- **传输层**(`accepted_parts`):上游会不会因为这种 content part 直接报错。在集合里的 part 原样放行;不在集合里但能无损改写的改写(`file` 装图片→`image_url`、装文本→内联 `text`);其余丢弃并在该消息末尾留一句**显式省略说明**(告诉模型附件没送到,而不是静默丢弃——静默丢会让模型对着看不见的附件编答案,整单 400 又会让用户拿不到任何回答)。
- **模型层**(模态与 `supports_*`):part 能送到也不代表模型能用。合并优先级 **目录声明 > 档案默认 > 省略**;"省略"是刻意的第三档——`supports_video_input: false` 会让客户端把视频入口藏起来,没有依据时不该做这种断言,所以用 None/缺省表示"不知道",不输出。

当前两家的取值(依据见上述实测记录):workbuddy 的 chat 路线收 text/image_url/video_url/input_audio,视频在模型层不可用、结构化输出与联网搜索不可用、对话中段 system 消息可用;opencode 的 chat 路线只收 text/image_url,走 `responses` 的模型另有 PDF 通道(见 `capabilities.py` 的 `_deliverable`)。

其余适配:

- **服务端工具(web_search 等)**:跨协议时**忽略并记日志**而不是 400——上游只认 `function` 工具,step 系带 `web_search` 会整单失败。function 工具不受影响。
- **结构化输出**:映射是通的(anthropic 的 `output_config.format` → chat 的 `response_format`,name 缺失补默认名),但 workbuddy 那条链路上游忽略该字段,所以按 false 声明。
- `/v1/models` 平铺能力字段:`input_modalities`/`output_modalities`/`supports_vision`/`supports_video_input`/`supports_pdf_input`/`supports_reasoning`/`supports_function_calling`/`supports_structured_output`/`supports_system_messages`/`supports_web_search`/`context_window`/`max_input_tokens`/`max_output_tokens`,值由 **provider** 算好(目录字段名只活在 provider 里,gateway 只平铺输出)。

