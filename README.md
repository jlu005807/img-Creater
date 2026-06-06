# GPT Img2 Creater

`GPT Img2 Creater` 是一个本地运行的桌面端图片工作台，用来管理多个异步图片生成 API 节点，并在文生图之外支持局部编辑流程。

当前版本只实现 PC 端体验，界面参考工作台式布局：左侧是导航和配置，右侧是生成控制台、局部编辑画布、任务状态和结果画廊。

## 当前能力

- 文生图：提交 `prompt`、尺寸和生成张数，走异步任务队列。
- 局部编辑：上传原图后，可用矩形框选或画笔涂抹生成 `mask`，再把原图和遮罩提交给 AI 修改。
- 多 API 节点：支持新增、编辑、删除、启用/禁用和拖拽排序。
- Fallback 提交：提交任务时会按节点优先级依次尝试，直到某个节点成功返回 `task_id`。
- 状态轮询：前端每 4 秒轮询一次任务状态，最长轮询 5 分钟。
- 结果画廊：生成完成后直接展示图片结果，并支持下载。
- PC-only：当前不做移动端适配，页面最小宽度为 `1280px`。

## 技术栈

- 后端：Python 3、Flask、flask-cors、requests
- 前端：Vue 3、Vite、Element Plus、Tailwind CSS、Axios
- 本地存储：`backend/data/configs.json`

## 目录结构

```text
backend/
  app.py
  data/configs.json
  routes/
    configs.py
    generation.py
  services/
    config_service.py
    image_service.py

frontend/
  src/
    api/
    components/
      APIConfig/
      Playground/
      RegionEditor/
    App.vue
    styles.css

docs/
  API.md
  ARCHITECTURE.md

tests/
  test_backend_routes.py
  test_backend_services.py
```

## 环境要求

- Windows PowerShell
- Python `3.10+`
- Node.js `18+`
- npm `9+`

## 快速开始

### 1. 安装后端依赖

如果项目还没有虚拟环境，可以先创建：

```powershell
python -m venv .venv
```

安装依赖：

```powershell
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

### 2. 启动后端

```powershell
.venv\Scripts\python.exe -m backend.app
```

默认地址：

- API: `http://127.0.0.1:5000`
- 健康检查: `http://127.0.0.1:5000/api/health`

### 3. 安装前端依赖

```powershell
cd frontend
npm.cmd install
```

### 4. 启动前端

```powershell
npm.cmd run dev
```

默认地址：

- Web UI: `http://127.0.0.1:5173`

开发环境下，Vite 会把 `/api` 请求代理到 `http://127.0.0.1:5000`。

### 5. 打开界面并配置第一个 API 节点

1. 打开 `http://127.0.0.1:5173`
2. 进入左侧 `设置`
3. 新增一个节点，至少填写：
   - `name`
   - `base_url`
   - `api_key`
   - `model`
4. 保持节点为启用状态
5. 如有多个节点，可在列表中拖拽调整优先级

## 使用说明

### 文生图

1. 进入左侧 `工作区`
2. 保持模式为 `文生图`
3. 输入 `Prompt`
4. 选择图片尺寸和生成张数
5. 点击提交按钮
6. 等待状态从 `queued/processing` 进入 `completed`
7. 在右侧结果区查看和下载图片

### 局部编辑

1. 在 `工作区` 切换到 `局部编辑`
2. 上传原图
3. 选择编辑工具：
   - `涂抹`：适合不规则区域
   - `框选`：适合矩形区域
4. 在画布上标出需要修改的区域
5. 输入编辑提示词，描述只应修改被遮罩覆盖的部分
6. 提交任务
7. 前端会自动轮询 `/api/status`，直到返回结果

## 异步任务模型

无论文生图还是局部编辑，后端都只负责：

1. 读取本地启用的 API 节点列表
2. 按优先级向上游提交异步图片任务
3. 返回 `task_id`、`api_id` 和尝试记录
4. 使用同一个 `api_id` 轮询任务状态

任务真正完成发生在第三方图片服务端，前端不会阻塞等待单个 HTTP 请求直到生成结束。

## 配置说明

本地节点配置保存在：

```text
backend/data/configs.json
```

每个节点包含：

- `id`
- `name`
- `base_url`
- `api_key`
- `model`
- `status`
- `created_at`
- `updated_at`

列表顺序就是任务提交时的优先级顺序。

## 上游接口约定

当前后端假设第三方中转站实现如下接口：

```text
POST {base_url}/async/images
GET  {base_url}/async/images/{task_id}
```

局部编辑提交时会额外带上：

- `image`
- `mask`
- `edit_mode`
- `selection`

详细字段见 [docs/API.md](docs/API.md)。

## 验证命令

后端测试：

```powershell
.venv\Scripts\python.exe -m unittest discover tests
```

前端构建：

```powershell
cd frontend
npm.cmd run build
```

## 常见问题

### 页面打不开

- 确认后端运行在 `127.0.0.1:5000`
- 确认前端运行在 `127.0.0.1:5173`
- 确认本机没有其他进程占用这两个端口

### 提交任务后立刻失败

- 检查 API 节点的 `base_url` 是否可访问
- 检查 `api_key` 是否有效
- 检查上游是否实现了 `/async/images` 协议

### 局部编辑无法提交

- 必须同时存在原图和遮罩
- `image` 与 `mask` 都必须是 `data:image/*;base64,...` 格式

## 相关文档

- [接口文档](docs/API.md)
- [实现架构](docs/ARCHITECTURE.md)
