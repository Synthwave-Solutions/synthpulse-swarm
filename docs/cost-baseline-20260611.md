# Cost baseline — 2026-06-11 (before token-cost optimization)

Snapshot taken immediately before deploying: tool-result aging, browser_steps,
delegate_task re-enable, live-context diet, supervisor sweep idle-skip, costs endpoint.
Compare 24-48h after rollout.

## LiteLLM Postgres (`"LiteLLM_SpendLogs"`, last 48h, prompt_tokens > 0)

| model | requests | spend USD | prompt tokens | completion tokens | avg prompt/req | % req >100k |
|---|---|---|---|---|---|---|
| DeepSeek-V4-Flash | 1994 | 30.05 | 164,121,454 | 444,497 | 82,308 | 43.9% |
| gpt-5.4-nano | 23 | 0.01 | 22,236 | 4,978 | 967 | 0% |
| Kimi-K2.6 | 2 | 0.00 | 9,732 | 100 | 4,866 | 0% |

Run rate ≈ $15/day in this window (earlier 48h windows measured up to $25/day).
~99% of spend is DeepSeek INPUT tokens. Output is negligible ($0.23/48h).

## monitoring.db (last 48h)

- `token_usage` events (agent turns): **122** → ~10-25 LLM API iterations per turn
  (1994 requests / 122 turns), each resending the full context.
- `supervisor_sweep` events: **17** (fired unconditionally, including all-idle windows).
- Worker history composition (live Hermes session DBs): tool results = 51-60% of chars
  (growth2 51%, sales2 54%, product2 60%); growth2 ≈ 1.7M tokens per turn.

## Caching facts (empirically probed 2026-06-11, for a possible future mini pilot)

- Azure prompt caching WORKS for gpt-5.4 family via the localhost:4000 proxy
  (gpt-5.4-nano: cached_tokens=4352/4638 on an identical repeat; ~90% discount on cached input).
- Azure does NOT cache DeepSeek-V4-Flash or Kimi-K2.6 (cached_tokens=0 on identical repeats).
- Breakeven cache-hit share for gpt-5.4-mini ($0.25/M in, $0.025/M cached) vs
  DeepSeek ($0.19/M): ~27%.
- The `litellm-model` alias shuffles across 5 Azure resources — any future caching
  model must be pinned per-agent to a single deployment for cache affinity.

## Comparison queries

```sql
-- LiteLLM Postgres
SELECT model, count(*), round(sum(spend)::numeric,3), round(avg(prompt_tokens))
FROM "LiteLLM_SpendLogs" WHERE "startTime" > now() - interval '48 hours'
AND prompt_tokens > 0 GROUP BY 1 ORDER BY 3 DESC;
```

```bash
# After rollout: GET /teams/saas2/costs?hours=24
curl -s localhost:8011/teams/saas2/costs?hours=24
```
