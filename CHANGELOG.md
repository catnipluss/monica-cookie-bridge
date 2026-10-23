# Changelog

## 0.1.0 — 2026-10-24

初始发布。

- OpenAI Chat Completions 兼容端点：`POST /v1/chat/completions`（流式 / 非流式）、`GET /v1/models`
- 内置 20+ Monica 官方模型 ID ↔ bot_uid 映射（对照自 Monica 网页 serverConfig，部分经实测验证）
- 富对话历史宽容转换：`tool` / `developer` 角色、`tool_calls`、多模态块自动转为文字描述，长对话不再 400
- 上游自动重试：首字前失败最多 4 次尝试（0.5s / 1.5s / 3s 退避），401/403 凭据类错误不重试
- 直连上游（`trust_env=False`），不绕本地系统代理
- 安全基线：仅回环监听、Bearer 鉴权、CSRF 保护、不记录请求体与上游响应体
- 本地 API key 持久化（`.bridge-api-key`），重启桥无需修改客户端配置
- Cookie 仅存进程内存，进程退出即清空
