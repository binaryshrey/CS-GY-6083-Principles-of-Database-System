# Evaluation results: experiment `main-v1`

_196 BIRD Mini-Dev questions x {baseline, plan_guided} x 3 repeats, claude-haiku-5-5, effort medium_  
Started 2026-11-25 21:45, frozen: True

## Setup

- Hardware: Apple M1, Darwin 25.4.0
- PostgreSQL: PostgreSQL 14.20 (Homebrew) on aarch64-apple-darwin25.1.0; Python 3.12.12; anthropic SDK 2.14.0
- Timing: 1 warm-up + 5 timed runs per query; median of timed runs; boundary = PostgreSQL `Execution Time` from `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)` (excludes network, client and LLM time); one query timed at a time
- Rewrite trigger: plan issue or median >= 50.0 ms; accepted if same multiset of rows and >= 10% faster than its base query
- Slow question: gold median >= 10 ms

| db_name | questions | tables | size_mb |
|---|---|---|---|
| formula_1 | 66 | 13 | 75.31 |
| european_football_2 | 51 | 7 | 143.48 |
| codebase_community | 49 | 8 | 391.98 |
| financial | 30 | 8 | 121.57 |
| california_schools | 0 | 3 | 11.38 |

| label | model_name | effort | prompt_version | max_fix_attempts | max_rewrite_attempts |
|---|---|---|---|---|---|
| baseline | claude-haiku-5-5 | medium | p1 | 2 | 0 |
| plan_guided | claude-haiku-5-5 | medium | p1 | 2 | 2 |

## Accuracy, speed and cost per config

| label | runs | ex_percent | rves | avg_ratio_vs_gold | median_ms_correct | avg_tokens | avg_calls | avg_llm_seconds |
|---|---|---|---|---|---|---|---|---|
| baseline | 588 | 62.2 | 54.34 | 1.214 | 3.608 | 5690 | 1.01 | 2.6 |
| plan_guided | 588 | 61.2 | 54.97 | 1.131 | 3.811 | 9332 | 1.48 | 4.7 |

## Slow vs fast questions

| label | bucket | runs | ex_percent | rves | avg_ratio_vs_gold |
|---|---|---|---|---|---|
| baseline | slow | 255 | 61.2 | 58.92 | 0.865 |
| baseline | fast | 333 | 63.1 | 50.83 | 1.473 |
| plan_guided | slow | 255 | 62.4 | 63.04 | 0.784 |
| plan_guided | fast | 333 | 60.4 | 48.80 | 1.405 |

## Head to head on the same questions

| config_a | config_b | both_correct | a_faster | b_faster | only_a_correct | only_b_correct |
|---|---|---|---|---|---|---|
| baseline | plan_guided | 128 | 27 | 37 | 5 | 6 |

## Effect of the rewrite stage (same run, before vs after)

| label | runs_with_rewrite_attempt | final_is_rewrite | correct_before | correct_after | geomean_speedup_rewritten | total_ms_before | total_ms_after |
|---|---|---|---|---|---|---|---|
| plan_guided | 131 | 54 | 75 | 75 | 1.81 | 8998.4 | 4574.6 |

## Rewrite outcomes

| label | outcome | n |
|---|---|---|
| plan_guided | not_faster | 188 |
| plan_guided | accepted | 59 |
| plan_guided | different_result | 10 |

## Plan issues before rewriting vs in the final query

| label | issue | before_rewrite | in_final |
|---|---|---|---|
| baseline | large_seq_scan | 146 | 146 |
| baseline | nested_loop_large | 3 | 3 |
| baseline | sort_spill | 3 | 3 |
| plan_guided | correlated_subplan | 1 | 1 |
| plan_guided | large_seq_scan | 143 | 143 |
| plan_guided | nested_loop_large | 5 | 3 |
| plan_guided | sort_spill | 2 | 0 |

## By database

| db_name | label | runs | ex_percent | rves |
|---|---|---|---|---|
| codebase_community | baseline | 147 | 65.3 | 62.41 |
| codebase_community | plan_guided | 147 | 63.9 | 62.59 |
| european_football_2 | baseline | 153 | 75.2 | 71.57 |
| european_football_2 | plan_guided | 153 | 77.1 | 75.49 |
| financial | baseline | 90 | 55.6 | 44.17 |
| financial | plan_guided | 90 | 48.9 | 41.11 |
| formula_1 | baseline | 198 | 53.0 | 39.65 |
| formula_1 | plan_guided | 198 | 52.5 | 39.77 |

## LLM calls by stage

| label | stage | calls | avg_in | avg_out | avg_latency_ms |
|---|---|---|---|---|---|
| baseline | fix | 4 | 5063 | 761 | 3616 |
| baseline | initial | 588 | 5198 | 453 | 2591 |
| plan_guided | fix | 3 | 5102 | 796 | 4037 |
| plan_guided | initial | 588 | 5198 | 451 | 2508 |
| plan_guided | rewrite | 278 | 6898 | 828 | 4504 |

## Run status

| label | status | runs |
|---|---|---|
| baseline | done | 588 |
| plan_guided | done | 588 |

## Largest accepted speedups (before / after)

### Run 889 (question 1014, plan_guided): 11.08x, 84.316 -> 7.610 ms, issues before: sort_spill, matches gold: False

