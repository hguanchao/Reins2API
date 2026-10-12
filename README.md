# Reins2API

多上游网关：把 **OpenCode Zen** 与 **WorkBuddy（腾讯 CodeBuddy 号池）** 包装成三种业界标准 API 端点，客户端任选一种协议即可调用，网关负责模型路由、协议转换(3×3 桥)与流式转码。

- **Python 3.13 零框架**(标准库 `ThreadingHTTPServer` + `curl_cffi`)+ **React 19** 管理台
- **协议桥**:OpenAI Chat / OpenAI Responses / Anthropic Messages 三协议两两互转(9 个方向一条转换路径),同协议字节级透传、未知字段原样保留
- **流转码**:同协议 SSE 透传;跨协议 parser → BridgeStreamEvent → emitter 转码;客户端要 JSON 而上游被强制流式时自动折叠
- **免费档自动塑形**:上游对免费档强制"流式 + agent 工具集",网关补齐后把强制流折叠回客户端要的形态,客户端完全无感
- **分库遥测**:每个上游一个 SQLite 文件(`server/data/opencode.db`、`server/data/workbuddy.db`),用量与请求日志同源
- **WorkBuddy 号池**:多账号加权选号、冷却/熔断、会话粘性、token 自动刷新、每日签到、OAuth 设备码登录、DSML 工具调用标记还原(见 `docs/workbuddy-provider-design.md`)
- **管理台**:OpenCode 状态页、WorkBuddy 号池页、系统配置页
- **插件轴**:`providers/` 按上游分包,后期加新上游 = 一个包 + 一行注册

## 接入

```bash
# 起服务(默认 127.0.0.1:8787;四步:自动装 uv/Node → 清理端口 → 后台启动 → 健康检查)
./start.sh                       # bash
.\start.ps1                      # PowerShell(CMD 双击 start.bat)
./start.sh 9000                  # 指定端口
./start.sh --rebuild             # 强制重建前端
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

裸名(无前缀)归默认 provider(opencode);`cn:`/`glb:` 只在该域账号池内选号——域内无可用账号时返回 429,不会串域借号。旧式 `provider/xxx`(如 `workbuddy/xxx`、`opencode/xxx`)仍兼容。

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat 协议 |
| `POST /v1/responses` | OpenAI Responses 协议 |
| `POST /v1/messages` | Anthropic Messages 协议 |
| `GET /v1/models` | 可服务模型清单(全 provider 合并,规范名 `oc:`/`cn:`/`glb:`) |
| `GET /healthz` | 健康检查 |
| `GET /api/{provider}/ops` | 运维聚合快照(状态+目录+用量;opencode / workbuddy 通用) |
| `GET /api/{provider}/requests` | 遥测明细分页 |
| `GET /api/{provider}/usage` | 用量聚合(`1h/24h/7d/30d`,含 by_account) |
| `GET/POST/DELETE /api/workbuddy/accounts[/{uid}]` | 账号列表 / 手动导入 / 删除 |
| `POST /api/workbuddy/accounts/{uid}/action` | pause / resume / disable / revive / refresh |
| `POST /api/workbuddy/accounts/{uid}/checkin` | 立即签到(force 绕过本地台账,上游幂等兜底) |
| `POST /api/workbuddy/checkin/run-all` | 一键全签(串行 + 账号间错峰;跳过 global/企业号) |
| `POST /api/workbuddy/login/start`、`GET .../login/poll` | OAuth 设备码登录(浏览器授权 → 自动入池) |

Web 管理台:构建前端后访问 `http://127.0.0.1:8787/`(SPA;根路径会 replace 重定向到 `/opencode`,三页各有独立地址 `/opencode`、`/workbuddy`、`/settings`,可直接进、可刷新——服务端对非 API 路径回退 index.html)。开发态 `cd web && npm run dev`(5274,代理到后端)。

## 凭证与安全

- **OpenCode**:凭证体系固定为**单一 `public` 匿名凭证**:只服务免费档模型(路由层拦截非免费模型,上游对它们一律回 "Missing API key")。
- **WorkBuddy**:多账号号池,凭证存 `server/data/workbuddy.db` 的 accounts 表(明文,与 wb2api 同款取舍);通过管理台「添加账号」走 OAuth 设备码登录,或手动粘贴 token;token 过期前 10 分钟自动刷新。**每日签到**按北京时间 `WB_CHECKIN_HOURS`(默认 9/21 点)各跑一轮,每轮账号间错峰;上游只对 CN 个人号开放,global 域与企业号在发请求前跳过;领取结果落 accounts 表(本地台账),接口本身幂等,重复调用只会返回"已签"。参数(冷却/熔断/超时/签到等)写死在 `server/core/settings.py` 的 `WB_*` 常量。
- **管理 API 与推理端点都无鉴权**,安全姿态是默认只监听 `127.0.0.1`。对外暴露前请自己加反代鉴权(WorkBuddy 号池有真实凭证,务必先加)。
- 运行时可持久化配置只有代理三档(`server/config.json`);其余值写死在 `server/core/settings.py` 与各 provider。

## 架构

