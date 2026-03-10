#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "=== CrossMix Installer Build ==="
echo

# Build Linux binary locally
echo "[1/2] Building Linux binary..."
python3 build.py
echo

# Build Windows binary via GitHub Actions CI
echo "[2/2] Building Windows binary via GitHub Actions..."

# Make sure we have a remote
if ! git remote get-url origin &>/dev/null; then
    echo "ERROR: No git remote 'origin' configured."
    echo "       Run: git remote add origin git@github.com:YOUR_USER/CrossMixInstaller.git"
    exit 1
fi

# Determine version tag
LATEST_TAG=$(git tag -l 'v*' --sort=-v:refname | head -n1)
if [ -z "$LATEST_TAG" ]; then
    NEXT_TAG="v0.1.0"
else
    # Bump patch version: v0.1.0 -> v0.1.1
    BASE=${LATEST_TAG#v}
    MAJOR=$(echo "$BASE" | cut -d. -f1)
    MINOR=$(echo "$BASE" | cut -d. -f2)
    PATCH=$(echo "$BASE" | cut -d. -f3)
    NEXT_TAG="v${MAJOR}.${MINOR}.$((PATCH + 1))"
fi

echo "  Latest tag: ${LATEST_TAG:-none}"
echo "  Next tag:   $NEXT_TAG"
read -rp "  Tag and push to trigger CI build? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
    # Make sure all changes are committed
    if [ -n "$(git status --porcelain)" ]; then
        echo "  ERROR: You have uncommitted changes. Commit them first."
        exit 1
    fi

    git tag "$NEXT_TAG"
    git push origin "$(git branch --show-current)"
    git push origin "$NEXT_TAG"

    echo
    echo "  Tag $NEXT_TAG pushed. GitHub Actions will build Windows + Linux binaries."

    # Parse repo name from remote URL
    REMOTE_URL=$(git remote get-url origin)
    REPO=""
    if [[ "$REMOTE_URL" =~ github\.com[:/](.+)\.git$ ]] || [[ "$REMOTE_URL" =~ github\.com[:/](.+)$ ]]; then
        REPO="${BASH_REMATCH[1]}"
        echo "  Watch progress: https://github.com/${REPO}/actions"
    fi

    # Wait for the CI release to finish, then download the Windows binary
    if [ -n "$REPO" ] && command -v gh &>/dev/null; then
        echo
        echo "  Waiting for GitHub Actions to finish..."

        # Find the workflow run for this tag
        sleep 5  # give GitHub a moment to register the run
        RUN_ID=""
        for i in $(seq 1 12); do
            RUN_ID=$(gh run list --repo "$REPO" --workflow build.yml --branch "$NEXT_TAG" --json databaseId,status -q '.[0].databaseId' 2>/dev/null)
            if [ -n "$RUN_ID" ]; then
                break
            fi
            sleep 5
        done

        if [ -n "$RUN_ID" ]; then
            echo "  Found workflow run $RUN_ID. Waiting for completion (this takes a few minutes)..."
            gh run watch "$RUN_ID" --repo "$REPO" --exit-status && CI_OK=true || CI_OK=false

            if $CI_OK; then
                echo
                echo "  CI build succeeded. Downloading Windows binary..."
                # Download the Windows artifact
                gh run download "$RUN_ID" --repo "$REPO" --name CrossMixInstaller-Windows --dir /tmp/crossmix-artifacts 2>/dev/null
                if [ $? -eq 0 ] && ls /tmp/crossmix-artifacts/CrossMixInstaller-* &>/dev/null; then
                    cp /tmp/crossmix-artifacts/CrossMixInstaller-* releases/
                    rm -rf /tmp/crossmix-artifacts
                    echo "  Windows binary downloaded to releases/"
                else
                    echo "  Could not download artifact. Trying release assets..."
                    # Fall back to downloading from the release page
                    sleep 5
                    gh release download "$NEXT_TAG" --repo "$REPO" --pattern "CrossMixInstaller-*.exe" --dir releases/ 2>/dev/null \
                        && echo "  Windows binary downloaded to releases/" \
                        || echo "  WARNING: Could not download Windows binary. Check the release page manually."
                fi
            else
                echo "  WARNING: CI build failed. Check: https://github.com/${REPO}/actions/runs/${RUN_ID}"
            fi
        else
            echo "  WARNING: Could not find workflow run. Check: https://github.com/${REPO}/actions"
        fi
    elif [ -n "$REPO" ]; then
        echo
        echo "  Install 'gh' (GitHub CLI) to auto-download the Windows binary:"
        echo "    sudo apt install gh && gh auth login"
        echo "  Or download manually: https://github.com/${REPO}/releases/tag/${NEXT_TAG}"
    fi
else
    echo "  Skipped. You can trigger it manually later with:"
    echo "    git tag $NEXT_TAG && git push origin main && git push origin $NEXT_TAG"
fi

echo
echo "=== Build complete. Binaries in releases/ ==="
ls -lh releases/ 2>/dev/null
