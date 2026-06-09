# API 文档

所有业务接口都以 `/api` 为前缀。

## 响应约定

成功响应：

```json
{
  "success": true,
  "data": {}
}
```

失败响应：

```json
{
  "success": false,
  "error": {
    "message": "error message",
    "details": {}
  }
}
```

## 1. 健康检查

### 请求

```http
GET /api/health
```

### 响应

```json
{
  "ok": true,
  "service": "img-Creater-backend"
}
```

## 2. 配置管理

### 2.1 获取全部节点

```http
GET /api/configs
```

返回值为节点数组，数组顺序就是提交任务时的优先级顺序。

> 出于安全考虑，所有返回节点的接口都**不会回传完整 `api_key`**，而是返回掩码字段：
>
> - `api_key_preview`: 形如 `••••1234`（末 4 位）
> - `has_api_key`: 是否已设置密钥
>
> 完整 Key 只在用户查看或编辑节点时通过 `GET /api/configs/{id}/secret` 临时读取。

### 2.2 创建节点

```http
POST /api/configs
Content-Type: application/json
```

请求体：

```json
{
  "name": "Primary",
  "base_url": "https://api.openai.com",
  "api_key": "sk-xxx",
  "model": "gpt-image-2",
  "api_type": "auto",
  "status": true
}
```

字段说明：

- `name`: 节点名称
- `base_url`: 上游服务根地址，必须以 `http://` 或 `https://` 开头
- `api_key`: 节点访问密钥
- `model`: 默认模型名
- `api_type`: 接入协议，`auto`（默认，当前节点内自动尝试 Images / async / Chat）/ `openai` / `chat` / `custom` / `async`
- `status`: 是否启用

成功时返回 `201 Created`。

### 2.3 更新节点

```http
PUT /api/configs/{id}
Content-Type: application/json
```

支持部分更新，例如只切换启用状态：

```json
{
  "status": false
}
```

更新时若 `api_key` 留空（空字符串或纯空格），后端会**保留原有密钥**，因此前端无需也无法拿回完整密钥再回填。

### 2.4 删除节点

```http
DELETE /api/configs/{id}
```

成功响应：

```json
{
  "success": true,
  "data": {
    "deleted": true,
    "id": "config-id"
  }
}
```

### 2.5 调整节点优先级

```http
POST /api/configs/reorder
Content-Type: application/json
```

请求体：

```json
{
  "ordered_ids": ["api-2", "api-1", "api-3"]
}
```

要求：

- `ordered_ids` 必须完整覆盖当前全部配置 ID
- 顺序会被持久化到 `backend/data/configs.json`
- 后端提交任务时会按这个顺序依次尝试

### 2.6 查看节点 API Key

```http
GET /api/configs/{id}/secret
```

返回：

```json
{
  "id": "config-id",
  "api_key": "sk-xxx"
}
```

前端只在用户选中节点或点击显示按钮时调用该接口；列表接口不会返回明文 Key。

## 3. 文生图

### 请求

```http
POST /api/generate
Content-Type: application/json
```

请求体：

```json
{
  "prompt": "A cinematic transparent desktop console",
  "size": "1024x1024",
  "n": 1
}
```

字段说明：

- `prompt`: 必填，不能为空
- `size`: 必填，格式必须为 `宽x高`，例如 `1024x1024`
- `n`: 必填，正整数

可选字段：

- `quality`: `auto` / `low` / `medium` / `high`（OpenAI 兼容节点透传）

### 响应

提交后任务进入后台 worker，立即返回 `202 Accepted`，只带本地 `task_id`：

```json
{
  "success": true,
  "data": {
    "task_id": "5f3c…",
    "status": "queued",
    "operation": "generate"
  }
}
```

命中的节点（`api_id`/`api_name`）与每个节点是否成功的 `attempts` 记录会在任务执行完成后，通过 `GET /api/status` 返回。

## 4. 局部编辑

### 请求

```http
POST /api/edit
Content-Type: application/json
```

请求体示例：

