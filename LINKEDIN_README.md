# Signal Desk — LinkedIn launch kit

This file is intentionally separate from the GitHub `README.md`. It contains
ready-to-edit LinkedIn copy, media order and accessible descriptions.

Before posting, replace:

- `[GITHUB_URL]` with the public repository;
- `[LIVE_URL]` with the Vercel deployment, if publishing a live showcase.

## Recommended post

I built **Signal Desk**, a local-first operations portal that turns raw
transaction records and mixed evidence into clear, reviewable outputs.

It brings two workflows into one application:

**Notice Studio**
• imports Excel transaction sheets  
• validates and organizes every row  
• generates neutral DOCX information requests  
• prepares unsent email drafts and a delivery manifest

**Investigation Engine**
• organizes documents, spreadsheets, images, email and logs  
• extracts entities, transactions and technical indicators  
• connects repeated values and similar evidence  
• builds searchable timelines and exportable reports

The default workflow is local and deterministic. For open-ended analysis, users
can choose a local model or explicitly opt into Gemini 3.1 Flash-Lite. Original
evidence files are not uploaded by the Gemini adapter.

The project is built with Python and Flask, runs locally on one port, includes
automated tests, and is prepared for GitHub and Vercel deployment.

Repository: [GITHUB_URL]  
Live showcase: [LIVE_URL]

#Python #Flask #WebDevelopment #Automation #LocalFirst #PrivacyByDesign
#DataEngineering #Vercel #GeminiAPI

## Short post

I built **Signal Desk**: a local-first Flask portal for Excel-to-notice
automation and mixed-evidence investigation.

It generates neutral DOCX notices and email drafts, extracts entities and
transactions from mixed files, connects repeated signals, builds timelines and
exports structured reports. Local analysis is the default, with Gemini 3.1
Flash-Lite available as an explicit option.

Repository: [GITHUB_URL]  
Demo: [LIVE_URL]

#Python #Flask #Automation #LocalFirst #Vercel

## LinkedIn project-section description

Designed and developed a local-first Flask operations portal combining Excel
notice automation with mixed-evidence analysis. Implemented DOCX and email
draft generation, full-text search, entity and transaction extraction,
similarity, timelines, relationship mapping, multi-format reports, optional
local/Gemini analysis, automated tests and Vercel deployment support.

## Recommended media order

1. `docs/media/linkedin-cover.png` — lead image.
2. `docs/media/signal-desk-tour.mp4` — 10-second silent product tour.
3. `docs/media/notice-studio.png` — Notice Studio detail.
4. `docs/media/investigation-engine.png` — Investigation Engine detail.

Use the MP4 as the primary media when LinkedIn allows only one media type. Use
the three PNG files as a carousel when a static post is preferable.

## Alt text

### Cover

Signal Desk dark interface with the headline “Turn raw records into clear next
steps” and cards for Notice Studio and Investigation Engine.

### Notice Studio

Notice Studio interface with sender settings, required Excel columns and a
large local workbook upload area.

### Investigation Engine

Investigation Engine interface with an investigation selector, workspace
statistics, quick-start guide and analysis navigation.

### Video

Ten-second silent tour transitioning from the Signal Desk launcher to Notice
Studio and then the Investigation Engine.

## Posting notes

- Keep the GitHub repository public before publishing the repository link.
- Verify the Vercel URL in a signed-out browser before adding it.
- Never include `.env.local`, API keys, uploaded evidence or runtime databases.
- If the live deployment has no authentication, present it as a showcase and
  do not upload sensitive material.
