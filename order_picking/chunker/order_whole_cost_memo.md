# Keeping a customer order in one picker job

This note is for whoever owns that rule. The number below is the cost of the rule on one sample wave. It is not a request to split orders.

## The rule

A source order that fits one picker stays in one job. Several orders may share a job, and those orders may be for different stops. The model does not split an order to shorten the walk.

## The number

Sample file: `order_picking/inputs/chunk_warehouse/truck_loads.csv`. Sixteen trucks, one generated wave. After six orders (48 lines) that fit no picker are removed, both sides cover the same 2,247 lines.

| Cover | Jobs | Minutes |
|---|---:|---:|
| Exact cover, orders kept whole | 171 | 2,975.885 |
| Savings chunker, allowed to split an order | 168 | 2,371.931 |

The rule costs at least 604 minutes on this file. About 589 of those minutes are walking. Fifteen minutes are three extra jobs at 5 minutes of overhead each.

## What the number is not

This is one sample wave, not a measured day of live volume.

604 minutes is a floor. The savings figure is one feasible way to split orders, not the best possible split. A better split would make the rule cost more than 604, not less.

On the same rule, a Clarke-Wright merge of whole orders takes 187 jobs and 3,164.469 minutes. The exact cover beats that by 189 minutes. Most of the 604 is the rule. A smaller part, 189 minutes, is the difference between the exact cover and a greedy merge that obeys the same rule.

## The question

If the rule is there so the truck is easier to pack, rather than for a hard operational or contractual reason, a middle option is to split only the large orders and leave the rest whole. That measurement waits for an answer. It has not been run.
