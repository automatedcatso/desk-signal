# Publishing guide

## 1. Finalize ownership and licensing

1. Choose a repository name such as `signal-desk`.
2. Add the license that matches how others may use the source.
3. Review the first-person LinkedIn copy in `LINKEDIN_README.md`.
4. Confirm that the repository contains no real customer, transaction,
   investigation or credential data.

## 2. Publish to GitHub

Create an empty GitHub repository, then run from the Signal Desk folder:

```powershell
git init
git add .
git commit -m "Initial Signal Desk release"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/signal-desk.git
git push -u origin main
```

After pushing:

1. Open the repository's **Actions** tab and confirm the CI workflow passes.
2. Add repository topics such as `flask`, `python`, `automation`,
   `local-first`, `document-generation`, `evidence-analysis` and `vercel`.
3. Add a short About description:
   `Local-first notice automation and mixed-evidence analysis workspace.`
4. Optionally enable branch protection for `main`.

## 3. Deploy a Vercel showcase

1. Sign in to Vercel.
2. Choose **Add New → Project**.
3. Import the GitHub repository.
4. Keep the repository root as the project root.
5. Add `GEMINI_API_KEY` only if the optional Gemini provider is required.
6. Deploy and test `/`, `/notices/` and `/investigations/`.

Important: Vercel function storage is ephemeral, and the app does not include
public-user authentication. Use the deployment as a showcase unless durable
storage and access control are added.

## 4. Prepare the LinkedIn post

1. Open `LINKEDIN_README.md`.
2. Replace `[GITHUB_URL]` and `[LIVE_URL]`.
3. Upload `docs/media/signal-desk-tour.mp4` as the primary media, or upload the
   cover and two product screenshots as a carousel.
4. Add the supplied alt text.
5. Preview the post on desktop and mobile before publishing.

## 5. Post-release checks

- Open the GitHub README in a signed-out browser and verify every image.
- Confirm that GitHub Actions passes.
- Test the Vercel deployment without a logged-in browser session.
- Check browser console and server logs for errors.
- Rotate any credential that was ever pasted into a terminal, screenshot or
  commit.
- Tag the first stable release:

```powershell
git tag -a v1.1.0 -m "Signal Desk v1.1.0"
git push origin v1.1.0
```
