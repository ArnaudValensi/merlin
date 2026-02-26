#!/usr/bin/env bash
# Merlin installer — curl -fsSL <url> | bash
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/master/install.sh | bash
#   bash install.sh              # Normal install
#   bash install.sh --dry-run    # Print what would be done without doing it
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MERLIN_HOME="${MERLIN_HOME:-$HOME/.merlin}"
GITHUB_REPO="${MERLIN_REPO:-ArnaudValensi/merlin}"  # owner/repo
GITHUB_TOKEN="${GITHUB_TOKEN:-}"                    # optional, for private repos
BIN_DIR="$MERLIN_HOME/bin"
VERSIONS_DIR="$MERLIN_HOME/versions"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { echo "  $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo "→ $*"; }

run() {
    if $DRY_RUN; then
        info "[dry-run] $*"
    else
        "$@"
    fi
}

confirm() {
    local prompt="$1"
    if $DRY_RUN; then
        info "[dry-run] Would ask: $prompt [y/N]"
        return 0
    fi
    # Read from /dev/tty so confirm works even when piped (curl | bash)
    read -rp "  $prompt [y/N] " answer < /dev/tty
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

detect_pkg_manager() {
    if command -v apt >/dev/null 2>&1; then
        echo "apt"
    elif command -v pacman >/dev/null 2>&1; then
        echo "pacman"
    elif command -v brew >/dev/null 2>&1; then
        echo "brew"
    else
        echo ""
    fi
}

install_cmd() {
    local pkg="$1"
    local mgr
    mgr=$(detect_pkg_manager)
    case "$mgr" in
        apt)    echo "sudo apt install -y $pkg" ;;
        pacman) echo "sudo pacman -S --noconfirm $pkg" ;;
        brew)   echo "brew install $pkg" ;;
        *)      echo "" ;;
    esac
}

install_pkg() {
    local pkg="$1"
    local mgr
    mgr=$(detect_pkg_manager)
    case "$mgr" in
        apt)    run sudo apt install -y "$pkg" ;;
        pacman) run sudo pacman -S --noconfirm "$pkg" ;;
        brew)   run brew install "$pkg" ;;
        *)      return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║      Installing Merlin...        ║"
echo "  ╚══════════════════════════════════╝"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Check for uv (required)
# ---------------------------------------------------------------------------

step "Checking for uv..."
if command -v uv >/dev/null 2>&1; then
    info "uv found: $(uv --version)"
else
    warn "uv not found (required)"
    if confirm "Install uv now?"; then
        run bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
        # Source the uv env so it's available for the rest of the script
        if [[ -f "$HOME/.local/bin/env" ]]; then
            . "$HOME/.local/bin/env" 2>/dev/null || true
        fi
        export PATH="$HOME/.local/bin:$PATH"
        if ! $DRY_RUN && ! command -v uv >/dev/null 2>&1; then
            error "uv installation failed. Install manually: https://docs.astral.sh/uv/"
            exit 1
        fi
        info "uv installed"
    else
        error "uv is required. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Step 2: Check for tmux (optional)
# ---------------------------------------------------------------------------

step "Checking for tmux..."
if command -v tmux >/dev/null 2>&1; then
    info "tmux found"
else
    warn "tmux not found (optional — needed for web terminal)"
    cmd=$(install_cmd tmux)
    if [[ -n "$cmd" ]]; then
        if confirm "Install tmux? ($cmd)"; then
            if install_pkg tmux; then
                info "tmux installed"
            else
                warn "tmux installation failed. Install later: $cmd"
            fi
        else
            info "Skipped. Install later: $cmd"
        fi
    else
        info "No supported package manager found. Install tmux manually."
    fi
fi

# ---------------------------------------------------------------------------
# Step 3: Check for cloudflared (optional)
# ---------------------------------------------------------------------------

step "Checking for cloudflared..."
if command -v cloudflared >/dev/null 2>&1; then
    info "cloudflared found"
