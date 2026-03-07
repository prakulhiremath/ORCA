"""
ORCA — Propagation Engine & Portfolio Scorer
When an event fires, this module:
  1. Finds all affected companies via the exposure graph
  2. Cross-references against the portfolio
  3. Computes an exposure score
  4. Generates an Alert with recommendations
"""

import uuid
import json
import logging
from datetime import datetime, timezone
from engine.schema import (
    Event, Alert, AlertLevel, AffectedCompany,
    PortfolioImpact, Recommendation, Portfolio, Position, Severity
)
from graph.exposure_graph import ExposureGraph

logger = logging.getLogger("orca.engine")


# ─────────────────────────────────────────────
# SEVERITY WEIGHTS
# ─────────────────────────────────────────────

SEVERITY_WEIGHT = {
    Severity.LOW:      2.0,
    Severity.MEDIUM:   5.0,
    Severity.HIGH:     8.0,
    Severity.CRITICAL: 10.0,
}

SERVICE_CRITICALITY = {
    # Compute
    "EC2": 1.0, "Compute Engine": 1.0, "VMs": 1.0,
    # Database
    "RDS": 1.2, "Azure SQL": 1.2, "Cloud SQL": 1.2, "Cosmos DB": 1.2, "DynamoDB": 1.2,
    # Networking
    "CloudFront": 1.5, "Azure AD": 1.5,
    # Storage
    "S3": 0.8, "Azure Storage": 0.8, "Cloud Storage": 0.8,
    # Containers / Orchestration
    "EKS": 1.0, "AKS": 1.0, "GKE": 1.0,
    # Serverless
    "Lambda": 0.9, "Azure Functions": 0.9,
    # Default
    "default": 1.0,
}

ALERT_THRESHOLDS = {
    AlertLevel.INFO:     0.00,
    AlertLevel.WARN:     0.05,
    AlertLevel.HIGH:     0.15,
    AlertLevel.CRITICAL: 0.30,
}


# ─────────────────────────────────────────────
# PROPAGATION ENGINE
# ─────────────────────────────────────────────

class PropagationEngine:
    """
    Propagates an event through the exposure graph.
    Returns a list of affected companies with confidence scores.
    """

    def __init__(self, graph: ExposureGraph):
        self.graph = graph

    def propagate(self, event: Event) -> list[AffectedCompany]:
        """
        Given an event, find all companies affected.
        Searches by region first (most specific), then by provider.
        """
        affected: dict[str, AffectedCompany] = {}

        # 1. Region-level match (highest confidence)
        if event.region and event.region != "global":
            region_companies = self.graph.companies_in_region(
                event.provider, event.region, min_confidence=0.5
            )
            for c in region_companies:
                affected[c["ticker"]] = AffectedCompany(
                    ticker=c["ticker"],
                    name=c["name"] or c["ticker"],
                    dependency_confidence=c["confidence"],
                    exposure_type="primary",
                    services_at_risk=event.services_affected,
                )

        # 2. Provider-level match (lower confidence — may not be in this region)
        if event.provider:
            provider_companies = self.graph.companies_on_provider(
                event.provider, min_confidence=0.6
            )
            for c in provider_companies:
                if c["ticker"] not in affected:
                    # Reduce confidence: we know they use this provider
                    # but don't know if they're in the affected region
                    affected[c["ticker"]] = AffectedCompany(
                        ticker=c["ticker"],
                        name=c["name"] or c["ticker"],
                        dependency_confidence=c["confidence"] * 0.6,
                        exposure_type="secondary",
                        services_at_risk=event.services_affected,
                    )

        logger.info(f"Propagation: {len(affected)} companies affected by {event.provider} {event.region}")
        return list(affected.values())


# ─────────────────────────────────────────────
# PORTFOLIO SCORER
# ─────────────────────────────────────────────

