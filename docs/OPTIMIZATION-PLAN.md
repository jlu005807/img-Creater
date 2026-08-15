# img-Creater 优化方案与功能设计

> 状态：设计稿（Draft）
> 更新时间：2026-08-15
> 适用范围：本地单用户、PC-only 图片工作台
> 关联文档：README.md、docs/API.md、docs/ARCHITECTURE.md、docs/CANVAS-NOTES.md、docs/ROADMAP.md

本文把现有架构审查结论和两项新增需求合并成一份可执行方案：

1. 历史记录按页加载，滚动到列表底部且仍有记录时自动继续加载；
2. 作品集支持图片多选并批量下载。

用户描述中的“还有基类”按“还有记录”理解。本文先确定边界、接口和验收标准，后续再按阶段实现。

## 1. 当前基线

### 1.1 系统链路

~~~text
Vue 3
  -> Axios /api
  -> Flask routes
  -> ImageService + ConfigService + TaskStore
  -> OpenAI-compatible / Chat / Custom / Async provider
  -> history/<session-id>/session.json + result files
  -> Playground polling / Gallery display
~~~

项目本身不负责模型推理，而是编排多个上游图片 API。当前定位是本地单用户工作台，不需要为了理论上的多用户规模立刻更换 Flask、Vue 或整个请求模型。

### 1.2 已确认的实现问题

| 位置 | 现状 | 影响 |
| --- | --- | --- |
| frontend/src/composables/useGenerationHistory.js | MAX_ENTRIES 为 30，新增和服务端合并时都 slice 到 30 条 | 服务端有更多会话时，左侧历史永远看不到旧记录 |
| backend/routes/generation.py | GET /api/sessions 扫描全部历史目录并返回完整 manifest | 首屏响应、JSON 解析和内存开销随历史增长 |
| frontend/src/components/Gallery/index.vue | 每次一次性获取全部 sessions；el-image lazy 只延迟图片元素加载 | 图片本身可能懒加载，但会话元数据仍然一次性加载 |
| Playground 与 Gallery | 各自把 urls 映射为 images | 分页后容易出现字段、排序和去重逻辑不一致 |
| Playground/index.vue | 表单、历史、轮询、草稿、结果和重试集中在一个大组件 | 页面切换和后续重构成本高 |
| backend/services/image_service.py | 协议适配、Fallback、任务编排、错误解析和持久化集中在一个大类 | 新增供应商或修改重试策略时回归风险较高 |
| ImageService._start_task | 每个任务启动一个无上限 daemon 线程 | 批量提交时可能耗尽线程和 HTTP 连接 |
| TaskStore | 仅进程内存储，重启后任务状态丢失 | 长时间异步任务无法恢复 |

当前历史目录还包含较长的 prompt、attempts 和 response_meta。列表接口返回这些大字段没有必要，会放大请求体。历史的“完整”应定义为：所有已经持久化且有结果的 completed 会话都可以通过分页访问；运行中的本地任务仍由前端任务状态单独维护。

基线验证已通过：后端测试 124 项、检测测试 14 项，前端 Vite 生产构建成功。当前工作区抽样可见约 175 条 completed 会话；全量 sessions 响应约 3MB 级别，这正是分页摘要有价值的直接证据。

## 2. 目标与非目标

### 2.1 目标

- 首屏只加载有限数量的历史，滚动到底部自动加载下一页。
- 使用稳定游标保证分页过程中不重复、不跳过。
- 搜索和时间筛选不能因为只加载了首屏而漏掉旧记录。
- Playground 和 Gallery 共用一套会话分页与图片映射模型。
- 作品集可选择多张图片，最终只触发一次 ZIP 下载。
- 处理跨域、失效链接、重复文件名、超大批量和部分失败。
- 保留现有单图下载、灯箱预览、后端 history 文件格式和本地使用方式。
- 每个阶段都能独立测试、回滚和发布。

### 2.2 非目标

