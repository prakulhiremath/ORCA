# ORCA
### Operational Risk & Cascade Analyzer

> Detect infrastructure events before they become portfolio losses.

---

## What ORCA Does

Most portfolio risk tools are backward-looking — they analyze historical volatility, factor exposure, and earnings revisions.

**ORCA is forward-looking and operational.**

It monitors real-world infrastructure signals — cloud outages, supply chain disruptions, cyber incidents — maps them to company dependencies, and computes live portfolio exposure *before* the market reacts.

---

## The Core Problem

| What PMs see today | What ORCA sees |
|---|---|
| Price movement | Cloud outage feeds |
| Analyst reports | BGP routing anomalies |
| Delayed news | Supply chain disruptions |
| Quarterly filings | Cyber incident signals |
| Volatility estimates | Satellite / AIS shipping data |

By the time a risk appears in price action, it's too late to act. ORCA connects events to exposure in real time.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        ORCA SYSTEM                          │
├─────────────────┬───────────────────┬───────────────────────┤
│  INGESTION      │  GRAPH            │  ENGINE               │
│                 │                   │                       │
│  Cloud feeds    │  Company          │  Event propagation    │
│  EDGAR scraper  │    → Provider     │  Exposure scoring     │
│  News signals   │    → Region       │  Portfolio impact     │
│  Cyber feeds    │    → Service      │  Alert generation     │
└─────────────────┴───────────────────┴───────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                      ALERT LAYER                            │
│   Severity score · Affected tickers · Portfolio % at risk   │
└─────────────────────────────────────────────────────────────┘
```

---

## Modules

### `/ingestion` — Signal Collection
Polls and normalizes raw event signals from multiple sources.
- `cloud_status_poller.py` — AWS / Azure / GCP status feeds
- `edgar_scraper.py` — Extracts cloud/vendor dependencies from SEC 10-K filings
- `cyber_feed.py` — CVE disclosures, ransomware leak site monitoring
- `normalizer.py` — Converts all sources to unified `Event` schema

### `/graph` — Exposure Graph
Builds and queries the dependency graph: Company → Infrastructure → Geography.
- `exposure_graph.py` — Core graph construction and querying
- `graph_builder.py` — Ingests company data and builds edges
- `schema.py` — Node and edge type definitions

### `/engine` — Risk Propagation
When an event fires, propagates it through the graph to compute portfolio impact.
- `propagation.py` — Event → affected companies
- `portfolio_scorer.py` — Calculates exposure weight and severity score
- `recommender.py` — Generates hedge / reduce / monitor recommendations

### `/alerts` — Output Layer
Formats and dispatches alerts.
- `alert_schema.py` — Alert object definition
- `dispatcher.py` — Webhook, Slack, email output

### `/data` — Seed Data & Samples
- `sample_portfolio.json` — Example positions file
- `seed_companies.json` — Manually curated cloud dependency seed set
- `cloud_regions.json` — AWS / Azure / GCP region reference

---

## Quickstart

```bash
# Clone
git clone [https://github.com/prakulhiremath/ORCA.git]
cd orca

# Install
pip install -r requirements.txt

# Run the cloud status poller
python ingestion/cloud_status_poller.py

# Build the exposure graph from seed data
python graph/graph_builder.py --input data/seed_companies.json

# Score a portfolio against current events
python engine/portfolio_scorer.py --portfolio data/sample_portfolio.json
```

---

## Event Schema

Every ingested event, regardless of source, is normalized to:

```json
{
  "event_id": "uuid",
  "source": "aws_status",
  "event_type": "cloud_outage",
  "provider": "AWS",
  "region": "us-east-1",
  "services_affected": ["EC2", "RDS"],
  "severity": "high",
  "detected_at": "2025-03-07T14:32:00Z",
  "acknowledged_at": "2025-03-07T15:10:00Z",
  "resolved_at": null,
  "raw": {}
}
```

---

## Exposure Graph Schema

```
Node types:
  Company       { ticker, name, sector }
  CloudProvider { name: AWS | Azure | GCP }
  Region        { provider, region_id, geography }
  Service       { provider, service_name }

Edge types:
  DEPENDS_ON    Company → CloudProvider
  HOSTED_IN     Company → Region
  USES          Company → Service
  CONTAINS      CloudProvider → Region
```

---

## Alert Schema

```json
{
  "alert_id": "uuid",
  "triggered_at": "ISO8601",
  "event": { ...event object },
  "affected_companies": [
    { "ticker": "SNOW", "dependency_confidence": 0.95, "exposure_type": "primary" }
  ],
  "portfolio_impact": {
    "exposed_weight": 0.14,
    "affected_tickers": ["SNOW", "DDOG", "MDB"],
    "severity_score": 7.2
  },
  "recommendations": [
    { "action": "hedge", "instrument": "SQQQ", "rationale": "14% tech exposure concentrated in us-east-1" }
  ]
}
```

---

## Data Sources

| Source | Type | Cost | Coverage |
|---|---|---|---|
| AWS Status API | Cloud outages | Free | AWS global |
| Azure Status RSS | Cloud outages | Free | Azure global |
| GCP Status JSON | Cloud outages | Free | GCP global |
| SEC EDGAR Full-Text | Vendor dependencies | Free | All public companies |
| IsDown API | Multi-cloud outage detection | Freemium | 3,000+ services |
| Shodan / Censys | IP/ASN fingerprinting | Freemium | Internet-wide |
| CVE NVD Feed | Vulnerability disclosures | Free | CVE database |

---

## Roadmap

- [x] Repo foundation & architecture
- [ ] Phase 1 — Cloud infrastructure domain (AWS / Azure / GCP)
- [ ] Phase 2 — EDGAR scraper + exposure graph seed
- [ ] Phase 3 — Propagation engine + portfolio scorer
- [ ] Phase 4 — Alert dispatcher (Slack / webhook)
- [ ] Phase 5 — Cyber incident domain
- [ ] Phase 6 — Supply chain / AIS shipping domain
- [ ] Phase 7 — Dashboard UI

---

## Contributing

ORCA is being built in public. The goal is a serious, institutional-grade open-source risk intelligence system.

PRs, issues, and data contributions welcome.

---

## License

MIT
