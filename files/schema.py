"""
ORCA — Core Schema Definitions
All data structures used across the system.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
from enum import Enum


# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

class Severity(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

class EventType(str, Enum):
    CLOUD_OUTAGE        = "cloud_outage"
    CYBER_INCIDENT      = "cyber_incident"
    SUPPLY_DISRUPTION   = "supply_disruption"
    REGULATORY_SIGNAL   = "regulatory_signal"
    WEATHER_EVENT       = "weather_event"

class CloudProvider(str, Enum):
    AWS     = "AWS"
    AZURE   = "Azure"
    GCP     = "GCP"
    ORACLE  = "Oracle"
    IBM     = "IBM"

class AlertLevel(str, Enum):
    INFO     = "info"      # < 5% portfolio exposure
    WARN     = "warn"      # 5-15%
    HIGH     = "high"      # 15-30%
    CRITICAL = "critical"  # > 30%

class DependencySource(str, Enum):
    EDGAR_FILING    = "edgar_filing"
    JOB_POSTING     = "job_posting"
    IP_FINGERPRINT  = "ip_fingerprint"
    DNS_ANALYSIS    = "dns_analysis"
    MANUAL          = "manual"
    INDUSTRY_INFER  = "industry_inference"


# ─────────────────────────────────────────────
# EVENT SCHEMA
# ─────────────────────────────────────────────

@dataclass
class Event:
    """
    Normalized event object. All ingestion sources produce this.
    Downstream layers never see raw source formats.
    """
    event_id:             str
    source:               str                    # which poller detected this
    event_type:           EventType
    provider:             Optional[str]          # cloud provider if applicable
    region:               Optional[str]          # e.g. us-east-1
    services_affected:    list[str]              # e.g. ["EC2", "RDS"]
    severity:             Severity
    detected_at:          datetime               # when WE detected it
    acknowledged_at:      Optional[datetime]     # when vendor acknowledged
    resolved_at:          Optional[datetime]     # when vendor marked resolved
    corroborating_sources: list[str] = field(default_factory=list)
    description:          str = ""
    raw:                  dict = field(default_factory=dict)

    @property
    def lead_time_minutes(self) -> Optional[float]:
        """Minutes between our detection and vendor acknowledgment."""
        if self.acknowledged_at:
            delta = self.acknowledged_at - self.detected_at
            return delta.total_seconds() / 60
        return None

    @property
    def is_active(self) -> bool:
        return self.resolved_at is None


# ─────────────────────────────────────────────
# GRAPH NODE SCHEMAS
# ─────────────────────────────────────────────

@dataclass
class CompanyNode:
    ticker:             str
    name:               str
    sector:             str                      # GICS sector
    market_cap:         Optional[float] = None   # USD
    cloud_primary:      Optional[str] = None
    cloud_secondary:    list[str] = field(default_factory=list)
    dependency_sources: list[DependencySource] = field(default_factory=list)
    confidence:         float = 0.5              # 0.0 - 1.0

@dataclass
class RegionNode:
    provider:   str
    region_id:  str          # e.g. us-east-1
    geography:  str          # US_EAST | EU_WEST | APAC | etc.
    tier:       int = 1      # 1=primary, 2=secondary

@dataclass
class ServiceNode:
    provider:     str
    service_name: str        # EC2 | S3 | RDS | Lambda | etc.
    criticality:  str        # compute | storage | database | networking


# ─────────────────────────────────────────────
# PORTFOLIO SCHEMA
# ─────────────────────────────────────────────

@dataclass
class Position:
    ticker:     str
    name:       str
    weight:     float        # 0.0 - 1.0, must sum to 1.0 across portfolio
    value_usd:  Optional[float] = None
    sector:     Optional[str] = None

@dataclass
class Portfolio:
    portfolio_id:   str
    name:           str
    positions:      list[Position]
    as_of:          datetime = field(default_factory=datetime.utcnow)

    @property
    def tickers(self) -> list[str]:
        return [p.ticker for p in self.positions]

    def get_weight(self, ticker: str) -> float:
        for p in self.positions:
            if p.ticker == ticker:
                return p.weight
        return 0.0


# ─────────────────────────────────────────────
# ALERT SCHEMA
# ─────────────────────────────────────────────

@dataclass
class AffectedCompany:
    ticker:               str
    name:                 str
    dependency_confidence: float
    exposure_type:        str       # primary | secondary | inferred
    services_at_risk:     list[str] = field(default_factory=list)
    portfolio_weight:     float = 0.0

@dataclass
class Recommendation:
    action:     str         # hedge | reduce | monitor | rebalance
    instrument: Optional[str] = None
    rationale:  str = ""
    urgency:    str = "normal"   # normal | urgent | immediate

@dataclass
class PortfolioImpact:
    exposed_weight:      float           # fraction of portfolio affected
    affected_tickers:    list[str]
    severity_score:      float           # 0.0 - 10.0
    alert_level:         AlertLevel
    recommendations:     list[Recommendation] = field(default_factory=list)

@dataclass
class Alert:
    alert_id:            str
    triggered_at:        datetime
    event:               Event
    affected_companies:  list[AffectedCompany]
    portfolio_impact:    PortfolioImpact

    def summary(self) -> str:
        tickers = ", ".join(self.portfolio_impact.affected_tickers)
        return (
            f"[{self.portfolio_impact.alert_level.upper()}] "
            f"{self.event.event_type} — {self.event.provider} {self.event.region} | "
            f"{self.portfolio_impact.exposed_weight*100:.1f}% portfolio exposed | "
            f"Affected: {tickers}"
        )