```
server/
├── main.py              入口:注册 provider(opencode + workbuddy) → 起服务
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
├── providers/opencode/  OpenCode Zen 接入:routing/discovery/upstream/identity
├── providers/workbuddy/ WorkBuddy 号池接入:pool/store/classify/shape/dsml/streamwrap/upstream/login/checkin
├── net/proxy.py         direct/system(环境变量→WinINET 注册表)/manual
└── db/                  分库遥测:每 provider 一个 SQLite(requests 表 + 用量聚合;workbuddy 另有 accounts 表)
web/                     React 管理台(OpenCodePage / WorkBuddyPage / SettingsPage),构建产物落 web/dist/
```

依赖方向:`api → inference → registry → provider → bridge`。`gateway/` 永不 import `providers/*`。

## 已知取舍

- 会话亲和:首条 user 消息哈希成规范 `ses_` 形态,跨轮次稳定,保住上游 prompt cache。
- 遥测只进不出(无保留期清理),涨大后需再加 janitor。
- 首字耗时:遥测记 `requests.first_byte_ms`(上游首个非空字节)。workbuddy 打上游恒流式,该列恒有值;opencode 的 curl 流模式跟随客户端意图——客户端要**非流式**时整包被 curl 缓冲,网关看不到"第一个字节",该列为 0(界面显示「—」)。不拿"整包到齐"冒充首字:那等于总耗时,是条错数据。
- 日志/时间戳强制北京时间(与 SQLite 字符串比较一致)。
- 错误透传:上游 4xx(如限流 429、凭证 401)原样带回,客户端需能区分上游与本地错误(看 `error.type`)。
- workbuddy 出站脱敏:上游内容审核按**逐字精确匹配**拦整单(400,`code 11128`)——会话里只要出现过 `You are Claude Code, Anthropic's official CLI for Claude` 这类模板句(客户端 system prompt 注入的,或工具结果里引用过参考实现源码),之后**每一轮**都被拒(历史每轮重发)。`providers/workbuddy/sanitize.py` 在出站前把这类指纹做最小改写(换一个词/整段删除 header 键值对),命中即改写并打一条 WARNING 日志。客户端自己的历史不受影响;只改发往上游的副本。

## 多模态与模型能力适配

三个端点都能**收**文本/图片/视频/PDF/音频,但"能收"≠"上游能用"。**厂商差异不在共享代码里**——`providers/profiles.py` 给每个 provider 一份上游档案(结论 + 证据 + 日期),`providers/capabilities.py` 是通用机制,`providers/parts.py`(出站附件归一)只按档案给的 `accepted_parts` 决定放行/改写/降级,不认识任何厂商名。加新上游 = 写一份档案,不改共享代码。

**两层能力分开表达**(实测学到的区分):

- **传输层**(`accepted_parts`):上游会不会因为这种 content part 直接报错。在集合里的 part 原样放行;不在集合里但能无损改写的改写(`file` 装图片→`image_url`、装文本→内联 `text`);其余丢弃并在该消息末尾留一句**显式省略说明**(pi-mono 口径:告诉模型附件没送到,而不是静默丢)。
- **模型层**(模态与 `supports_*`):part 能送到也不代表模型能用。合并优先级 **目录声明 > 档案默认 > 省略**;"省略"是刻意的第三档——`supports_video_input: false` 会让客户端把视频入口藏起来,没有依据时不该做这种断言,所以用 None/缺省表示"不知道",不输出。

已实测的档案(2026-10-12):

- **workbuddy**:`file` → 400(装图片/文本/PDF 都一样);`video_url`/`input_audio` → 200。图片确实能用(实测识别出颜色);**视频是模型层的死路**——收得下但平台明确告诉模型"不支持视频"(换真实 http mp4 同样看不到);`response_format` 被忽略(json_schema 返回散文)→ 结构化输出标 false;对话中段 `role:system` 模型照做 → 标 true;`tools` 里的 `web_search` 走工具会回 `unsupported_content_type`。
- **opencode**:`text`/`image_url` → 200(实测模型准确答出图片颜色);`file`/`video_url`/`input_audio` → 400。目录里声明的 video/pdf/audio 模态因此在 chat 路线上被过滤掉(送不达就不声明);走 `responses` 协议的模型(muse-spark)保留 pdf——`input_file` 是那条路的原生载体。样本只有一个模型(free 端点多数时间 429),下一轮验证扩样。

其余适配:

- **服务端工具(web_search 等)**:跨协议时**忽略并记日志**而不是 400——上游只认 `function` 工具(StepFun 文档明确),step 系带 `web_search` 会整单失败。function 工具不受影响。
- **结构化输出**:映射是通的(anthropic 的 `output_config.format` → chat 的 `response_format`,name 缺失补默认名),但 workbuddy 那条链路上游忽略该字段,所以按 false 声明。
- `/v1/models` 平铺能力字段:`input_modalities`/`output_modalities`/`supports_vision`/`supports_video_input`/`supports_pdf_input`/`supports_reasoning`/`supports_function_calling`/`supports_structured_output`/`supports_system_messages`/`supports_web_search`/`context_window`/`max_input_tokens`/`max_output_tokens`,值由 **provider** 算好(目录字段名只活在 provider 里,gateway 只平铺输出)。

