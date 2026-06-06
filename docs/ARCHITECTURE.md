# 实现架构

## 1. 总览

项目采用前后端分离结构：

```text
Vue 3 Desktop UI
  -> Axios /api
  -> Flask Routes
  -> Service Layer
  -> Third-party async image API
```

系统目标不是自己生成图片，而是作为一个本地工作台去管理配置、提交异步任务、轮询状态和展示结果。

## 2. 运行角色划分

### 前端负责

- PC 工作台布局
- 文生图与局部编辑两种交互模式
- 原图上传和遮罩绘制
- 任务状态轮询节奏
- 超时控制
- 结果展示与下载

### 后端负责

- 管理本地 API 节点配置
- 校验请求参数
- 按节点优先级执行 Fallback 提交
- 将异步任务状态查询代理到原始接单节点
- 对前端统一返回结构化错误

### 上游服务负责

- 真正执行图片生成或编辑
- 返回 `task_id`
- 提供任务状态和结果 URL

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

- 校验生成请求参数
- 校验局部编辑请求参数
- 调用上游异步图片接口
- 处理提交阶段的 Fallback
- 轮询指定节点上的任务状态

关键方法：

- `submit_generation(...)`
- `submit_edit_generation(...)`
- `poll_generation_status(...)`

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
  -> generation.py
  -> ImageService.submit_generation()
  -> Try enabled providers in order
  -> Upstream returns task_id
  -> Frontend stores api_id + task_id
  -> Frontend polls GET /api/status every 4s
  -> Upstream returns completed + urls
  -> Gallery renders images
```

## 6. 局部编辑数据流

```text
User uploads image
  -> RegionEditor draws mask
  -> RegionEditor exports image + mask + selection
  -> Playground calls POST /api/edit
  -> generation.py
  -> ImageService.submit_edit_generation()
  -> Try enabled providers in order
  -> Upstream returns task_id
  -> Frontend polls GET /api/status every 4s
  -> Completed result displayed in gallery
```

## 7. Fallback 机制

Fallback 只发生在“提交任务”这一步。

具体规则：

1. 读取全部启用节点
2. 按配置数组顺序依次尝试提交到 `{base_url}/async/images`
3. 某个节点成功返回 `task_id` 后立即停止继续尝试
4. 把每次尝试结果写入 `attempts`
5. 前端后续轮询必须使用成功接单节点的 `api_id`

这样做的原因是：

- 任务一旦进入某个上游系统，就必须回到原系统查询状态
- 轮询阶段不能再做节点切换，否则无法保证任务一致性

## 8. 局部编辑的遮罩模型

前端局部编辑器内部维护一张独立的遮罩画布：

- 背景透明
- 用户涂抹或框选时写入白色不透明区域
- 提交前导出为 PNG `data URL`

后端只验证格式，不对遮罩内容做图像级处理。

这意味着：

- 具体如何理解 `mask`，由上游图片服务决定
- 本项目只保证把原图、遮罩和附带元数据稳定发送出去

## 9. 错误处理策略

### 配置错误

- 无效字段：返回 `400`
- 配置不存在：返回 `404`
- 本地存储读写失败：返回 `500`

### 图片任务错误

- 请求参数错误：返回 `400`
- 轮询时找不到节点：返回 `404`
- 上游全部失败：返回 `502`
- 上游返回异常结构：返回 `502`

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