```json
{
  "prompt": "Only modify the masked area and replace it with a glass control panel",
  "image": "data:image/png;base64,...",
  "mask": "data:image/png;base64,...",
  "size": "1024x1024",
  "n": 1,
  "edit_mode": "mask",
  "selection": {
    "type": "brush",
    "canvas": {
      "width": 720,
      "height": 520
    },
    "bbox": {
      "x": 10,
      "y": 20,
      "width": 120,
      "height": 160
    }
  }
}
```

字段说明：

- `prompt`: 必填，描述希望如何修改遮罩区域
- `image`: 必填，原图 `data URL`
- `mask`: 必填，遮罩图 `data URL`
- `size`: 必填，输出尺寸
- `n`: 必填，生成张数
- `edit_mode`: 当前支持 `mask` 或 `selection`
- `selection`: 可选，前端附带的选区元数据

关于 `selection`：

- `type`: `brush` 或 `rect`
- `canvas`: 前端画布尺寸
- `bbox`: 当前 mask 的最小包围盒

### 响应

成功时同样返回 `202 Accepted`，结构与 `/api/generate` 相同，只是：

- `operation` 固定为 `edit`

### 后端行为

- `openai` 节点：以 `multipart/form-data` 调用 `POST {base_url}/v1/images/edits`，把原图与遮罩作为 `image`、`mask` 文件上传。遮罩由后端用 Pillow 依据 `selection.box` 裁剪回原图区域、缩放到原图尺寸，并反转透明区域（OpenAI 约定：透明处即编辑区）。
- `async` 节点：以 JSON 调用 `POST {base_url}/async/images`，在请求体中附带 `image`、`mask`、`edit_mode`、`selection`。

## 5. 查询任务状态

### 请求

```http
GET /api/status?task_id={task_id}
```

任务状态保存在后端进程内的任务表中，因此只需 `task_id` 即可查询（`api_id` 参数可选，仅作兼容保留）。

### 响应

```json
{
  "success": true,
  "data": {
    "api_id": "config-id",
    "api_name": "Primary",
    "task_id": "5f3c…",
    "operation": "generate",
    "status": "completed",
    "urls": ["data:image/png;base64,...", "https://cdn.example.com/image.png"],
    "attempts": [
      { "api_id": "config-id", "api_name": "Primary", "ok": true }
    ],
    "expires_at": null,
    "error": null
  }
}
```

`status` 取值：

- `queued`
- `processing`
- `completed`
- `failed`

说明：

- `api_id`/`api_name` 表示 worker 最终命中（或正在尝试）的节点，可能为 `null`（任务刚入队时）。
- `attempts` 记录每个被尝试节点是否成功，便于排查容灾路径。
- `urls` 只在 `completed` 时有意义；OpenAI 兼容节点通常是 `data:` URL，异步中转节点通常是远程 URL。
- `max_wait_seconds` 表示前端本次轮询可等待的秒数。OpenAI 兼容同步请求会取节点 `timeout_seconds` 与后端 `generation_timeout` 的较大值；异步中转进入云端生成后返回 `null`，表示持续等待直到完成、失败或手动停止。
- 任务不存在或已过期返回 `404`。

## 6. 会话、草稿与作品集

### 6.1 查询已完成会话

```http
GET /api/sessions
```

读取后端 `history/<会话ID>/session.json`，返回所有已完成且有图片的会话。作品集页面使用该接口渲染照片墙。

### 6.2 局部编辑草稿

```http
GET /api/edit-drafts/{history_id}
PUT /api/edit-drafts/{history_id}
```

草稿保存到对应会话目录下的 `edit-draft.json`，包含上传原图、蒙版、工具状态和画笔参数。切回局部编辑历史节点时前端会恢复这些痕迹。

### 6.3 提示词模板

```http
GET    /api/prompt-templates
POST   /api/prompt-templates
PUT    /api/prompt-templates/{id}
DELETE /api/prompt-templates/{id}
```

提示词模板保存到 `backend/data/prompt_templates.json`。前端「示例」按钮会从该模板列表随机选择一条；当前输入非空时会确认是否覆盖。

## 7. 常见错误

### 400 Bad Request

常见原因：

