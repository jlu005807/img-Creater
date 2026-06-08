# AI 生成图像检测模块（Beta）

强解耦的本地图像取证模块：用**纯传统信号处理 + 统计**判断一张图片是否由 AI 生成（Stable Diffusion / Midjourney / DALL·E / GAN 等），**不使用任何深度学习模型、不依赖 GPU**。

## 设计要点

- **强解耦**：不 import 宿主项目的任何代码；宿主单向调用本模块。
- **懒加载降级**：重依赖（numpy/scipy/PyWavelets/invisible-watermark）按需探测；缺失时接口返回「功能未启用」而非崩溃，宿主项目不装也能照常运行。
- **多阶段级联**：高置信规则（水印 / 元数据）命中即直接判定；否则跑频域 / 噪声 / JPEG / 颜色多模块，加权融合评分。
- **配置驱动**：所有阈值与权重在 [`config.json`](config.json)，可迭代调参。
- **模块级容错**：任一分析器报错或缺其专属依赖 → 该模块记为「无信号」，权重重归一化，不影响其他模块。

## 安装（可选，仅启用检测时需要）

```bash
pip install -r detection/requirements.txt
```

不安装则 `/api/detect/health` 返回 `available=false`，宿主项目其余功能不受影响。

## 接口契约

```python
from detection import detect_image, detector_health

detector_health()  # -> {available, missing_required, missing_optional, analyzers}
detect_image(image_bytes, filename="x.png")
# -> {
#   "available": bool,
#   "verdict": "ai" | "suspicious" | "real" | "unavailable",
#   "score": 0.0~1.0 | None,
#   "label": "AI 生成" | "可疑" | "真实" | "功能未启用",
#   "stages": { "watermark": {...}, "metadata": {...},
#               "frequency": {...}, "noise": {...}, "jpeg": {...}, "color": {...} },
#   "evidence": [str], "elapsed_ms": int, "missing_deps": [str]
# }
```

## HTTP 接口（由 `backend/routes/detection.py` 暴露）

- `GET  /api/detect/health` → 能力报告。
- `POST /api/detect` body `{"image": "data:image/...;base64,..."}` → 检测结果（`{success, data}` 信封）。

## 各分析器

| 模块 | 信号 | 依赖 |
| --- | --- | --- |
| `watermark` | invisible-watermark 解码并精确匹配已知标记（StableDiffusionV1） | imwatermark |
| `metadata` | EXIF / PNG chunk / ICC / XMP 关键词 | Pillow |
| `frequency` | 多尺度 FFT 径向功率偏差、周期尖峰密度、DCT 直方图卡方 | numpy(+scipy) |
| `noise` | Haar 小波 HH 子带 GLCM、去噪残差方差峰度 | numpy + PyWavelets |
| `jpeg` | 量化表距离、重压缩一致性 | numpy + Pillow |
| `color` | 灰世界偏移、Cb/Cr 集中度、明暗噪声差 | numpy + Pillow |

## 评分

权重默认：频域 0.3 / 噪声 0.25 / JPEG 0.25 / 颜色 0.2。
判定：`score > 0.6 → AI`，`0.3~0.6 → 可疑`，`< 0.3 → 真实`。水印/元数据命中直接 100% AI。

## 测试

```bash
python -m unittest discover detection/tests
```

（评分引擎 + 降级路径为纯逻辑测试，无需安装可选依赖。）

## 局限（Beta）

传统方法对最新扩散模型的判别力有限，目标是「可调阈值 + 多信号融合 + 证据透明」，不保证绝对准确率。属实验性功能。
