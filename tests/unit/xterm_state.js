// The vendored xterm.js parser's state after a byte stream, for the native
// terminal's differential tests (tests/unit/test_terminal_native.py): the
// same parser the page uses, never open()ed, run by node.
//
// stdin: {"cols": n, "rows": n, "streams": {"name": [base64 chunk, ...], ...}}
// stdout: {"name": state, ...}
const path = require('path');
const { Terminal } = require(path.join(__dirname, '..', '..', 'static', 'vendor', 'xterm.min.js'));

function cells(line, cols) {
  const out = [];
  const cell = line.getCell(0);
  for (let x = 0; x < cols; x++) {
    line.getCell(x, cell);
    // A cell never written and a written space look and copy the same: a
    // capture writes the spaces before text that a cursor move skipped.
    out.push([
      cell.getChars() || (cell.getWidth() === 1 ? ' ' : ''), cell.getWidth(),
      cell.getFgColorMode(), cell.getFgColor(), cell.getBgColorMode(), cell.getBgColor(),
      cell.isBold(), cell.isItalic(), cell.isDim(), cell.isUnderline(), cell.isInverse(),
      cell.isInvisible(), cell.isStrikethrough(),
    ]);
  }
  return out;
}

function state(term) {
  const core = term._core;
  const buf = term.buffer.active;
  const screen = (b) => {
    const rows = [];
    for (let y = 0; y < term.rows; y++) {
      const line = b.getLine(b.baseY + y);
      rows.push({ wrapped: line.isWrapped, cells: cells(line, term.cols) });
    }
    return rows;
  };
  return {
    type: buf.type,
    cursor: [core.buffer.x, core.buffer.y],
    modes: term.modes,
    cursorHidden: core.coreService.isCursorHidden,
    active: screen(buf),
    normal: screen(term.buffer.normal),
    // Every line of the main buffer, history included.
    text: (() => {
      const b = term.buffer.normal; const out = [];
      for (let y = 0; y < b.length; y++) out.push(b.getLine(y).translateToString(true));
      return out;
    })(),
    recentHistory: (() => {
      const b = term.buffer.normal; const out = [];
      for (let y = Math.max(0, b.baseY - 5); y < b.baseY; y++) out.push(b.getLine(y).translateToString(true));
      return out;
    })(),
  };
}

async function run(cols, rows, chunks) {
  const term = new Terminal({ cols, rows, scrollback: 100000, allowProposedApi: true });
  for (const c of chunks) {
    await new Promise((res) => term.write(Buffer.from(c, 'base64'), res));
  }
  return state(term);
}

let input = '';
process.stdin.on('data', (d) => { input += d; });
process.stdin.on('end', async () => {
  const req = JSON.parse(input);
  const out = {};
  for (const [name, chunks] of Object.entries(req.streams)) out[name] = await run(req.cols, req.rows, chunks);
  process.stdout.write(JSON.stringify(out));
});
