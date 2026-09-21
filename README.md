# ChatGPT Web Images 0.7.2

## 窗口无感（0.7.2）

车道用的是**有头浏览器 + 窗口生在屏幕外**，不是无头。实测（2026-09-21，隔离 profile 播种真实登录态）：去掉 `--headed` 后 ChatGPT 直接返回 Cloudflare 的 `Just a moment...`，`auth_state=browser_challenge`，进不去应用。**所以不要改成无头**，有守卫钉着。

关键在于*时机*：隐藏只能在窗口已经存在之后做，而走到那一步要起两个子进程（open，再加一次给页面改标题的脚本），所以冷启动的车道会先在屏幕上闪几秒；四条车道同时冷启动就是四个窗口。现在窗口由 Chrome 在 `--window-position=-32000,-32000` 直接创建，**一出生就在屏幕外**。

实测对比（0.5 秒采样、150 秒冷启动 + 生成）：

| | 屏幕上的车道窗口 |
|---|---|
| 只靠隐藏 | 最多 4 个 |
| 生在屏幕外 | **0 个** |

另外用 `--hide-crash-restore-bubble` 等开关掐掉 Chrome 自己的窗口：非正常退出后它会弹标题为 `Restore pages?` 的气泡，那个标题不是我们设的，找窗口匹配不到，于是永远藏不掉。

隐藏动作仍然保留（让车道也不出现在任务栏），失败只记 `window_hidden=False`，不影响任务。唯一会把窗口挪回屏幕的是 `image_open`——那是给你登录用的，`check` 之后会重新收起来。

## 对话连续性与出错可调整（0.7.0）

**改图不用重头说。** `continue_from=<job_id>` 把追问发进那个任务自己的对话里，ChatGPT 仍然看得见之前的图，所以"保持版式、配色调深一点"这种说法能直接用。

```jsonc
image_generate({prompt: "保持版式，配色调深一点，标题换成深红", continue_from: "20260919-xxxx"})
```

实测确认的三条机制：

1. **对话属于账号，不属于浏览器 Profile。** 同一登录的**任意空闲车道**都能接续同一个对话，不用等原车道空出来。
2. **同一对话同时只允许一个写入者**，避免两条追问在一个会话里交错；不相关的任务照常用其它车道。
3. 追问自动钉到拥有该对话的登录账号；指定了别的账号会明确报 `continue_wrong_account`。

新开对话仍是默认行为，**换个不相关的图就别用 `continue_from`**。

## 出错要让 agent 能立刻调整（0.7.0）

- **答完但没出图 → 立刻失败**，不再干等 15 分钟。判据取自真实 DOM：一个回合只有完成后才长出操作栏，`copy-turn-action-button` 是图片回合与文本回合的共同信号。明确回绝报 `generation_failed`，其它情况报 `no_image_in_reply`，两者都带回 ChatGPT 的原话（800 字符）。
- **错误按"谁能处理"分类**，结果里的 `error.actionable_by`：

  | 值 | 含义 | 例子 |
  |---|---|---|
  | `agent` | 改提示词重投 | `no_image_in_reply`、`generation_failed`、数量不符 |
  | `retry` | 等一下重试即可 | `browser_timeout`、`busy`、观察超时 |
  | `human` | 需要人处理 | `login_required`、`browser_challenge`、`conversation_changed` |

  未知错误码一律归 `human`，绝不谎称 agent 能自行解决。

## 最大化并行：MCP 说明直接把规则讲出来（0.6.0）

MCP 的 server instructions 和工具描述现在显式写明四条，agent 不读 skill 也能拿到：

1. **并发 = 车道数，不是账号数**；`image_status` 给出每个登录的车道与负载。
2. **同一提示词的多张备选 → 一个 `image_generate(count=N)`**，一次网页请求出全部 N 张。
3. **多个不同提示词 → 一次 `image_generate_batch`**，一次调用排满所有空闲车道。
4. **绝不串行**：先把已知的活全部提交，再统一轮询；`submit → 等 → submit` 会让车道空转，这是批量变慢的主因。

新增与改动的工具：

```jsonc
// 一次排入整批不同提示词
image_generate_batch({
  requests: [{prompt_file: "...p1.txt", name: "p1", request_id: "20260919-deck-p1"},
             {prompt_file: "...p2.txt", name: "p2", request_id: "20260919-deck-p2"}],
  output_dir: "C:/.../out"
})

// 一次等待整批，而不是一个一个 poll
image_poll({job_ids: [...], wait_seconds: 20, return_when: "all"})   // "any" 可提前取回先完成的
```

批量提交先整体校验再统一入队，不会留下"半个批次"；同 `request_id` 重发幂等。批量轮询里的未知 job_id 只在结果里标出来，不会让整批失败或空等。CLI 用重复的 `--job-id` 达到同样效果。

## 请求形状：一次多张 vs 多个任务（0.5.1）

`count=N` 让**一次**网页请求产出 N 张独立图片，早已支持（N=1..20），并非本轮新增。

判据很简单：

- **同一提示词要多张备选**（一页 PPT 的若干方案）→ 一个 `count=N` 请求。只占一条车道、只消耗账号一次请求额度，提交与导航的固定开销也只付一次。
- **提示词确实不同**（不同页、不同主题）→ 各自独立任务，这样能并行跑在多条车道上，也能各自成败。

同一个 `output_dir` 下 15 分钟内同时在途 3 个以上 count=1 任务时，提交结果会带 `hint.code=consider_one_multi_image_request`。它只是提醒，不改变行为；**只统计在途任务**，所以失败后的重投不会被误判成批量。

