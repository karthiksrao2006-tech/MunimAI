# MUDRA KB sources (audit trail)

Fetched 2026-11-16 (UTC). Clause file: mudra.json

## Fetched and used
- https://www.mudra.org.in/offerings (2026-11-16) - Loan bands Shishu/Kishor/Tarun/Tarun Plus, purpose of loan, agri-allied exclusions, commercial-use-only vehicles, "collateral free" up to 20 lakh, no direct lending. Page footer shows copyright 2026.
- https://www.mudra.org.in/FAQ (2026-11-16) - Tarun Plus condition, eligible applicants, target segment, RBI collateral mandate, interest "deregulated", PAN/KYC, lender-governed terms.

Note: WebFetch got HTTP 403 from mudra.org.in; the pages were retrieved with curl (browser user-agent) and text-extracted. The rupee symbol did not survive extraction (rendered blank), so figures in the Shishu clause are written "Rs"; the numbers themselves are as on the page.

## Fetched, corroboration only (no clause taken)
- https://www.sidbi.in/blog/how-to-apply-for-pm-mudra-loan-online - Summarised via WebFetch; consistent with the four categories incl. Tarun Plus (10-20 lakh) and "collateral-free". Not quoted because the tool returned a summary, not raw text.

## Search results seen, not fetched
- WebSearch (official domains) surfaced SIDBI MSME report PDF, RAMP msme.gov.in PDFs, PMMY Bankers Kit (https://www.mudra.org.in/Home/PMMYBankersKit). Not fetched.

## Not attempted
- jansamarth.in, msme.gov.in, india.gov.in, rbi.org.in: not fetched; no RBI circular verified.

---
# Multi-scheme KB (added 2026-11-17)

Produced by `python -m backend.kb.fetch` then `python -m backend.kb.loader`. Clause text is verbatim page/PDF text,
split by heading (HTML) or by page and sentence groups (PDF); no model writes or paraphrases clauses. A re-fetch retires
older clauses of the same URL (`superseded_at`), never deletes them. One JSON file per scheme in this folder.

## Loaded (verified reachable with real content)
| scheme_code | clauses | sources |
|---|---|---|
| PMEGP | 46 | pmegp.msme.gov.in /Home/FAQ, /Home/TermsAndConditions, /Home/HomePage |
| CGTMSE | 168 | www.cgtmse.in home, /Home/VS/71 (FAQ), CGS-I Scheme Document PDF (updated 1 Apr 2025, first 30 pages) |
| PM_SVANIDHI | 7 | pmsvanidhi.mohua.gov.in |
| STANDUP_INDIA | 3 | www.standupmitra.in (success stories excluded) |
| PM_VISHWAKARMA | 12 | pmvishwakarma.gov.in |
| NSIC | 42 | www.nsic.co.in /Schemes/SinglePointRegistration, /RawMaterialAgainstBG, /MSMESaarthi |
| SIDBI | 21 | www.sidbi.in /en/working-capital, /en/machinery-loan, /en/business-loan |
| STARTUP_INDIA | 44 | startupindia.gov.in startup-scheme page; DPIIT recognition and tax-exemption page |
| SISFS | 41 | Startup India Seed Fund Scheme guidelines PDF (startupindia.gov.in) |
| STARTUP_PLAYBOOK | 91 | Startup Schemes Playbook, June 2026 PDF (startupindia.gov.in): Fund of Funds, central startup schemes |

## Gaps (not loaded, not guessed)
- **PMEGP subsidy-rate table (rural/urban %, own contribution):** not present in the reachable pmegp.msme.gov.in pages; it is on kviconline.gov.in, unreachable from here (connection fails from laptop and lab server).
- **CLCSS / SCLCSS / MSE-CDP and other MSME Ministry schemes:** msme.gov.in /schemes is JavaScript-rendered (0 clauses); dcmsme.gov.in, sclcss.msme.gov.in, my.msme.gov.in unreachable; scheme booklet PDFs on msme.gov.in return 404.
- **Udyam registration benefits:** udyamregistration.gov.in returns only a landing stub (1.7 KB, no scheme text).
- **Jan Samarth:** jansamarth.in is JavaScript-rendered (1 boilerplate line).
- **NIDHI (DST):** nidhi.dst.gov.in unreachable. Only what the Startup Playbook says about it is covered.
- **Karnataka startup/MSME schemes:** startup.karnataka.gov.in unreachable; ksfc.karnataka.gov.in pages are circular indexes (no rules text).
- **PM SVANidhi, Stand-Up India:** pages are thin (7 and 3 clauses); detailed guidelines PDFs not fetched.
- **kviconline.gov.in PMEGP guideline PDFs:** unreachable.

## Allowlist additions (backend/config.py)
pmegp.msme.gov.in, pmsvanidhi.mohua.gov.in, pmvishwakarma.gov.in, www.standupmitra.in (SIDBI-run for Dept of Financial Services),
www.nsic.co.in / nsic.co.in (Govt of India PSU), www.sidbi.in / sidbi.in, www.startupindia.gov.in / startupindia.gov.in.
