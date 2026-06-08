# img-Creater

`img-Creater` 是一个本地运行的桌面端图片工作台，用来管理多个图片生成 API 节点（兼容 OpenAI 协议的 gpt-image-2 等），并在文生图之外支持局部编辑流程。

当前版本只实现 PC 端体验，采用工作台式布局：顶栏放标题、主题切换与设置入口（齿轮 → 弹窗）；下方左侧是会话历史队列，中间是生成控制台与局部编辑画布，右侧是任务状态与结果画廊。

## 当前能力

- 文生图：提交 `prompt` 与尺寸，由后台任务完成生成（默认 1 张，按上游返回数量展示）。
- 参考图：文生图可上传最多 N 张参考图辅助生成（上限在设置中可配置，默认 3）。
- 局部编辑：上传/拖拽原图后直接在图上涂抹、框选、橡皮擦标记修改区域，半透明遮罩实时叠加；无需手动制作蒙版，前端自动合成混合图。支持按钮撤销、遮罩显隐切换、整体放大编辑，并会按会话保存原图与编辑痕迹。
- 尺寸：宽高自定义输入 + 比例预设（1:1 / 4:3 / 3:4 / 16:9 / 9:16）。
- 多接入协议：每个节点可选 **OpenAI 兼容**（`/v1/images`）、**Chat Completions**（`/v1/chat/completions`）、**自定义 URL**（直接请求）或 **异步中转**（`/async/images`）。
- 多 API 节点：支持新增、编辑、删除、启用/禁用和拖拽排序；返回时 `api_key` 自动脱敏。
- Fallback 容灾：后台 worker 按节点优先级依次尝试，直到某个节点成功产出图片。
- 状态轮询：前端每 4 秒轮询一次任务状态，轮询上限跟随当前 API 节点的 `timeout_seconds`。
- 结果画廊：按实际宽高比完整展示（不裁剪），支持全屏灯箱预览与可靠的跨域下载。
- 生成历史：左侧会话队列含实时状态标记，支持模糊搜索、时间筛选、失败重试覆盖、复用参数、回看结果；成功结果会按会话目录写入后端 `history/`。
- 作品集：顶部「作品集」页面读取后端已完成会话，以照片墙展示所有成功图片，支持放大预览和下载。
- 设置：弹窗式，含 API 设置、偏好（最大字数 / 参考图上限）、提示词模板管理。
- 提示词：示例按钮会从设置中的模板随机抽取；当前输入非空时会确认是否覆盖。支持放大编辑、字数外显、可配置上限（默认 3000）。
- 主题：浅色 / 深色双主题（首启跟随系统，可手动切换并记忆）。
- PC-only：当前不做移动端适配，页面最小宽度为 `1280px`。

## 技术栈

- 后端：Python 3、Flask、flask-cors、requests、Pillow
- 前端：Vue 3、Vite、Element Plus、Tailwind CSS、Axios
- 后端配置存储：`backend/data/configs.json`（API 节点与 Key）、`backend/data/prompt_templates.json`（提示词模板）、`history/<会话ID>/`（图片、会话参数、局部编辑草稿）
- 前端本地存储：表单草稿、主题、偏好与本地历史索引仍会缓存于浏览器 `localStorage`；成功会话会从后端历史目录恢复

## 目录结构

