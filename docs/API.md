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
  "service": "gpt-img2-creater-backend"
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
  "api_type": "openai",
  "status": true
}
```

字段说明：

- `name`: 节点名称
- `base_url`: 上游服务根地址，必须以 `http://` 或 `https://` 开头
- `api_key`: 节点访问密钥
- `model`: 默认模型名
- `api_type`: 接入协议，`openai`（OpenAI 兼容，默认）或 `async`（自定义异步中转）
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
- 任务不存在或已过期返回 `404`。

## 6. 常见错误

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

## 7. 上游图片服务契约

### 7.1 OpenAI 兼容（`api_type = openai`，默认）

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

### 7.2 自定义异步中转（`api_type = async`）

```text
POST {base_url}/async/images
GET  {base_url}/async/images/{task_id}
```

提交体为 JSON，局部编辑时在 `model/prompt/size/n` 基础上增加 `image`、`mask`、`edit_mode`、`selection`；后端在 worker 内提交后轮询 `GET` 直至 `completed`/`failed`。
