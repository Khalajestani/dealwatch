# Roadmap

Ordered by expected value for the goal (buy household stuff and wish-list items at the best moment, cheaply).

1. **Structured deal sources (no LLM needed) - NEXT.** The interface exists (`services/sources/__init__.py`:
   `DealSource.fetch(items, settings, today) -> [SourceOffer]`, registry `SOURCES`, `FakeSource` as the reference
   implementation; the runner already stores results with `source=<name>` and folds them into price points and deals).
   Per shop:
   - **Albert Heijn** (`ah.nl`): unofficial mobile API used by the AH app (anonymous token, product search, bonus
     endpoints). The `supermarktconnector` PyPI package wraps it (`AHConnector().search_products(query=...)`) together
     with Jumbo; evaluate it first, it may lag behind API changes. Returns product name, price, bonus price/mechanism,
     unit size - enough for `pack_quantity`/`unit`.
   - **Jumbo** (`jumbo.com`): unofficial mobile API (`mobileapi.jumbo.com`, search + promotions); also covered by
     `supermarktconnector` (`JumboConnector`).
   - **Lidl** (`lidl.nl`): no product API; weekly offers are on lidl.nl folder pages. Options: scrape the offers page
     (fragile), or leave Lidl to the LLM allowlist. The Lidl Plus app API (`lidl-plus` package) covers coupons/receipts,
     not the folder.
   - **Kruidvat / Etos / Action**: no APIs; folder aggregator pages or LLM allowlist.
   Matching items to products: build the query from brand + name + hints, score candidates by token overlap and parsed
   size (regex for `8 rollen`, `500 ml`, `60 stuks`), keep the best match above a threshold, and store the matched
   product id on the item (add a nullable `Item.external_ids` JSON column) so later runs re-check the exact product
   without searching. Policy to add: a setting to skip LLM batches for items a source already covered this run.
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
