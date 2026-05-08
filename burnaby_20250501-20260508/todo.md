# Burnaby Duplex Scraper TODO

## Current status
- Latest regenerated file: `duplex_134331.json`
- Address recovery improved significantly
- Still not production-clean for all months/layouts

## Remaining issues to fix
1. Recover missing permit IDs
   - Current missing permit IDs: 9
   - Add stronger fallback extraction for malformed PDF blocks
   - Try deriving permit numbers from neighboring text items and block-level context

2. Recover missing descriptions
   - Current missing descriptions: 9
   - Improve description boundary detection for PDFs where labels or columns collapse
   - Add fallback block text assembly when structured description extraction fails

3. Deduplicate repeated permit rows
   - Current duplicate permit IDs remaining: 2
   - Add logic to collapse same-permit repeats across adjacent issue dates or repeated sub-permit rows
   - Decide whether dedupe key should be permit ID only, or permit ID + issued date + address

4. Improve address extraction quality
   - Address nulls are now fixed, but some inferred addresses are messy
   - Example: address strings that still include legal-description fragments before the street address
   - Tighten address regex and prefer tail-end civic-address extraction

5. Improve classifier reliability
   - Make sure duplex detection prefers true residential duplex signals
   - Continue excluding commercial/demolition false positives automatically
   - Consider explicit residential/use checks before assigning duplex

6. Add verification against tabulation reports
   - Compare extracted monthly duplex counts with Burnaby tabulation aggregates where available
   - Flag mismatch months automatically in output metadata or a coverage report

7. Make it a one-stop-shop workflow
   - Add one command to scrape a multi-month date window instead of one month at a time
   - Write one combined output file plus a coverage/quality report automatically
   - Optionally emit a cleaned and deduplicated final JSON by default

8. Add output quality report
   - Include counts for missing permit IDs, missing descriptions, duplicates, and inferred addresses
   - Save report alongside the main output JSON

## Recommended next pass
- Fix null permit IDs/descriptions first
- Then add dedupe logic
- Then rerun the full `2025-05` to `2026-05` Burnaby duplex export
