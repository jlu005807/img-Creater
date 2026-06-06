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

### 2.2 创建节点

```http
POST /api/configs
Content-Type: application/json
```

请求体：

```json
{
  "name": "Primary",
  "base_url": "https://example.com",
  "api_key": "sk-xxx",
  "model": "gpt-image-2",
  "status": true
}
```

字段说明：

- `name`: 节点名称
- `base_url`: 上游服务根地址，必须以 `http://` 或 `https://` 开头
- `api_key`: 节点访问密钥
- `model`: 默认模型名
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

### 响应

成功时返回 `202 Accepted`：

```json
{
  "success": true,
  "data": {
    "task_id": "task-123",
    "api_id": "config-id",
    "api_name": "Primary",
    "status": "queued",
    "poll_url": "/async/images/task-123",
    "model": "gpt-image-2",
    "operation": "generate",
    "attempts": [
      {
        "api_id": "config-id",
        "api_name": "Primary",
        "ok": true
      }
    ]
  }
}
```

`attempts` 用来记录本次提交经过了哪些节点，以及每个节点是否成功接单。

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

局部编辑不会走单独的新协议，后端仍然向上游发送：

```text
POST {base_url}/async/images
```

只是会在 JSON 体中增加 `image`、`mask`、`edit_mode`、`selection` 这些字段。

## 5. 查询任务状态

### 请求

```http
GET /api/status?api_id={api_id}&task_id={task_id}
```

### 响应

```json
{
  "success": true,
  "data": {
    "api_id": "config-id",
    "api_name": "Primary",
    "task_id": "task-123",
    "status": "completed",
    "urls": ["https://cdn.example.com/image.png"],
    "expires_at": 1780309714,
    "error": null,
    "raw": {
      "status": "completed",
      "urls": ["https://cdn.example.com/image.png"],
      "expires_at": 1780309714
    }
  }
}
```

`status` 常见取值：

- `queued`
- `processing`
- `completed`
- `failed`

说明：

- 轮询时必须带上原始提交时返回的 `api_id`
- 一旦某个节点成功接单，后续状态查询必须回到同一个节点
- `urls` 只在任务完成时有意义

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
- 轮询时提供了无效的 `api_id`

### 502 Bad Gateway

常见原因：

- 所有启用节点都提交失败
- 上游接口超时
- 上游返回了非 JSON 响应
- 上游返回了 4xx/5xx，或响应中缺少 `task_id`

## 7. 上游图片服务契约

当前项目约定上游服务至少提供两类接口：

```text
POST {base_url}/async/images
GET  {base_url}/async/images/{task_id}
```

示例提交体：

```json
{
  "model": "gpt-image-2",
  "prompt": "A cinematic transparent desktop console",
  "size": "1024x1024",
  "n": 1
}
```

局部编辑时会在此基础上增加：

```json
{
  "image": "data:image/png;base64,...",
  "mask": "data:image/png;base64,...",
  "edit_mode": "mask",
  "selection": {}
}
```
