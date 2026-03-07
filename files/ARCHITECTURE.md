# ORCA — Architecture Deep Dive

## Design Philosophy

Three principles guide every decision in ORCA:

1. **Signals before headlines** — Every data source must be capable of detecting an event before it appears in financial news.
2. **Graph over lists** — Risk is relational. A list of affected companies is less useful than a map of *why* they're affected and *how* they connect.
3. **Portfolio-first output** — Every alert must terminate in a portfolio action. Awareness without actionability is noise.

---

## System Flow

```
External World
      │
      ▼
┌─────────────┐
│  INGESTION  │  ← polls cloud feeds, EDGAR, cyber sources
│   LAYER     │
└──────┬──────┘
       │ normalized Event objects
       ▼
┌─────────────┐
│    GRAPH    │  ← company → provider → region → service
│    LAYER    │
└──────┬──────┘
       │ affected node subgraph
       ▼
┌─────────────┐
│   ENGINE    │  ← propagates event through graph
│   LAYER     │     scores exposure vs portfolio
└──────┬──────┘
       │ scored alert
       ▼
┌─────────────┐
│   ALERTS    │  ← dispatches to Slack / webhook / dashboard
│   LAYER     │
└─────────────┘
```

---

## Layer 1 — Ingestion

### Responsibilities
- Poll external data sources on configurable intervals
- Parse heterogeneous formats (JSON, RSS, HTML, Atom)
- Normalize everything into a standard `Event` object
- Deduplicate events (same outage reported by multiple sources)
- Assign initial severity estimate

### Key Design Decisions

**Why normalize immediately?**
Downstream layers should never know or care where an event came from. AWS status JSON and Azure RSS look completely different — the graph layer should receive identical objects.

**Why track `acknowledged_at` separately from `detected_at`?**
The gap between detection and acknowledgment is the edge. An event detected at 14:32 but not acknowledged until 15:10 means 38 minutes of actionable lead time. This delta should be tracked and reported.

**Deduplication strategy**
Events are deduplicated by `(provider, region, event_type, date)`. The first source to detect wins. All subsequent sources for the same event are merged into `corroborating_sources[]`.

---

## Layer 2 — Exposure Graph

### Graph Structure

```
(Company) -[DEPENDS_ON]→ (CloudProvider)
(Company) -[HOSTED_IN]→  (Region)
(Company) -[USES]→       (Service)
(CloudProvider) -[CONTAINS]→ (Region)
(Region) -[OFFERS]→      (Service)
```

### Node Properties

```
Company:
  ticker          string      required
  name            string      required
  sector          string      GICS sector
  market_cap      float       USD
  cloud_primary   string      primary cloud provider
  cloud_secondary string[]    fallback providers
  dependency_source string[]  how we know (edgar, fingerprint, manual)
  confidence      float       0.0 - 1.0

CloudProvider:
  name            string      AWS | Azure | GCP | Oracle | IBM
  market_share    float       approximate

Region:
  provider        string
  region_id       string      e.g. us-east-1
  geography       string      US_EAST | EU_WEST | APAC etc.
  tier            int         1 = primary, 2 = secondary

Service:
  provider        string
  service_name    string      EC2 | S3 | RDS | Lambda | etc.
  criticality     string      compute | storage | database | networking
```

### Confidence Scoring

Not all dependencies are equally certain. ORCA tracks how each edge was established:

| Source | Base Confidence |
|---|---|
| SEC 10-K explicit mention | 0.90 |
| Job posting infrastructure keywords | 0.70 |
| IP/ASN fingerprinting | 0.65 |
| DNS/CDN analysis | 0.60 |
| Industry inference (SaaS → AWS typical) | 0.40 |
| Manual curation | 1.00 |

Edges below 0.50 confidence are flagged as `unverified` and excluded from severity-scored alerts by default.

### Graph Storage

Phase 1 uses **NetworkX** (in-memory, no infrastructure required).
Phase 2 migrates to **Neo4j** for production-scale querying and Cypher traversal.

---

## Layer 3 — Propagation Engine

### Event → Affected Subgraph

When an event fires:

```python
event = {
  "provider": "AWS",
  "region": "us-east-1",
  "services_affected": ["EC2", "RDS"],
  "severity": "high"
}
```

The engine queries the graph:
1. Find all `Region` nodes matching `(provider=AWS, region_id=us-east-1)`
2. Traverse `←[HOSTED_IN]` to find all `Company` nodes
3. Filter by `[USES]` — only companies using affected services
4. Return subgraph with confidence weights

### Severity Score Formula

```
severity_score = base_severity × service_criticality × confidence × portfolio_weight

base_severity:        low=2, medium=5, high=8, critical=10
service_criticality:  compute=1.0, database=1.2, networking=1.5, storage=0.8
confidence:           0.0 - 1.0 (from graph edge)
portfolio_weight:     company weight in portfolio (0.0 - 1.0)
```

### Portfolio Exposure Calculation

```
portfolio_exposure = Σ (position_weight × severity_score) for all affected companies

Thresholds:
  < 0.05   → INFO    (monitor)
  0.05-0.15 → WARN   (review hedges)
  0.15-0.30 → HIGH   (reduce or hedge)
  > 0.30   → CRITICAL (immediate action required)
```

---

## Layer 4 — Alert Layer

### Alert Priority Logic

Alerts are suppressed or escalated based on:
- Portfolio exposure threshold exceeded
- Confidence score of dependency
- Time since event detected (escalate if unresolved > 2h)
- Market hours (different urgency pre-market vs intraday)

### Recommendation Engine (Phase 3)

Initial recommendations are rule-based:

```
IF exposure > 0.15 AND sector_concentration > 0.30:
    recommend: "Consider SQQQ or sector put options"

IF single_company_weight > 0.05 AND confidence > 0.80:
    recommend: "Reduce {ticker} position or buy protective put"

IF cloud_provider = AWS AND portfolio_aws_exposure > 0.40:
    recommend: "Portfolio over-concentrated in AWS infrastructure risk"
```

---

## Data Pipeline — Phase 1 Sequence

```
1. Seed the graph
   edgar_scraper.py → pulls 10-K filings for S&P 500
   extract cloud mentions → build Company → CloudProvider edges
   store as seed_companies.json

2. Start event polling
   cloud_status_poller.py → polls AWS/Azure/GCP every 60s
   normalizes → emits Event objects to event queue

3. On event received
   propagation.py → queries exposure graph
   portfolio_scorer.py → loads positions, computes exposure
   alert if threshold exceeded

4. Alert dispatch
   dispatcher.py → formats alert
   sends to configured channel (Slack / webhook / stdout)
```

---

## Technology Decisions

| Component | Phase 1 | Phase 2 (Production) |
|---|---|---|
| Graph store | NetworkX (in-memory) | Neo4j |
| Event queue | Python queue / deque | Redis Streams or Kafka |
| Scheduler | APScheduler | Celery + Redis |
| Storage | JSON files | PostgreSQL |
| API | None | FastAPI |
| Dashboard | None | React + D3 |

---

## What ORCA Is Not

- **Not a prediction system** — ORCA detects events that have already begun, not forecasts.
- **Not a trading signal** — ORCA produces risk alerts, not buy/sell signals. Recommendations are defensive hedging, not alpha generation.
- **Not a replacement for financial analysis** — ORCA adds an operational layer *on top of* existing portfolio tools, not instead of them.
