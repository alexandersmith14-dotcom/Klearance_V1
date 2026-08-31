"""Country-level risk reference for the jurisdiction panel.

Four sources, joined by ISO3 country code into country_risk.json:

  - FATF call-for-action ("black") list          hand-maintained
  - FATF increased-monitoring ("grey") list      hand-maintained
  - EU high-risk third countries (AML)           hand-maintained
  - INCSR major money-laundering jurisdictions   hand-maintained
  - Transparency International CPI score          fetched at build (OWID mirror)

The list bodies can't be scraped (fatf-gafi.org and transparency.org are behind
a Cloudflare JS challenge; EUR-Lex and the INCSR publish prose only), but each
moves on a known, slow cadence, so dated constants beat a fragile scrape. CPI
comes from Our World in Data's CSV mirror (CC BY, ISO3-keyed).

World Bank WGI was considered and left out: its Control-of-Corruption measure
correlates ~0.95 with CPI, and its only current bulk source is a stale XLSX that
would add an openpyxl dependency to an otherwise stdlib-lean project.

Run order: before dashboard.py.
"""
import csv
import io
import json

import fetcher

OUT_PATH = "country_risk.json"

# --- FATF -----------------------------------------------------------------
# fatf-gafi.org/en/countries/black-and-grey-lists.html
FATF_AS_OF = "19 June 2026"
FATF_SOURCE = "https://www.fatf-gafi.org/en/countries/black-and-grey-lists.html"
FATF_CALL_FOR_ACTION = ["Iran", "North Korea (DPRK)", "Myanmar"]
FATF_INCREASED_MONITORING = [
    "Angola", "Bolivia", "Bosnia and Herzegovina", "Bulgaria", "Cameroon",
    "Côte d'Ivoire", "Democratic Republic of the Congo", "Haiti", "Iraq",
    "Kenya", "Kuwait", "Laos", "Lebanon", "Monaco", "Nepal",
    "Papua New Guinea", "South Sudan", "Syria", "Venezuela", "Vietnam",
    "Virgin Islands (UK)", "Yemen",
]

# --- EU high-risk third countries ---------------------------------------
# Commission Delegated Regulation (EU) 2016/1675 as amended (2026/46, 2026/83).
EU_HIGH_RISK_AS_OF = "29 January 2026"
EU_HIGH_RISK_SOURCE = "https://finance.ec.europa.eu/financial-crime/anti-money-laundering-and-countering-financing-terrorism-international-level_en"
EU_HIGH_RISK = [
    "Afghanistan", "Algeria", "Angola", "Bolivia", "British Virgin Islands",
    "Cameroon", "Côte d'Ivoire", "North Korea (DPRK)",
    "Democratic Republic of the Congo", "Haiti", "Iran", "Kenya", "Laos",
    "Lebanon", "Monaco", "Myanmar", "Namibia", "Nepal", "Russia",
    "South Sudan", "Syria", "Trinidad and Tobago", "Vanuatu", "Venezuela",
    "Vietnam", "Yemen",
]

# --- INCSR ------------------------------------------------------------------
# US State Dept, International Narcotics Control Strategy Report, Volume 2
# (Money Laundering). "Major Money Laundering Jurisdictions". Inclusion flags
# significant *volume* of laundering-related activity, not weak controls -
# major financial centres (US, UK, Germany) are on it. Public domain.
INCSR_YEAR = "2024"
INCSR_REPORT = "2025 INCSR"
INCSR_SOURCE = "https://www.state.gov/international-narcotics-control-strategy-report/"
INCSR_JURISDICTIONS = [
    "Afghanistan", "Albania", "Algeria", "Antigua and Barbuda", "Argentina",
    "Aruba", "Bahamas", "Barbados", "Belgium", "Belize", "Bolivia", "Brazil",
    "British Virgin Islands", "Burma (Myanmar)", "Cabo Verde", "Cambodia",
    "Canada", "Cayman Islands", "China", "Colombia", "Costa Rica", "Curaçao",
    "Cyprus", "Dominica", "Dominican Republic", "Ecuador", "El Salvador",
    "Germany", "Ghana", "Guatemala", "Guinea-Bissau", "Guyana", "Haiti",
    "Honduras", "Hong Kong", "India", "Indonesia", "Iran", "Iraq", "Italy",
    "Jamaica", "Kazakhstan", "Kenya", "Kyrgyzstan", "Laos", "Liberia",
    "Macau", "Malaysia", "Mexico", "Mozambique", "Netherlands", "Nicaragua",
    "Nigeria", "Pakistan", "Panama", "Paraguay", "Peru", "Philippines",
    "Saint Kitts and Nevis", "Saint Lucia", "Saint Vincent and the Grenadines",
    "Senegal", "Sint Maarten", "South Africa", "Spain", "Suriname", "Syria",
    "Tajikistan", "Taiwan", "Tanzania", "Thailand", "Trinidad and Tobago",
    "Türkiye", "Turkmenistan", "Ukraine", "United Arab Emirates",
    "United Kingdom", "United States", "Uzbekistan", "Venezuela", "Vietnam",
]

