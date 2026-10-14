# Roadmap

Ordered by expected value for the goal (buy household things and wish-list items at the best moment, cheaply).

1. **Structured shop sources (no Claude needed).** Supermarket offers are structured data:
   - **Albert Heijn** (`ah.nl`) and **Jumbo** (`jumbo.com`): unofficial mobile APIs (product search, bonus
     offers). The `supermarktconnector` package wraps both; evaluate it first, it may lag behind API changes.
   - **Lidl**, **Kruidvat**, **Etos**, **Action**: no APIs; leave them to Claude's search.
   Shape: a `services/sources/<shop>.py` with `fetch(items, settings, today) -> [(item_id, Offer)]`, called by the
   runner before the batches for every active item; offers stored like Claude's, with a new `Observation.source`
   column. Match products to items by brand, name, hints and parsed pack size; keep the matched product id on the
   item so later checks go straight to it. A setting could then skip Claude for items a source already covered.
2. **Verify top deals** by opening the product page of the top few deals in a second small run before reporting.
3. **Import from TOOL·E**: its `/api/shopping-list` already uses Dealwatch's field names (low-stock and resupply
   items as need, wishlist as wishlist). Needs `TOOLE_API_TOKEN` in both `.env` files and a daily import step.
4. **Stock planner**: consumption rate and current stock give "you run out in about 3 weeks", so a deal today is
   worth more than one next month.
5. **Sale periods**: denser checks for wish-list items around Black Friday, Singles Day, Prime Day and Sinterklaas.
6. **Evaluation set**: collect real batch results, mark right and wrong reads, and tune the instructions, effort and
   model against them.
7. **Tweakers Pricewatch / Kieskeurig** as aggregator shops for electronics and tools.
8. **Quality of life**: "ignore this listing" feedback that lowers confidence for a URL, per-item shop overrides.
