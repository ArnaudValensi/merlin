# Terminal Clipboard — Requirements

## Goal

Enable copy and paste in the web terminal on mobile. Selecting text in tmux should automatically copy to the browser clipboard. A paste button in the toolbar should paste clipboard content into the terminal.

## Context

The web terminal (`terminal/templates/terminal.html`) runs xterm.js connected to a tmux session via WebSocket. On mobile, a transparent `#touch-overlay` captures all touch events and translates them to SGR mouse sequences for tmux:

- **Vertical swipe** → scroll (SGR btn 64/65)
- **Tap** → click (SGR btn 0 press + release)
- **Horizontal drag** → select (SGR btn 0 press + motion + release)

Selection works — tmux highlights text when you drag. But the selected text only goes into tmux's paste buffer, NOT the browser clipboard. And there's no way to paste from the browser clipboard into the terminal on mobile (the native long-press menu is blocked by the overlay).

### Why OSC 52

OSC 52 is the standard terminal escape sequence for clipboard integration:
- **Copy**: terminal application sends `\x1b]52;c;<base64-text>\x1b\\` → the terminal emulator writes to system clipboard
- **Paste**: not used for paste (we'll use a toolbar button instead)

tmux supports OSC 52 natively with `set -g set-clipboard on`. When this is set, any tmux copy operation (mouse select, copy-mode yank, etc.) sends OSC 52 to the outer terminal. Since our "outer terminal" is xterm.js in the browser, we just need to intercept the escape sequence and call `navigator.clipboard.writeText()`.

## Requirements

### R1: Copy via OSC 52

- **Status**: `accepted`
- Add `set -g set-clipboard on` to `~/.tmux.conf`
- Register an OSC 52 handler in xterm.js using `term.parser.registerOscHandler(52, data => ...)`
- Decode the base64 payload and call `navigator.clipboard.writeText(decoded)`
- Handle errors gracefully (clipboard API requires HTTPS and user activation)
- Should work with all tmux copy methods: mouse select, copy-mode, `tmux set-buffer`

### R2: Paste button in toolbar

- **Status**: `accepted`
- Add a clipboard/paste icon button to `#terminal-toolbar`
- On tap: call `navigator.clipboard.readText()` and send the text to the terminal via `sendToTerminal()`
- Button should be visible on both mobile and desktop toolbar (when toolbar is shown)
- Handle errors: clipboard read requires HTTPS + user gesture + permission; show a brief error in the terminal if it fails
- Style consistently with existing toolbar buttons (`.tk` class)

### R3: Visual feedback

- **Status**: `accepted`
- On successful copy: brief visual indicator (e.g., flash the status bar text "Copied!" for 1-2s)
- On paste: no special feedback needed (text appears in terminal)
- On error: write a red bracketed message to terminal (matches existing voice error pattern)

## Architecture

### Key files

| File | Change |
|------|--------|
| `terminal/templates/terminal.html` | OSC 52 handler, paste button, visual feedback |
| `~/.tmux.conf` | Add `set -g set-clipboard on` |
| `docs/web-terminal.md` | Document clipboard integration |

### OSC 52 handler (pseudocode)

```javascript
// After term.open(container):
term.parser.registerOscHandler(52, (data) => {
    // data format: "c;<base64>" or "p;<base64>" or just "<base64>"
    const parts = data.split(';');
    const b64 = parts.length > 1 ? parts[parts.length - 1] : parts[0];
    const text = atob(b64);
    navigator.clipboard.writeText(text).then(() => {
        // show "Copied!" feedback
    }).catch(err => {
        // clipboard write failed (no HTTPS, no permission, etc.)
    });
    return true; // handled
});
```

### Paste button (pseudocode)

```html
<button class="tk" id="paste-btn" title="Paste from clipboard">📋</button>
```

```javascript
pasteBtn.addEventListener('click', async () => {
    try {
        const text = await navigator.clipboard.readText();
        if (text) sendToTerminal(text);
    } catch (err) {
        term.write('\r\n\x1b[31m[Paste error: clipboard permission denied]\x1b[0m\r\n');
    }
    term.focus();
});
```

### Clipboard API constraints

- Requires **HTTPS** (available via Cloudflare Tunnel)
- `writeText()` (copy) works in response to user gesture or trusted events
- `readText()` (paste) requires user activation (click/tap) + permission prompt on first use
- The paste button click IS a user activation, so permission should be granted

## Non-goals

- Paste via long-press context menu (complex, can be added later)
- OSC 52 paste queries (tmux requesting clipboard content — unnecessary with paste button)
- Desktop keyboard Ctrl+V paste (already works natively via xterm.js)

## Validation

- On mobile: drag-select text in tmux → verify it appears in clipboard (paste elsewhere to check)
- On mobile: copy text elsewhere → tap paste button → verify it appears in terminal
- On desktop: verify mouse selection + Ctrl+C/Ctrl+V still work normally (no regression)
- Verify HTTPS requirement: test over Cloudflare Tunnel (should work) and HTTP localhost (should show error)
- Take screenshots with the screenshot skill to verify paste button styling
