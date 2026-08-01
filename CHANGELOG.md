# Changelog

本文件记录 maibot-fetch-url-plugin 的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.3.0] - 2026-08-01

### 新增

- 公开 `@API fetch_url`（`com.0-hz.fetch-url.fetch_url`）：参数对齐工具，供其他插件调用
- API 图片默认 `return_image=false`：经 alt_text/VLM 返回文字描述；VLM 不可用或失败时返回 metadata-only；`return_image=true` 时与工具相同回传图片

## [0.2.9] - 2026-07-20

### 新增

- `[llm] rpc_timeout_ms`：本插件所有 `llm.generate`（内容总结与 VLM 图片描述）的 cap.call RPC 超时（毫秒），默认 120000（120 秒）；`config_version` 升至 1.9.0

## [0.2.8] - 2026-07-12

### 变更

- `alt_text.max_images` 默认改为 `0`（关闭自动 VLM alt，缩短单次工具调用耗时；`config_version` 1.8.0）
- 网页 Markdown 中每张图片的 alt 追加提示：`You can call fetch_url again to obtain this image.`

## [0.2.7] - 2026-07-11

### 修复

- 持久化配置前去除 `None`；WebUI 清空可选字段时按默认值处理

## [0.2.6] - 2026-07-10

### 变更

- 默认 summarize prompt 以具名 AI 生命体的「网页抓取」模块自居，并标注性格与表达风格

## [0.2.5] - 2026-06-14

### 新增

- 内存抓取缓存（30 分钟 TTL，128 条）
- 返回页面元数据与格式化 JSON 响应
- jina 不可用时回退到本地 `pypdf` 解析 PDF

## [0.2.4] - 2026-06-14

### 修复

- Content-Type 缺失时读取足够字节以识别 RIFF/WEBP
- VLM alt 替换仅在截断 / 摘要决策之后应用，保证 `returned_range` 与原始 markdown 偏移一致

## [0.2.3] - 2026-06-13

### 变更

- 未设置的配置字段跟随代码默认值；从 1.5.0 迁移时保留用户自定义（`config_version` 1.6.0）

## [0.2.2] - 2026-06-13

### 修复

- 将配置迁移内联进 `plugin.py`（Host 仅通过 importlib 加载该文件，独立迁移模块运行时不可导入）

## [0.2.1] - 2026-06-13

### 修复

- 拆分入站直抓与 VLM 图片路径：可接受格式且在尺寸限制内时直通，避免 v0.2.0 过度压缩
- 增加配置升级迁移

## [0.2.0] - 2026-06-13

### 变更

- 多遍图片压缩改为基于探测的单遍质量 / 缩放估算，降低 CPU
- 增加动图策略（`keep_animated` / `skip` / `first_frame`）

## [0.1.0] - 2026-06-11

### 新增

- 首次发布：`fetch_url` 工具
- 添加 MIT LICENSE；默认 `max_content_length` 提至 8192