- 本阶段不改成移动端布局。
- 不立即引入 WebSocket/SSE；轮询优化放在后续阶段。
- 不在本阶段实现无限画布或重写 RegionEditor 坐标系统。
- 不允许客户端提交任意本地路径或任意远程 URL 让后端打包，避免路径穿越和 SSRF。
- 不把 localStorage 变成完整历史数据库。后端 history 才是已完成会话的事实来源。

## 3. 总体优先级

### P0：可靠性与任务边界

1. 把每任务 daemon 线程改为有界 ThreadPoolExecutor 或任务队列，增加最大并发、排队上限、任务超时和优雅关闭。
2. 将 TaskStore 的关键状态持久化到 SQLite（本地场景足够），支持启动恢复、过期清理和幂等键。
3. 明确异步上游的提交状态：未提交、提交结果未知、已接受并轮询、可重试失败、不可重试失败。保留当前“可能已扣费时不重复提交”的安全语义。

### P1：本次需求相关的结构改造

1. 为 /api/sessions 增加分页、稳定游标和摘要响应。
2. 把历史和 Gallery 的会话数据提升到共享 store/composable，组件销毁不影响分页状态或任务轮询。
3. 将 ImageService 拆为请求规范化、任务编排、Provider Adapter 和结果持久化等职责。
4. 将 Playground 拆为任务管理、历史、表单和编辑草稿 composable。
5. 统一 API 字段名和错误结构，逐步淘汰 urls/images、referenceImages/reference_images 的双重表示。
6. 实现历史无限滚动和 Gallery 多选；先支持小批量 ZIP，随后补充后端大批量导出。

### P2：规模化和体验优化

- 轮询采用分阶段退避，或在任务量上升后引入 SSE。
- 检测模块的大图/批量检测改为后台任务。
- 用 Vitest 和 Playwright 替换主要的源码字符串断言。
- 对 Element Plus、检测弹窗和批量下载依赖做懒加载。
- 同步 README、ARCHITECTURE、API 和 ROADMAP，说明分页、历史来源和下载限制。

## 4. 功能 A：历史记录分页与无限滚动

### 4.1 现状与问题根因

当前流程是：

1. 前端调用一次 GET /api/sessions；
2. 后端遍历 history 下所有目录，读取所有 session.json；
3. 前端合并结果后，useGenerationHistory.js 用 MAX_ENTRIES=30 截断；
4. Playground 只在挂载时加载一次。

因此，即使后端有上百条 completed 会话，左侧历史也最多保留 30 条。Gallery 的 el-image lazy 不解决这个问题，因为它只延迟图片元素的资源加载，不能减少 /api/sessions 的全量元数据请求。

另外，单个损坏或正在写入的 manifest 如果直接让整个 list_sessions 失败，会导致一页历史全部不可见。分页实现应逐目录隔离读取错误，记录 warning 并跳过坏条目。

### 4.2 用户体验

#### 首次加载

- 首次进入 Playground 或 Gallery 加载一页，建议默认 30 条，允许后续配置为 20～50。
- 显示列表级 loading，不阻塞已经显示的本地运行任务。
- 当首批记录不足以填满滚动容器且 has_more 为 true 时，自动继续请求，直到出现滚动空间或到达末页。

#### 触底加载

- 历史侧栏和 Gallery 主滚动容器各放一个底部 sentinel。
- 优先使用 IntersectionObserver，root 指向实际滚动容器；兼容性或复杂布局下可退回 scrollTop + clientHeight >= scrollHeight - 160px。
- 同一时刻只允许一个 loadMore 请求。
- 加载中显示“正在加载”，末页显示“已加载全部”，失败保留当前 cursor 并显示“重试”。
- 每次查询维护 request generation；筛选、排序或刷新后，旧请求即使晚到也不能覆盖新列表。

#### 筛选、排序和刷新

