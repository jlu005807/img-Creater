# 实现架构

## 1. 总览

项目采用前后端分离结构：

```text
Vue 3 Desktop UI
  -> Axios /api
  -> Flask Routes
  -> Service Layer (worker thread + in-process TaskStore)
  -> Upstream image API (OpenAI 兼容 /v1/images，或自定义 /async/images)
```

系统目标不是自己生成图片，而是作为一个本地工作台去管理配置、把任务交给后台 worker 调用上游、轮询状态和展示结果。

## 2. 运行角色划分

### 前端负责

- PC 工作台布局
- 文生图与局部编辑两种交互模式
- 原图上传和遮罩绘制
- 任务状态轮询节奏
- 超时控制
- 结果展示与下载

### 后端负责

- 管理本地 API 节点配置（含每节点的 `api_type`）
- 校验请求参数
- 在 worker 线程内按节点优先级执行容灾，并适配不同上游协议（OpenAI 兼容 / 异步中转）
- 把任务生命周期与结果保存在进程内 `TaskStore`，供 `/status` 查询
- 对前端统一返回结构化错误

### 上游服务负责

- 真正执行图片生成或编辑
- OpenAI 兼容：同步返回 `data[].b64_json` / `url`
- 异步中转：返回 `task_id` 并提供状态查询

## 3. 后端结构

```text
backend/
  app.py
  routes/
    configs.py
    generation.py
  services/
    config_service.py
    image_service.py
    task_store.py
  data/
    configs.json
```

### `app.py`

职责：

- 创建 Flask 应用
- 注册路由蓝图
- 暴露 `/api/health`
- 配置 CORS

### `routes/configs.py`

职责：

- 暴露配置管理接口
- 把请求 JSON 转成 `ConfigService` 调用
- 对配置相关异常做 HTTP 映射

接口包括：

- `GET /api/configs`
- `POST /api/configs`
- `PUT /api/configs/{id}`
- `DELETE /api/configs/{id}`
- `POST /api/configs/reorder`

### `routes/generation.py`

职责：

- 接收文生图请求
- 接收局部编辑请求
- 接收任务状态查询请求
- 把 `ImageService` 的结果统一包装成标准响应

接口包括：

- `POST /api/generate`
- `POST /api/edit`
- `GET /api/status`

### `services/config_service.py`

职责：

- 维护 `backend/data/configs.json`
- 处理配置的增删改查
- 处理节点启用状态
- 处理拖拽排序后的优先级持久化

关键规则：

- 配置顺序就是任务提交顺序
- 只会从启用节点中挑选候选项
- 更新和创建时统一做字段归一化

### `services/image_service.py`

职责：

- 校验生成 / 局部编辑请求参数
- 在 worker 线程内按节点优先级容灾执行
- 按节点 `api_type` 适配上游协议：
  - `openai`：`/v1/images/generations`（JSON）、`/v1/images/edits`（multipart），把 `b64_json`/`url` 规整为可展示链接；编辑时用 Pillow 对齐并反转遮罩
  - `async`：提交 `/async/images` 后在 worker 内轮询直至完成
- 把状态/结果写入 `TaskStore`

关键方法：

- `submit_generation(...)` / `submit_edit_generation(...)`：建任务、起 worker，立即返回 `task_id`
- `_execute_task(...)`：worker 主体，遍历节点 + 容灾
- `poll_generation_status(task_id)`：从 `TaskStore` 读取状态

### `services/task_store.py`

职责：

- 进程内、线程安全的任务表（`create` / `update` / `get`）
- 记录 `status` / `urls` / `attempts` / `api_id` / `api_name` / `error` / `expires_at`
- 按 TTL 自动回收，进程重启不保留

## 4. 前端结构

```text
frontend/src/
  api/
    client.js
    configs.js
    generation.js
  components/
    APIConfig/index.vue
    Playground/index.vue
    RegionEditor/index.vue
  App.vue
  styles.css
```

### `App.vue`

这是整个桌面工作台的外壳。

职责：

- 渲染左侧导航
- 在 `工作区` 和 `设置` 两个主视图之间切换
- 保持 PC-only 布局

### `components/APIConfig/index.vue`

职责：

- 展示节点配置列表
- 编辑节点表单
- 控制启用/禁用
- 通过拖拽提交重排结果

前端调用：

- `listConfigs()`
- `createConfig()`
- `updateConfig()`
- `deleteConfig()`
- `reorderConfigs()`

### `components/Playground/index.vue`

职责：

- 管理工作区的主状态
- 切换 `文生图` 与 `局部编辑`
- 发起 `/api/generate` 或 `/api/edit`
- 每 4 秒轮询一次 `/api/status`
- 维护任务信息、耗时、错误和结果图片列表

这里是整个交互链路的中心组件。

### `components/RegionEditor/index.vue`

职责：

- 上传本地原图
- 在固定画布上绘制遮罩
- 支持两种编辑工具：
  - `brush`
  - `rect`
- 导出：
  - `image`
  - `mask`
  - `selection`

