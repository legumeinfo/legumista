# reporting — checking identifiers and filing data defects: verify_ids, report_data_issue

## verify_ids
Pass a draft answer as `text`, or lists of DOIs, gene IDs, collection IDs and GCA_/GCF_
accessions. `citations` as `[{doi, title}]` also catches a real DOI attached to the wrong
paper. One verdict per ID: FOUND, NOT FOUND, MISMATCH, RETRACTED or UNCHECKED. FOUND means
the identifier exists, not that it supports your claim. A gene ID is checked in
legumemine, then in its species' genus mine, which can spell the same gene differently
(ArachisMine's `arahy.Tifrunner.gnm2.ann1.GHMM2H` is legumemine's
`arahy.Tifrunner.gnm2.ann1.Arahy.GHMM2H`); NOT FOUND names the mines checked.

## report_data_issue
Served only where enabled. For defects a curator would fix in the Data Store or a mine: a
README that does not parse or names the wrong collection, a field that contradicts its
siblings, a typo'd identifier, a mine record that contradicts the store. Not for missing
files, CDS notes, site or browser configuration, or suspicions.

- One `subject` (a collection, or `<mine>/<Class>/<primaryIdentifier>`), one `field`
  (`readme`, `readme.<key>`, `catalog.<key>`, or a mine attribute) and the value you
  `observed`. The server re-reads the field and refuses if it differs, so quote exactly.
- `summary` is the issue title. `expected` and `reason` are shown as unverified.
- The user confirms before anything is filed: the server asks them, or you get a PREVIEW
  to show them, and call again with `confirm` only after they agree. Never confirm on
  their behalf. A duplicate returns the existing issue.
