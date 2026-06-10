# 优化路线图（UI / 正确性 / 安全 / 体验）

本文件记录对 `img-Creater` 的分阶段优化计划。每个阶段都是一次独立、可验证、可回滚的提交。
设计灵感参考两个开源项目：

- **CookSleep/gpt_image_playground**（React + Vite + Tailwind + Zustand）：本地生成历史（瀑布流 + 状态筛选 + 全屏预览 + 快速下载）、灯箱滑动切换、参考图拖拽上传、尺寸预设并自动对齐到 16 的倍数、"提交后清空/重启后保留输入"等习惯设置、蒙版编辑器自动预处理到分辨率上限。
- **yuzujr/AIWatch**（Vue 3 + Vite + Pinia）：矩形框选 + 画笔涂抹画布、非破坏式编辑树（分支/回滚）、切换节点恢复选区轨迹、Before/After 对比滑块、提示词历史（去重、时间倒序）+ 可复用模板、面板折叠设计、`useCanvasSelection` / `useNodeTree` 等组合式封装。

总体布局策略：**保持 PC-only（最小宽度 1280px），仅在桌面宽度内优化栅格/留白/滚动**，暂不投入移动端。

---

## 进度总览（全部完成 ✅）

| 阶段 | 内容 | commit |
| --- | --- | --- |
| 阶段一 | 深/浅色主题 + Element Plus 调色板统一 | `2e76759` |
| P1 | 后端支持 OpenAI 兼容协议（worker + TaskStore） | `6620824` |
| P2 | 前端协议选择器 + 状态流 + 标注层几何 + 文档 | `37520f4` |
| C2 | 跨域下载修复 + 大图灯箱 | `83cfc2d` |
| C3 | 标注层橡皮擦/撤销/画笔光标/拖拽/等比画布 | `ae2b089` |
| C4 | API key 脱敏 + 上传体积上限 | `5319513` |
| C5 | 生成历史 + 提示词复用 + 快捷键 + 空节点引导 | `eab2eb2` |
| C6 | 无障碍/尺寸预设 + 一键安装/运行脚本 + 文档 | （本次） |

> 下面保留各阶段的原始设计说明，作为实现依据与回溯参考。

---

## ✅ 阶段一（已完成，commit `2e76759`）

`feat(ui): add dark/light theme with unified Element Plus palette`

- 在 `styles.css` 引入语义化 studio 设计令牌（light + dark 两套）：表面、文字、实心药丸按钮、强调色、外观。
- 用 `color-mix` 把 Element Plus 的 primary/success/warning 映射到 studio 调色板，一段定义同时适配明暗两套主题；深色模式下顺带"暖化"EP 核心表面变量。
- 新增 `composables/useTheme.js`：优先读取已保存偏好，其次跟随系统 `prefers-color-scheme`，在 `<html>` 上切换 `.dark` 类（EP 约定）。
- 侧边栏新增主题切换按钮（太阳/月亮图标）。
- 把 App / Playground / APIConfig / RegionEditor 里所有硬编码的 `bg-white`、ink-on-white、画布颜色替换为令牌，使深色模式干净渲染。

> 待办（验证类）：用 Playwright 截图复核明暗两套主题与关键流程的视觉效果（放到 UI 重头阶段 C2/C3 之后统一做一次）。

---

## ⏳ 阶段二 · C2：跨域下载修复 + 大图灯箱

**问题背景**

- 当前结果图下载用 `<a download>`，但 `download` 属性对**跨域 URL 会被浏览器忽略**，实际只是新标签页打开，并未保存文件——这是一个真实 bug。
- 结果区没有"查看大图"的能力，只有悬停时一个下载按钮。

**具体改动**

1. 新增 `src/utils/download.js`：`downloadImage(url, baseName)`，`fetch` 取 blob → `URL.createObjectURL` → 触发 `<a download>` → 释放对象 URL；跨域/网络失败时回退到新标签页打开，并返回是否成功。
2. `Playground/index.vue` 结果画廊：
   - 用 Element Plus `el-image` + `:preview-src-list="images"` + `:initial-index` + `preview-teleported` 提供内置灯箱（缩放/上一张下一张/关闭），缩略图 `cursor-zoom-in`。
   - 悬停下载按钮改为 `<button @click="downloadOne(url, i)">`，调用 `downloadImage`，失败时 `ElMessage` 提示"已在新标签页打开（跨域无法直接下载）"。
3. 捕获并展示 `expires_at`：完成时存下，画廊头部显示"链接将于 X 过期"的提示（unix 秒 → 本地时间）。

**涉及文件**：`src/utils/download.js`（新增）、`src/components/Playground/index.vue`

**验收标准**：跨域图片点击下载能落盘（或明确回退提示）；点击图片可打开全屏灯箱并切换多张；存在 `expires_at` 时显示过期提示；`npm run build` 通过。

---

## ✅ 阶段三 · C3：RegionEditor 标注层 + 橡皮擦/撤销 + 画笔光标

**问题背景**

- 当前局部编辑不再把单独 `mask` 作为上游请求字段，而是在前端维护内部标注层，提交前合成 `marked_image`（原图 + 半透明彩色标注）。
- 需要橡皮擦、撤销、画笔光标预览、拖拽上传和标注草稿恢复，保证用户切换会话后还能看到原图与修改痕迹。

**具体改动**

