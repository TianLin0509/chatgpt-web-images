# ChatGPT Web Images

[English](README.en.md)

使用自己的 **ChatGPT 网页账号**，通过本地 MCP 制作一张或多张图片，并下载网页提供的原图。工具返回文件路径、数量、尺寸与校验值，**不把图片 base64 放进模型上下文**。

这是非官方的本地浏览器自动化项目。v0.2 支持 **Windows、Python 3.11+、Node.js 18+、已安装的 Google Chrome**。需要自己的可生图 ChatGPT 账号；实际可用数量和生成速度由网页版决定。

## 快速开始

在项目目录运行 PowerShell：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install .
& .\.venv\Scripts\chatgpt-web-images.exe setup
& .\.venv\Scripts\chatgpt-web-images.exe open
# 在专属浏览器中自行登录后：
& .\.venv\Scripts\chatgpt-web-images.exe status
```

`setup` 仅在独立数据目录安装固定版本 `@playwright/cli@0.1.19`，不会全局安装 npm 包。生图期间不调用 npx、不在线安装依赖。没有 Chrome 时先安装 Chrome。

### 接入 Codex

```powershell
$imagePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
codex mcp add chatgpt-web-images -- $imagePython -m web_images mcp
```

在 Codex 配置的 `[mcp_servers.chatgpt-web-images]` 表中设置 `tool_timeout_sec = 180`，避免默认超时打断网页操作；然后开启新会话。MCP 的等待预算最多 45 秒，进行中的浏览器操作还可能占用额外时间。[官方 MCP 配置说明](https://learn.chatgpt.com/zh-Hans/docs/extend/mcp)

其他 stdio MCP 客户端：command 使用安装环境的 Python 绝对路径，args 为 `["-m", "web_images", "mcp"]`，工具超时设置至少 180 秒。无需 API key 或 MCP OAuth 登录。

可选 Codex 插件清单在 `.codex-plugin/`；其 `.mcp.json` 要求 `chatgpt-web-images` 可从客户端进程的 PATH 找到。首次用户优先使用上面的绝对路径 MCP 配置；不要同时配置两个操作同一数据目录的服务器。GitHub 发布本身不等于进入官方插件商店。

## 用法与接口

告诉模型：

> 用 ChatGPT 网页为我的这页汇报制作 5 种独立布局，保存原图；不要拼图。

| 工具 | 作用 |
|---|---|
| `image_generate` | 提交一次网页请求；`count=1..20`，默认 1 |
| `image_poll` | 内部等待、查询与逐张下载；`wait_seconds=40` 默认，范围 0–45 |
| `image_status` | 检查专属浏览器登录及活动任务 |
| `image_open` | 显示专属浏览器，供用户登录或处理网页校验 |
| `image_cancel` | 按用户要求取消当前任务 |

生成参数：`prompt` / `prompt_file` 二选一，`output_dir` 与参考图必须为本地绝对路径，`reference_images` 最多 5 个 PNG/JPEG/WebP，`name` 为短文件名。建议使用 UTF-8 提示词文件。

- **LLM 选择张数**：同主题探索适合 3–5 张；用户明确指定时遵从用户。多图逐项编号，要求独立图片，不能用拼图代替。20 是本地请求上限，未验证网页版能交付 20 张。
- **请求去重**：传入唯一 `request_id`（1–100 个 ASCII 字母、数字、`_`、`-`）。相同 ID 与输入返回原任务，即使任务已完成；相同 ID 搭配不同输入报错。新任务用新 ID。去重记录持久保存，不自动过期。
- **精简返回**：默认去掉重复提示词哈希和内部下载标识；文件路径、数量、实际尺寸、字节数与 SHA-256 保留。`detail=true` 可取完整来源证据，磁盘记录不减少。
- **内部等待**：一次 `image_poll` 可等待多个网页检查周期，减少模型参与轮询。预算结束可继续查询同一 ID，不重新提交。
- **完整性**：只在请求数量与实际下载数量相符时返回 `complete`。少图是 `partial`，多图是 `count_mismatch`；保留全部实际文件并报错。每张下载后保存进度，下一次查询恢复剩余文件。
- **错误**：MCP 使用 `isError=true`。提交不确定时保留原任务；下载失败时返回已有进度。原始页面、签名图片地址、Cookie 与浏览器错误全文不回传。

命令行也支持 `generate --prompt-file ... --count 5 --request-id ...` 和 `poll --job-id ... --wait-seconds 40 --detail`。

## 数据与配置

默认数据：`%LOCALAPPDATA%\ChatGPTWebImages`；默认输出为其 `output` 子目录。浏览器 Profile、认证状态、任务、去重索引都在数据目录。设置文件位于 `~/.config/chatgpt-web-images/settings.json`。

```powershell
# 按实际需要传入绝对目录；配置后重启 MCP 进程。
chatgpt-web-images configure --data-dir $myDataDir --output-dir $myOutputDir
chatgpt-web-images doctor
```

可覆盖环境变量：`CHATGPT_WEB_IMAGES_CONFIG_DIR`、`CHATGPT_WEB_IMAGES_DATA`、`CHATGPT_WEB_IMAGES_OUTPUT`、`CHATGPT_WEB_IMAGES_CLI`、`CHATGPT_WEB_IMAGES_AUTH`。优先级为环境变量 > 设置文件 > 默认值。`configure --cli-entry` 可以复用固定运行时；`configure --auth-file` 可以导入用户明确选择的 Playwright storage state，默认不搜索或导入其他程序认证。源认证文件不改写。

首次登录推荐 `open` 手动登录。认证只用于本机专属浏览器；不要把数据目录、认证文件、个人任务记录或生成文件提交到仓库。

## 边界与验证

- 每个数据目录只允许一个活动生成任务。多图是一个请求中的多张图片，不是多账号并发。
- 保持网页当前模型与思考强度；内部图片模型不可可靠核实，返回 `image_model_verified=false`。
- 网页尺寸是实际尺寸，不自动放大，也不用截图或裁切冒充原图。
- 不绕过网页登录、校验或限额。网页 DOM 或按钮文案变化可能需要适配。
- 多图减少重复会话设置；没有对照实验支持“生图快 N 倍”。工具文字、提示词和可选看图仍会占用模型上下文。
- v0.1 已实测单次 5 张独立原图；v0.2 的新增行为、协议测试与公开版限制见 [验证记录](docs/VALIDATION.md)。尚未承诺 macOS/Linux 或全部语言网页兼容。

```powershell
python scripts/test_web_images.py
python scripts/test_mcp_protocol.py
python scripts/check_release.py
python -m pip wheel --no-deps . -w dist
```

## 发布与贡献

MIT 许可证。提交问题请提供版本、错误码和已脱敏的状态，勿附登录态或原始浏览器日志。查看 [发布步骤](docs/RELEASING.md)、[隐私说明](SECURITY.md) 和 [变更记录](CHANGELOG.md)。

浏览器驱动依赖 [Microsoft Playwright CLI](https://github.com/microsoft/playwright-cli)，MCP 服务依赖 [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)。第三方运行时在安装时单独获取，未把浏览器或依赖包复制进仓库。
