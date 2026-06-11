# MaiBot Fetch URL 插件

为麦麦提供 `fetch_url` 工具：抓取任意 URL 并智能分流处理。

- **网页 / PDF** → 优先通过 [jina.ai Reader](https://jina.ai/reader/) 转为 Markdown（PDF 也支持）；jina 失败或关闭时回退到本地抓取 + markdownify
- **图片** → 下载后按配置自动转码 / 压缩，直接以图像形式回传给麦麦观察
- **超长内容** → 默认调用 LLM（注入麦麦人设）总结，或按需截断；支持 `start_char` / `end_char` 分页读取原文
- **网页图片** → 自动用 VLM 为页面中最大的几张图片生成中文描述并替换 alt 文本，描述结果持久缓存

## 功能特性

### 内容类型自动判定

对目标 URL 发起流式请求，根据响应头 `Content-Type`（辅以魔数嗅探）判定内容类型：


| 类型        | 处理方式                                      |
| --------- | ----------------------------------------- |
| `image/`* | 下载 → 校验 → 按需转码压缩 → 以 `content_items` 回传图像 |
| PDF       | jina Reader 转 Markdown（本地解析器不支持 PDF）      |
| HTML / 文本 | jina Reader（默认）或本地 markdownify            |


### 图片转码与压缩

- 格式不在 `acceptable_formats`（默认 jpeg/png/gif/webp）内 → 转为 `convert_format`（默认 webp）
- 大小超过 `max_image_size`（默认 2 MB）→ 压缩：
  1. 超大图先缩到 `max_dimension`（默认 2048 px）
  2. 从质量 80 开始自适应搜索（按 `q × sqrt(目标/实际)` 跳跃，下限 10）
  3. 质量下限仍超标时按比例逐步缩小尺寸
- 动图转 webp 时尽量保留动画；需要缩放时取首帧

### 超长内容窗口与总结

- 文档超过 `max_content_length`（默认 8000 字符）时：
  - `llm_summarize = true`（默认）→ 调用 LLM 总结，提示词注入麦麦的昵称 / 人格 / 表达风格
  - 关闭总结或工具调用时指定 `on_exceed=truncate` → 截断返回原文
- 麦麦可通过 `start_char` / `end_char` 指定窗口分段读取未删改原文；自定义窗口若仍超限，则只总结该窗口
- 返回结果始终标注文档总长、本次窗口范围以及内容被总结 / 截断的事实

### VLM 图片描述（alt 文本替换）

替换优先级：**VLM 描述 > jina 生成的 alt（需配置 `jina.api_key` 且开启 `with_generated_alt`）> 网页原始 alt**。

- 页面图片多于 `max_images`（默认 3）时，按 HEAD `Content-Length` 排序优先描述**最大**的几张（跳过最短边小于 `min_dimension` 的图标 / Logo）
- 描述结果以图片内容 sha256 为键持久缓存在 `data/alt_text_cache.json`（LRU，默认 1024 条），重复抓取不再消耗 VLM 调用
- VLM 任务未配置时自动跳过，保留 jina / 原始 alt
- 直接抓取的单张图片无需此机制——MaiBot 主程序会自动为工具回传的图片生成并缓存描述

## 安装

1. 将本插件目录放入 MaiBot 的 `plugins/` 目录：
  ```bash
   cd <MaiBot 根目录>/plugins
   git clone https://github.com/yufei-pan/maibot-fetch-url-plugin.git
  ```
2. 重启 MaiBot（依赖 `httpx`、`markdownify`、`beautifulsoup4`、`pillow` 会按 `_manifest.json` 声明自动安装）
3. 在 WebUI 插件管理中确认插件已加载并启用

## 配置

完整配置项见 [config.toml](config.toml)，均可在 WebUI 中编辑。常用项：


| 配置项                                      | 默认值               | 说明                             |
| ---------------------------------------- | ----------------- | ------------------------------ |
| `plugin.always_visible_for_planner`      | false             | 让 `fetch_url` 始终对 Planner 可见（无需 `tool_search`）；**修改后需重新加载插件** |
| `fetch.timeout`                          | 15                | 直接抓取超时（秒）                      |
| `fetch.proxy`                            | （空）               | 代理地址，如 `http://127.0.0.1:7890` |
| `fetch.cookies` / `fetch.domain_cookies` | （空）               | 全局 / 按域名 Cookie                |
| `fetch.allow_private_networks`           | false             | 是否允许抓取内网地址（SSRF 防护）            |
| `jina.enabled`                           | true              | 优先使用 jina Reader               |
| `jina.api_key`                           | （空）               | jina API Key（可选，不填有频率限制）       |
| `jina.timeout`                           | 30                | jina 超时（秒），超时回退本地抓取            |
| `jina.engine`                            | browser           | jina 渲染引擎（质量最好）                |
| `image.acceptable_formats`               | jpeg/png/gif/webp | 可直接回传的图片格式                     |
| `image.convert_format`                   | webp              | 转换目标格式                         |
| `image.max_image_size`                   | 2 MB              | 回传图片大小上限                       |
| `content.max_content_length`             | 8000              | 单次返回文本上限（字符）                   |
| `content.llm_summarize`                  | true              | 超长内容是否 LLM 总结                  |
| `llm.model`                              | planner           | 总结用的模型**任务名**（非原始模型 ID）        |
| `alt_text.max_images`                    | 3                 | 每次抓取最多 VLM 描述的图片数（0 关闭）        |
| `alt_text.cache_size`                    | 1024              | 持久描述缓存条目上限                     |


> **安全提醒**：`fetch.cookies`、`fetch.domain_cookies`、`jina.api_key` 属于敏感信息，请勿提交到版本库。
> 另外 jina 在转发 Cookie（`X-Set-Cookie`）时会绕过其缓存，请求可能稍慢。

### Planner 常显（可选）

默认情况下 `fetch_url` 在 **deferred** 工具池里，麦麦需要先通过 `tool_search` 发现它才能调用。若希望 Planner 每轮都能直接看到并调用 `fetch_url`，在配置中开启：

```toml
[plugin]
always_visible_for_planner = true
```

开启后等同于声明 `core_tool=True`。请谨慎使用——常显工具会增加 Planner 的选择成本。修改此项后，需要在 WebUI 中**禁用并重新启用插件**（或重启 MaiBot）才能生效；仅保存配置不会更新已注册的工具元数据。

## 工具用法（麦麦视角）

```text
fetch_url(url, start_char=0, end_char=-1, on_exceed="summarize", summary_focus="")
```

- 抓取整页：`fetch_url(url="https://example.com/article")`
- 内容太长被总结后，分页读原文：`fetch_url(url=..., start_char=0, end_char=7000)`，下一页 `start_char=7000`
- 强制要原文不要摘要：`fetch_url(url=..., on_exceed="truncate")`
- 定向总结：`fetch_url(url=..., summary_focus="价格和发布时间")`
- 抓图片：`fetch_url(url="https://example.com/photo.png")` → 图片直接出现在上下文中

## 测试

```bash
# 离线冒烟测试（不需要 MaiBot Host）
cd maibot-fetch-url-plugin
PYTHONPATH=<maibot-plugin-sdk 路径> python tests/smoke_test.py
```

本地加载测试：将插件放入 `MaiBot/plugins/` 后启动 MaiBot，观察日志中的
`fetch_url 插件已加载`；随后在聊天中让麦麦抓取一个网页 / 图片 / PDF 验证各路径。

## 常见问题

- **抓取内网地址被拦截**：默认开启 SSRF 防护，确有需要时打开 `fetch.allow_private_networks`
- **PDF 抓取失败**：PDF 仅支持通过 jina 解析，请确认 `jina.enabled = true` 且 jina 可达
- **图片描述没有生效**：确认 Host 已配置 `vlm` 模型任务（与麦麦收图识别共用），且 `alt_text.max_images > 0`
- **总结质量不佳**：可调整 `llm.model`（如换成 `replyer`）或自定义 `llm.summarize_prompt_template`
- **需要登录的页面**：在 `fetch.domain_cookies` 中为对应域名配置 Cookie

## License

MIT