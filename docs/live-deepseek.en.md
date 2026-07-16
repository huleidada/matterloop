[简体中文](live-deepseek.md) | English

# Running the paid DeepSeek smoke test

The default test suite is entirely offline. `live_deepseek` is used only before a release to confirm that the
provider protocol has not drifted. It incurs real charges and may be affected by account permissions and provider
rate limits.

MatterLoop source code does not read environment variables. The variables below are read only by the pytest
composition root, which uses them to create the SDK client and `TokenRateCard` and then injects those ordinary objects
into the model adapter.

## One-time setup

First verify the current price of the test model on the
[official DeepSeek pricing page](https://api-docs.deepseek.com/quick_start/pricing/), then create `.env.local` in the
repository root:

```dotenv
MATTERLOOP_RUN_LIVE_DEEPSEEK=1
DEEPSEEK_API_KEY=<one-time-test-key>

DEEPSEEK_PRICING_EFFECTIVE_DATE=<YYYY-MM-DD>
DEEPSEEK_INPUT_MICROS_PER_MILLION=<integer>
DEEPSEEK_OUTPUT_MICROS_PER_MILLION=<integer>
DEEPSEEK_CACHE_HIT_INPUT_MICROS_PER_MILLION=<integer>
DEEPSEEK_CACHE_MISS_INPUT_MICROS_PER_MILLION=<integer>
DEEPSEEK_REASONING_OUTPUT_MICROS_PER_MILLION=<integer>
```

Rates are expressed in micro-USD per million Tokens. For example, 1 USD equals 1,000,000 micro-USD. Do not copy a
historical price. The test does not download pricing or guess missing values.

```bash
chmod 0600 .env.local
uv run --env-file .env.local pytest -m live_deepseek -s
```

If the enable flag, key, rate, or effective date is missing, the test is skipped rather than falling back to an
unbudgeted call.

## What the test does

- Runs one complete “plan → human revision → two Agents → verification → team review” loop.
- Verifies that, under a low budget, the local ledger blocks the second model call before it begins.
- Enforces a maximum of 12 model calls, 40,000 total Tokens, 2 concurrent model calls, 2 Agent tasks, and 6
  side-effect-free tool calls, with an estimated-cost ceiling of 30,000 micro-USD.
- Prints only state, events, call counts, Tokens, and estimated cost to standard output. It does not print prompts,
  continuation, reasoning, or credentials.

This is not a model-quality benchmark and does not query historical account usage. When it fails, first distinguish
among a protocol regression, account permission, balance, rate limiting, and a network problem. Do not make the test
pass by relaxing the local hard limits.

## Cleanup

After the test, delete `.env.local` and revoke the one-time key in the provider console. Do not commit that file, real
request snapshots, or logs containing provider exception text.
