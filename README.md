# Reins2API

多上游网关：把 **OpenCode Zen** 与 **WorkBuddy（腾讯 CodeBuddy 号池）** 包装成三种业界标准 API 端点，客户端任选一种协议即可调用，网关负责模型路由、协议转换(3×3 桥)与流式转码。

- **Python 3.13 零框架**(标准库 `ThreadingHTTPServer` + `curl_cffi`)+ **React 19** 管理台
- **协议桥**:OpenAI Chat / OpenAI Responses / Anthropic Messages 三协议两两互转(9 个方向一条转换路径),同协议字节级透传、未知字段原样保留
- **流转码**:同协议 SSE 透传;跨协议 parser → BridgeStreamEvent → emitter 转码;客户端要 JSON 而上游被强制流式时自动折叠
- **免费档自动塑形**:上游对免费档强制"流式 + agent 工具集",网关补齐后把强制流折叠回客户端要的形态,客户端完全无感
- **分库遥测**:每个上游一个 SQLite 文件(`server/data/opencode.db`、`server/data/workbuddy.db`),用量与请求日志同源
- **WorkBuddy 号池**:多账号加权选号、冷却/熔断、会话粘性、token 自动刷新、OAuth 设备码登录、DSML 工具调用标记还原(见 `docs/workbuddy-provider-design.md`)
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
| `POST /api/workbuddy/accounts/{uid}/action` | pause / resume / disable / revive / refresh / credits |
| `POST /api/workbuddy/login/start`、`GET .../login/poll` | OAuth 设备码登录(浏览器授权 → 自动入池) |

Web 管理台:构建前端后访问 `http://127.0.0.1:8787/`(SPA)。开发态 `cd web && npm run dev`(5274,代理到后端)。

## 凭证与安全

- **OpenCode**:凭证体系固定为**单一 `public` 匿名凭证**:只服务免费档模型(路由层拦截非免费模型,上游对它们一律回 "Missing API key")。
- **WorkBuddy**:多账号号池,凭证存 `server/data/workbuddy.db` 的 accounts 表(明文,与 wb2api 同款取舍);通过管理台「添加账号」走 OAuth 设备码登录,或手动粘贴 token;token 过期前 10 分钟自动刷新。参数(冷却/熔断/超时等)写死在 `server/core/settings.py` 的 `WB_*` 常量。
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
├── providers/workbuddy/ WorkBuddy 号池接入:pool/store/classify/shape/dsml/streamwrap/upstream/login
├── net/proxy.py         direct/system(环境变量→WinINET 注册表)/manual
└── db/                  分库遥测:每 provider 一个 SQLite(requests 表 + 用量聚合;workbuddy 另有 accounts 表)
web/                     React 管理台(OpenCodePage / WorkBuddyPage / SettingsPage),构建产物落 web/dist/
```

依赖方向:`api → inference → registry → provider → bridge`。`gateway/` 永不 import `providers/*`。

## 已知取舍

- 会话亲和:首条 user 消息哈希成规范 `ses_` 形态,跨轮次稳定,保住上游 prompt cache。
- 遥测只进不出(无保留期清理),涨大后需再加 janitor。
- 日志/时间戳强制北京时间(与 SQLite 字符串比较一致)。
- 错误透传:上游 4xx(如限流 429、凭证 401)原样带回,客户端需能区分上游与本地错误(看 `error.type`)。
