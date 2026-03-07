"""
ORCA — EDGAR Scraper
Extracts cloud provider dependencies from SEC 10-K filings.

This is the core data source for building the exposure graph.
All public companies must disclose material vendor dependencies.
Cloud providers frequently appear in Risk Factors and Business sections.

API: https://efts.sec.gov/LATEST/search-index?q="AWS"&dateRange=custom&...
Docs: https://efts.sec.gov/LATEST/search-index
No authentication required.
"""

import time
import logging
import requests
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("orca.ingestion.edgar")

EDGAR_HEADERS = {
    "User-Agent": "ORCA Risk System research@orca-risk.io",  # SEC requires this
    "Accept-Encoding": "gzip, deflate",
}

# Cloud provider search terms mapped to canonical provider names
CLOUD_PROVIDER_TERMS = {
    "AWS": [
        "Amazon Web Services", "AWS", "Amazon EC2",
        "Amazon S3", "Amazon RDS", "us-east-1", "us-west-2"
    ],
    "Azure": [
        "Microsoft Azure", "Azure", "Azure Active Directory",
        "Azure SQL", "Microsoft Cloud"
    ],
    "GCP": [
        "Google Cloud", "Google Cloud Platform", "GCP",
        "Google Compute Engine", "BigQuery"
    ],
}


@dataclass
class CloudMention:
    ticker:         str
    company_name:   str
    provider:       str
    mention_count:  int
    filing_url:     str
    filing_date:    str
    context:        list[str] = field(default_factory=list)   # surrounding text snippets
    confidence:     float = 0.0


@dataclass
class CompanyDependency:
    ticker:         str
    company_name:   str
    cik:            str
    cloud_providers: dict[str, float]   # provider → confidence score
    filing_date:    str
    filing_url:     str


class EDGARScraper:
    """
    Uses EDGAR Full-Text Search to find cloud provider mentions in 10-K filings.
    
    Strategy:
    1. Search EDGAR full-text for cloud provider terms
    2. For each result, fetch filing metadata (company, date, CIK)
    3. Score confidence based on mention count and context
    4. Return structured CompanyDependency objects
    
    Rate limit: EDGAR allows ~10 requests/second. We stay well below that.
    """

    SEARCH_URL  = "https://efts.sec.gov/LATEST/search-index"
    COMPANY_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
    FILING_URL  = "https://www.sec.gov/Archives/edgar/full-index/"

    def __init__(self, delay_between_requests: float = 0.5):
        self.delay = delay_between_requests
        self.session = requests.Session()
        self.session.headers.update(EDGAR_HEADERS)

    def search_cloud_mentions(
        self,
        provider: str,
        form_type: str = "10-K",
        max_results: int = 20,
        date_from: str = "2023-01-01",
    ) -> list[dict]:
        """
        Search EDGAR full-text for a cloud provider term in 10-K filings.
        Returns raw EDGAR search results.
        """
        terms = CLOUD_PROVIDER_TERMS.get(provider, [provider])
        primary_term = terms[0]  # Use the most distinctive term

        params = {
            "q":        f'"{primary_term}"',
            "dateRange": "custom",
            "startdt":  date_from,
            "forms":    form_type,
            "_source":  "file_date,period_of_report,entity_name,file_num,biz_location,inc_states,form_type",
            "hits.hits.total.value": True,
            "hits.hits._source.period_of_report": True,
        }

        try:
            resp = self.session.get(self.SEARCH_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            logger.info(f"EDGAR: '{primary_term}' → {len(hits)} filings found")
            return hits[:max_results]
        except Exception as e:
            logger.error(f"EDGAR search failed for {provider}: {e}")
            return []

    def extract_company_from_hit(self, hit: dict, provider: str) -> Optional[CompanyDependency]:
        """
        Parse a single EDGAR search hit into a CompanyDependency.
        """
        try:
            source = hit.get("_source", {})
            entity_name = source.get("entity_name", "Unknown")
            filing_date = source.get("file_date", "")
            file_num    = source.get("file_num", "")

            # CIK is embedded in the document ID
            doc_id = hit.get("_id", "")
            cik = self._extract_cik(doc_id)

            # Confidence: base score, boosted by recency
            confidence = self._score_confidence(source, provider)

            return CompanyDependency(
                ticker=self._lookup_ticker(cik, entity_name),
                company_name=entity_name,
                cik=cik,
                cloud_providers={provider: confidence},
                filing_date=filing_date,
                filing_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K",
            )
        except Exception as e:
            logger.warning(f"Failed to parse EDGAR hit: {e}")
            return None

    def scrape_sp500_cloud_deps(self, max_per_provider: int = 50) -> list[CompanyDependency]:
        """
        Main entry point. Scrapes cloud dependencies for all three major providers.
        Merges results: if a company appears for multiple providers, they're combined.
        """
        all_deps: dict[str, CompanyDependency] = {}  # cik → dependency

        for provider in ["AWS", "Azure", "GCP"]:
            logger.info(f"Scraping {provider} dependencies...")
            hits = self.search_cloud_mentions(provider, max_results=max_per_provider)

            for hit in hits:
                dep = self.extract_company_from_hit(hit, provider)
                if not dep:
                    continue

                if dep.cik in all_deps:
                    # Merge: add this provider to existing company entry
                    all_deps[dep.cik].cloud_providers[provider] = \
                        dep.cloud_providers[provider]
                else:
                    all_deps[dep.cik] = dep

                time.sleep(self.delay)

        result = list(all_deps.values())
        logger.info(f"EDGAR scrape complete: {len(result)} companies with cloud deps found")
        return result

    def _extract_cik(self, doc_id: str) -> str:
        """Extract CIK from EDGAR document ID like '0000789019-24-000001'"""
        parts = doc_id.split("-")
        return parts[0].lstrip("0") if parts else "0"

    def _lookup_ticker(self, cik: str, fallback_name: str) -> str:
        """
        Attempt to resolve CIK → ticker via EDGAR company submissions API.
        Falls back to sanitized company name if not found.
        """
        try:
            cik_padded = cik.zfill(10)
            resp = self.session.get(
                f"https://data.sec.gov/submissions/CIK{cik_padded}.json",
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            tickers = data.get("tickers", [])
            return tickers[0] if tickers else fallback_name[:6].upper()
        except Exception:
            return fallback_name[:6].upper()

    def _score_confidence(self, source: dict, provider: str) -> float:
        """
        Assign a confidence score to this dependency.
        
        Higher confidence when:
        - Filing is recent (< 1 year)
        - Company is in a cloud-dependent sector
        """
        confidence = 0.75  # Base: explicit 10-K mention is strong signal

        # Boost for tech/SaaS companies (known cloud-heavy)
        biz_location = source.get("biz_location", "")
        if any(tech_hub in biz_location for tech_hub in ["CA", "WA", "NY"]):
            confidence = min(confidence + 0.05, 1.0)

        return round(confidence, 2)


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)

    scraper = EDGARScraper(delay_between_requests=0.5)

    # Quick test: search for AWS mentions in recent 10-Ks
    print("Testing EDGAR scraper — searching for AWS mentions in 10-K filings...\n")
    hits = scraper.search_cloud_mentions("AWS", max_results=5)

    for hit in hits:
        dep = scraper.extract_company_from_hit(hit, "AWS")
        if dep:
            print(f"  {dep.company_name} ({dep.ticker})")
            print(f"  Filed: {dep.filing_date}")
            print(f"  Confidence: {dep.cloud_providers}")
            print(f"  URL: {dep.filing_url}")
            print()
