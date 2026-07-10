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

INSTALLER_VERSION="0.17.0"
MERLIN_HOME="${MERLIN_HOME:-$HOME/.merlin}"
GITHUB_REPO="${MERLIN_REPO:-ArnaudValensi/merlin}"  # owner/repo
# The active version's bin/ goes on PATH. The launcher (bin/merlin) ships
# in the repo and is reached through the current symlink, so it tracks the
# release instead of being a generated, drift-prone install artifact.
BIN_DIR="$MERLIN_HOME/current/bin"
VERSIONS_DIR="$MERLIN_HOME/versions"

# --non-interactive (-y): no prompts. Required deps auto-install (uv, a
# user-level installer), optional deps are skipped with a warning, and the
# PATH line is added without asking. Combined with the managed container
# (uv/tmux already present, PATH set by the image), this collapses to a
# pure code install — which is how merlin-setup.sh reuses this script.
DRY_RUN=false
NON_INTERACTIVE=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --non-interactive | -y) NON_INTERACTIVE=true ;;
    esac
done

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
    if $NON_INTERACTIVE; then
        return 0  # no prompts; callers decide per-step what "yes" means
    fi
    # Read from /dev/tty so confirm works even when piped (curl | bash)
    read -rp "  $prompt [y/N] " answer < /dev/tty
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

detect_pkg_manager() {
    if command -v brew >/dev/null 2>&1; then
        echo "brew"
    elif command -v apt >/dev/null 2>&1; then
        echo "apt"
    elif command -v pacman >/dev/null 2>&1; then
        echo "pacman"
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
echo "  ███╗   ███╗███████╗██████╗ ██╗     ██╗███╗   ██╗"
echo "  ████╗ ████║██╔════╝██╔══██╗██║     ██║████╗  ██║"
echo "  ██╔████╔██║█████╗  ██████╔╝██║     ██║██╔██╗ ██║"
echo "  ██║╚██╔╝██║██╔══╝  ██╔══██╗██║     ██║██║╚██╗██║"
echo "  ██║ ╚═╝ ██║███████╗██║  ██║███████╗██║██║ ╚████║"
echo "  ╚═╝     ╚═╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝╚═╝  ╚═══╝"
echo ""
echo "                installer v$INSTALLER_VERSION"
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
    if $NON_INTERACTIVE; then
        info "Skipped (non-interactive). Install later: ${cmd:-via your package manager}"
    elif [[ -n "$cmd" ]]; then
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
# Step 3: Fetch latest tag
# ---------------------------------------------------------------------------

step "Fetching latest tag..."
TAGS_URL="https://api.github.com/repos/$GITHUB_REPO/tags"

if $DRY_RUN; then
    TAG="0.1.0"
    info "[dry-run] Would fetch from $TAGS_URL"
    info "[dry-run] Using placeholder version: $TAG"
else
    TAG_JSON=$(curl -fsSL "$TAGS_URL" 2>/dev/null) || {
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
# Step 4: Download and extract
# ---------------------------------------------------------------------------

VERSION_DIR="$VERSIONS_DIR/$TAG"

step "Installing version $TAG..."
if [[ -d "$VERSION_DIR" ]]; then
    info "Version $TAG already exists at $VERSION_DIR"
else
    TARBALL_URL="https://github.com/$GITHUB_REPO/archive/refs/tags/v$TAG.tar.gz"

    if $DRY_RUN; then
        info "[dry-run] Would download $TARBALL_URL"
        info "[dry-run] Would extract to $VERSION_DIR"
    else
        mkdir -p "$VERSIONS_DIR"
        TMPFILE=$(mktemp)
        # Clean up temp file and partial extraction on failure
        trap 'rm -f "$TMPFILE"; if [[ -d "$VERSION_DIR" ]]; then rm -rf "$VERSION_DIR"; fi' EXIT

        curl -fsSL "$TARBALL_URL" -o "$TMPFILE" || {
            # Try without v prefix
            TARBALL_URL="https://github.com/$GITHUB_REPO/archive/refs/tags/$TAG.tar.gz"
            curl -fsSL "$TARBALL_URL" -o "$TMPFILE" || {
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
        # The launcher ships in the release (bin/merlin), reached via
        # current/bin on PATH. Fail fast if a release lacks it.
        if [[ ! -x "$VERSION_DIR/bin/merlin" ]]; then
            error "Extraction incomplete — bin/merlin missing or not executable"
            rm -rf "$VERSION_DIR"
            exit 1
        fi

        rm -f "$TMPFILE"
        trap - EXIT
        info "Extracted to $VERSION_DIR"
    fi
fi

# ---------------------------------------------------------------------------
# Step 5: Create current symlink
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
# Step 6: Launcher
# ---------------------------------------------------------------------------
# No generation: bin/merlin (and bin/merlin-clip) ship in the release and
# are exposed on PATH via current/bin below. This is what keeps the
# launcher versioned with the code instead of frozen at install time.
info "Launcher: $BIN_DIR/merlin (shipped in the release)"

# ---------------------------------------------------------------------------
# Step 7: Add to PATH
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
        # Skip if already added (match the current/bin fragment, robust to
        # the absolute home path differing across machines)
        if grep -qF 'merlin/current/bin' "$SHELL_CONFIG" 2>/dev/null; then
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
# Step 8: Create data directories
# ---------------------------------------------------------------------------

step "Creating data directories..."
for dir in notes jobs data logs; do
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
    info "On first start, Merlin exposes its skills to your own agents via"
    info "~/.claude/skills and ~/.agents/skills (symlinks, refreshed each start)."
fi
echo ""
