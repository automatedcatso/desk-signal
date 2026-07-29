# Investigation Engine

The Investigation Engine is a local-first Flask workspace for evidence intake,
safe file extraction, search, entities, transactions, messages, indicators,
similarity, relationships, timelines, optional local AI or Gemini analysis,
and report exports.

Files and SQLite data are stored below this module's ignored `instance/`
directory. Optional OCR dependencies are listed in the repository-level
`requirements-local.txt`.

Smart and Deep analysis can use either the local assistant or the optional
Gemini 3.1 Flash-Lite provider. Configure Gemini with `GEMINI_API_KEY` in the
project-root `.env.local` file or the host environment. The UI displays
provider readiness and keeps Standard mode deterministic.