Before:
```sql
SELECT DISTINCT ON (c.circuitid) c.name, l.time FROM laptimes l JOIN races r ON r.raceid = l.raceid JOIN circuits c ON c.circuitid = r.circuitid WHERE c.country = 'Italy' ORDER BY c.circuitid, l.milliseconds
```
After:
```sql
SELECT c.name, best.time FROM circuits c JOIN LATERAL (SELECT lt.time FROM races r JOIN laptimes lt ON lt.raceid = r.raceid WHERE r.circuitid = c.circuitid ORDER BY lt.milliseconds LIMIT 1) best ON true WHERE c.country = 'Italy' ORDER BY c.circuitid
```

### Run 349 (question 595, plan_guided): 6.33x, 2815.197 -> 444.618 ms, issues before: correlated_subplan, large_seq_scan, nested_loop_large, matches gold: False

Before:
```sql
SELECT u.id AS userid FROM users u WHERE u.views >= 1000 AND EXISTS (SELECT 1 FROM posthistory ph WHERE ph.userid = u.id) AND NOT EXISTS (SELECT 1 FROM posthistory ph WHERE ph.userid = u.id GROUP BY ph.postid HAVING COUNT(*) > 1)
```
After:
```sql
WITH ph_users AS (SELECT DISTINCT userid FROM posthistory WHERE userid IS NOT NULL), dup_users AS (SELECT DISTINCT userid FROM posthistory WHERE userid IS NOT NULL GROUP BY userid, postid HAVING COUNT(*) > 1) SELECT u.id AS userid FROM users u JOIN ph_users p ON p.userid = u.id WHERE u.views >= 1000 AND NOT EXISTS (SELECT 1 FROM dup_users d WHERE d.userid = u.id)
```

### Run 348 (question 595, plan_guided): 5.56x, 347.276 -> 62.504 ms, issues before: large_seq_scan, matches gold: False

Before:
```sql
WITH per_post AS (SELECT userid, postid, COUNT(*) AS cnt FROM posthistory GROUP BY userid, postid) SELECT p.userid FROM per_post p JOIN users u ON u.id = p.userid WHERE u.views >= 1000 GROUP BY p.userid HAVING MAX(p.cnt) = 1
```
After:
```sql
WITH good_users AS (SELECT id FROM users WHERE views >= 1000), per_post AS (SELECT ph.userid, ph.postid, COUNT(*) AS cnt FROM posthistory ph WHERE ph.userid IN (SELECT id FROM good_users) GROUP BY ph.userid, ph.postid) SELECT userid FROM per_post GROUP BY userid HAVING MAX(cnt) = 1
```

### Run 888 (question 1014, plan_guided): 5.42x, 68.770 -> 12.690 ms, issues before: sort_spill, matches gold: False

Before:
```sql
SELECT DISTINCT ON (c.circuitid) c.name, lt.time AS lap_record FROM circuits c JOIN races r ON r.circuitid = c.circuitid JOIN laptimes lt ON lt.raceid = r.raceid WHERE c.country = 'Italy' ORDER BY c.circuitid, lt.milliseconds ASC
```
After:
```sql
WITH italy AS (SELECT circuitid, name FROM circuits WHERE country = 'Italy'), best AS (SELECT r.circuitid, MIN(lt.milliseconds) AS ms FROM italy i JOIN races r ON r.circuitid = i.circuitid JOIN laptimes lt ON lt.raceid = r.raceid GROUP BY r.circuitid) SELECT DISTINCT ON (i.circuitid) i.name, lt.time AS lap_record FROM italy i JOIN best b ON b.circuitid = i.circuitid JOIN races r ON r.circuitid = i.circuitid JOIN laptimes lt ON lt.raceid = r.raceid AND lt.milliseconds = b.ms ORDER BY i.circuitid
```

### Run 86 (question 116, plan_guided): 5.20x, 207.834 -> 39.970 ms, issues before: large_seq_scan, matches gold: True

Before:
```sql
WITH acc AS (SELECT l.account_id FROM loan l WHERE l.date = DATE '1993-07-05' ORDER BY l.loan_id LIMIT 1), bal_b AS (SELECT t.balance FROM trans t JOIN acc ON t.account_id = acc.account_id WHERE t.date <= DATE '1993-03-22' ORDER BY t.date DESC, t.trans_id DESC LIMIT 1), bal_a AS (SELECT t.balance FROM trans t JOIN acc ON t.account_id = acc.account_id WHERE t.date <= DATE '1998-12-27' ORDER BY t.date DESC, t.trans_id DESC LIMIT 1) SELECT (CAST(bal_a.balance AS NUMERIC) - bal_b.balance) / bal_b.balance * 100 AS increase_rate FROM bal_a, bal_b
```
After:
```sql
WITH acc AS (SELECT l.account_id FROM loan l WHERE l.date = DATE '1993-07-05' ORDER BY l.loan_id LIMIT 1), tr AS (SELECT t.date, t.trans_id, t.balance FROM trans t WHERE t.account_id = (SELECT acc.account_id FROM acc) AND t.date <= DATE '1998-12-27'), bal_b AS (SELECT tr.balance FROM tr WHERE tr.date <= DATE '1993-03-22' ORDER BY tr.date DESC, tr.trans_id DESC LIMIT 1), bal_a AS (SELECT tr.balance FROM tr WHERE tr.date <= DATE '1998-12-27' ORDER BY tr.date DESC, tr.trans_id DESC LIMIT 1) SELECT (CAST(bal_a.balance AS NUMERIC) - bal_b.balance) / bal_b.balance * 100 AS increase_rate FROM bal_a, bal_b
```

