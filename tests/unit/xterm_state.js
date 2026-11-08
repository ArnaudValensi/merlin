// The native terminal's differential checks (tests/unit/test_terminal_native.py),
// run by node with the vendored xterm.js parser (never opened):
//
// - "stream": the oracle, a parser fed a program's whole raw output;
// - "frames": the page's model, built from the bridge's frames with the
//   page's own decoder (terminal/static/native/core.js).
//
// Both come out in one shape: the screen rows (cells and wrap flag, tmux's
// sense: the row continues on the next), the last history rows, the cursor and
// whether the alternate screen is on.
//
// stdin:  {"cols": n, "rows": n, "history": k, "inputs": {"name": {"stream": [b64...]} | {"frames": [b64...]}}}
// stdout: {"name": state, ...}
const path = require('path');
const { Terminal } = require(path.join(__dirname, '..', '..', 'static', 'vendor', 'xterm.min.js'));
const { Unicode11Addon } = require(path.join(__dirname, '..', '..', 'static', 'vendor', 'xterm-addon-unicode11.min.js'));
const Core = require(path.join(__dirname, '..', '..', 'terminal', 'static', 'native', 'core.js'));

function cells(line, cols) {
  const out = [];
  const cell = line.getCell(0);
  for (let x = 0; x < cols; x++) {
    line.getCell(x, cell);
    // A cell never written and a written space look and copy the same.
    out.push([
      cell.getChars() || (cell.getWidth() === 1 ? ' ' : ''), cell.getWidth(),
      cell.getFgColorMode(), cell.getFgColor(), cell.getBgColorMode(), cell.getBgColor(),
      cell.isBold(), cell.isItalic(), cell.isDim(), cell.isUnderline(), cell.isInverse(),
      cell.isInvisible(), cell.isStrikethrough(),
    ]);
  }
  return out;
}

async function stream(cols, rows, keep, chunks) {
  // Widths as tmux counts them (Unicode 11), like the page's decoder.
  const term = new Terminal({ cols, rows, scrollback: 100000, allowProposedApi: true });
  term.loadAddon(new Unicode11Addon());
  term.unicode.activeVersion = '11';
  for (const c of chunks) await new Promise((res) => term.write(Buffer.from(c, 'base64'), res));
  const lines = (buf, from, to) => {
    const out = [];
    for (let y = from; y < to; y++) {
      const next = buf.getLine(y + 1);
      out.push({ cells: cells(buf.getLine(y), cols), wrapped: !!(next && next.isWrapped) });
    }
    return out;
  };
  const active = term.buffer.active;
  const normal = term.buffer.normal;
  const core = term._core;
  return {
    alternate: active.type === 'alternate',
    screen: lines(active, active.baseY, active.baseY + rows),
    history: lines(normal, keep ? Math.max(0, normal.baseY - keep) : 0, normal.baseY),
    cursor: [Math.min(core.buffer.x, cols - 1), core.buffer.y],
  };
}

function frames(cols, rows, keep, list) {
  const decoder = Core.newDecoder(Terminal, Unicode11Addon, cols);
  let history = [];
  let screen = [];
  let cursor = [0, 0];
  let alternate = false;
  for (const b64 of list) {
    const frame = Core.parseFrame(new Uint8Array(Buffer.from(b64, 'base64')));
    if (!frame) continue;
    const h = frame.header;
    if (frame.kind === Core.FRAME_HISTORY) {
      const older = Core.decodeRows(decoder, frame.rows, h.skip)
        .map((l, i) => ({ cells: cells(l, cols), wrapped: h.wrapped[i] === '1' }));
      history = older.concat(history);
      continue;
    }
    if (h.reset) history = [];
    const hist = Core.decodeRows(decoder, frame.histRows, h.hist.skip).map((l) => cells(l, cols));
    for (let i = 0; i < h.hist.count; i++) history.push({ cells: hist[i], wrapped: h.hist.wrapped[i] === '1' });
    screen = Core.decodeRows(decoder, frame.screenRows, h.screen.skip)
      .map((l, i) => ({ cells: cells(l, cols), wrapped: h.screen.wrapped[i] === '1' }));
    cursor = [h.cursor.x, h.cursor.y];
    alternate = h.alternate;
  }
  return { alternate, screen, history: keep ? history.slice(Math.max(0, history.length - keep)) : history, cursor };
}

let input = '';
process.stdin.on('data', (d) => { input += d; });
process.stdin.on('end', async () => {
  const req = JSON.parse(input);
  const out = {};
  for (const [name, spec] of Object.entries(req.inputs)) {
    out[name] = spec.stream
      ? await stream(req.cols, req.rows, req.history, spec.stream)
      : frames(req.cols, req.rows, req.history, spec.frames);
  }
  process.stdout.write(JSON.stringify(out));
});
