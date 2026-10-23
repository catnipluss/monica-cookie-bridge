# monica-cookie-bridge

[中文](#工作原理) | [English](#english)

把 Monica 网页版的登录会话桥接成本地 **OpenAI 兼容 API**，供 [DeepSeek Harness (DSH)](https://github.com/deepseek-ai) 或任何 OpenAI 兼容客户端使用。纯本地运行，单文件，无第三方服务。

> **⚠️ 免责声明 / Disclaimer**
>
> - 本项目是**非官方**的实验性工具，与 Monica（monica.im）无任何关联。它调用的是 Monica 网页版的内部接口，不是官方 API。
> - Monica 的服务条款可能禁止此类自动化使用。使用本工具**可能导致你的账号受到限制**，接口也可能随时变更失效。是否使用、风险自负。
> - 请仅用于个人学习目的。Cookie 是你账号的登录凭据，本工具不会上传、记录或展示它，但也请勿将它粘贴到任何聊天、截图或 issue 中。

## 工作原理

```
DSH / OpenAI 客户端
      │  POST /v1/chat/completions (OpenAI 格式)
      ▼
本地桥 (127.0.0.1:8765)
      │  转换为 Monica 网页端内部格式 (SSE)
      ▼
api.monica.im  ← 使用你自己在浏览器登录所得的 Cookie
```

- 桥只监听 `127.0.0.1`，API 路由需要本地生成的 key 鉴权，配置页有 CSRF 保护。
- Cookie 只保存在进程内存中（`/setup` 页面手动粘贴），进程退出即清空，不落盘、不打日志。
- 本地 API key 持久化在 `.bridge-api-key`（自动生成，已 gitignore），重启桥不需要改客户端配置。

## 快速开始

环境要求：Python 3.11+

```bash
pip install -r requirements.txt
python server.py
```

启动后打开 `http://127.0.0.1:8765/setup`：

1. **获取 Cookie**：浏览器登录 monica.im → 按 `F12` 打开开发者工具 → `Network`（网络）标签 → 随便刷新页面点一个请求 → 在请求头里找到 `Cookie:`，复制**整行值**粘贴到桥的输入框。
2. 模型 ID 输入框保持默认即可（内置 20+ 官方模型映射），点 **Save in memory**。
3. 页面上会显示本地 API key，记下来。

### 在 DSH 中配置

**Settings → Models → Add model provider → Custom model API**：

| 项 | 值 |
|---|---|
| Provider ID | `monica-local` |
| Base URL | `http://127.0.0.1:8765/v1` |
| 协议 | OpenAI Chat Completions |
| API key | `/setup` 页面显示的本地 key |

其他 OpenAI 兼容客户端同理：Base URL 填 `http://127.0.0.1:8765/v1`，模型列表可通过 `GET /v1/models` 发现。

### 模型列表

默认模型 ID 与 Monica 官方网页配置对照（`server.py` 内 `MODEL_BOT_UIDS`），包括 GPT、Claude、Gemini、Grok、GLM、Qwen、Kimi、DeepSeek 等系列。可在 `/setup` 页面自定义逗号分隔的模型 ID 列表，或用环境变量 `MONICA_MODELS` 指定。

## 特性

- ✅ OpenAI Chat Completions 兼容（流式 / 非流式）
- ✅ 富对话历史宽容转换：`tool` 角色、`tool_calls`、多模态块自动转为文字描述（长对话不炸 400）
- ✅ 上游偶发 502/超时自动重试（首字前 4 次尝试、指数退避，调用方无感）
- ✅ 直连上游（`trust_env=False`），不绕本地代理
- ✅ 安全基线：仅回环监听、Bearer 鉴权、CSRF、不记录请求体

## 已知限制

- 仅支持**文本对话**：不支持图片生成、文件上传、原生 function calling、usage 统计。
- Monica 后端存在轮换式限流，个别请求可能挂起；桥的重试能兜住大部分，偶尔仍需重发。
- Cookie 会过期（通常数天到数周），过期后在 `/setup` 重新粘贴即可。
- 完整对话历史会被序列化为一条 Monica 消息，system 指令的优先级会弱化。

## 测试

```bash
python -m unittest discover -s tests
```

20 个测试覆盖 HTTP 鉴权/回环/Origin/体积限制、模型白名单、错误脱敏、富历史转换，以及通过 fake transport 模拟的 Monica 请求/SSE 契约——全部使用假凭据，不发任何真实外部请求。

## English

Bridge a signed-in Monica web session into a **local OpenAI-compatible API** for DeepSeek Harness or any OpenAI-compatible client. Single-file, localhost-only.

```bash
pip install -r requirements.txt
python server.py
# open http://127.0.0.1:8765/setup
# paste your Monica Cookie header (F12 → Network → any request → Cookie header)
# point your client at http://127.0.0.1:8765/v1 with the local key shown on the setup page
```

**This is unofficial and experimental.** It uses an undocumented web endpoint and may violate Monica's Terms of Use; account restrictions or breakage are possible. Use at your own risk, for personal/educational purposes only. Never share your Cookie header. Text chat only.

## 文档

- [CHANGELOG.md](CHANGELOG.md) — 版本改动记录
- [docs/notes.md](docs/notes.md) — 技术决策与踩坑记录（代理直连、重试设计、限流观察等实测结论）

## License

[MIT](LICENSE)