- 搜索词、时间范围、排序方式变化时，清空已加载的服务端页并从首页重新请求。
- 筛选必须在服务端执行，否则用户会误以为“旧记录不存在”。如果第一阶段暂时只做客户端筛选，界面必须明确“仅筛选当前已加载记录”，不应继续宣称全历史搜索。
- 刷新会重置 cursor 和分页状态，但要保留当前运行中的本地任务。
- 新任务完成后插入顶部；正在运行的本地条目不能被旧 manifest 覆盖。

### 4.3 后端 API 设计

#### 请求

~~~text
GET /api/sessions
  ?limit=30
  &cursor=<opaque-cursor>
  &q=<optional-search>
  &from=<optional-iso-time>
  &to=<optional-iso-time>
  &sort=updated_desc
  &view=summary
~~~

约束：

- limit 只接受 1～100，超出范围时裁剪或返回 400；建议默认 30。
- cursor 为不透明字符串，内部编码排序键，不让前端依赖具体格式。
- 默认排序为 updated_at（缺失时使用 created_at）降序，再以 id 降序作为稳定 tie-break。
- q、from、to 和 sort 的具体组合应在 API.md 中固定，筛选变化即视为新查询。
- 逐目录读取 manifest；单个 JSON 损坏、权限错误或并发写入时记录 warning 并跳过，不让整个请求 500。
- 默认只返回 status=completed 且 urls 非空的会话。失败、排队和取消任务仍由本地任务状态展示；是否增加 include_status 作为后续需求单独评估。
- 缺失时间字段时使用文件 mtime 作为排序候选，并以 id 做最终稳定排序；不得因为单个时间字段异常破坏整页排序。

#### 响应

apiClient 解包 success/data 后，新的分页请求返回：

~~~json
{
  "items": [
    {
      "id": "session-id",
      "prompt": "short or full prompt",
      "mode": "generate",
      "size": "1024x1024",
      "n": 1,
      "images": [
        {
          "index": 0,
          "url": "/api/results/session-id/image.png",
          "filename": "image.png"
        }
      ],
      "reference_images": [],
      "api_name": "provider",
      "created_at": "2026-08-15T00:00:00Z",
      "updated_at": "2026-08-15T00:00:00Z",
      "expires_at": null
    }
  ],
  "next_cursor": "opaque-cursor-or-null",
  "has_more": true
}
~~~

设计规则：

- 列表页只返回展示和选择所需的摘要及图片元数据，默认省略 attempts、response_meta 等大字段。
- prompt 在列表中应有明确长度上限；完整 prompt 只在详情或复用操作时按需读取，服务端搜索仍针对原始值执行。
- 如某个编辑场景需要完整 manifest，增加 GET /api/sessions/{history_id} 详情接口，不把大字段塞进分页列表。
- images 作为新的规范字段；urls 可在过渡期继续返回，前端统一通过 mapper 兼容旧数据。
- 没有分页参数的旧 GET /api/sessions 可暂时继续返回数组，保证旧前端可用。新前端应优先带 limit，并同时兼容“数组”和“分页对象”，待所有调用方迁移后再移除旧形态。
- 不强制返回 total。目录扫描下 total 成本高，has_more 和 next_cursor 已足够驱动无限滚动。

#### 游标规则

游标至少包含最后一条记录的排序键：

~~~text
(updated_at or created_at, id)
~~~

下一页查询严格取排序键小于该游标的记录。这样在用户滚动期间新增会话，不会因为 offset 改变而产生重复或跳过。无效游标返回结构化 400 错误。

第一阶段仍可能每次扫描所有目录再排序后切片，这能先解决首屏响应和前端内存问题，但不完全消除后端扫描成本。历史量继续增长后，应建立 SQLite session index 或带 mtime 的 metadata cache。


### 4.4 前端状态设计

建议新增共享的 useSessionHistory/useSessionStore，供 Playground 和 Gallery 使用，至少包含：

~~~text
items
nextCursor
hasMore
loading
loadingMore
error
query
timeRange
sort
loadFirstPage()
loadMore()
refresh()
resetQuery()
mergeLocalEntry()
removeEntry()
~~~