```text
install.ps1 / install.sh   # 一键安装脚本（Windows / Linux·macOS）
run.ps1 / run.sh           # 一键运行脚本
.gitattributes             # 锁定行尾（*.sh 用 LF）

backend/
  app.py
  data/
    configs.example.json   # 配置模板（configs.json 运行时生成且被忽略）
    prompt_templates.json  # 提示词模板（运行时生成）
  routes/
    configs.py
    generation.py
  services/
    config_service.py
    image_service.py
    task_store.py          # 进程内任务表

frontend/
  src/
    api/                   # client / configs / generation
    components/
      APIConfig/           # API 节点管理
      Playground/          # 生成控制台 + 历史 + 结果画廊
      RegionEditor/        # 局部编辑画布
      Settings/            # 设置弹窗（API / 偏好 / 提示词模板）
    composables/
      useTheme.js
      useGenerationHistory.js
      useSettings.js       # 最大字数 / 参考图上限
      usePromptTemplates.js
    utils/
      download.js
    App.vue
    styles.css

docs/
  API.md
  ARCHITECTURE.md
  ROADMAP.md

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

### 方式一：一键脚本（推荐）

脚本会检查环境、创建虚拟环境、安装前后端依赖，然后同时启动前后端并自动打开浏览器。

Windows（PowerShell，在项目根目录）：

```powershell
.\install.ps1   # 一键安装：建 venv + 装后端/前端依赖
.\run.ps1       # 一键运行：启动 Flask + Vite，并打开 http://127.0.0.1:5173
```

> 若提示脚本被禁止运行，可先执行：`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

Linux / macOS：

```bash
chmod +x install.sh run.sh   # 首次需要赋予执行权限
./install.sh                  # 一键安装
./run.sh                      # 一键运行（Ctrl+C 同时停止前后端）
```

安装脚本会校验 Python `3.10+` 与 Node.js `18+`，缺失时给出明确提示。

### 方式二：手动步骤

#### 1. 安装后端依赖

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

#### 2. 启动后端

```powershell
python -m backend.app
```

默认地址：

- API: `http://127.0.0.1:5000`
- 健康检查: `http://127.0.0.1:5000/api/health`

#### 3. 安装前端依赖

```powershell
cd frontend
npm install
```

#### 4. 启动前端

```powershell
npm run dev
```

默认地址：

- Web UI: `http://127.0.0.1:5173`

开发环境下，Vite 会把 `/api` 请求代理到 `http://127.0.0.1:5000`。

### 5. 打开界面并配置第一个 API 节点

1. 打开 `http://127.0.0.1:5173`
2. 点击右上角 **齿轮图标** 打开设置，进入 `API 设置` 标签
3. 新增一个节点，至少填写：
   - `name`
   - `base_url`
   - `api_key`
   - `model`
   - `接入协议`（默认 `OpenAI 兼容`；可选 Chat Completions / 自定义 URL / 异步中转）
4. 保持节点为启用状态
5. 如有多个节点，可在列表中拖拽调整优先级（后端按从上到下顺序容灾）

## 使用说明

### 文生图

1. 保持模式为 `文生图`
2. 输入 `Prompt`（可点「示例」从设置模板中随机填入，或「放大」全屏编辑）
3. 选择尺寸（比例预设或自定义宽高）
4. 可选：上传最多 N 张参考图辅助生成（上限在设置中可调）
5. 点击「生成图片」
6. 等待状态从 `queued/processing` 进入 `completed`
7. 在右侧结果区查看（按原始比例完整展示）、点击放大灯箱预览、下载

### 局部编辑

1. 切换到 `局部编辑`
2. 上传或拖拽原图
3. 在原图上直接标记修改区域（半透明青色实时叠加）：
   - `涂抹`：画笔标记不规则区域
   - `框选`：矩形标记
   - `擦除`：去掉多余标记；可通过撤销按钮回退；可切换遮罩显隐、点「放大」精细编辑
4. 输入编辑提示词，描述只应修改被标记覆盖的部分
5. 提交任务（前端自动合成「原图 + 半透明遮罩」混合图与遮罩一并提交，无需手动制作蒙版）
6. 前端会自动轮询 `/api/status`，直到返回结果

> 历史记录在左侧队列实时显示状态（生成中 ⟳ / 已完成 ✓ / 失败 ✗），支持搜索、时间筛选、复用参数、查看结果，失败项可一键「重试并覆盖」。

## 任务模型