`selection` 不是图像内容本身，而是辅助元数据，用于把前端选区信息一起传给后端和上游服务。

### `api/client.js`

职责：

- 创建统一 Axios 实例
- 设置 `/api` 作为默认前缀
- 拦截统一响应格式
- 标准化错误对象

## 5. 文生图数据流

```text
User fills prompt and options
  -> Playground calls POST /api/generate
  -> generation.py -> ImageService.submit_generation()
  -> TaskStore.create() + start worker thread, return task_id (202)
  -> Worker: try enabled providers in order
       openai -> POST {base}/v1/images/generations -> b64_json/url
       async  -> POST {base}/async/images then poll until completed
  -> Worker writes status/urls/attempts into TaskStore
  -> Frontend polls GET /api/status?task_id every 4s
  -> Gallery renders images
```

## 6. 局部编辑数据流

```text
User uploads image
  -> RegionEditor draws mask + exports image + mask + selection(box)
  -> Playground calls POST /api/edit
  -> generation.py -> ImageService.submit_edit_generation()
  -> TaskStore.create() + start worker, return task_id (202)
  -> Worker per provider:
       openai -> Pillow rebuilds mask (crop to box, resize, invert alpha)
               -> POST {base}/v1/images/edits (multipart image+mask)
       async  -> POST {base}/async/images (JSON, with image/mask) then poll
  -> Frontend polls GET /api/status?task_id every 4s
  -> Completed result displayed in gallery
```

## 7. 容灾（Fallback）机制

容灾发生在 worker 线程内，对两种协议统一生效。

具体规则：

1. worker 读取全部启用节点，按配置数组顺序依次尝试
2. 每个节点执行完整生命周期（openai 同步调用；async 提交后轮询）
3. 某个节点成功产出图片即停止，写入 `completed` + `urls`
4. 每次尝试结果写入任务的 `attempts`
5. 全部失败则任务置为 `failed`，并带上 `error`

因为状态保存在后端 `TaskStore` 中，前端只用 `task_id` 查询，无需在轮询阶段绑定具体节点。

## 8. 局部编辑的遮罩模型与数据流

用户**无需手动制作蒙版**：上传原图后直接在图上涂抹/框选，标记区域以半透明青色实时叠加显示。前端局部编辑器内部维护一张独立的遮罩画布：

- 背景透明
- 用户涂抹（画笔/橡皮擦）或框选时写入白色不透明区域
- 提交前自动导出三样东西（PNG `data URL`）：
  - `image`：原图
  - `mask`：原图分辨率的遮罩（标记区域为不透明白）
  - `composite`：**原图 + 半透明遮罩叠加合成的一张混合图**，可被需要单图的上游直接使用
  - 并附带图片在画布中的 letterbox 矩形 `selection.box`

完整数据流：

```text
上传原图 + 涂抹/框选 → 生成遮罩 + 合成半透明混合图(composite)
  → 连同 prompt 提交 /api/edit
  → 后端按协议处理并提交上游 → 输出修改后的图片
```

后端处理因协议而异：

- `openai`：用 Pillow 把画布尺寸的遮罩按 `selection.box` 裁回原图区域、缩放到原图尺寸，并**反转 alpha**（OpenAI 约定：透明处即编辑区）后作为 `mask` 文件上传
- `async` / `custom`：原样把 `image`/`mask`/`composite`/`selection` 透传给上游

## 8.1 参考图（文生图）

文生图可附带最多 N 张参考图（上限在设置中可配置，默认 3；后端硬上限 8）。前端以 `reference_images`（data URL 数组）提交，后端按协议转发：

- `openai`：走 `/v1/images/edits`，参考图作为多个 `image[]` 文件上传
- `chat`：作为 `image_url` 内容块加入 `messages`
- `custom` / `async`：内联到 JSON 请求体的 `reference_images` 字段

## 9. 错误处理策略

### 配置错误

- 无效字段：返回 `400`
- 配置不存在：返回 `404`
- 本地存储读写失败：返回 `500`

### 图片任务错误

- 请求参数错误：返回 `400`
- 轮询时任务不存在或已过期：返回 `404`
- 上游全部失败 / 返回异常结构：记录在任务的 `attempts` 与 `error`，轮询得到 `failed`

前端会把后端返回的 `error.message` 直接展示给用户，并停止当前轮询。

## 10. PC-only 约束

当前界面明确以桌面端为目标：

- `body` 最小宽度为 `1280px`
- 主布局采用固定双栏工作台
- 没有为触屏和窄屏做交互让步

这么做是为了先把核心桌面工作流稳定下来，再决定是否拆分移动端体验。

## 11. 测试覆盖

当前自动化测试覆盖重点：

- 配置持久化与排序
- 生成任务的 Fallback 提交
- 局部编辑 payload 提交
- 状态轮询逻辑
- 配置路由
- 生成和编辑路由

执行命令：

```powershell
.venv\Scripts\python.exe -m unittest discover tests
```

前端构建验证：

```powershell
cd frontend
npm.cmd run build
```