实现要求：

- 以 session id 去重；同一页重复返回不能重复渲染。
- 保留现有 task_id 不匹配保护，防止旧 manifest 把正在运行的新任务改成 completed。
- 不再对内存中的完整服务端历史做 slice(0, 30)。
- localStorage 只保存运行态和轻量索引（例如 id、prompt 摘要、时间、状态），不保存所有 base64 图片、完整 prompt、attempts 或全部分页结果。
- 后端分页数据作为事实来源；切换 Playground/Gallery 后仍复用已加载页，不重新从第一页覆盖状态。
- 删除会话后从共享列表和 selected image 集合同时移除；刷新后失效的 session id 自动清理。

### 4.5 历史功能验收标准

- 当后端有超过 30 条 completed 会话时，用户可以连续滚动直到访问末页，且没有重复或跳过。
- 滚动到末页后 has_more=false，不再产生请求。
- 首批不足一屏时会自动补页，不要求用户先制造滚动条。
- 网络失败只影响当前页；点击重试后从原 cursor 继续，不重复追加成功页。
- 新建任务、切换页面、刷新和删除操作不会丢失或覆盖正在运行的本地条目。
- 搜索和时间筛选能命中未加载的旧记录；如果第一版暂不支持服务端筛选，必须在 UI 中明确范围。
- 旧数组响应仍能被前端识别，升级后不会因后端未重启而出现难以理解的错误。
- python -m unittest discover tests 和 npm run build 通过；补充分页游标、坏 manifest、重复页和并发 loadMore 测试。

## 5. 功能 B：作品集多选与批量下载

### 5.1 用户体验

作品集顶部增加选择工具栏，默认保持当前浏览模式：

- 进入/退出选择模式；
- 全选当前已加载图片；
- 清空选择；
- 显示“已选择 N 张”；
- 有选择时启用“批量下载”按钮；
- 下载中显示已完成数量、失败数量和总体进度。

卡片交互：

- 卡片左上角提供键盘可达的 checkbox 或选择按钮。
- 点击选择控件只改变选中状态，不打开灯箱；点击图片主体仍打开预览。
- 卡片选中时有明显边框/遮罩，但不能改变图片尺寸或网格布局。
- 选择集合跨分页保留，使用稳定键 sessionId:imageIndex；刷新或删除后清理不存在的键。
- “全选当前已加载”不等同于“全选整个历史库”。全库选择需要额外的服务端计数/选择协议，第一版不隐式承诺。
- 分页请求失败或被取消时保留已选集合；查询条件改变后只清理已确认不存在的图片，不因短暂的空页误删选择。

下载规则：

- 多张图片只触发一次浏览器下载，文件名为类似 img-Creater-20260815-120000.zip。
- ZIP 内文件名使用安全化的 session id 和图片序号，例如 img-Creater-session-id-1.png。
- 同名文件自动追加序号，不覆盖已有条目。
- 已失效、无法 fetch 或不是图片 MIME 的项列入失败清单；混合成功时仍下载成功项，并在完成后提示失败项。
- 所有项目都失败时不生成空 ZIP，只显示可重试错误。

### 5.2 第一阶段：前端小批量 ZIP

第一阶段可在不新增后端导出接口的前提下实现可用版本：

1. 在 frontend/src/utils/download.js 抽出 fetchImageBlob 和 triggerBlobDownload，不复用失败时会打开新标签页的 downloadImage 作为批量内部逻辑。
2. 使用体积较小的 ZIP 库（例如 fflate；具体依赖在实现时确认）。
3. 以受控并发（建议 3～4）读取同源 /api/results 和 data URL，逐个校验响应状态、Content-Type 和最大字节数。
4. 只把成功 Blob 写入 ZIP；用 AbortController 支持取消。
5. ZIP 完成后只调用一次 Blob URL 下载，随后释放对象 URL。

