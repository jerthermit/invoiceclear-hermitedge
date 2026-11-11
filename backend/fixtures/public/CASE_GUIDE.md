# Public case pack: staffing-36-v3

All businesses, contacts, invoices, line items, timecards, POs, reviews and correspondence are fictional, permitted workflow inputs. The retained reviewer Lena Ortiz is also fictional. Email domains use reserved `.example` names. The pack contains 36 invoice cases, 35 two-page PDF POs (including retained revisions), 16 manually retained customer messages and two constructed internal approval records. None is a connected-provider receipt, live model finding, client outcome or proof of payment. Initial counts are checked by regression tests.

The representative business context is a US staffing supplier processing dated billing batches for eight legal accounts across multiple branches and departments. This small exception pack is motivated by the variety such an operation can encounter. It is not 50,000 invoices, a monthly-throughput simulation, a client-industry assertion or validated production capacity. The separate bounded stress script exercises 5,000 private records and does not establish a 50,000-invoice monthly limit or service level.

Invoice amounts equal the sum of billing lines rounded individually to cents, with quantities, rates, service period, net terms and batch references retained in source provenance. The PO's labeled total is its authorization ceiling. Page 2 reconciles the related invoice and cannot increase that ceiling. Checks compare the latest retained document, not an older convenient revision. The two constructed approvals use the same transactional checks as an ordinary approval and reserve only local workspace allocations; external PO consumption is unknown.

| Cases | Legal account / branch context | Initial work and evidence |
| --- | --- | --- |
| 1041–1042 | Mercer Valley Home Health LLC / Columbus East; Leonard & Reyes Logistics LLC / Memphis South DC | Missing POs; matching source files are in this ZIP. |
| 1043–1046 | A. Quinn Financial Services Inc / Phoenix Operations; logistics returns; ambulatory nursing | Transposed reference, insufficient ceiling, reviewed headroom, and an unreviewed PO. |
| 1047–1049 | Thompson-Hale Aerospace Components Inc / Wichita; Foster & Ortiz Food Processing LLC / Des Moines; S.E. Howard Hotel Management LLC / Savannah | Missing evidence, including retained replies and legal-buyer clarification. |
| 1050–1051 | K.B. Duncan Diagnostic Laboratories LLC / Raleigh; Benton Utility Field Services LLC / Tulsa | Unreviewed multiline POs; Net 30 and Net 60 terms. |
| 1052–1054 | Foods packaging, logistics night sort, aerospace inspection | Wrong digits, absent invoice reference despite retained PO, revision-suffix conflict. |
| 1055–1056 | Health outpatient intake; logistics freight scheduling | Reviewed POs name different legal affiliates; a shared brand cannot pass customer checks. |
| 1057–1058 | Diagnostics accessioning; aerospace supplier inspection | CAD and EUR POs cannot authorize USD invoices. |
| 1059–1060 | Foods second shift; hospitality guest services | USD 15,818.76 exceeds USD 15,818.75 by one cent; a separate overtime shortfall is USD 1,250.50. |
| 1061 | Utility storm response | Retained old USD 9,000 revision and new USD 16,000 revision; the new revision awaits review. |
| 1062 | Diagnostics processing | Corrected second revision is reviewed; the old reference remains retained. |
| 1063 | Health outpatient intake | A newer reviewed revision names the wrong legal customer; the earlier passing revision cannot bypass the hold. |
| 1064 | Aerospace supplier inspection | Three revisions retain an old ceiling, wrong currency, then reviewed current USD authorization. |
| 1065–1067 | Utility meter scheduling; diagnostics accessioning; finance servicing | Exact-cent coverage, excess authorized headroom, and legal-name case normalization. |
| 1068–1070 | Aerospace inspection; hospitality events; foods sanitation | Significant PO punctuation, a supported retained correction, and two replies without a PO. |
| 1071–1072 | Leonard & Reyes Logistics LLC / Dallas Distribution | Prepared USD 14,837.50 allocation against USD 22,150.15 leaves exactly USD 7,312.65 for 1072. |
| 1073–1074 | Mercer Valley Home Health LLC / Dayton Regional | Prepared USD 12,500.00 against USD 18,000.00 leaves USD 5,500.00; 1074 is USD 5,500.01 and stays blocked. |
| 1075–1076 | Utility meter scheduling; health cross-border scheduling | The same PO text under another legal customer or a separate CAD authorization does not consume the USD health allocation. |

Fresh public state: 6 missing POs, 4 awaiting document review, 14 mismatches, 10 ready for approval and 2 prepared internal handoffs. All 36 invoices remain unpaid. No agent runs are seeded. Opening, searching and reading cases performs no provider calls.

The source ZIP contains all 36 source invoice rows, the exact 35 retained PDFs with a revision manifest, source billing lines, the 16 retained messages, and three incoming PDFs for 2101/1041/1042. The source-file import adds separate case 2101. Importing it twice skips the second identical row; a conflicting duplicate is rejected. Start with the README walkthrough for the complete retained-evidence, review, supported-correction and handoff sequence.
