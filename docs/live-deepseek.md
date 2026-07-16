# 运行 DeepSeek 付费冒烟测试

默认测试套件完全离线。`live_deepseek` 只用于发布前确认供应商协议没有漂移，会产生真实费用，
也可能受到账号权限和供应商限流影响。

MatterLoop 源码不读取环境变量。下面的变量只由 pytest 组合根读取，用来创建 SDK client 和
`TokenRateCard`，随后以普通对象注入模型适配器。

## 准备一次性配置

先在 [DeepSeek 官方价格页](https://api-docs.deepseek.com/quick_start/pricing/) 核对测试模型当前价格，
再在仓库根目录创建 `.env.local`：

```dotenv
MATTERLOOP_RUN_LIVE_DEEPSEEK=1
DEEPSEEK_API_KEY=<一次性测试密钥>

DEEPSEEK_PRICING_EFFECTIVE_DATE=<YYYY-MM-DD>
DEEPSEEK_INPUT_MICROS_PER_MILLION=<整数>
DEEPSEEK_OUTPUT_MICROS_PER_MILLION=<整数>
DEEPSEEK_CACHE_HIT_INPUT_MICROS_PER_MILLION=<整数>
DEEPSEEK_CACHE_MISS_INPUT_MICROS_PER_MILLION=<整数>
DEEPSEEK_REASONING_OUTPUT_MICROS_PER_MILLION=<整数>
```

费率单位是每百万 Token 的 micro-USD。例如 1 USD 等于 1,000,000 micro-USD。不要照抄历史价格；
测试不会下载价格或猜测缺失值。

```bash
chmod 0600 .env.local
uv run --env-file .env.local pytest -m live_deepseek -s
```

缺少启用开关、密钥、费率或生效日期时，测试会跳过，而不是退回到无预算调用。

## 测试会做什么

- 运行一次“计划 → 人工修订 → 两个 Agent → 验证 → 团队审查”的完整闭环。
- 验证低额度场景会在第二次模型调用前被本地账本阻止。
- 强制最多 12 次模型调用、40,000 总 Token、2 个并发模型调用、2 个 Agent 任务、6 次无副作用
  工具调用，估算费用上限为 30,000 micro-USD。
- 标准输出只打印状态、事件、调用计数、Token 与估算费用，不打印提示词、continuation、reasoning
  或密钥。

这不是模型质量基准，也不会查询账户历史用量。失败时先区分协议回归、账号权限、余额、限流和
网络问题，不要通过放宽本地硬限额来“让测试通过”。

## 收尾

测试结束后删除 `.env.local`，并在供应商控制台撤销一次性密钥。不要提交该文件、真实请求快照
或包含供应商异常正文的日志。
