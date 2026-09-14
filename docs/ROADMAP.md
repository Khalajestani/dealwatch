# Roadmap

Ordered by expected value for the goal (buy household stuff and wish-list items at the best moment, cheaply).

1. **Structured deal sources (no LLM needed)** - adapters that read weekly offers directly: Albert Heijn Bonus, Jumbo,
   Kruidvat/Etos folders, Lidl/Action folders. Structured data is exact, free and covers the biggest household wins
   (1+1, 2e halve prijs). Design: `services/sources/<shop>.py` returning `Offer`s for matching items, merged into the
   same `observations` table with `promo_type`/validity; the LLM search then only covers what sources don't.
2. **Batch API for scheduled runs** - the Message Batches API halves token cost and supports web search. Runs are
   asynchronous anyway: submit all batches, poll until done (needs a "pending" run state and a resume step).
3. **bol.com Partner/Open API** for exact prices and stock on bol-listed items (requires affiliate registration).
4. **Verify top deals with `web_fetch`** - fetch the product page of the top N deals to confirm price/pack before
   reporting (raises confidence, costs one fetch per deal).
5. **Stock planner** - per item: consumption rate + current stock → "you run out in ~3 weeks" so a deal today is worth
   more than a deal next month; also drives urgency and quantity suggestions.
6. **Price-drop alerts for wish-list items between runs** - cheap single-item checks at a higher cadence near sale
   periods (Black Friday, Singles Day, Prime Day, Sinterklaas), plus a static seasonal calendar hint in the prompt.
7. **Evaluation set** - collect real batch outputs, label correct/incorrect extractions, and tune prompt/effort/model
   against it (`/claude-api build-eval`).
8. **Tweakers Pricewatch / Kieskeurig** as aggregator sources for electronics and tools.
9. **Quality of life** - CSV import/export of items, "ignore this listing" feedback that lowers confidence for a URL,
   per-item retailer overrides, dark-mode polish, mobile layout tweaks.
10. **Deployment** - Dockerfile + compose for a NAS, optional basic auth if ever exposed on a LAN, DB backups.
