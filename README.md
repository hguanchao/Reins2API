# Reins2API

OpenCode Zen 网关:把 [OpenCode Zen](https://opencode.ai/zen) 上游包装成三种业界标准 API 端点,客户端任选一种协议即可调用,网关负责模型路由、协议转换(3×3 桥)与流式转码。

- **Python 3.13 零框架**(标准库 `ThreadingHTTPServer` + `curl_cffi`)+ **React 19** 管理台
- **协议桥**:OpenAI Chat / OpenAI Responses / Anthropic Messages 三协议两两互转(9 个方向一条转换路径),同协议字节级透传、未知字段原样保留
- **流转码**:同协议 SSE 透传;跨协议 parser → BridgeStreamEvent → emitter 转码;客户端要 JSON 而上游被强制流式时自动折叠
- **免费档自动塑形**:上游对免费档强制"流式 + agent 工具集",网关补齐后把强制流折叠回客户端要的形态,客户端完全无感
- **分库遥测**:每个上游一个 SQLite 文件(`server/data/opencode.db`),用量与请求日志同源
- **管理台**:状态 KPI、用量趋势、模型目录、最近请求(OpenCode 单页)
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

# 推理(任选协议;模型名用 /v1/models 列出的 opencode/xxx,或裸名)
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/big-pickle","messages":[{"role":"user","content":"hi"}],"stream":true}'

curl http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"opencode/big-pickle","max_tokens":128,"messages":[{"role":"user","content":"hi"}]}'
```

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat 协议 |
| `POST /v1/responses` | OpenAI Responses 协议 |
| `POST /v1/messages` | Anthropic Messages 协议 |
| `GET /v1/models` | 可服务模型清单(已过滤) |
| `GET /healthz` | 健康检查 |
| `GET /api/opencode/ops` | 运维聚合快照(状态+目录+用量) |
| `GET /api/opencode/requests` | 遥测明细分页 |
| `GET /api/opencode/usage` | 用量聚合(`1h/24h/7d/30d`) |

Web 管理台:构建前端后访问 `http://127.0.0.1:8787/`(SPA)。开发态 `cd web && npm run dev`(5274,代理到后端)。

## 凭证与安全

- 凭证体系固定为**单一 `public` 匿名凭证**:只服务免费档模型(路由层拦截非免费模型,上游对它们一律回 "Missing API key")。要接付费 key 时,凭证轮换逻辑需重新引入(见下方架构)。
- **管理 API 与推理端点都无鉴权**,安全姿态是默认只监听 `127.0.0.1`。对外暴露前请自己加反代鉴权。
- 无配置文件:全部值写死在 `server/core/settings.py`(端口、代理档)与 `server/providers/opencode/upstream.py`(上游地址、凭证、超时)。

## 架构

```
server/
├── main.py              入口:注册 provider → 起服务
├── core/                settings(写死值) / logger(北京时间滚动) / http_body / respond
├── api/server.py        零框架分发层(限流 503/413/408/SPA 托管),路由 /v1 /api /healthz
├── gateway/             ★ 与上游无关的协议核心
│   ├── types.py         Bridge 中间结构 + Usage
│   ├── bridge/          decode→Bridge→encode(3×3 唯一路径)+ reasoning 档位可逆映射
│   ├── stream/          SSE parser/emitter + forward/transcode/collapse
│   ├── inference.py     编排:路由→转换→塑形→上游→回译→遥测
│   └── base.py          Provider 接口 + registry(插件轴)
├── providers/opencode/  opencode 上游接入:routing/discovery/upstream/identity
├── net/proxy.py         direct/system(环境变量→WinINET 注册表)/manual
└── db/                  分库遥测:每 provider 一个 SQLite(requests 表 + 用量聚合)
web/                     React 管理台(OpenCodePage 单页),构建产物落 web-dist/
```

依赖方向:`api → inference → registry → provider → bridge`。`gateway/` 永不 import `providers/*`。

## 已知取舍

- 会话亲和:首条 user 消息哈希成规范 `ses_` 形态,跨轮次稳定,保住上游 prompt cache。
- 遥测只进不出(无保留期清理),涨大后需再加 janitor。
- 日志/时间戳强制北京时间(与 SQLite 字符串比较一致)。
- 错误透传:上游 4xx(如限流 429、凭证 401)原样带回,客户端需能区分上游与本地错误(看 `error.type`)。
