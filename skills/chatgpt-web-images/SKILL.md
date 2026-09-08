---
name: chatgpt-web-images
description: 使用用户自己的 ChatGPT 网页账号生图、以本地参考图制作变体并下载原图。仅在明确要求 ChatGPT 网页生图或点名此插件时使用。MCP 只返回文字与文件路径，避免图片 base64 进入上下文。
---

# ChatGPT 网页生图

1. 已验证登录时直接生成；首次使用或登录不确定时调用 `image_status`。需要登录时用 `image_open` 展示专属浏览器，用户自行登录。不要操作其他浏览器、Hub 或中转会话。
2. 将完整提示词保存为带日期和任务标识的 UTF-8 文件；调用 `image_generate(prompt_file=绝对路径, count=张数, request_id=任务唯一标识, output_dir=绝对路径, name=任务名)`。`prompt` 与 `prompt_file` 二选一。
3. 由 LLM 按需求选择张数；同主题布局或变体通常 3–5 张，默认 1。用户指定张数时遵从用户。1–20 是插件请求上限，不是网页版额度保证。多图提示词逐项编号；要求独立图片，不能用拼图代替。
4. 参考图用 `reference_images` 传至多五个本地 PNG/JPEG/WebP 绝对路径，不转 base64，不静默压缩。
5. 使用稳定的 `request_id`；相同参数和 ID 返回原任务，新任务使用新 ID。提交不确定时查询同一任务，不自动换 ID 重发。只允许一个活动生成任务。
6. 调用 `image_poll(job_id, wait_seconds=40)`，由 MCP 内部等待和下载，完成后交付。若等待预算结束仍未完成，再调用同一任务；不必额外高频查询。默认返回精简信息，需要完整来源与提示词哈希时传 `detail=true`。
7. 核对 requested / observed / downloaded 数量。只有数量相符才是 `complete`；`partial` / `count_mismatch` 必须明确说明差额并交付已有文件。生成中的 observed 数量会变化。下载错误返回任务与已保存文件，继续查询可恢复；不得隐瞒错误或自动补交新请求。
8. 确认本地文件存在再交付。图片检查使用本地 `view_image`，不把图片字节读入工具文本。不要把文件大小或响应字节减少比例说成精确 token 节省比例。
9. 仅在用户要求取消时调用 `image_cancel`。不要擅自取消任务以便重试。

称为“ChatGPT 网页生成”；内部图片模型未核实，不能宣称精确型号。不能自动切换模型、思考强度、API 或原生 imagegen。尺寸以下载文件为准。工具不绕过网页登录、校验或限额。

## MCP 尚未加载

使用安装环境中的 `chatgpt-web-images` CLI，同一实现支持 `status`、`open`、`generate --prompt-file ... --count 5 --request-id ...`、`poll --job-id ... --wait-seconds 40`。新会话加载更新后的工具。安装与配置见项目 README；不要复制其他人的认证文件。