第一阶段设置数量和总字节上限，例如不超过 50 张或 200MB；实际值应做成配置常量并在界面显示。超过上限时提示用户分批选择，而不是并行触发几十个浏览器下载。远程 URL 没有 CORS 时不应静默重试或打开多个新窗口，应作为失败项明确提示。

### 5.3 第二阶段：后端大批量导出

历史目录已经可能包含数百 MB 图片，长期方案应增加后端导出接口，避免浏览器内存峰值和跨域限制：

#### 请求

POST /api/sessions/export

~~~json
{
  "items": [
    {"session_id": "session-id", "image_index": 0},
    {"session_id": "session-id", "image_index": 1}
  ]
}
~~~

安全要求：

- 只接受 session_id 和 image_index，不接受任意 URL、绝对路径或用户提供的文件名。
- 服务端从对应 session.json 的 urls/images 解析目标，并确认目标属于该 history 目录。
- 只允许本地已持久化的 /api/results 文件；manifest 中仍是远程 URL 的项目默认跳过并返回失败明细，若将来要支持远程抓取，必须另行配置安全 allowlist、超时和大小限制。
- 校验 history id、索引、扩展名、MIME、单文件大小、总大小和总数量。
- ZIP 临时文件或流式输出完成后清理；响应设置 application/zip 和安全的 Content-Disposition。
- 生成失败项不能泄露服务器路径；只返回 session_id、image_index 和可读错误原因。
- 若部分文件被跳过，ZIP 内附带 export-report.json，记录成功和失败的资源标识；若没有任何成功文件，返回结构化 JSON 错误而不是空 ZIP。
- 为便于界面在保存前提示部分失败，响应额外返回可被 CORS 暴露的跳过数量（例如 X-Export-Skipped-Count）；详细明细仍以 export-report.json 为准。

#### 响应与前端

- Axios 调用使用 responseType=blob，不经过普通 JSON 错误解包。
- 成功时前端只触发一次 ZIP 保存。
- 413、部分失败、超出限制和空选择都转换成统一的用户提示。
- 由于错误响应可能仍是 JSON，Blob 响应解析器需要在下载失败时先读取并还原统一错误信封，不能把 JSON 错误当成 ZIP 保存。
- 大批量导出可以后续复用 P0 任务队列，返回 export_task_id 并在任务完成后下载；第一版可先限制为同步流式/临时文件导出。

### 5.4 批量下载验收标准

- 可以选择、取消选择、全选当前已加载和清空；选择跨页不丢失。
- 预览和选择互不误触发，键盘可达且有清晰焦点态。
- 批量下载只产生一个 ZIP 下载，不产生 N 个新标签页。
- ZIP 文件名和内部文件名安全、唯一、扩展名正确。
- 混合成功/失败时成功项可下载，失败项有数量和重试提示。
- 空选择、网络失败、链接过期、超出数量/大小上限都有明确反馈。
- 后端导出拒绝路径穿越、任意 URL、越权 session 和无效 image_index。
- 覆盖单元测试、前端组件测试和 Playwright 端到端测试。

## 6. 共享数据模型与模块边界

### 6.1 会话与图片模型

统一 mapper 输出以下形状，历史侧栏和作品集都消费同一模型：

~~~text
SessionSummary
  id
  prompt
  mode
  size
  status
  createdAt
  updatedAt
  images[]
    key = sessionId:imageIndex
    sessionId
    imageIndex
    url
    filename
    expiresAt
  referenceImages[]
~~~

后端字段可以继续使用 snake_case；在 api 层一次性转换为前端 camelCase，避免 Gallery 和 Playground 各自映射。

### 6.2 状态归属

- 服务端分页 store：已完成 session 摘要、游标和选择集合。
- 应用级 task manager：queued、processing、completed、failed、cancelled 以及轮询定时器。
- localStorage：运行中任务的最小恢复信息、用户偏好和轻量历史索引。
- history 文件：图片、session manifest 和编辑草稿的持久事实。

