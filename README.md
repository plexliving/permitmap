# Vancouver Multiplex Permit Scraper

This is a small Python CLI that searches the City of Vancouver public permit portal for submitted permits created in a date range, opens each permit detail page, and filters for one or more multiplex sizes:

- `2` = duplex
- `3` = triplex
- `4` = fourplex
- `5` = fiveplex
- `6` = sixplex
- `7` = sevenplex
- `8` = eightplex

It uses the public permit search page at:

- `https://plposweb.vancouver.ca/Public/Default.aspx?PossePresentation=PermitSearchByDate`

## Requirements

- Python 3.10+
- No third-party packages

## Usage

Show duplex submissions from the last 30 days and save them to a timestamped JSON file such as `duplex_20260424_141522.json`:

```bash
python vancouver_multiplex_scraper.py 2
```

Search only development permits in a custom date range:

```bash
python vancouver_multiplex_scraper.py 4 --permit-type development --from-date 2026-01-01 --to-date 2026-04-14
```

Run a chain of sizes in order, writing one timestamped JSON file per size:

```bash
python vancouver_multiplex_scraper.py 2-3-4 --permit-type building --from-date 2025-04-08 --to-date 2025-04-12
```

The April 8-12, 2025 building-permit window is useful as a map-friendly sample because it includes issued Open Data matches with coordinates:

```bash
python vancouver_multiplex_scraper.py 2-3-4 --permit-type building --from-date 2025-04-08 --to-date 2025-04-12
```

In the latest test run, that window produced 22 mappable duplex records, 1 mappable triplex record, and 3 mappable fourplex records.

Run every supported size from duplex through eightplex:

```bash
python vancouver_multiplex_scraper.py all --from-date 2025-01-01 --to-date 2026-04-24
```

Write JSON output:

```bash
python vancouver_multiplex_scraper.py 6 --output sixplexes.json
```

Write CSV output:

```bash
python vancouver_multiplex_scraper.py 8 --format csv --output eightplexes.csv
```

For multiple sizes with `--format csv` or `--format json`, omit `--output` to create timestamped files, or pass a directory:

```bash
python vancouver_multiplex_scraper.py 2-3-4 --format csv --output exports
```

## Notes

- The portal is a live public website and its internal search payload can change.
- The script classifies permits by matching duplex/triplex/fourplex/etc. language and Vancouver permit wording such as `Multiple Dwelling`, `Multiplex`, and unit-count phrases on the permit detail page.
- JSON output includes portal fields plus issued-building-permit Open Data enrichment when available, including `permitElapsedDays`, `projectValue`, `PermitCategory`, `PropertyUse`, `geom`, `geoLocalArea`, and `geo_point_2d`.
- If a portal record has no Open Data geometry, the scraper geocodes its address with the BC Geocoder and marks `geometry_source` as `bc_geocoder`.
- The default date range is the last 30 days. Recent `In Review` portal records often do not exist in the issued-building-permits Open Data dataset yet, so Open Data-only fields may remain blank even when address-derived geometry is available.
- The preview app can toggle status-based pin colouring; status colours are variants of each layer's base colour.
- If the City changes its HTML or search form behavior, the script may need a small update.

## Burnaby Permit PDF Scraper

Burnaby publishes official daily permit-issued reports as PDFs on:

- `https://www.burnaby.ca/services-and-payments/permits-and-applications/building-permits-issued-and-tabulation-reports`

Parse every official PDF link found for a month:

```bash
python burnaby_permit_pdf_scraper.py --year 2026 --month 4
```

The scraper treats the daily PDFs listed inside each `Month Year` permits-issued
section as the primary source, for example `April-1-2026.pdf` and
`April-2-2026.pdf`. Monthly tabulation PDFs are aggregate reports only; the
script discovers them separately for backup/manual verification and does not use
them as permit-record input.

Write CSV instead of JSON:

```bash
python burnaby_permit_pdf_scraper.py --year 2026 --month 4 --format csv --output burnaby_2026_04.csv
```

Parse already-downloaded PDFs:

```bash
python burnaby_permit_pdf_scraper.py --year 2026 --month 4 --pdf April-28-2026.pdf --output burnaby_apr28.json
```

Write a best-effort monthly tabulation summary sidecar when one exists:

```bash
python burnaby_permit_pdf_scraper.py --year 2026 --month 2 --tabulation-output burnaby_2026_02_tabulation.json
```

Optionally filter to records classified as duplex through eightplex by unit count or matching text:

```bash
python burnaby_permit_pdf_scraper.py --year 2026 --month 4 --multiplex-only
python burnaby_permit_pdf_scraper.py --year 2026 --month 4 --sizes 2-4
```

For Burnaby residential reports, the `Number of Units` column is not always the
best classifier. Many two-unit residential records show `0` or `1` in that
column and put the useful value in the description, such as `Total units: 2`.
The scraper therefore checks explicit multiplex words first, then `Total units`
phrases in the description, and only then the `Number of Units` column.

The Burnaby parser uses only Python's standard library. It downloads the official PDFs, extracts their embedded positioned text, and outputs permit number, issue date, address, legal description, zoning, permit category, type of change, value, units, applicant, contractor, contractor address, description, and source PDF metadata.