# --- CPI ------------------------------------------------------------------
# Transparency International Corruption Perceptions Index, 0 (highly corrupt)
# to 100 (very clean). Our World in Data's mirror: entity, code (ISO3), year,
# cpi_score. CC BY 4.0 - credit Transparency International.
CPI_SOURCE = "https://www.transparency.org/en/cpi"
CPI_URL = ("https://ourworldindata.org/grapher/TI-corruption-perception-index"
           ".csv?csvType=full&useColumnShortNames=true")

# Every jurisdiction named in the four lists above, mapped to its ISO3 code so
# the lists join to the CPI scores. Spellings here must match the list bodies.
NAME_TO_ISO3 = {
    "Iran": "IRN", "North Korea (DPRK)": "PRK", "Myanmar": "MMR",
    "Burma (Myanmar)": "MMR", "Angola": "AGO", "Bolivia": "BOL",
    "Bosnia and Herzegovina": "BIH", "Bulgaria": "BGR", "Cameroon": "CMR",
    "Côte d'Ivoire": "CIV", "Democratic Republic of the Congo": "COD",
    "Haiti": "HTI", "Iraq": "IRQ", "Kenya": "KEN", "Kuwait": "KWT",
    "Laos": "LAO", "Lebanon": "LBN", "Monaco": "MCO", "Nepal": "NPL",
    "Papua New Guinea": "PNG", "South Sudan": "SSD", "Syria": "SYR",
    "Venezuela": "VEN", "Vietnam": "VNM", "Virgin Islands (UK)": "VGB",
    "British Virgin Islands": "VGB", "Yemen": "YEM", "Afghanistan": "AFG",
    "Algeria": "DZA", "Namibia": "NAM", "Russia": "RUS",
    "Trinidad and Tobago": "TTO", "Vanuatu": "VUT", "Albania": "ALB",
    "Antigua and Barbuda": "ATG", "Argentina": "ARG", "Aruba": "ABW",
    "Bahamas": "BHS", "Barbados": "BRB", "Belgium": "BEL", "Belize": "BLZ",
    "Brazil": "BRA", "Cabo Verde": "CPV", "Cambodia": "KHM", "Canada": "CAN",
    "Cayman Islands": "CYM", "China": "CHN", "Colombia": "COL",
    "Costa Rica": "CRI", "Curaçao": "CUW", "Cyprus": "CYP", "Dominica": "DMA",
    "Dominican Republic": "DOM", "Ecuador": "ECU", "El Salvador": "SLV",
    "Germany": "DEU", "Ghana": "GHA", "Guatemala": "GTM",
    "Guinea-Bissau": "GNB", "Guyana": "GUY", "Honduras": "HND",
    "Hong Kong": "HKG", "India": "IND", "Indonesia": "IDN", "Italy": "ITA",
    "Jamaica": "JAM", "Kazakhstan": "KAZ", "Kyrgyzstan": "KGZ",
    "Liberia": "LBR", "Macau": "MAC", "Malaysia": "MYS", "Mexico": "MEX",
    "Mozambique": "MOZ", "Netherlands": "NLD", "Nicaragua": "NIC",
    "Nigeria": "NGA", "Pakistan": "PAK", "Panama": "PAN", "Paraguay": "PRY",
    "Peru": "PER", "Philippines": "PHL", "Saint Kitts and Nevis": "KNA",
    "Saint Lucia": "LCA", "Saint Vincent and the Grenadines": "VCT",
    "Senegal": "SEN", "Sint Maarten": "SXM", "South Africa": "ZAF",
    "Spain": "ESP", "Suriname": "SUR", "Tajikistan": "TJK", "Taiwan": "TWN",
    "Tanzania": "TZA", "Thailand": "THA", "Türkiye": "TUR",
    "Turkmenistan": "TKM", "Ukraine": "UKR", "United Arab Emirates": "ARE",
    "United Kingdom": "GBR", "United States": "USA", "Uzbekistan": "UZB",
}

