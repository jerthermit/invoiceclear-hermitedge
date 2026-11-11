# Publish InvoiceClear

Export the reviewed source into a new public repository. The deployment projects can continue to use the private development checkout as their source.

## GitHub fields

| Field | Value |
| --- | --- |
| Owner | `jerthermit` |
| Repository name | `invoiceclear-hermitedge` |
| Visibility | Public |
| Description / About | AI agent for invoice resolution that investigates purchase-order discrepancies, cites the records it checked, and recommends the next step. Reviewers approve the billing handoff. |
| Website | https://invoiceclear.hermitedge.com |
| License | All rights reserved; do not choose MIT or another open-source license without intentionally changing the rights. |

Topics, ready to copy:

```text
ai-agent invoice-management accounts-receivable purchase-orders human-in-the-loop quickbooks together-ai python sqlite
```

Description and About use the same GitHub description field. The README does not set repository metadata automatically.

## First release

Draft **InvoiceClear v1.0 — Public Portfolio Release** with tag `v1.0.0` and the [release notes](releases/v1.0.0.md). Tag the exact deployed and tested commit in the development repo, or the equivalent reviewed source snapshot in the public repo. Wait for a live investigation, one approval and export, and a saved test change that survives a Railway redeploy before publishing. GitHub generates the source archives for a tagged release; no separate package is distributed.

## Public structure

The public root matches [Charge](https://github.com/jerthermit/charge-hermitedge): `frontend/`, `backend/`, `README.md` and `.gitignore`. The centered brand header, capabilities, workflow, reliability, architecture, implementation boundaries, local setup, verification, deployment and copyright follow that presentation, with InvoiceClear's actual stack and scope.

Operating guides and the portable Makefile live in `backend/`. Existing local root shortcuts and private notes remain on disk but are excluded from publication. No runtime files were removed.

## Before upload

From the project root:

```bash
make -C backend release
git status --short --untracked-files=all
git check-ignore .env backend/.env frontend/.env backend/data/workspace.sqlite3 frontend/dist/index.html
```

Publish only `.gitignore`, `README.md`, `frontend/` and `backend/`. Do not force-add ignored files. The ignore rules cover credentials, databases and sidecars, backups, local agent notes, virtual environments, generated builds, coverage and recordings. They do not replace a final review for client material or secrets.

## Manual GitHub upload

Start with an empty public GitHub repository. Run this from the development checkout after committing the reviewed source:

```bash
(
  set -e
  set -o pipefail
  test -z "$(git status --porcelain)"
  invoiceclear_public="$HOME/Documents/invoiceclear-hermitedge"
  mkdir "$invoiceclear_public"
  git archive HEAD -- .gitignore README.md frontend backend |
    tar -xf - -C "$invoiceclear_public"
  git -C "$invoiceclear_public" init -b main
  git -C "$invoiceclear_public" add .gitignore README.md frontend backend
  git -C "$invoiceclear_public" -c core.whitespace=trailing-space,space-before-tab,cr-at-eol \
    diff --cached --check -- . ':(exclude,glob)**/*.pdf' ':(exclude,glob)frontend/web/fonts/*-LICENSE.txt'
  git -C "$invoiceclear_public" diff --cached --stat
)
```

Review the staged files. The whitespace check skips PDFs and bundled font licenses to preserve their original bytes. Then commit and push the new history:

```bash
(
  set -e
  cd "$HOME/Documents/invoiceclear-hermitedge"
  invoiceclear_url="https://github.com/jerthermit/invoiceclear-hermitedge.git"
  invoiceclear_refs=$(git ls-remote --refs "$invoiceclear_url")
  if [ -n "$invoiceclear_refs" ]; then
    printf '%s\n' 'The public repository already has refs. Stopping.'
    exit 1
  fi
  git commit -m "Publish InvoiceClear public edition"
  git remote add origin "$invoiceclear_url"
  git push -u origin main
)
```

Do not force-push if the public repository already has history. Keep the live Railway and Vercel projects linked to the private repo unless you intentionally change their deployment source.

Set About, topics and the verified website through the public repository page. Use [hosted acceptance](RELEASE.md#4-hosted-acceptance) to distinguish tested behavior from planned or unverified integrations.