else
    warn "cloudflared not found (optional — needed for tunnel access)"
    cmd=$(install_cmd cloudflared)
    if [[ -n "$cmd" ]]; then
        if confirm "Install cloudflared? ($cmd)"; then
            if install_pkg cloudflared; then
                info "cloudflared installed"
            else
                warn "cloudflared installation failed. Install later: $cmd"
            fi
        else
            info "Skipped. Install later: $cmd"
        fi
    else
        info "No supported package manager found. Install cloudflared manually."
    fi
fi

# ---------------------------------------------------------------------------
# Step 4: Fetch latest tag
# ---------------------------------------------------------------------------

step "Fetching latest tag..."
TAGS_URL="https://api.github.com/repos/$GITHUB_REPO/tags"

if $DRY_RUN; then
    TAG="0.1.0"
    info "[dry-run] Would fetch from $TAGS_URL"
    info "[dry-run] Using placeholder version: $TAG"
else
    CURL_AUTH=()
    if [[ -n "$GITHUB_TOKEN" ]]; then
        CURL_AUTH=(-H "Authorization: token $GITHUB_TOKEN")
    fi
    TAG_JSON=$(curl -fsSL "${CURL_AUTH[@]}" "$TAGS_URL" 2>/dev/null) || {
        error "Could not fetch tags from GitHub."
        error "Check your internet connection and that $GITHUB_REPO exists."
        exit 1
    }
    TAG=$(echo "$TAG_JSON" | grep -o '"name": *"[^"]*"' | head -1 | cut -d'"' -f4 | sed 's/^v//')
    if [[ -z "$TAG" ]]; then
        error "No tags found for $GITHUB_REPO"
        exit 1
    fi
fi

info "Latest version: $TAG"

# ---------------------------------------------------------------------------
# Step 5: Download and extract
# ---------------------------------------------------------------------------

VERSION_DIR="$VERSIONS_DIR/$TAG"

step "Installing version $TAG..."
if [[ -d "$VERSION_DIR" ]]; then
    info "Version $TAG already exists at $VERSION_DIR"
else
    if [[ -n "$GITHUB_TOKEN" ]]; then
        TARBALL_URL="https://api.github.com/repos/$GITHUB_REPO/tarball/v$TAG"
    else
        TARBALL_URL="https://github.com/$GITHUB_REPO/archive/refs/tags/v$TAG.tar.gz"
    fi

    if $DRY_RUN; then
        info "[dry-run] Would download $TARBALL_URL"
        info "[dry-run] Would extract to $VERSION_DIR"
    else
        mkdir -p "$VERSIONS_DIR"
        TMPFILE=$(mktemp)
        # Clean up temp file and partial extraction on failure
        trap 'rm -f "$TMPFILE"; if [[ -d "$VERSION_DIR" ]]; then rm -rf "$VERSION_DIR"; fi' EXIT

        curl -fsSL "${CURL_AUTH[@]}" "$TARBALL_URL" -o "$TMPFILE" || {
            # Try without v prefix
            if [[ -n "$GITHUB_TOKEN" ]]; then
                TARBALL_URL="https://api.github.com/repos/$GITHUB_REPO/tarball/$TAG"
            else
                TARBALL_URL="https://github.com/$GITHUB_REPO/archive/refs/tags/$TAG.tar.gz"
            fi
            curl -fsSL "${CURL_AUTH[@]}" "$TARBALL_URL" -o "$TMPFILE" || {
                error "Could not download release tarball"
                exit 1
            }
        }

        mkdir -p "$VERSION_DIR"
        tar xzf "$TMPFILE" --strip-components=1 -C "$VERSION_DIR"

        # Verify extraction produced expected files
        if [[ ! -f "$VERSION_DIR/main.py" ]]; then
            error "Extraction appears incomplete — main.py not found"
            rm -rf "$VERSION_DIR"
            exit 1
        fi

        rm -f "$TMPFILE"
        trap - EXIT
        info "Extracted to $VERSION_DIR"
    fi
fi

# ---------------------------------------------------------------------------
# Step 6: Create current symlink
# ---------------------------------------------------------------------------