# Names for jurisdictions with no CPI score (too small / not assessed), so the
# lookup still has something to show.
ISO3_FALLBACK_NAME = {"VGB": "British Virgin Islands", "MAC": "Macau"}


def fetch_cpi():
    """({iso3: (score:int, year:int)}, {iso3: name}) from the OWID CPI mirror -
    most recent score per country, plus that mirror's country spellings."""
    text = fetcher.get(CPI_URL, timeout=60)
    latest, names = {}, {}
    for r in csv.DictReader(io.StringIO(text)):
        code = (r.get("code") or "").strip()
        if len(code) != 3:
            continue
        names.setdefault(code, (r.get("entity") or "").strip())
        raw = (r.get("cpi_score") or "").strip()
        if not raw:
            continue
        year = int(r["year"])
        if code not in latest or year > latest[code][1]:
            latest[code] = (int(round(float(raw))), year)
    return latest, names


def _iso3(name):
    iso = NAME_TO_ISO3.get(name)
    if not iso:
        raise KeyError(f"no ISO3 mapping for {name!r} - add it to NAME_TO_ISO3")
    return iso


def main():
    try:
        cpi, cpi_names = fetch_cpi()
        print(f"  CPI: {len(cpi)} scored countries")
    except Exception as e:  # never fail the build on the score feed
        cpi, cpi_names = {}, {}
        print(f"  CPI: SKIPPED ({type(e).__name__}: {str(e)[:120]})")

    fatf_call = {_iso3(n) for n in FATF_CALL_FOR_ACTION}
    fatf_grey = {_iso3(n) for n in FATF_INCREASED_MONITORING}
    eu = {_iso3(n) for n in EU_HIGH_RISK}
    incsr = {_iso3(n) for n in INCSR_JURISDICTIONS}

    # Display names: prefer the CPI mirror's spelling, then our own list
    # spelling, then a hand fallback for jurisdictions with no CPI row.
    names = dict(cpi_names)
    for iso, disp in ISO3_FALLBACK_NAME.items():
        names.setdefault(iso, disp)
    for name, iso in NAME_TO_ISO3.items():
        names.setdefault(iso, name)

    all_iso = set(cpi) | fatf_call | fatf_grey | eu | incsr
    countries = []
    for iso in sorted(all_iso, key=lambda i: names.get(i, i)):
        score = cpi.get(iso)
        countries.append({
            "iso3": iso,
            "name": names.get(iso, iso),
            "cpi": score[0] if score else None,
            "cpi_year": score[1] if score else None,
            "fatf": "call" if iso in fatf_call else ("grey" if iso in fatf_grey else None),
            "eu": iso in eu,
            "incsr": iso in incsr,
        })

    out = {
        "as_of": {
            "fatf": FATF_AS_OF, "eu": EU_HIGH_RISK_AS_OF,
            "incsr": f"{INCSR_YEAR} ({INCSR_REPORT})",
            "cpi": (max(c["cpi_year"] for c in countries if c["cpi_year"]) if cpi else None),
        },
        "sources": {
            "fatf": FATF_SOURCE, "eu": EU_HIGH_RISK_SOURCE,
            "incsr": INCSR_SOURCE, "cpi": CPI_SOURCE,
        },
        "countries": countries,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    listed = sum(1 for c in countries if c["fatf"] or c["eu"] or c["incsr"])
    scored = sum(1 for c in countries if c["cpi"] is not None)
    print(f"country_risk.json: {len(countries)} countries "
          f"({listed} on a list, {scored} with a CPI score)")


if __name__ == "__main__":
    main()
