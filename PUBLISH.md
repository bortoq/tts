# Publish to GitHub

The prepared Git history is in `tts.bundle`. Run these commands from this project
in a terminal with a writable `.git` directory and access to GitHub:

```bash
git init --initial-branch=main
git fetch ./tts.bundle main
git reset --mixed FETCH_HEAD
git status --short
gh auth status
gh repo create tts --private --source=. --remote=origin --description "$(cat ABOUT.txt)"
git push -u origin main
```

The mixed reset restores the prepared commit and index without overwriting files.
Review any local changes shown by `git status` before publishing. Use `--public`
instead of `--private` if you want a public repository. If authentication fails
once GitHub is reachable, use `gh auth login`.