step "Setting active version..."
CURRENT_LINK="$MERLIN_HOME/current"

if $DRY_RUN; then
    info "[dry-run] Would symlink $CURRENT_LINK -> $VERSION_DIR"
else
    # Atomic symlink swap (mv -T is GNU-only, fall back for macOS/BSD)
    ln -sfn "$VERSION_DIR" "${CURRENT_LINK}.tmp"
    if ! mv -Tf "${CURRENT_LINK}.tmp" "$CURRENT_LINK" 2>/dev/null; then
        rm -f "$CURRENT_LINK"
        mv "${CURRENT_LINK}.tmp" "$CURRENT_LINK"
    fi
    info "current -> versions/$TAG"
fi

# ---------------------------------------------------------------------------
# Step 7: Write launcher script
# ---------------------------------------------------------------------------

step "Creating launcher..."
if $DRY_RUN; then
    info "[dry-run] Would write $BIN_DIR/merlin"
else
    mkdir -p "$BIN_DIR"
    cat > "$BIN_DIR/merlin" << LAUNCHER
#!/usr/bin/env bash
exec uv run "${MERLIN_HOME}/current/cli.py" "\$@"
LAUNCHER
    chmod +x "$BIN_DIR/merlin"
    info "Launcher: $BIN_DIR/merlin"
fi

# ---------------------------------------------------------------------------
# Step 8: Add to PATH
# ---------------------------------------------------------------------------

step "Checking PATH..."
if echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    info "$BIN_DIR already in PATH"
else
    # Detect shell config file based on active shell
    SHELL_CONFIG=""
    case "${SHELL:-}" in
        */zsh)  SHELL_CONFIG="$HOME/.zshrc" ;;
        */bash) SHELL_CONFIG="$HOME/.bash_profile"
                [[ -f "$SHELL_CONFIG" ]] || SHELL_CONFIG="$HOME/.bashrc" ;;
    esac
    # Fallback: check common files if $SHELL didn't match
    if [[ -z "$SHELL_CONFIG" ]]; then
        if [[ -f "$HOME/.zshrc" ]]; then
            SHELL_CONFIG="$HOME/.zshrc"
        elif [[ -f "$HOME/.bashrc" ]]; then
            SHELL_CONFIG="$HOME/.bashrc"
        fi
    fi

    PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\""

    if [[ -n "$SHELL_CONFIG" ]]; then
        # Skip if already added
        if grep -qF 'merlin/bin' "$SHELL_CONFIG" 2>/dev/null; then
            info "PATH entry already exists in $SHELL_CONFIG"
        elif confirm "Add $BIN_DIR to PATH in $SHELL_CONFIG?"; then
            if $DRY_RUN; then
                info "[dry-run] Would append to $SHELL_CONFIG: $PATH_LINE"
            else
                echo "" >> "$SHELL_CONFIG"
                echo "# Merlin" >> "$SHELL_CONFIG"
                echo "$PATH_LINE" >> "$SHELL_CONFIG"
                info "Added to $SHELL_CONFIG"
                info "Run: source $SHELL_CONFIG  (or restart your shell)"
            fi
        else
            info "Add manually: $PATH_LINE"
        fi
    else
        info "No .bashrc or .zshrc found. Add manually: $PATH_LINE"
    fi
fi

# ---------------------------------------------------------------------------
# Step 9: Create data directories
# ---------------------------------------------------------------------------

step "Creating data directories..."
for dir in memory cron-jobs data logs; do
    target="$MERLIN_HOME/$dir"
    if [[ -d "$target" ]]; then
        info "$dir/ exists"
    else
        run mkdir -p "$target"
        info "Created $dir/"
    fi
done

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║      Merlin installed! ✓         ║"
echo "  ╚══════════════════════════════════╝"
echo ""
info "Version: $TAG"
info "Location: $MERLIN_HOME"
echo ""
if $DRY_RUN; then
    info "[dry-run] No changes were made."
else
    info "Run 'merlin' to start (you may need to restart your shell first)."
fi
echo ""
