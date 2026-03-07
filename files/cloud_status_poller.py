"""
ORCA — Cloud Status Poller
Polls AWS, Azure, and GCP status feeds.
Normalizes all events into the standard Event schema.

Data sources (all free, no auth required):
  AWS:   https://status.aws.amazon.com/data.json
  Azure: https://azure.status.microsoft/en-us/status/feed/  (RSS)
  GCP:   https://status.cloud.google.com/incidents.json
"""

import uuid
import logging
import requests
import feedparser
from datetime import datetime, timezone
from typing import Optional
from engine.schema import Event, EventType, Severity

logger = logging.getLogger("orca.ingestion.cloud")


# ─────────────────────────────────────────────
# SEVERITY MAPPING
# ─────────────────────────────────────────────

AWS_SEVERITY_MAP = {
    0: Severity.LOW,
    1: Severity.MEDIUM,
    2: Severity.HIGH,
    3: Severity.CRITICAL,
}

GCP_SEVERITY_MAP = {
    "LOW":      Severity.LOW,
    "MEDIUM":   Severity.MEDIUM,
    "HIGH":     Severity.HIGH,
}


# ─────────────────────────────────────────────
# AWS POLLER
# ─────────────────────────────────────────────

class AWSPoller:
    """
    Polls the AWS Service Health Dashboard.
    
    AWS exposes a public JSON endpoint with current and recent events.
    Each event contains: service, region, start/end times, severity, description.
    
    No API key required.
    """

    STATUS_URL = "https://status.aws.amazon.com/data.json"

    def poll(self) -> list[Event]:
        try:
            resp = requests.get(self.STATUS_URL, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"AWS poll failed: {e}")
            return []

        events = []
        for item in data.get("current", []):
            event = self._parse_item(item)
            if event:
                events.append(event)

        logger.info(f"AWS: {len(events)} active event(s)")
        return events

    def _parse_item(self, item: dict) -> Optional[Event]:
        try:
            # AWS service names encode region: e.g. "ec2-us-east-1"
            service_key = item.get("service", "")
            parts = service_key.rsplit("-", 1)
            service = parts[0].upper() if len(parts) > 1 else service_key.upper()
            region = parts[1] if len(parts) > 1 else "global"

            severity_int = item.get("status", 1)
            severity = AWS_SEVERITY_MAP.get(severity_int, Severity.MEDIUM)

            detected_at = datetime.fromtimestamp(
                item.get("date", datetime.utcnow().timestamp()), tz=timezone.utc
            )

            return Event(
                event_id=str(uuid.uuid4()),
                source="aws_status",
                event_type=EventType.CLOUD_OUTAGE,
                provider="AWS",
                region=region,
                services_affected=[service],
                severity=severity,
                detected_at=detected_at,
                acknowledged_at=None,
                resolved_at=None,
                description=item.get("summary", ""),
                raw=item,
            )
        except Exception as e:
            logger.warning(f"Failed to parse AWS item: {e}")
            return None


# ─────────────────────────────────────────────
# AZURE POLLER
# ─────────────────────────────────────────────

class AzurePoller:
    """
    Polls the Azure Status RSS feed.
    
    Azure exposes a public RSS feed with active incidents.
    No API key required.
    """

    RSS_URL = "https://azure.status.microsoft/en-us/status/feed/"

    def poll(self) -> list[Event]:
        try:
            feed = feedparser.parse(self.RSS_URL)
        except Exception as e:
            logger.error(f"Azure poll failed: {e}")
            return []

        events = []
        for entry in feed.entries:
            event = self._parse_entry(entry)
            if event:
                events.append(event)

        logger.info(f"Azure: {len(events)} active event(s)")
        return events

    def _parse_entry(self, entry) -> Optional[Event]:
        try:
            title = entry.get("title", "")
            region = self._extract_region(title)
            services = self._extract_services(title)
            severity = self._infer_severity(title)

            detected_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc) \
                if hasattr(entry, "published_parsed") else datetime.utcnow().replace(tzinfo=timezone.utc)

            return Event(
                event_id=str(uuid.uuid4()),
                source="azure_status",
                event_type=EventType.CLOUD_OUTAGE,
                provider="Azure",
                region=region,
                services_affected=services,
                severity=severity,
                detected_at=detected_at,
                acknowledged_at=detected_at,  # Azure RSS = already acknowledged
                resolved_at=None,
                description=entry.get("summary", ""),
                raw=dict(entry),
            )
        except Exception as e:
            logger.warning(f"Failed to parse Azure entry: {e}")
            return None

    def _extract_region(self, title: str) -> str:
        regions = [
            "East US", "West US", "North Europe", "West Europe",
            "Southeast Asia", "East Asia", "UK South", "Australia East"
        ]
        for r in regions:
            if r.lower() in title.lower():
                return r.lower().replace(" ", "-")
        return "global"

    def _extract_services(self, title: str) -> list[str]:
        known = [
            "Azure Kubernetes", "Azure SQL", "Azure Storage",
            "Azure Functions", "Azure Active Directory", "Azure DevOps",
            "App Service", "Virtual Machines", "Cosmos DB"
        ]
        found = [s for s in known if s.lower() in title.lower()]
        return found if found else ["Unknown"]

    def _infer_severity(self, title: str) -> Severity:
        title_lower = title.lower()
        if any(w in title_lower for w in ["outage", "down", "unavailable"]):
            return Severity.HIGH
        if any(w in title_lower for w in ["degraded", "intermittent", "slow"]):
            return Severity.MEDIUM
        return Severity.LOW


