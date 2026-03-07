"""
ORCA — Exposure Graph
Builds and queries the dependency graph:
  Company → CloudProvider → Region → Service

Phase 1: NetworkX (in-memory, no infrastructure needed)
Phase 2: Migrate to Neo4j for production scale
"""

import json
import logging
import networkx as nx
from typing import Optional
from dataclasses import asdict
from engine.schema import CompanyNode, RegionNode, ServiceNode

logger = logging.getLogger("orca.graph")


# Node type constants
NODE_COMPANY  = "company"
NODE_PROVIDER = "cloud_provider"
NODE_REGION   = "region"
NODE_SERVICE  = "service"

# Edge type constants
EDGE_DEPENDS_ON = "DEPENDS_ON"     # Company → CloudProvider
EDGE_HOSTED_IN  = "HOSTED_IN"      # Company → Region
EDGE_USES       = "USES"           # Company → Service
EDGE_CONTAINS   = "CONTAINS"       # CloudProvider → Region
EDGE_OFFERS     = "OFFERS"         # Region → Service


class ExposureGraph:
    """
    Core graph for ORCA. Answers questions like:
      - "Which companies are hosted in AWS us-east-1?"
      - "What services does SNOW depend on in us-east-1?"
      - "If EC2 in us-east-1 goes down, which portfolio tickers are affected?"
    """

    def __init__(self):
        self.G = nx.DiGraph()
        self._build_provider_infrastructure()

    # ─────────────────────────────────────────────
    # GRAPH CONSTRUCTION
    # ─────────────────────────────────────────────

    def _build_provider_infrastructure(self):
        """
        Pre-populate the graph with known cloud provider → region → service structure.
        This is static reference data and doesn't change often.
        """
        infrastructure = {
            "AWS": {
                "regions": [
                    ("us-east-1",    "US_EAST",   1),
                    ("us-east-2",    "US_EAST",   1),
                    ("us-west-1",    "US_WEST",   1),
                    ("us-west-2",    "US_WEST",   1),
                    ("eu-west-1",    "EU_WEST",   1),
                    ("eu-central-1", "EU_CENTRAL",1),
                    ("ap-southeast-1","APAC",     1),
                    ("ap-northeast-1","APAC",     1),
                ],
                "services": ["EC2", "S3", "RDS", "Lambda", "EKS", "CloudFront", "SQS", "DynamoDB"]
            },
            "Azure": {
                "regions": [
                    ("eastus",        "US_EAST",    1),
                    ("westus",        "US_WEST",    1),
                    ("northeurope",   "EU_NORTH",   1),
                    ("westeurope",    "EU_WEST",    1),
                    ("southeastasia", "APAC",       1),
                ],
                "services": ["VMs", "Azure SQL", "Azure Storage", "AKS", "Azure Functions", "Cosmos DB", "Azure AD"]
            },
            "GCP": {
                "regions": [
                    ("us-central1",    "US_CENTRAL", 1),
                    ("us-east1",       "US_EAST",    1),
                    ("europe-west1",   "EU_WEST",    1),
                    ("asia-southeast1","APAC",       1),
                ],
                "services": ["Compute Engine", "Cloud Storage", "Cloud SQL", "GKE", "BigQuery", "Pub/Sub"]
            }
        }

        for provider, data in infrastructure.items():
            # Add provider node
            self.G.add_node(provider, node_type=NODE_PROVIDER, name=provider)

            # Add region nodes and edges
            for region_id, geography, tier in data["regions"]:
                node_id = f"{provider}:{region_id}"
                self.G.add_node(node_id, node_type=NODE_REGION,
                                provider=provider, region_id=region_id,
                                geography=geography, tier=tier)
                self.G.add_edge(provider, node_id, edge_type=EDGE_CONTAINS)

                # Add service nodes in each region
                for service in data["services"]:
                    svc_id = f"{provider}:{region_id}:{service}"
                    self.G.add_node(svc_id, node_type=NODE_SERVICE,
                                    provider=provider, service_name=service,
                                    region=region_id)
                    self.G.add_edge(node_id, svc_id, edge_type=EDGE_OFFERS)

        logger.info(f"Provider infrastructure loaded: {self.G.number_of_nodes()} nodes")

    def add_company(self, company: CompanyNode):
        """Add a company node and its cloud dependency edges."""
        ticker = company.ticker
        self.G.add_node(
            ticker,
            node_type=NODE_COMPANY,
            name=company.name,
            sector=company.sector,
            market_cap=company.market_cap,
            confidence=company.confidence,
        )

        # Primary cloud provider edge
        if company.cloud_primary:
            self._add_company_provider_edge(ticker, company.cloud_primary, company.confidence)

        # Secondary providers
        for provider in company.cloud_secondary:
            self._add_company_provider_edge(ticker, provider, company.confidence * 0.7)

        logger.debug(f"Added company: {ticker} → {company.cloud_primary}")

    def _add_company_provider_edge(self, ticker: str, provider: str, confidence: float):
        """Add DEPENDS_ON edge from company to provider."""
        if provider in self.G:
            self.G.add_edge(ticker, provider,
                            edge_type=EDGE_DEPENDS_ON,
                            confidence=confidence)

    def add_company_region(self, ticker: str, provider: str, region_id: str, confidence: float = 0.8):
        """Add HOSTED_IN edge: company knows to be in a specific region."""
        node_id = f"{provider}:{region_id}"
        if node_id in self.G:
            self.G.add_edge(ticker, node_id,
                            edge_type=EDGE_HOSTED_IN,
                            confidence=confidence)

    def load_from_json(self, filepath: str):
        """Load seed company data from JSON file."""
        with open(filepath) as f:
            companies = json.load(f)

        for c in companies:
            node = CompanyNode(
                ticker=c["ticker"],
                name=c["name"],
                sector=c["sector"],
                market_cap=c.get("market_cap"),
                cloud_primary=c.get("cloud_primary"),
                cloud_secondary=c.get("cloud_secondary", []),
                confidence=c.get("confidence", 0.75),
            )
            self.add_company(node)

            # Add region-level edges if available
            for region_dep in c.get("regions", []):
                self.add_company_region(
                    c["ticker"],
                    region_dep["provider"],
                    region_dep["region_id"],
                    region_dep.get("confidence", 0.7)
                )

        logger.info(f"Loaded {len(companies)} companies from {filepath}")

    # ─────────────────────────────────────────────
    # GRAPH QUERIES
    # ─────────────────────────────────────────────

    def companies_in_region(self, provider: str, region_id: str,
                            min_confidence: float = 0.5) -> list[dict]:
        """
        Find all companies hosted in a specific cloud region.
        This is the primary query used by the propagation engine.
        """
        region_node = f"{provider}:{region_id}"
        if region_node not in self.G:
            logger.warning(f"Region not found: {region_node}")
            return []

        companies = []
        # Traverse backward: find all nodes with HOSTED_IN edge to this region
        for node, attrs in self.G.nodes(data=True):
            if attrs.get("node_type") != NODE_COMPANY:
                continue
            if self.G.has_edge(node, region_node):
                edge_data = self.G.edges[node, region_node]
                confidence = edge_data.get("confidence", 0.5)
                if confidence >= min_confidence:
                    companies.append({
                        "ticker": node,
                        "name": attrs.get("name"),
                        "sector": attrs.get("sector"),
                        "confidence": confidence,
                    })

        return companies

    def companies_on_provider(self, provider: str,
                               min_confidence: float = 0.5) -> list[dict]:
        """Find all companies that depend on a cloud provider."""
        if provider not in self.G:
            return []

        companies = []
        for node, attrs in self.G.nodes(data=True):
            if attrs.get("node_type") != NODE_COMPANY:
                continue
            if self.G.has_edge(node, provider):
                confidence = self.G.edges[node, provider].get("confidence", 0.5)
                if confidence >= min_confidence:
                    companies.append({
                        "ticker": node,
                        "name": attrs.get("name"),
                        "sector": attrs.get("sector"),
                        "confidence": confidence,
                    })

        return companies

    def get_company_cloud_profile(self, ticker: str) -> dict:
        """Return full cloud dependency profile for a company."""
        if ticker not in self.G:
            return {}

        profile = {
            "ticker": ticker,
            "providers": [],
            "regions": [],
        }

        for neighbor in self.G.successors(ticker):
            node_type = self.G.nodes[neighbor].get("node_type")
            edge = self.G.edges[ticker, neighbor]

            if node_type == NODE_PROVIDER:
                profile["providers"].append({
                    "provider": neighbor,
                    "confidence": edge.get("confidence", 0.5)
                })
            elif node_type == NODE_REGION:
                attrs = self.G.nodes[neighbor]
                profile["regions"].append({
                    "provider": attrs.get("provider"),
                    "region_id": attrs.get("region_id"),
                    "geography": attrs.get("geography"),
                    "confidence": edge.get("confidence", 0.5)
                })

        return profile

    def stats(self) -> dict:
        """Return graph statistics."""
        companies = [n for n, a in self.G.nodes(data=True) if a.get("node_type") == NODE_COMPANY]
        regions   = [n for n, a in self.G.nodes(data=True) if a.get("node_type") == NODE_REGION]

        return {
            "total_nodes":    self.G.number_of_nodes(),
            "total_edges":    self.G.number_of_edges(),
            "companies":      len(companies),
            "regions":        len(regions),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    g = ExposureGraph()

    # Load seed data
    try:
        g.load_from_json("data/seed_companies.json")
    except FileNotFoundError:
        print("No seed data yet. Run edgar_scraper.py first.")

    print("\nGraph stats:", g.stats())
    print("\nCompanies in AWS us-east-1:")
    for c in g.companies_in_region("AWS", "us-east-1"):
        print(f"  {c['ticker']} — confidence: {c['confidence']:.2f}")