合并的代价要知道：N 张在一个任务里，少交付就整体记 `partial`，不像拆开时能各自成败。

## 并行度与账号均衡（0.5.0）

- 并发等于**车道数**，不等于账号数。一条车道 = 一个独立浏览器 Profile，一次一个任务。
- `account-scale --account-id primary --lanes 4` 一次把某个已登录账号扩到 4 条车道；减少数量时只停用不删除 Profile。
- auto 任务按**登录账号**均衡派发：某个账号已经在跑的任务更多时，它的空闲车道让另一个账号先拿。显式指定账号名仍然优先，并且可以落到该账号的任意一条车道。
- 空闲车道 5 分钟后主动关闭浏览器交还内存，下次有活从持久 Profile 重开，不需要重新登录。实测一条车道常驻约 0.6 GB，16 条就是约 10 GB，这条回收是大车道池可用的前提。
- 车道总数上限 16，限制来自本机内存，不是 ChatGPT。`status` 里 `logins` 给出每个账号的车道数、可用车道和在跑任务数。

## 吞吐与卡死修复（0.4.0）

- 暂停的任务不再长期占用它的账号。超过 120 秒无人处理就 parked：交回浏览器，保留同一账号、同一会话和已下载文件，排队任务立即继续。parked 不是失败，也不重发提示词。
- parked 任务按 60/300/900 秒退避自动回到原会话重新观察，最多三次；仍不成功才等人处理，此时 next_action 为 inspect_account_then_resume。resume 只是让它重新排上队，不抢占正在跑的任务。
- 身份校验失败先强制重载记录在案的会话再判断，最多两次。此前刷新写在校验之后，恢复路径在真正需要它时永远走不到，resume 必然再次失败。
- 浏览器故障后的账号按 60/300/900 秒自动重检，最多三次；此前 browser_unhealthy 永不自检，一次抖动就让整条车道静默下线直到人工 account_check。
- `account-clone` 为同一个已登录账号增加独立 Profile 车道。并发上限来自「一个账号一个浏览器页面」，不是 ChatGPT 的限制。
- 生成期间无新图时后台轮询放慢到 10 秒，减少浏览器子进程启动；出现图片后恢复 3 秒节奏。

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

**并发上限等于车道数，不等于账号数。** 一条车道是一个浏览器 Profile，一次只做一个任务；单个任务从提交到下载完通常要几分钟。想提高并发，给已登录账号加车道：

```powershell
# 每个账号 4 条车道，合计 8 路并发
chatgpt-web-images account-scale --account-id primary --lanes 4
chatgpt-web-images account-scale --account-id secondary --lanes 4
# 每条新车道各做一次登录检查
chatgpt-web-images check --account-id primary-2
```

扩容会新建独立 Profile 并复制该账号已保存的登录态，源账号必须先 check 过一次；缩容只停用车道，不删除 Profile。账号重新登录后，用 `account-reseed --account-id primary-2` 把新登录态同步给空闲车道。

auto 任务在**登录账号**之间均衡：正在跑得多的账号会让空闲车道先给另一个账号，这正是把压力摊到两个账号、少碰官方限流的手段。指定 `account_id=primary` 仍然可以钉死到某个账号，并落在它的任意车道上。

代价说清楚：ChatGPT 侧会看到同一账号的多个并发会话，与手工多开多个窗口等价；额度、限流和网页可用性仍由服务决定。加车道只让更多张图同时跑，不会让单张图更快。一条车道常驻约 0.6 GB 内存，空闲 5 分钟自动关闭回收。

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
| image_generate | 持久入队，立即返回 job_id；account_id=auto 或指定账号；request_id 防重复；continue_from=<job_id> 在原对话里追问 |
| image_generate_batch | 一次排入整批不同提示词，立刻占满所有空闲车道；整体校验、按 request_id 幂等 |
| image_poll | 读取/等待自己的任务；传 job_ids 一次等待整批，return_when=any 可提前取回 |
| image_status | 缓存健康、后台存活、任务状态数量和账号操作结果 |
| image_open | 异步打开指定账号专属浏览器 |
| image_account_check | 用户登录后检查账号状态 |
| image_select_account | 只选择用户授权的已记住账号，不处理密码或验证码 |
| image_resume | 接续 needs_attention 或 parked 原任务，保持同一账号和请求身份 |
| image_cancel | 按用户要求取消排队任务，或异步请求取消它自己的网页生成 |

prompt / UTF-8 prompt_file 二选一，参考图最多五个本地 PNG/JPEG/WebP，输出目录为绝对路径。count=1..20 是插件参数上限，不是网页额度保证。

状态为 queued → dispatching → running → complete；phase 是浏览器实际阶段。连续三次操作失败暂停；会话串线、参考文件改变、输出完整性异常也会停止继续操作。处理后显式 resume，不换账号重发。

needs_attention 仍暂停该账号，但只暂停 120 秒：之后转为 parked 交回车道，队列继续推进。parked 保留账号、会话和已存文件，按退避自动重新观察，结果里带 auto_retry_at 与 auto_retry_attempts_left；次数用完才需要人工检查后 resume。从未进入浏览器的任务 parked 后不自动重试，resume 会让它重新派发，start 通过持久请求索引保持幂等。

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
python scripts/test_throughput.py
python scripts/test_account_browser.py
```

模拟测试覆盖多客户端、双进程、去重、断开继续执行、故障隔离、取消与恢复，不代表真实账号生成成功。真实验收单独验证登录与原图下载。内部图片模型未核实，不宣称精确型号，不绕过网页校验或额度。
