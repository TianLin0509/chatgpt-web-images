# ChatGPT Web Images 0.3.1

## 并发与故障恢复修复

- 相同账号的待执行 open/check/select 操作合并；控制操作与任务交替执行，避免多个 agent 的检查请求挤占生成和下载。
- MCP 等待使用异步休眠，不占用线程池等待名额。后台启动采用跨进程门锁与 20 秒启动预约；stop 标记阻止新版本客户端反复拉起后台。
- 浏览器准备或生成失败后，账号进入 browser_unhealthy，停止领取新单；检查浏览器并显式 account_check 成功后恢复。已有失败任务不自动重发。
- queued 结果包含 blocking_accounts；status 包含 worker_version 和 worker_stalled。心跳存在但一次操作超过 300 秒会标记停滞，不强杀浏览器或重发请求。
- resume 保留原账号、任务和会话，重新给予 15 分钟观察时间，并在确认提示词身份后刷新原会话一次。它不会重新发送提示词。
- 活动任务期间禁止切换账号；取消连续失败三次后暂停，保留错误，不无限重复操作。
- 默认返回精简文件信息；detail=true 查看完整记录。后台日志仅记任务阶段、账号别名及错误码，不记提示词、Cookie 或网页正文。

升级后后台 worker 需要在操作间隙重启以加载代码；已有 MCP 客户端需要重新加载或新会话才能获得新的异步等待行为。不要重启生产 AI Hub 或手动删除任务记录。

使用自己的 ChatGPT 网页账号生图，下载网页原图，只返回本地文件路径与文字元数据。非官方 Windows 本地自动化工具，需要 Python 3.11+、Node.js 18+、Chrome 和可生图账号。

## 多 agent 与多账号

所有 stdio MCP 客户端共用 SQLite 持久队列。每账号一个独立后台进程、独立 Profile，同时执行一个任务。多个 agent 可同时提交，空闲账号领取，其余排队。后台自主检查生成、下载和收尾，客户端断开不会终止任务。

支持本机的标准 stdio MCP 客户端，不等于认证所有 agent 或提供远程服务。两个账号的实际速度、额度和网页可用性仍由服务决定。

## 安装与初始化

在项目独立目录安装：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install .
& .\.venv\Scripts\chatgpt-web-images.exe setup
```

setup 安装固定 Playwright CLI 0.1.19。每账号独立配置目录，settings.json 至少含绝对 data_dir、现有 cli_entry 和绝对 output_dir。第二个账号不能复制第一个的 auth_file 或登录状态。

所有客户端设置相同的 CHATGPT_WEB_IMAGES_POOL 绝对路径，建议在 C:\VibeData 下。未设置时优先读配置 pool_dir，回退到浏览器数据目录旁的 ChatGPTWebImagesPool。

```powershell
chatgpt-web-images account-add --account-id primary --data-dir $primaryData --config-dir $primaryConfig
chatgpt-web-images account-add --account-id secondary --data-dir $secondaryData --config-dir $secondaryConfig
chatgpt-web-images open --account-id secondary
# 用户完成登录后：
chatgpt-web-images check --account-id secondary
chatgpt-web-images status
```

open/check/select-account 异步执行；查看 accounts[].last_control 确认结果。account-enable --account-id secondary --disabled 停止领取新任务，不取消正在执行的任务；去掉 --disabled 恢复领取。

## Claude、Codex 与其他客户端

客户端启动同一 Python 入口并指定同一队列。工具描述已内含排队和恢复规则，不依赖某个 agent 的私有 skill。无需 API key 或 Cookie 复制。

```powershell
# Claude Code 用户级注册
claude mcp add --scope user --transport stdio chatgpt-web-images -e PYTHONIOENCODING=utf-8 -e "CHATGPT_WEB_IMAGES_POOL=$poolDir" -- $imagePython -m web_images mcp
# Codex：已用插件安装时不要重复注册
codex mcp add chatgpt-web-images --env PYTHONIOENCODING=utf-8 --env "CHATGPT_WEB_IMAGES_POOL=$poolDir" -- $imagePython -m web_images mcp
```

其他客户端使用标准 mcpServers JSON：command 为安装环境 Python 绝对路径；args 为 ["-m","web_images","mcp"]；env 含 PYTHONIOENCODING=utf-8 与 CHATGPT_WEB_IMAGES_POOL。源码安装可把 -m web_images 换为 scripts/web_images.py 的绝对路径。

默认 poll 等待 20 秒，最大 45 秒；浏览器操作在独立后台，不延长等待预算。建议客户端超时至少 60 秒。新会话/重新加载 MCP 后获得新版工具；已运行的旧服务不会自动更新。

## 工具与状态

| 工具 | 行为 |
|---|---|
| image_generate | 持久入队，立即返回 job_id；account_id=auto 或指定账号；request_id 防重复 |
| image_poll | 读取/等待自己的任务，后台不依赖它执行 |
| image_status | 缓存健康、后台存活、任务状态数量和账号操作结果 |
| image_open | 异步打开指定账号专属浏览器 |
| image_account_check | 用户登录后检查账号状态 |
| image_select_account | 只选择用户授权的已记住账号，不处理密码或验证码 |
| image_resume | 接续 needs_attention 原任务，保持同一账号和请求身份 |
| image_cancel | 按用户要求取消排队任务，或异步请求取消它自己的网页生成 |

prompt / UTF-8 prompt_file 二选一，参考图最多五个本地 PNG/JPEG/WebP，输出目录为绝对路径。count=1..20 是插件参数上限，不是网页额度保证。

状态为 queued → dispatching → running → complete；phase 是浏览器实际阶段。needs_attention 阻塞该任务所属账号，另一账号仍可工作。连续三次操作失败暂停；会话串线、参考文件改变、输出完整性异常也会停止继续操作。处理后显式 resume，不换账号重发。

partial / count_mismatch / failed 明确返回，保留已有文件。完成结果再次读取会检查文件哈希，缺失或修改不能冒充成功。request_id 在整个队列中唯一，不自动过期；不同 agent 使用含日期和任务/席位的 ID。

## 旧任务与后台运维

旧活动任务保留所有权，不自动取消。`import-legacy --account-id primary` 把当前旧任务纳入后台观察，只接续查询、不重新提交。旧历史结果可用 `poll --legacy --job-id ...` 读取。

worker 退出后，下次 status/generate/poll 自动拉起。目前没有安装 Windows 开机自启；系统重启后第一次客户端调用恢复后台，磁盘队列保留。

运维停止某个 worker：在队列目录创建 stop-<account_id> 文件，等其退出；删除该精确文件后下次调用恢复。不要终止其他浏览器或生产 Hub。

每个 worker 启动时清除其他 CHATGPT_WEB_IMAGES_* 环境覆盖，再设置自己的配置、数据和共享队列。登录态只保存在本机专属 Profile。队列含提示词、文件路径和任务记录，按本机私有数据管理，不提交到仓库。

## 验证

```powershell
python scripts/test_web_images.py
python scripts/test_mcp_protocol.py
python scripts/test_image_pool.py
python scripts/test_pool_protocol.py
python scripts/test_account_contracts.py
python scripts/test_account_browser.py
```

模拟测试覆盖多客户端、双进程、去重、断开继续执行、故障隔离、取消与恢复，不代表真实账号生成成功。真实验收单独验证登录与原图下载。内部图片模型未核实，不宣称精确型号，不绕过网页校验或额度。
