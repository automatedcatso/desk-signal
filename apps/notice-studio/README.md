# Notice Studio

Notice Studio imports `.xlsx` transaction rows, validates and filters them,
provides a live notice preview, and generates an organized ZIP containing:

- one neutral DOCX notice per row;
- one local `.eml` draft per row with the DOCX attached;
- a `delivery_manifest.csv` that maps recipients to generated files.

Required workbook columns are Bank, Layer, Account No, IFSC and Transaction
Amount. `Company Email` is optional and can also be added in the review drawer.

The template is `notice_template.docx`. It contains no letterhead, personal
identity, station, or preloaded account/contact values.
