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

