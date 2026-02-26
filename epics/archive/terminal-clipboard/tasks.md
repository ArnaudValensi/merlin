# Terminal Clipboard — Tasks

## Tasks

### T1: Configure tmux for OSC 52
- **Status**: `done`
- **Assignee**: Claude
- **Deps**: none
- Add `set -g set-clipboard on` to `~/.tmux.conf`
- Reload tmux config (`tmux source ~/.tmux.conf`)
- Verify tmux sends OSC 52 by running `tmux show -g set-clipboard`

### T2: Implement OSC 52 handler in xterm.js
- **Status**: `done`
- **Assignee**: Claude
- **Deps**: T1
- Register handler via `term.parser.registerOscHandler(52, ...)`
- Decode base64 payload, call `navigator.clipboard.writeText()`
- Add "Copied!" flash in status bar (reuse `statusText` element, restore after 1.5s)
- Handle errors (no HTTPS, permission denied)

### T3: Add paste button to toolbar
- **Status**: `done`
- **Assignee**: Claude
- **Deps**: none
- Add clipboard icon button to `#terminal-toolbar` (use SVG icon, not emoji)
- On click: `navigator.clipboard.readText()` → `sendToTerminal(text)`
- Style with `.tk` class, place before the separator or at end of row
- Error handling: write red message to terminal on failure

### T4: Update docs
- **Status**: `done`
- **Assignee**: Claude
- **Deps**: T2, T3
- Update `docs/web-terminal.md` with clipboard section
- Document OSC 52 mechanism, paste button, HTTPS requirement

### T5: Validate on mobile
- **Status**: `done`
- **Assignee**: Claude
- **Deps**: T2, T3
- Test copy: select text in tmux → paste in another app
- Test paste: copy text elsewhere → tap paste button → verify in terminal
- Take screenshots with screenshot skill to verify button styling
- Test error case: HTTP (no tunnel) should show error gracefully
