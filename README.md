# GPT Img2 Creater

`GPT Img2 Creater` 是一个本地运行的桌面端图片工作台，用来管理多个异步图片生成 API 节点，并在文生图之外支持局部编辑流程。

当前版本只实现 PC 端体验，界面参考工作台式布局：左侧是导航和配置，右侧是生成控制台、局部编辑画布、任务状态和结果画廊。

## 当前能力

- 文生图：提交 `prompt`、尺寸和生成张数，由后台任务完成生成。
- 局部编辑：上传原图后，可用矩形框选或画笔涂抹生成 `mask`，再把原图和遮罩提交给 AI 修改。
- 多接入协议：每个节点可选 **OpenAI 兼容**（标准 `/v1/images` 接口，gpt-image-2 默认）或 **异步中转**（自定义 `/async/images`）。
- 多 API 节点：支持新增、编辑、删除、启用/禁用和拖拽排序。
- Fallback 容灾：后台 worker 按节点优先级依次尝试，直到某个节点成功产出图片。
- 状态轮询：前端每 4 秒轮询一次任务状态，最长轮询 5 分钟。
- 结果画廊：生成完成后直接展示图片结果，并支持下载。
- PC-only：当前不做移动端适配，页面最小宽度为 `1280px`。

## 技术栈

- 后端：Python 3、Flask、flask-cors、requests、Pillow
- 前端：Vue 3、Vite、Element Plus、Tailwind CSS、Axios
- 本地存储：`backend/data/configs.json`
- 主题：浅色 / 深色双主题（首启跟随系统，可手动切换并记忆）

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

如果项目还没有虚拟环境，可以先创建并且激活(linux也类似，但是激活命令不一样)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# linux
.venv/bin/activate 
```

安装依赖：

```powershell
python -m pip install -r backend\requirements.txt
```

### 2. 启动后端

```powershell
python -m backend.app
```

默认地址：

- API: `http://127.0.0.1:5000`
- 健康检查: `http://127.0.0.1:5000/api/health`

### 3. 安装前端依赖

```powershell
cd frontend
npm install
```

### 4. 启动前端

```powershell
npm run dev
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

## 任务模型

GPT-Image-2 通过兼容 OpenAI 协议的接口访问，单次生成可能耗时数十秒甚至数分钟，超过浏览器请求超时。因此整个生命周期放在**后端 worker 线程**中执行，结果写入进程内的任务表（`task_store`）：

1. 前端 `POST /api/generate` 或 `/api/edit`，后端立即创建本地任务并返回 `task_id`（HTTP 202）。
2. worker 读取启用节点，按优先级依次调用上游：
   - `openai` 节点：同步调用 `/v1/images/generations` 或 `/v1/images/edits`，把返回的 `b64_json`/`url` 规整为可直接展示的链接。
   - `async` 节点：提交 `/async/images` 后由后端在 worker 内轮询直到完成。
   - 任一节点失败即切换到下一个启用节点（容灾在 worker 内统一完成）。
3. 前端每 4 秒 `GET /api/status?task_id=...` 读取最新状态、命中的节点与尝试记录，直到 `completed`/`failed`。

任务表仅存在于内存中，进程重启后不保留；并设有 TTL 自动回收，适合本地单用户场景。

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
- `api_type`：`openai`（OpenAI 兼容，默认）或 `async`（自定义异步中转）
- `status`
- `created_at`
- `updated_at`

列表顺序就是任务提交时的优先级顺序。

## 上游接口约定

默认按**兼容 OpenAI 协议**的接口访问（`api_type = openai`）：

```text
POST {base_url}/v1/images/generations   # 文生图，JSON
POST {base_url}/v1/images/edits         # 局部编辑，multipart（image + mask 文件）
```

- `base_url` 已以 `/v1` 结尾时不会重复拼接。
- 响应中的 `b64_json` 会转成 `data:image/...;base64,...`，`url` 则原样透传。
- 局部编辑的 `mask` 会由后端用 Pillow 按原图尺寸对齐，并反转透明区域以符合 OpenAI「透明处即编辑区」的约定。

若节点设为 `api_type = async`，则改用自定义异步中转协议：

```text
POST {base_url}/async/images
GET  {base_url}/async/images/{task_id}
```

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
