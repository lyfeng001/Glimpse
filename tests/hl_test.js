// web/hl.js unit tests (run by tests/test_web.py with node). Prints "ok N" or throws.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const HL = require('../web/hl.js');

const unesc = (s) => s.replace(/<[^>]*>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
// the text never changes: same lines, same characters, only <span class="hl-*"> added, nothing unescaped
function check(text, lang) {
  const out = HL.lines(text, lang);
  assert.strictEqual(out.length, text.split('\n').length, 'line count ' + lang);
  out.forEach((l, k) => {
    assert.strictEqual(unesc(l), text.split('\n')[k], 'line ' + (k + 1) + ' of ' + lang);
    assert.ok(!/<(?!\/?span\b)/.test(l) && !/<span(?! class="hl-[a-z]+">)/.test(l), 'only hl spans: ' + l);
    const opens = (l.match(/<span /g) || []).length; const closes = (l.match(/<\/span>/g) || []).length;
    assert.strictEqual(opens, closes, 'balanced spans: ' + l);
  });
  return out;
}
const has = (out, cls, txt) => out.some((l) => l.includes('<span class="hl-' + cls + '">' + txt + '</span>'));
let n = 0;
const t = (name, fn) => { fn(); n++; };

t('python', () => {
  const o = check('import os\n@app.route("/x")\ndef f(a=1, b=None):\n    s = "a # not a comment"  # real\n    """doc\n  spans"""\n    return f\'{a}\'', 'python');
  assert.ok(has(o, 'kw', 'def') && has(o, 'kw', 'import') && has(o, 'fn', '@app.route') && has(o, 'lit', 'None') && has(o, 'num', '1'));
  assert.ok(has(o, 'str', '&quot;a # not a comment&quot;') && has(o, 'com', '# real'));
  assert.ok(has(o, 'str', '&quot;&quot;&quot;doc') && has(o, 'str', '  spans&quot;&quot;&quot;'));     // reopened on the next line
  assert.ok(has(o, 'fn', 'f') && !has(o, 'kw', 'route'));
});
t('js', () => {
  const o = check("const re = /[\"'](a|b)\\//g; const x = a / b / c;\nlet s = `t ${x}\nmore`; // c\n/* block\n */ if (x) return /y/.test(s);", 'js');
  assert.ok(has(o, 'str', '/[&quot;&#39;](a|b)\\//g'), 'regex literal with quotes inside');
  assert.ok(!o[0].includes('hl-str">/ b /'), 'division is not a regex');
  assert.ok(has(o, 'str', '`t ${x}') && has(o, 'str', 'more`') && has(o, 'com', '// c') && has(o, 'com', '/* block'));
  assert.ok(has(o, 'kw', 'return') && has(o, 'str', '/y/') && has(o, 'fn', 'test'));
});
t('json', () => {
  const o = check('{"a": 1, "b": [true, null, "x"], "c": {"d": -2.5e3}}', 'json');
  assert.ok(has(o, 'prop', '&quot;a&quot;') && has(o, 'str', '&quot;x&quot;') && has(o, 'lit', 'true') && has(o, 'lit', 'null') && has(o, 'num', '1'));
});
t('shell', () => {
  const o = check('#!/bin/sh\n# comment\necho "$HOME ${#arr} $(date)" a#b $1 # tail\nif [ -f x ]; then exit 1; fi', 'sh');
  assert.ok(has(o, 'com', '#!/bin/sh') && has(o, 'com', '# comment') && has(o, 'com', '# tail') && has(o, 'var', '$1'));
  assert.ok(!o[2].includes('hl-com">#b'), 'a#b is not a comment');
  assert.ok(has(o, 'kw', 'if') && has(o, 'kw', 'then') && has(o, 'kw', 'fi'));
  assert.strictEqual(HL.detect('run', '#!/usr/bin/env bash\n'), 'sh');
});
t('rust / go / c', () => {
  const r = check("fn f<'a>(x: &'a str) -> Option<i32> { let c = 'z'; let n = '\\n'; println!(\"{}\", x); None }", 'rust');
  assert.ok(has(r, 'kw', '&#39;a') && has(r, 'str', '&#39;z&#39;') && has(r, 'str', '&#39;\\n&#39;') && has(r, 'fn', 'println!') &&
    has(r, 'lit', 'None') && has(r, 'ty', 'i32'));
  const g = check('package main\n// c\nfunc main() { s := `raw\nraw`; fmt.Println(s, nil) }', 'go');
  assert.ok(has(g, 'kw', 'func') && has(g, 'lit', 'nil') && has(g, 'str', '`raw') && has(g, 'fn', 'Println'));
  const c = check('#include <stdio.h>\nint main(void) { char c = \'x\'; return 0; } // done', 'c');
  assert.ok(has(c, 'pre', '#include') && has(c, 'ty', 'int') && has(c, 'str', '&#39;x&#39;') && has(c, 'com', '// done'));
});
t('css / html / yaml / toml / sql / md / diff', () => {
  const css = check('.mt-2 { margin-top: -2px; color: #fff; }\n#app a:hover { --gap: 1.5em; }\n@media (min-width: 900px) {}', 'css');
  assert.ok(has(css, 'prop', 'margin-top') && has(css, 'num', '-2px') && has(css, 'num', '#fff') && has(css, 'var', '--gap') && has(css, 'kw', '@media'));
  assert.ok(!css[0].includes('hl-num">-2</span> {'), '.mt-2 is a class name');
  const h = check('<!-- c -->\n<a href="x" class=\'y\'>a > b</a>', 'html');
  assert.ok(has(h, 'com', '&lt;!-- c --&gt;') && has(h, 'tag', '&lt;a') && has(h, 'attr', 'href') && has(h, 'str', '&quot;x&quot;'));
  assert.ok(!h[1].includes('hl-tag">&gt;</span> b'), 'a > b in text is plain');
  const y = check('# c\nname: glimpse\nlist:\n  - a: 1\n    b: true\nurl: http://x.y\nanchor: &x foo', 'yaml');
  assert.ok(has(y, 'prop', 'name') && has(y, 'prop', '  - a') && has(y, 'lit', 'true') && has(y, 'var', '&amp;x') && !has(y, 'prop', 'url: http'));
  const tm = check('[server]\nport = 8443  # c\nname = "x"', 'toml');
  assert.ok(has(tm, 'kw', '[server]') && has(tm, 'prop', 'port') && has(tm, 'num', '8443') && has(tm, 'com', '# c'));
  const s = check("SELECT a, count(*) FROM t WHERE b = 'it''s' AND c IS NULL; -- c", 'sql');
  assert.ok(has(s, 'kw', 'SELECT') && has(s, 'str', '&#39;it&#39;&#39;s&#39;') && has(s, 'lit', 'NULL') && has(s, 'com', '-- c'));
  const m = check('# Title\n- item with `code` and [link](https://x)\n> quote\n---\n```py\nx = 1\n```', 'md');
  assert.ok(has(m, 'kw', '# Title') && has(m, 'str', '`code`') && has(m, 'fn', '[link](https://x)') && has(m, 'com', '&gt; quote') && has(m, 'com', '---'));
  const d = check('diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n ctx', 'diff');
  assert.ok(has(d, 'del', '-old') && has(d, 'add', '+new') && has(d, 'hunk', '@@ -1 +1 @@'));
});
t('detect / lang / safety', () => {
  assert.strictEqual(HL.detect('a/b/server.py'), 'python');
  assert.strictEqual(HL.detect('Dockerfile'), 'docker');
  assert.strictEqual(HL.detect('.env.local'), 'sh');
  assert.strictEqual(HL.detect('x.constructor'), '');                 // no prototype keys
  assert.strictEqual(HL.lang('__proto__'), '');
  assert.strictEqual(HL.lang('TS'), 'ts');
  assert.strictEqual(HL.lang('text'), '');
  for (const lang of ['python', 'js', 'html', 'md', 'sh', 'css', 'json', 'yaml', 'sql', '']) {
    const out = HL.lines('<script>alert(1)</script> "<img onerror=x>" \'&amp;\'', lang).join('\n');
    assert.ok(!/<script|<img/i.test(out), 'escaped in ' + lang);
  }
});
t('real files: lossless and fast', () => {
  const root = path.join(__dirname, '..');
  for (const [f, lang] of [['agent/glimpse_agent.py', 'python'], ['hub/server.py', 'python'], ['web/app.js', 'js'], ['web/app.css', 'css'],
    ['web/index.html', 'html'], ['README.md', 'md'], ['config.example.json', 'json'], ['gli', 'sh'], ['web/hl.js', 'js']]) {
    const text = fs.readFileSync(path.join(root, f), 'utf8').slice(0, 390 * 1024);
    const t0 = Date.now();
    check(text, lang);
    const ms = Date.now() - t0;
    assert.ok(ms < 1500, f + ' took ' + ms + ' ms');
  }
});
console.log('ok ' + n);