# ─────────────────────────────────────────────
# GCP POLLER
# ─────────────────────────────────────────────

class GCPPoller:
    """
    Polls the Google Cloud Status JSON feed.
    
    GCP exposes a public incidents JSON endpoint.
    No API key required.
    """

    STATUS_URL = "https://status.cloud.google.com/incidents.json"

    def poll(self) -> list[Event]:
        try:
            resp = requests.get(self.STATUS_URL, timeout=10)
            resp.raise_for_status()
            incidents = resp.json()
        except Exception as e:
            logger.error(f"GCP poll failed: {e}")
            return []

        # Only return active (unresolved) incidents
        active = [i for i in incidents if not i.get("end")]
        events = []
        for incident in active:
            event = self._parse_incident(incident)
            if event:
                events.append(event)

        logger.info(f"GCP: {len(events)} active event(s)")
        return events

    def _parse_incident(self, incident: dict) -> Optional[Event]:
        try:
            services = [s.get("title", "Unknown") for s in incident.get("affected_products", [])]
            regions = [l.get("id", "global") for l in incident.get("affected_locations", [])]
            region = regions[0] if regions else "global"

            severity_str = incident.get("severity", "MEDIUM").upper()
            severity = GCP_SEVERITY_MAP.get(severity_str, Severity.MEDIUM)

            detected_at = datetime.fromisoformat(
                incident["begin"].replace("Z", "+00:00")
            ) if "begin" in incident else datetime.utcnow().replace(tzinfo=timezone.utc)

            return Event(
                event_id=str(uuid.uuid4()),
                source="gcp_status",
                event_type=EventType.CLOUD_OUTAGE,
                provider="GCP",
                region=region,
                services_affected=services,
                severity=severity,
                detected_at=detected_at,
                acknowledged_at=detected_at,
                resolved_at=None,
                description=incident.get("external_desc", ""),
                raw=incident,
            )
        except Exception as e:
            logger.warning(f"Failed to parse GCP incident: {e}")
            return None


# ─────────────────────────────────────────────
# UNIFIED POLLER
# ─────────────────────────────────────────────

class CloudStatusPoller:
    """
    Aggregates all cloud provider pollers.
    Deduplicates events across sources.
    """

    def __init__(self):
        self.pollers = {
            "aws":   AWSPoller(),
            "azure": AzurePoller(),
            "gcp":   GCPPoller(),
        }
        self._seen: set[str] = set()

    def poll_all(self) -> list[Event]:
        all_events = []
        for name, poller in self.pollers.items():
            try:
                events = poller.poll()
                all_events.extend(events)
            except Exception as e:
                logger.error(f"{name} poller crashed: {e}")

        deduped = self._deduplicate(all_events)
        logger.info(f"Total active cloud events: {len(deduped)}")
        return deduped

    def _deduplicate(self, events: list[Event]) -> list[Event]:
        """
        Deduplicate by (provider, region, event_type).
        In production this would also check time windows.
        """
        seen_keys = set()
        unique = []
        for event in events:
            key = f"{event.provider}:{event.region}:{event.event_type}"
            if key not in seen_keys:
                seen_keys.add(key)
                unique.append(event)
        return unique


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    poller = CloudStatusPoller()
    events = poller.poll_all()

    if not events:
        print("No active cloud events detected.")
    else:
        for e in events:
            print(f"[{e.severity.upper()}] {e.provider} {e.region} — {', '.join(e.services_affected)}")
            print(f"  Detected: {e.detected_at.isoformat()}")
            print(f"  {e.description[:120]}")
            print()
