# DeepSeek V4 Flash 真实闭环测试

该测试是显式 opt-in 的付费组合测试。MatterLoop 发行包不读取环境变量；只有测试组合根读取
`.env.local`，并由调用方构造 `openai.AsyncOpenAI` 后注入
`matterloop_models.providers.DeepSeekChatModelClient`。供应商适配子包依赖
`matterloop_models` 的稳定协议与 DTO，模型抽象层不会反向导入 providers。

## 1. 准备临时配置

每次执行前先核对 [DeepSeek 官方价格页](https://api-docs.deepseek.com/quick_start/pricing/)，
再在仓库根目录手工创建 `.env.local`。下面是 **2026-07-14 核对的
`deepseek-v4-flash` 示例**，单位均为每一百万 Token 的 micro-USD；它不是库内默认价格：

```dotenv
MATTERLOOP_RUN_LIVE_DEEPSEEK=1
DEEPSEEK_API_KEY=<临时密钥>

DEEPSEEK_PRICING_EFFECTIVE_DATE=2026-07-14
DEEPSEEK_INPUT_MICROS_PER_MILLION=140000
DEEPSEEK_OUTPUT_MICROS_PER_MILLION=280000
DEEPSEEK_CACHE_HIT_INPUT_MICROS_PER_MILLION=2800
DEEPSEEK_CACHE_MISS_INPUT_MICROS_PER_MILLION=140000
DEEPSEEK_REASONING_OUTPUT_MICROS_PER_MILLION=280000
```

如果官方价格已变更，必须同时更新价格值和
`DEEPSEEK_PRICING_EFFECTIVE_DATE`。测试不会猜测、下载或内置价格。

限制文件权限：

```bash
chmod 0600 .env.local
```

## 2. 运行

```bash
uv run --env-file .env.local pytest -m live_deepseek -s
```

测试使用精确模型名 `deepseek-v4-flash` 和官方 OpenAI 格式端点
`https://api.deepseek.com`。未显式启用、未配置密钥、价格或价格生效日期时，测试会跳过。

本地硬限额为：

- 最多 12 次模型调用，模型并发最多 2。
- 最多 40,000 总 Token，估算费用最多 30,000 micro-USD（0.03 USD）。
- 最多 2 个 Agent 任务和 6 次无副作用内存工具调用。

标准输出只包含状态、事件类型、调用计数、Token 和估算费用，不包含提示词、
`reasoning_content` 或密钥。

## 3. 清理

执行完成后删除 `.env.local`，并在 DeepSeek 平台撤销临时密钥。不要将该文件、真实密钥或付费请求快照提交到仓库。