gpt-image-2 等模型通过兼容 OpenAI 协议的接口访问，单次生成可能耗时数十秒甚至数分钟，超过浏览器请求超时。因此整个生命周期放在**后端 worker 线程**中执行，结果写入进程内的任务表（`task_store`），成功图片与会话元数据会同步保存到后端 `history/<会话ID>/`：

1. 前端 `POST /api/generate` 或 `/api/edit`，后端立即创建本地任务并返回 `task_id`（HTTP 202）。
2. worker 读取启用节点，按优先级依次调用上游（按节点 `api_type` 适配协议）：
   - `openai`：同步调用 `/v1/images/generations` 或 `/v1/images/edits`，把返回的 `b64_json`/`url` 规整为可直接展示的链接。
   - `chat`：调用 `/v1/chat/completions`，从响应中解析图片。
   - `custom`：直接 POST 到填写的完整 URL，不拼接任何路径。
   - `async`：提交 `/async/images` 后由后端在 worker 内轮询直到完成。
   - 任一节点失败即切换到下一个启用节点；节点内部自动模式会优先尝试当前选中协议，再尝试该节点的其它协议。
3. 前端每 4 秒 `GET /api/status?task_id=...` 读取最新状态、命中的节点与尝试记录，直到 `completed`/`failed`。

任务状态表仅存在于内存中，进程重启后不保留运行中任务；已完成图片和会话元数据会保留在后端历史目录。

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
- `api_type`：接入协议，`openai`（默认）/ `chat` / `custom` / `async`
- `status`
- `created_at`
- `updated_at`

列表顺序就是任务提交时的优先级顺序。

> 安全：`backend/data/configs.json` 仅保存在本地，且已加入 `.gitignore`；列表接口只返回 `api_key` 脱敏预览。选中节点编辑或点击查看时，前端会通过 `/api/configs/{id}/secret` 临时取回完整 Key，并默认以密文输入框显示。

## 上游接口约定

默认按**兼容 OpenAI 协议**的接口访问（`api_type = openai`）：

```text
POST {base_url}/v1/images/generations   # 文生图，JSON
POST {base_url}/v1/images/edits         # 局部编辑，multipart（image + mask 文件）
```

- `base_url` 已以 `/v1` 结尾时不会重复拼接。
- 响应中的 `b64_json` 会转成 `data:image/...;base64,...`，`url` 则原样透传。
- 局部编辑的 `mask` 会由后端用 Pillow 按原图尺寸对齐，并反转透明区域以符合 OpenAI「透明处即编辑区」的约定。

其他协议：

- `api_type = chat`：`POST {base_url}/v1/chat/completions`，从响应中解析图片。
- `api_type = custom`：直接 `POST {base_url}`（填写完整地址，不拼接路径）。
- `api_type = async`：自定义异步中转——

```text
POST {base_url}/async/images
GET  {base_url}/async/images/{task_id}
```

文生图可附带 `reference_images`（参考图）；局部编辑会附带 `image` / `mask` / `composite`（自动合成的混合图）。详细字段见 [docs/API.md](docs/API.md)。

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

### 提交任务后失败

- 检查 API 节点的 `base_url` 是否可访问、`api_key` 是否有效
- 检查节点的 `接入协议` 是否与上游实际支持的协议一致（OpenAI 兼容 / Chat / 自定义 URL / 异步中转）
- 任务监视器的「尝试记录」会列出每个节点的失败原因，便于排查容灾路径

### 局部编辑无法提交

- 必须先上传原图，并在图上涂抹或框选出至少一块修改区域
- 无需手动制作蒙版——遮罩与混合图由前端自动生成

### 跨域 / 端口

- 后端默认仅允许 `http://127.0.0.1:5173`、`http://localhost:5173` 跨域；如改了前端地址，用 `FRONTEND_ORIGIN` 环境变量覆盖
- 上传体积上限 25MB（局部编辑会内联原图、遮罩、混合图的 base64）

## 相关文档

- [接口文档](docs/API.md)
- [实现架构](docs/ARCHITECTURE.md)