- `prompt` 为空
- `size` 不是 `1024x1024` 这类格式
- `n` 不是正整数
- `image` 或 `mask` 不是合法的 `data:image/*;base64,...`
- `ordered_ids` 不是完整的节点 ID 列表

### 404 Not Found

常见原因：

- 查询或修改了不存在的配置项
- 轮询时提供了不存在或已过期的 `task_id`

### 413 Payload Too Large

- 请求体超过 `25MB`（局部编辑会内联原图 + 遮罩的 base64，注意原图大小）

### 502 Bad Gateway

常见原因（记录在任务的 `attempts` 与 `error` 中）：

- 所有启用节点均失败
- 上游接口超时
- 上游返回了非 JSON 响应
- 上游返回了 4xx/5xx，或响应缺少图片数据（OpenAI 的 `data[].b64_json`/`url`，或异步中转的 `task_id`）

## 8. 上游图片服务契约

### 8.1 自动尝试协议（`api_type = auto`，默认）

后端会在当前节点内依次尝试协议候选：OpenAI Images、异步中转、Chat Completions；已知异步中转域名会优先尝试 async。只有当前节点的协议候选全部失败后，才会切换到下一个启用节点。状态响应会返回 `configured_api_type`、`effective_api_type` 和 `request_url`，该过程不会改写 `backend/data/configs.json` 中保存的节点协议。

`auto` 只表示“当前节点内自动尝试已接入协议”，不控制后续节点 fallback；后续启用节点始终会在当前节点全部候选失败后继续尝试。`custom` 需要完整 URL，无法从 `base_url` 安全推断，因此不参与自动协议序列。

### 8.2 OpenAI 兼容（`api_type = openai`）

```text
POST {base_url}/v1/images/generations   # JSON
POST {base_url}/v1/images/edits         # multipart/form-data
```

文生图示例提交体（JSON）：

```json
{
  "model": "gpt-image-2",
  "prompt": "A cinematic transparent desktop console",
  "size": "1024x1024",
  "n": 1
}
```

局部编辑使用 `multipart/form-data`，字段：`model`、`prompt`、`size`、`n`、`image`（文件）、`mask`（文件，PNG，透明处即编辑区）。

响应（两类接口一致）：

```json
{
  "created": 1780309714,
  "data": [{ "b64_json": "..." }]
}
```

后端会把 `b64_json` 转成 `data:image/...;base64,...`，或直接透传 `url`。

#### xAI / Grok Imagine

文生图可作为 OpenAI 兼容节点接入：

```text
base_url = https://api.x.ai
api_type = openai 或 auto
model    = grok-imagine-image-quality（或服务商实际开放的 Grok Imagine 模型名）
```

当模型名以 `grok-imagine-image` 开头或 `base_url` 为 `https://api.x.ai` 时，后端仍请求 `POST /v1/images/generations`，但会把前端 `size` 转换为 xAI 风格的 `aspect_ratio` 和 `resolution`，并不发送任意 `size` 字段。当前已按 OpenAI 兼容路径适配文生图；Grok 局部编辑不保证兼容当前 multipart edits，如需使用 xAI JSON 形态编辑接口需要单独增加适配。

### 8.3 自定义异步中转（`api_type = async`）

```text
POST {base_url}/async/images
GET  {base_url}/async/images/{task_id}
```

提交体为 JSON，局部编辑时在 `model/prompt/size/n` 基础上增加 `image`、`mask`、`edit_mode`、`selection`；后端在 worker 内提交后轮询 `GET` 直至 `completed`/`failed`。

提交成功响应必须包含 `task_id`，可选包含 `poll_url`：

```json
{
  "task_id": "47528f39a8644bdfae66dc0bb1f430dd",
  "status": "queued",
  "poll_url": "/async/images/47528f39a8644bdfae66dc0bb1f430dd"
}
```

如果返回 `poll_url`，后端按该地址轮询；否则默认轮询 `GET {base_url}/async/images/{task_id}`。轮询响应可直接返回状态对象，也可包在 `data` 对象内。`status=completed` 时从 `urls` 数组取图片；`status=failed` 时读取 `error`。拿到 `task_id` 后后端不会再提交第二次任务，单次轮询超时只更新状态并继续轮询，避免已扣费任务丢失。