1. **标注层**：内部维护一张透明标注画布，用户可用画笔/框选写入当前颜色；提交时导出 `source_image` 和 `marked_image`，不转发独立 `mask`。
2. **橡皮擦**：工具增加 `eraser`，用 `globalCompositeOperation = 'destination-out'` 擦除标注；与 brush 共用画笔大小。
3. **撤销栈**：每次落笔/框选/擦除前快照标注画布（`getImageData` 或离屏 canvas），支持按钮撤销，限制栈深度（如 20）。当前版本会拦截局部编辑区域的 Ctrl/⌘+Z，避免触发浏览器默认撤回。
4. **画笔光标预览**：在展示画布上跟随指针绘制一个表示画笔半径的圆环。
5. **拖拽上传**：画布支持把图片文件拖入加载（参考 gpt_image_playground 参考图拖拽）。
6. **颜色选择**：支持红、黄、绿、白等多色组合，提示词可以描述不同颜色区域的不同修改意图。
7. 主题联动：监听主题变化重绘画布（阶段一已让 `drawScene` 读 CSS 变量，这里补一个 watch 触发重绘）。

**涉及文件**：`src/components/RegionEditor/index.vue`（必要时抽 `composables/useCanvasSelection.js`）

**验收标准**：提交 payload 只包含 `source_image` / `marked_image` / 可选参考图；橡皮擦/撤销可用；画笔光标可见；拖拽上传可用；切换主题画布同步换色；草稿可恢复原图与标注痕迹；`npm run build` 通过。

---

## ⏳ 阶段四 · C4：API key 脱敏 + 上传体积上限（后端安全）

**问题背景**

- `GET /api/configs` 把**完整 `api_key` 返回给浏览器**，存在泄露面。应只回显末 4 位掩码。
- 后端未对 base64 图片请求设上限，超大请求可能拖垮进程。

**具体改动**

1. `config_service.py`：新增对外序列化（`public_view`），列表/详情对前端只返回 `api_key_preview`（形如 `••••1234`）与是否已设置 key 的布尔；服务内部（fallback 提交、状态轮询）仍用完整 key。
2. `routes/configs.py`：列表/创建/更新返回脱敏视图；`image_service` 取 key 时走内部完整读取。
3. **编辑不覆盖**：当编辑表单 `api_key` 留空时，保留原 key（不要被空串覆盖）。
4. `app.py`：设置 `MAX_CONTENT_LENGTH`（如 25MB），新增 413 处理器返回统一错误结构。
5. `APIConfig/index.vue`：占位显示 `••••1234`，仅在用户改动时才发送 key；新增节点必填、编辑节点可空。
6. 同步更新 `tests/test_backend_*.py`（断言列表不含完整 key、留空更新不清空 key、超限返回 413）与 `docs/API.md`。

**涉及文件**：`backend/services/config_service.py`、`backend/routes/configs.py`、`backend/app.py`、`frontend/src/components/APIConfig/index.vue`、`tests/`、`docs/API.md`

**验收标准**：前端拿不到完整 key；留空编辑不清空 key；超大上传返回 413；`python -m unittest discover tests` 全绿；`npm run build` 通过。

---

## ⏳ 阶段五 · C5：生成历史 + 提示词复用 + 快捷键 + 空节点引导

**具体改动**

1. **生成历史**（localStorage）：新增 `composables/useGenerationHistory.js`，记录最近 N 条（prompt、mode、size、n、urls、节点、时间戳）；Playground 增加历史条/抽屉，点击可回看结果并回填 prompt/参数（参考 gpt_image_playground 历史 + 复用任务配置、AIWatch 提示词历史去重时间倒序）。
2. **重启保留输入**：表单（prompt/size/n/mode）持久化到 localStorage，刷新后恢复（习惯设置可选）。
3. **快捷键**：早期版本支持 Ctrl/⌘+Enter 提交；当前版本已移除该快捷提交，只通过按钮提交任务。
4. **空节点引导**：当没有启用节点时，结果区/提交处显示行内 CTA "请先到『设置』添加并启用一个 API 节点"，点击切到设置，而不是抛原始错误。

**涉及文件**：`src/composables/useGenerationHistory.js`（新增）、`src/components/Playground/index.vue`、`src/App.vue`（标签切换联动）

**验收标准**：历史可记录/回看/复用；刷新后输入恢复；无节点时给出引导而非裸错误；`npm run build` 通过。

---

## ⏳ 阶段六 · C6：无障碍 / 排版打磨 + 尺寸预设 + 文档

**具体改动**

1. **无障碍**：图标按钮补 `aria-label`；统一 `:focus-visible` 焦点环；检查明暗对比度；列表拖拽提供键盘可达的上移/下移备选。
2. **排版打磨**：统一圆角/间距/滚动条；细节 hover/active 态。
3. **尺寸预设**：扩展尺寸选项（auto/1024²/竖图/横图等），自定义尺寸自动对齐到 16 的倍数并做总像素校验（参考 gpt_image_playground）。
4. **文档**：更新 `README.md`、`docs/ARCHITECTURE.md`、`docs/API.md`，补充主题、历史、key 脱敏、局部编辑标注图、参考图持久化和异步中转协议等新能力。

**涉及文件**：各组件、`README.md`、`docs/`

**验收标准**：键盘可达 + 焦点可见；尺寸预设与校验生效；文档与实现一致；`npm run build` 通过。

---

## 统一验证命令

```powershell
# 后端测试
.venv\Scripts\python.exe -m unittest discover tests
# 前端构建
cd frontend; npm.cmd run build
```

> 备注：阶段二之后建议做一次 Playwright 视觉复核（明暗主题 + 文生图/局部编辑/设置三视图截图）。
