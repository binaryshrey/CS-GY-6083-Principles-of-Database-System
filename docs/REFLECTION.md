# Evaluation and reflection

Experiment `main-v1`: the 196 unique BIRD Mini-Dev questions of formula_1, european_football_2,
codebase_community and financial, each run 3 times by each pipeline (1,176 runs, all finished),
on PostgreSQL 14.20 (Apple M1), with `claude-haiku-5-5` at effort `medium`, prompt version `p1`.
Full tables: `docs/evaluation_results.md`.

- **baseline** = write SQL + up to 2 error fixes.
- **plan_guided** = baseline + up to 2 rewrites guided by `EXPLAIN ANALYZE`, triggered when the plan
  shows an issue or the query takes ≥ 50 ms; a rewrite is kept only if it returns exactly the same
  multiset of rows and its median is ≥ 10 % lower.

## Headline numbers

| | baseline | plan_guided |
|---|---|---|
| Execution accuracy (EX) | 62.2 % | 61.2 % |
| R-VES | 54.3 | 55.0 |
| Avg runtime ÷ gold (correct runs) | 1.21× | 1.13× |
| R-VES on slow questions (gold ≥ 10 ms) | 58.9 | 63.0 |
| LLM calls per run | 1.01 | 1.48 |
| Tokens per run | 5,690 | 9,332 |
| LLM time per run | 2.6 s | 4.7 s |

## 1. Did reading the plan make queries faster without breaking them?

Yes, where there was something to fix. The cleanest measurement compares the query before and
after the rewrite stage **inside the same run**, which removes the noise of two different
initial queries:

- 131 of 588 plan-guided runs tried a rewrite; 54 ended with a rewritten query.
- Correct answers: 75 before, 75 after. No rewrite changed a correct answer into a wrong one.
- Total runtime of those 131 queries: 9.0 s before, 4.6 s after (−49 %). Kept rewrites were
  1.81× faster (geometric mean).
- Of 257 rewrite attempts, 59 were kept, 188 were not faster, and 10 returned different rows.
  Those 10 are the case Part 1 warned about ("faster can mean wrong"): the model quietly changed
  the query's meaning. The multiset comparison against the base query caught every one, and it
  never needs the gold answer, so the same guard works on users' own questions.

Typical successful rewrites, matching the optimizer lessons from class:

- **Sort spill → top-1 lookup (11.1×, formula_1 q1014).** `DISTINCT ON (circuitid) … ORDER BY
  circuitid, milliseconds` sorted ~420k lap times and spilled to disk; the rewrite uses a
  `LATERAL (… ORDER BY milliseconds LIMIT 1)` per circuit.
- **Decorrelation (6.3×, codebase_community q595).** Two correlated `EXISTS / NOT EXISTS`
  subqueries over `posthistory` ran once per user (`correlated_subplan`, 2.8 s); the rewrite
  computes the two user sets once in CTEs (0.44 s).
- **Read a large table once (5.2×, financial q116).** Two CTEs each scanned `trans` (1M rows);
  the rewrite filters it once and reads the small result twice.
- **Cheaper predicate (3.6×, formula_1 q1011).** A regular expression on every lap time
  became two `LIKE` patterns.

Several of the largest speedups are on queries that were already wrong. The rewrite preserved
their (wrong) rows exactly, as designed: rewriting optimizes the query it is given; it does not
re-check the meaning against the question.

## 2. Where it helps and where it does not

| Base query median | Rewrite attempts | Kept | Different rows (caught) |
|---|---|---|---|
| < 10 ms | 6 | 1 | 0 |
| 10–50 ms | 180 | 23 (13 %) | 10 |
| 50–100 ms | 59 | 24 (41 %) | 0 |
| ≥ 100 ms | 12 | 11 (92 %) | 0 |

The gains come from a small group of slow queries, as Part 1 predicted. Most attempts (180)
were on 10–50 ms queries that were flagged only for a large sequential scan; there the model
rarely found a faster equivalent, because the scan itself is the cost, and all 10 wrong-result
rewrites came from this group. Above 50 ms the plan usually showed something a rewrite can
remove, and above 100 ms 11 of 12 attempts were kept. On slow questions R-VES rose from 58.9 to
63.0; on fast questions it moved within noise (50.8 vs 48.8).

The `large_seq_scan` flag appears in about 145 final queries for both pipelines and almost
never disappears after rewriting. Many BIRD tables have no index on the filtered column, so no
SQL rewrite can avoid the full scan. That is the motivation for the stretch goal (index
suggestions): the plan says *what* is slow, but only a physical change fixes it.

## 3. What did not change, and why that matters

Overall EX (62.2 % vs 61.2 %) and R-VES (54.3 vs 55.0) differ by less than the spread between
repeats of the **same** pipeline (baseline 61.2–62.8 %, plan-guided 59.7–62.2 %). The two
pipelines write their initial query independently, and that sampling noise is larger than the
effect of the rewrite stage on accuracy. Comparing configs only by their headline numbers would
have hidden the real result; the within-run comparison and the repeats were necessary.

## 4. Cost

Plan-guided runs used 64 % more tokens (9.3k vs 5.7k per run), 0.47 more LLM calls, and about
2.1 s more model time. The database time saved on the 131 rewritten runs averaged 34 ms per run.
A rewrite therefore only pays off for queries that run many times (a report, a dashboard query)
or for genuinely slow ones, which suggests triggering it on runtime (for example ≥ 10 ms)
rather than on the `large_seq_scan` flag, which fired on many fast queries. The whole experiment
cost under $1.40 by the project's deliberately pessimistic count.

## 5. Limitations

- One model (Haiku 5.5, the cheapest) at one effort; a stronger model would raise EX but the
  study compares pipelines on the same model.
- Timings are on one laptop with warm caches; medians of 5 runs, one query timed at a time.
  Sub-millisecond queries are dominated by noise.
- Correctness uses BIRD's set comparison; the rewrite guard uses the stricter multiset rule.
- Mini-Dev lists questions 137 and 138 twice; 196 unique questions were used.