这样可以解决 App.vue 通过 v-if 切换 Playground/Gallery 时组件销毁导致分页和轮询状态重建的问题。

## 7. 分阶段实施计划

### 阶段 0：接口和测试基线

- 在 docs/API.md 补充分页响应、游标、摘要字段和导出接口草案。
- 增加 session mapper 和分页响应的契约测试。
- 固定坏 manifest、重复 cursor、旧数组响应的测试样例。

### 阶段 1：历史分页

- 后端实现 limit/cursor/筛选，保留旧接口兼容。
- 前端共享 store/composable，移除服务端历史的 30 条硬截断。
- Playground 和 Gallery 接入 sentinel、loadMore、重试和末页状态。
- 完成分页、刷新、筛选和任务合并的测试。

### 阶段 2：Gallery 多选和小批量 ZIP

- 增加选择模式、稳定 image key、工具栏和进度状态。
- 抽取 Blob 下载工具，加入受控并发和数量/大小上限。
- 完成前端组件测试与一次浏览器端到端验证。

### 阶段 3：大批量后端导出

- 增加 POST /api/sessions/export，严格按 session_id + image_index 解析文件。
- 增加 ZIP 内容、路径安全、大小限制和临时文件清理测试。
- 视实际批量规模决定是否接入持久化任务队列。

### 阶段 4：基础架构收敛

- worker 池化和 TaskStore SQLite 持久化。
- 拆分 ImageService、Playground 和共享任务管理。
- 增加结构化日志、task_id/history_id/provider/attempt 关联信息。

## 8. 风险与决策记录

### 已作出的建议

- 使用 cursor，不使用 offset，避免新会话插入造成分页错位。
- 前端第一阶段只对小批量做 ZIP，长期以服务端导出为准。
- 全选默认限定为当前已加载图片，避免把“分页选择”误解成全库选择。
- 后端导出只接受资源标识，不接受任意 URL。
- localStorage 不保存完整历史，避免解除 30 条限制后再次触发配额问题。

### 实现前需要确认的参数

- 默认 page size（建议 30）。
- 小批量 ZIP 的最大图片数和总字节（建议 50 张/200MB）。
- 历史是否需要把失败、取消和排队会话作为可分页记录。
- 远程 URL 持久化失败时是跳过、提示重新生成，还是通过显式 allowlist 下载。
- 是否需要“全库全选”以及全库选择的 UX。

## 9. 完成定义

本方案对应的实现只有同时满足以下条件才算完成：

1. README、API.md、ARCHITECTURE.md 和本文件对历史来源、分页和下载行为描述一致。
2. 历史记录可以从首屏滚动加载到末页，筛选不会静默漏掉旧记录。
3. Gallery 多选和批量下载在同源、跨页、部分失败和超限情况下都有可解释行为。
4. 后端不会因坏 manifest、路径穿越或任意远程 URL 导出而暴露整个接口或本地文件。
5. 后端测试、前端构建和关键 Playwright 流程均通过。

## 10. 预期改动文件

以下是实现阶段的边界清单，实际拆分时可以增加小型 service 或 composable，但不应把无关重构混入本需求：

- 后端：backend/routes/generation.py、backend/services/session_service.py（可新建）、backend/services/task_store.py、backend/app.py。
- 前端：frontend/src/api/generation.js、frontend/src/composables/useGenerationHistory.js（或新的共享 session composable）、frontend/src/components/Playground/index.vue、frontend/src/components/Gallery/index.vue、frontend/src/utils/download.js。
- 测试：tests/test_backend_routes.py、tests/test_backend_services.py、新增分页/导出契约测试，以及前端 Vitest/Playwright 测试。
- 文档：README.md、docs/API.md、docs/ARCHITECTURE.md、docs/ROADMAP.md 与本文件。

实现时应先更新接口契约和测试，再改 UI；不要在没有分页/导出安全测试的情况下直接依赖前端隐藏旧记录或拼接客户端路径。