class PortfolioScorer:
    """
    Takes a list of affected companies and a portfolio.
    Computes the portfolio's exposure and generates an alert.
    """

    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio

    def score(self, event: Event, affected: list[AffectedCompany]) -> Alert:
        """
        Cross-reference affected companies against portfolio positions.
        Compute exposure weight and severity score.
        """
        portfolio_tickers = set(self.portfolio.tickers)

        # Filter to only companies in our portfolio
        exposed: list[AffectedCompany] = []
        for company in affected:
            if company.ticker in portfolio_tickers:
                company.portfolio_weight = self.portfolio.get_weight(company.ticker)
                exposed.append(company)

        # Compute severity score
        severity_score = self._compute_severity_score(event, exposed)

        # Compute total exposed weight
        exposed_weight = sum(c.portfolio_weight for c in exposed)

        # Determine alert level
        alert_level = self._classify_alert_level(exposed_weight)

        # Generate recommendations
        recommendations = self._recommend(event, exposed, exposed_weight, alert_level)

        impact = PortfolioImpact(
            exposed_weight=exposed_weight,
            affected_tickers=[c.ticker for c in exposed],
            severity_score=round(severity_score, 2),
            alert_level=alert_level,
            recommendations=recommendations,
        )

        alert = Alert(
            alert_id=str(uuid.uuid4()),
            triggered_at=datetime.now(timezone.utc),
            event=event,
            affected_companies=exposed,
            portfolio_impact=impact,
        )

        return alert

    def _compute_severity_score(self, event: Event, exposed: list[AffectedCompany]) -> float:
        """
        severity_score = Σ (base_severity × service_criticality × confidence × portfolio_weight)
        Normalized to 0-10 scale.
        """
        if not exposed:
            return 0.0

        base = SEVERITY_WEIGHT.get(event.severity, 5.0)

        # Average service criticality across affected services
        criticality_scores = [
            SERVICE_CRITICALITY.get(s, SERVICE_CRITICALITY["default"])
            for s in event.services_affected
        ]
        avg_criticality = sum(criticality_scores) / len(criticality_scores) if criticality_scores else 1.0

        total_score = 0.0
        for company in exposed:
            total_score += base * avg_criticality * company.dependency_confidence * company.portfolio_weight

        return min(total_score * 10, 10.0)  # normalize to 0-10

    def _classify_alert_level(self, exposed_weight: float) -> AlertLevel:
        if exposed_weight >= ALERT_THRESHOLDS[AlertLevel.CRITICAL]:
            return AlertLevel.CRITICAL
        elif exposed_weight >= ALERT_THRESHOLDS[AlertLevel.HIGH]:
            return AlertLevel.HIGH
        elif exposed_weight >= ALERT_THRESHOLDS[AlertLevel.WARN]:
            return AlertLevel.WARN
        else:
            return AlertLevel.INFO

    def _recommend(
        self,
        event: Event,
        exposed: list[AffectedCompany],
        exposed_weight: float,
        level: AlertLevel
    ) -> list[Recommendation]:
        recs = []

        if level == AlertLevel.CRITICAL:
            recs.append(Recommendation(
                action="hedge",
                instrument="SQQQ or sector put options",
                rationale=f"{exposed_weight*100:.1f}% of portfolio exposed to {event.provider} {event.region}",
                urgency="immediate"
            ))

        if level in [AlertLevel.HIGH, AlertLevel.CRITICAL]:
            for c in exposed:
                if c.portfolio_weight > 0.05:
                    recs.append(Recommendation(
                        action="reduce",
                        instrument=c.ticker,
                        rationale=f"{c.ticker} ({c.portfolio_weight*100:.1f}% weight) has {c.exposure_type} dependency on affected region",
                        urgency="urgent"
                    ))

        if level == AlertLevel.WARN:
            recs.append(Recommendation(
                action="monitor",
                rationale=f"Low-level exposure ({exposed_weight*100:.1f}%). Watch for escalation.",
                urgency="normal"
            ))

        if level == AlertLevel.INFO:
            recs.append(Recommendation(
                action="monitor",
                rationale="Marginal exposure. No immediate action required.",
                urgency="normal"
            ))

        return recs


# ─────────────────────────────────────────────
# ORCA RUNTIME
# ─────────────────────────────────────────────

class ORCARuntime:
    """
    Connects all layers:
      ingestion → propagation → scoring → alert
    """

    def __init__(self, graph: ExposureGraph, portfolio: Portfolio):
        self.graph     = graph
        self.portfolio = portfolio
        self.engine    = PropagationEngine(graph)
        self.scorer    = PortfolioScorer(portfolio)

    def process_event(self, event: Event) -> Alert:
        logger.info(f"Processing event: {event.provider} {event.region} [{event.severity}]")
        affected = self.engine.propagate(event)
        alert    = self.scorer.score(event, affected)
        logger.info(f"Alert generated: {alert.summary()}")
        return alert

    def process_events(self, events: list[Event]) -> list[Alert]:
        return [self.process_event(e) for e in events]


def load_portfolio_from_json(filepath: str) -> Portfolio:
    with open(filepath) as f:
        data = json.load(f)
    positions = [
        Position(
            ticker=p["ticker"],
            name=p.get("name", p["ticker"]),
            weight=p["weight"],
            value_usd=p.get("value_usd"),
            sector=p.get("sector"),
        )
        for p in data["positions"]
    ]
    return Portfolio(
        portfolio_id=data.get("portfolio_id", "default"),
        name=data.get("name", "Portfolio"),
        positions=positions,
    )


if __name__ == "__main__":
    import uuid
    from datetime import datetime, timezone
    from engine.schema import Event, EventType, Severity

    logging.basicConfig(level=logging.INFO)

    # Load graph
    graph = ExposureGraph()
    try:
        graph.load_from_json("data/seed_companies.json")
    except FileNotFoundError:
        print("No seed data found. Add data/seed_companies.json first.")
        exit(1)

    # Load portfolio
    portfolio = load_portfolio_from_json("data/sample_portfolio.json")

    # Simulate an event
    test_event = Event(
        event_id=str(uuid.uuid4()),
        source="aws_status",
        event_type=EventType.CLOUD_OUTAGE,
        provider="AWS",
        region="us-east-1",
        services_affected=["EC2", "RDS"],
        severity=Severity.HIGH,
        detected_at=datetime.now(timezone.utc),
        acknowledged_at=None,
        resolved_at=None,
        description="EC2 and RDS degraded performance in us-east-1",
    )

    runtime = ORCARuntime(graph, portfolio)
    alert = runtime.process_event(test_event)

    print("\n" + "="*60)
    print("ORCA ALERT")
    print("="*60)
    print(alert.summary())
    print(f"\nSeverity score: {alert.portfolio_impact.severity_score}/10")
    print(f"Portfolio exposed: {alert.portfolio_impact.exposed_weight*100:.1f}%")
    print(f"\nAffected positions:")
    for c in alert.affected_companies:
        print(f"  {c.ticker} — weight: {c.portfolio_weight*100:.1f}% — confidence: {c.dependency_confidence:.2f}")
    print(f"\nRecommendations:")
    for r in alert.portfolio_impact.recommendations:
        print(f"  [{r.urgency.upper()}] {r.action}: {r.rationale}")
