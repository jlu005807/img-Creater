# 无限画布演进笔记

2026-07 全面审计与加固后沉淀的结论，供后续把工作台演进为无限画布时参考。

## 已就绪的地基

- **协议适配层可直接复用**：`ImageService` 的四种上游协议（OpenAI 兼容 / Chat Completions / 自定义 URL / 异步中转）与 Fallback 链完全独立于 Flask 路由，`tests/test_backend_services.py` 的 FakeHttpClient + `run_async=False` 测试环境可原样服务画布后端。
- **会话结果寻址已统一**：`/api/results/<history_id>/<path:filename>` 支持任意深度的节点资源路径（含 `references/` 子目录），并带 history_id 规范化校验。
- **manifest 不再单槽覆盖**：重新生成失败/排队不会清掉上次成功结果（`last_*` 字段记录最近尝试），这是画布节点"保留最近成功内容"语义的雏形；真正的多代记录仍需 per-generation 追加结构。
- **detection/ 是插件化模板**：单向依赖、能力探测降级、config 驱动，画布插件可照此设计；注意其秒级延迟与内存占用，必须放后台队列。

## 画布动工前必须做的重构

1. **蒙版从面板空间迁移到图像空间**：RegionEditor 的蒙版数据仍固定 400×360 面板分辨率，导出时放大到原图尺寸（大图上笔刷精度被量化）。画布需要以图像原始坐标存蒙版、显示端按视口缩放——这也是可平移/缩放画布的同一套坐标变换。DPR 渲染、rAF 合帧、指针事件清理已完成，坐标迁移是剩余的大项。
2. **轮询/任务管理提取为模块级 store**：Playground 目前以组件生命周期持有轮询定时器（disposed 标志只是止血）。画布多节点并发生成需要一个独立于组件的 task manager（按 task_id 键控），页面切换不中断。
3. **后端 worker 池化**：现在每次提交起一个不设上限的 daemon 线程；画布批量触发节点时需要有界 ThreadPoolExecutor + 可恢复的任务存储（TaskStore 目前内存态、不落盘）。
4. **grep 型前端测试先删后改**：`tests/test_frontend_*.py` 是源码子串断言，画布重构拆分 Playground/RegionEditor 时会整批误报；动工前用 Vitest 组件测试替换（frontend 目前没有任何 JS 测试运行器）。
5. **Gallery 与历史合流**:Gallery 与 `useGenerationHistory` 各自映射 `/api/sessions`（字段名都不同：`images` vs `urls`），画布的节点数据模型确定后应收敛为单一 session 映射层，并给 `/api/sessions` 加分页与缩略图。
