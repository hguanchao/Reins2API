# 上游行为实测记录（结论 + 复测方法）

`providers/profiles.py` 里 `accepted_parts` / `defaults` 的取值依据都在这里；
重测后**先改这份记录，再同步代码**。生产代码里只留机制解释，不放探针明细与日期。

## 复测方法

网关本地跑着（默认 8787），payload 写成 JSON 文件再 `curl --data-binary @file`，
比在 shell 里拼引号可靠（中文与嵌套引号容易出错）。要件：

- 判定**传输层**（part 收不收）：同样的文本请求加/不加该 part，看 400 还是 200；
  200 时再看 `prompt_tokens` 有没有涨、模型回答是否引用了内容。
- 判定**模型层**（能不能用）：传输层 200 之后，看模型是否真答对内容
  （别拿"上游没报错"当"模型看得见"——收下 `video_url` 但看不见就属于这种）。
- 探针之间隔 5–8 秒：workbuddy 有软限流（429 `too many requests`），
  opencode free 端点会长时间 429。

## workbuddy（2026-10-12，账号 41c30f6b，模型 glb:step-5-preview）

| 探针 | 结果 |
| --- | --- |
| `file` 装图片 / 装文本 / 装 PDF | **400**（三种都一样） |
| `video_url`（data URL 与真实 http mp4 各试一次） | 200，但模型被告知"不支持视频"；`prompt_tokens` 未含视频，模型答"无法查看" |
| `image_url`（data URL 纯色图） | 200，模型准确答出颜色 |
| `input_audio` | 200 |
| 对话中段插入 `role:system` | 200，且模型照做（"从现在开始…"生效） |
| `response_format`（json_schema 与 json_object） | **被忽略**，返回散文 |
| `tools` 里带 `type:"web_search"` | 200，但模型自述无联网能力（StepFun 文档：step 系不支持，会回 `unsupported_content_type`） |
| 裸字符串 `11128`（user / assistant+reasoning / tool_calls.arguments / tool 结果 / system） | 全部 200，**不拦** |
| `You are Claude Code, Anthropic's official CLI for Claude` 放进 system | **400 `code 11128`**；去掉即 200 → `providers/workbuddy/sanitize.py` 出站改写后实测 200 |

## opencode（2026-10-12，模型 oc:big-pickle；其余 free 端点多数时间 429）

| 探针 | 结果 |
| --- | --- |
| `text` | 200 |
| `image_url` | 200，模型答出颜色 |
| `file`（装文本 / 装 PDF） | **400** |
| `video_url` | **400** |
| `input_audio` | **400** |

样本限制与待办：

- 只测到一个模型；`mimo` / `space-bunny` / `step-5-preview-free` 等声明视频的模型当时全 429，
  视频在**模型层**到底能不能用还没测出来，所以对外不声明视频。
- `oc:big-pickle` 的目录声明是 `inputModalities: ["text"]`，实测却能看图——
  目录与实测有出入，下一轮验证扩大样本后单独收口。
- 走 `responses` 协议的模型（muse-spark）走 `input_file` 送 PDF，与 chat 路线不同，
  协议例外见 `providers/capabilities.py` 的 `_deliverable`。

## opencode 请求形态对照（2026-10-15，对照官方客户端 2.0.29 源码 + 线上探针）

对照源：`E:\Projects\Reins\reference\opencode`（官方仓库，包版本 2.0.29）。
探针口径：与生产同一条出站路径（`build_upstream_headers` + `shape_free_request_body`）；
免费档必须是 agent 会话形态（缺 tools 的裸探针会被 403 FreeTierError 挡下，别误读成
"模型不可用"）。探针出口须走代理，直连同样 403。

| 探针 | 结果 |
| --- | --- |
| chat `max_tokens:16` / `max_completion_tokens:16`（big-pickle） | 都 200，都截到 16（finish=length、completion_tokens=16）→ 两种字段名等价；官方客户端只发 `max_completion_tokens`，网关跟随 |
| responses 路由默认（`store:false` + `include:["reasoning.encrypted_content"]`，muse-spark-1.2-contributor-free） | 200；官方两条 responses route 的 defaults 就是这一对 |
| responses 带 `prompt_cache_key=会话id`（同上模型，同前缀两连发） | 200；缓存命中 1777 tokens（不带只有 113）→ 会话级缓存键实测有效 |
| anthropic 免费模型（qwen3.6-plus-free / minimax-m3-free 等） | 400 `Model is unavailable`：目录标 free 但**不在凭证可见清单**里，网关本就该路由拒绝。官方 anthropic beta 集合（恒 `interleaved-thinking-2025-05-14`，其余按 compaction / thinking binding 条件追加）只能在源码层对照，线上无从实测 |

配套改动（同一批）：chat 字段名归一、responses 路由默认值、responses 会话缓存键、
父会话双头名与编码归一、出站孤立代理项 `toWellFormed`（官方 `utils/sanitize.ts` 同义）。

## 结构化输出（跨上游的现状）

| 方向 | 结果 |
| --- | --- |
| anthropic 端点收 `output_config.format` → chat `response_format` | 映射生效（name 缺失补默认名），但 workbuddy 上游忽略该字段（同上表） |
| chat 端点直传 `response_format` | 上游忽略 |

## workbuddy 目录发现：UA 敏感（2026-10-11）

`/v3/config` 的返回随 User-Agent 变：

| UA | 下发 |
| --- | --- |
| global 完整桌面端 UA | 28 条（含 gpt-6-luna / gpt-6-sol / grok-4.7 等新模型） |
| 裸 `CLI/<v> CodeBuddy/<v>` | 降级成 22 条旧目录（缺 gpt-6-*） |
| IDE UA | 只剩别名/路由条目 |

另外 UA 必须能解析出 CodeBuddy 版本号，否则 400 `code=12403`。
→ 代码里 global 用完整桌面端 UA（`default_user_agent("global")`），CN 沿用 IDE UA。

