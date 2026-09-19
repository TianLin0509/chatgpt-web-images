---
name: chatgpt-web-images
description: 使用用户自己的 ChatGPT 网页账号生成图片、制作参考图变体并下载原图。适用于明确要求 ChatGPT 网页生图或点名本工具；支持多个 agent 共用持久队列和独立账号并行，返回文字与本地文件路径。
---

# ChatGPT 网页生图

通过已注册的 chatgpt-web-images MCP 调用，Claude、Codex 等客户端使用相同协议。不要另写浏览器脚本或后台轮询脚本。

1. 首次使用调用 `image_status`，读取账号健康、活动任务和最近账号操作结果，不访问网页。未登录时 `image_open(account_id)`；用户登录后 `image_account_check(account_id)`，再查看 status。账号操作返回 queued 只表示已接收。
2. 要在之前某张图的基础上修改（改配色、换标题、保持版式微调），传 `continue_from=<该任务的 job_id>`，追问会发进原对话，ChatGPT 仍看得见那些图；不相关的新图不要带 `continue_from`。同一对话同时只允许一个追问在跑，它会自动落到拥有该对话的账号的任意空闲车道。
   长提示词保存为带日期、任务标识的 UTF-8 文件。`image_generate(prompt_file=绝对路径, count=张数, request_id=全局唯一任务标识, output_dir=绝对路径, name=任务名, account_id="auto")`。prompt 与 prompt_file 二选一；reference_images 接受至多五个 PNG/JPEG/WebP 绝对路径，排队期间保持文件不变。
3. count=1..20 是**一次网页请求**产出的图片数量，不是账号并发数或额度保证。遵从指定张数，多图要求独立附件，不能用拼图或裁切代替，不静默压缩或降低要求。
   判据：**同一提示词要多张备选**（例如一页 PPT 的若干方案）就用一个 count=N 请求，不要拆成 N 个任务——一个请求只占一条车道、只消耗账号一次请求额度，固定的提交与导航开销也只付一次。**提示词确实不同**（不同页、不同主题）才分成多个任务。提交结果里出现 `hint.code=consider_one_multi_image_request` 时，先回答"这些是不是同一提示词的备选"，是就合并重提，不是就照常继续。失败后重投同一任务属于重试，与本条无关。
4. 提交立即返回 job_id；queued、dispatching、running 是正常状态。多个 agent 可以同时提交，每条车道执行一个任务，其余持久排队。auto 分配空闲车道，也可明确指定已经配置的账号别名。并发上限是车道数（一个浏览器 Profile 一条），不是账号数，也不随排队时长变化；排队久属正常，不要改 ID 重投。auto 会在登录账号之间自动均衡，不要为了"换个账号试试"而改 account_id。`image_status` 的 `logins` 给出每个账号的车道数与在跑数量；车道不够时由用户决定是否扩容，agent 不自行扩。
5. 保持同一 request_id 和参数；超时重试返回原任务。ID 应含日期、任务或席位标识。不得自动换 ID 或账号重发不确定提交。
6. **先把已知的活全部提交，再统一轮询**；`submit → 等 → submit` 会让车道空转，是批量变慢的主因。多个不同提示词用一次 `image_generate_batch(requests=[...], output_dir=...)` 排入，每条 entry 自带 request_id；等待用 `image_poll(job_ids=[...], wait_seconds=20)` 一次覆盖整批，`return_when="any"` 可先取回已完成的。批量里未知的 job_id 只在结果中标出，不会让整批失败。
7. `image_poll(job_id, wait_seconds=20)` 等待单个结果。后台自主执行、下载、收尾，agent 断开仍会继续。无需额外 shell 循环、高频 status 或自建后台任务。phase 是浏览器实际阶段。查询自己的任务，不接管其他人的任务。
8. 失败结果的 `error.actionable_by` 指明谁能处理：`agent` 就自己改提示词重投（典型是 `no_image_in_reply` / `generation_failed`，`message` 里有 ChatGPT 的原话）；`retry` 等一下重试；`human` 交回用户，不要空转重试。
9. needs_attention 表示暂停核查并保留原账号和请求身份。按错误检查账号，必要时 image_open；处理后 `image_resume(job_id)` 接续原任务，不换账号重发。仅在用户要求取消时 `image_cancel(job_id)`；活动任务取消为异步请求，确认 cancelled 后才宣称取消成功。
10. parked 表示该任务已交回车道让队列继续，账号、会话和已存文件都保留，不是失败也没有重发。auto_retry_attempts_left 大于零时它会自行回到原会话重新观察，继续 image_poll 即可；为零且 next_action 为 inspect_account_then_resume 才需要检查账号后 image_resume。不要因为看到 parked 就换账号、换 request_id 或重新提交。
   queued 时先看 blocking_accounts；账号故障后后台会自行重检有限次，仍是 browser_unhealthy 才需排查后 account_check，不通过反复提交试错。worker_stalled 表示进程仍有心跳但操作停滞，不等于可用。已有活动任务不可切换账号。resume 只恢复观察，不重发；同一超时反复出现时交代阻塞原因，不无限 resume。
11. 只有请求、观察与下载数量相符才是 complete。partial / count_mismatch / failed 必须如实交代，交付已保存文件。确认本地文件存在并用本地看图工具检查，不把图片 base64 放进工具文字。

内部图片模型未核实，称为“ChatGPT 网页生成”；保留网页模型和思考强度，不自动改用 API 或其他生图工具。尺寸以原图为准。不绕过登录、校验、额度；密码和验证码只由用户在专属浏览器输入。

## MCP 尚未加载

优先重新加载 MCP 或开启新会话。统一 CLI 同样使用持久队列：`chatgpt-web-images generate --prompt-file ... --request-id ...`、`poll --job-id ... --wait-seconds 20`、`status`。不截断 JSON，不另建轮询守护进程。安装与账号初始化见项目 README；普通调用无需阅读源码或编辑任务文件。
