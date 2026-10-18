/* Glimpse syntax highlighter: small, dependency-free, safe (all text is escaped; only <span class="hl-*"> with a
   fixed set of class names is added). Comments, strings, numbers, keywords, literals, types, function calls,
   keys per language. HL.lines(text, lang) -> HTML strings, one per line of `text` (a token that spans lines, e.g.
   a block comment, is closed and reopened on each line). HL.detect(path, text) -> language id or ''. */
(function (root) {
  'use strict';
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => s.replace(/[&<>"']/g, (c) => ESC[c]);
  const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  const words = (s) => new Set(s.split(/\s+/).filter(Boolean));
  const sticky = (re) => new RegExp(re.source, 'y' + (re.ignoreCase ? 'i' : ''));
  const HASH = { tok: '#', word: 1 };   // '#' as a comment only at the start of a word (shell: a#b, ${#a}, $# are not)

  const PY = words('and as assert async await break class continue def del elif else except finally for from global if import in is ' +
    'lambda nonlocal not or pass raise return try while with yield');
  const JS = words('async await break case catch class const continue debugger default delete do else export extends finally for from function ' +
    'if import in instanceof let new of return static switch throw try typeof var void while with yield');
  const TS = new Set([...JS, ...words('abstract as declare enum implements interface keyof namespace private protected public readonly satisfies type infer')]);
  const GO = words('break case chan const continue default defer else fallthrough for func go goto if import interface map package range return ' +
    'select struct switch type var');
  const RUST = words('as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return ' +
    'self Self static struct super trait type unsafe use where while');
  const CLIKE = words('auto break case catch class const constexpr continue default delete do else enum explicit extern final for friend goto if ' +
    'inline namespace new noexcept operator override private protected public register return sizeof static struct switch template this throw try ' +
    'typedef typename union using virtual volatile while abstract extends implements import instanceof interface package super synchronized ' +
    'throws transient fun val var when object companion data sealed internal is in out by init lateinit suspend func let guard defer ' +
    'extension protocol where self Self async await yield');
  const SH = words('if then else elif fi case esac for select while until do done in function time return exit local export readonly declare ' +
    'unset shift source alias set trap break continue eval exec');
  const SQL = words('select from where and or not insert into values update set delete create table index view drop alter add column ' +
    'primary key foreign references join inner left right full outer on as group by order having limit offset union all distinct case when ' +
    'then else end exists in is like between with returning default constraint unique check begin commit rollback transaction asc desc');
  const RE_KW = words('return typeof case in of void delete throw new yield await else do');   // a '/' after these starts a regex

  const L = Object.create(null);
  L.python = { line: ['#'], str: [{ open: '"""', ml: 1 }, { open: "'''", ml: 1 }, { open: '"', esc: 1 }, { open: "'", esc: 1 }],
    kw: PY, lit: words('True False None'), ty: words('int str float bool list dict tuple set bytes object type Exception'), deco: /@[A-Za-z_][\w.]*/ };
  L.js = { line: ['//'], block: ['/*', '*/'], re: 1, str: [{ open: '`', esc: 1, ml: 1 }, { open: '"', esc: 1 }, { open: "'", esc: 1 }],
    kw: JS, lit: words('true false null undefined NaN Infinity this super'), id: /[A-Za-z_$][\w$]*/ };
  L.ts = Object.assign({}, L.js, { kw: TS, ty: words('string number boolean any unknown never void object bigint symbol'), deco: /@[A-Za-z_$][\w$.]*/ });
  L.json = { str: [{ open: '"', esc: 1, key: 1 }], lit: words('true false null'), noId: 1 };
  L.go = { line: ['//'], block: ['/*', '*/'], str: [{ open: '`', ml: 1 }, { open: '"', esc: 1 }, { open: "'", esc: 1 }], kw: GO,
    lit: words('true false nil iota'), ty: words('int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 uintptr float32 float64 byte rune string bool error any') };
  // Rust: a quote is a lifetime ('a) unless it closes as a char literal ('a', '\n')
  L.rust = { line: ['//'], block: ['/*', '*/'], str: [{ open: 'r#"', close: '"#', ml: 1 }, { open: '"', esc: 1, ml: 1 }], chr: 1, kw: RUST,
    lit: words('true false None Some Ok Err'), ty: words('i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64 bool char str String Vec Option Result Box'),
    extra: [[/'[A-Za-z_]\w*(?!')/, 'kw'], [/[A-Za-z_]\w*!(?=[(\[{])/, 'fn']] };
  L.c = { line: ['//'], block: ['/*', '*/'], str: [{ open: '"""', ml: 1 }, { open: '"', esc: 1 }], chr: 1, kw: CLIKE,
    lit: words('true false null NULL nullptr nil'), ty: words('void int char short long float double bool unsigned signed size_t String Int Long Boolean Double Float Unit Any'),
    start: [[/[ \t]*#[ \t]*[a-z]+/, 'pre']], deco: /@[A-Za-z_][\w.]*/ };
  L.sh = { line: [HASH], str: [{ open: '"', esc: 1, ml: 1 }, { open: "'", ml: 1 }], kw: SH, lit: words('true false'), nofn: 1,
    extra: [[/\$(?:\{[^}\n]*\}|\([^)\n]*\)|[A-Za-z_]\w*|[0-9@*#?$!-])/, 'var']], id: /[A-Za-z_][\w-]*/ };
  L.sql = { line: ['--'], block: ['/*', '*/'], str: [{ open: "'", dbl: 1 }, { open: '"', esc: 1 }], kw: SQL, lit: words('true false null'), ci: 1,
    ty: words('int integer bigint smallint text varchar char boolean bool date timestamp timestamptz numeric decimal real json jsonb uuid serial') };
  L.css = { block: ['/*', '*/'], line: [{ tok: '//', word: 1 }], str: [{ open: '"', esc: 1 }, { open: "'", esc: 1 }], noId: 1, wordRe: /[\w-]+/,
    extra: [[/@[\w-]+/, 'kw'], [/--[\w-]+/, 'var'], [/[A-Za-z-]+(?=[ \t]*:[^{};\n]*;)/, 'prop'], [/#[0-9a-fA-F]{3,8}(?![\w-])/, 'num'],
      [/-?(?:\d+\.?\d*|\.\d+)(?:%|[a-z]+)?/, 'num'], [/!important/, 'kw']] };
  L.html = { html: 1 };
  L.yaml = { line: [HASH], str: [{ open: '"', esc: 1 }, { open: "'", dbl: 1 }], lit: words('true false null yes no on off True False Null ~'), noId: 1,
    start: [[/[ \t]*(?:- +)*(?:"[^"\n]*"|'[^'\n]*'|[^\s#:"'][^:#\n]*?)(?=:(?:[ \t]|\n|$))/, 'prop']], extra: [[/[&*][\w-]+/, 'var']] };
  L.toml = { line: [HASH], str: [{ open: '"""', ml: 1 }, { open: "'''", ml: 1 }, { open: '"', esc: 1 }, { open: "'" }], lit: words('true false'), noId: 1,
    start: [[/[ \t]*\[\[?[^\]\n]*\]\]?/, 'kw'], [/[ \t]*[\w."-]+(?=[ \t]*=)/, 'prop']] };
  L.ini = Object.assign({}, L.toml, { line: [HASH, ';'] });
  L.md = { str: [{ open: '```', ml: 1 }, { open: '`' }], noId: 1, nonum: 1,
    start: [[/#{1,6}[ \t][^\n]*/, 'kw'], [/[ \t]*>[^\n]*/, 'com'], [/ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*(?=\n|$)/, 'com'],
      [/[ \t]*(?:[-*+]|\d+[.)])(?=[ \t])/, 'kw']],
    extra: [[/!?\[[^\]\n]*\]\([^)\n]*\)/, 'fn']] };
  L.docker = Object.assign({}, L.sh, { start: [[/[ \t]*(?:FROM|RUN|CMD|LABEL|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|VOLUME|USER|WORKDIR|ARG|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL|MAINTAINER)\b/i, 'kw']] });
  L.make = { line: [HASH], str: [{ open: '"', esc: 1 }, { open: "'" }], noId: 1, wordRe: /[\w.-]+/,
    extra: [[/\$[({][^)}\n]*[)}]|\$[@<^*?%]/, 'var']],
    start: [[/[A-Za-z_][\w.-]*(?=[ \t]*(?::=|\+=|\?=|=))/, 'var'], [/[^\s:=#][^:=#\n]*?(?=:(?!=))/, 'fn']] };
  L.ruby = { line: ['#'], str: [{ open: '"', esc: 1 }, { open: "'", esc: 1 }], lit: words('true false nil self'),
    kw: words('alias and begin break case class def defined do else elsif end ensure for if in module next not or redo rescue retry return then ' +
      'undef unless until when while yield require require_relative attr_accessor attr_reader private protected public'),
    extra: [[/@@?[A-Za-z_]\w*/, 'var'], [/:[A-Za-z_]\w*[?!]?(?!:)/, 'lit']] };
  L.lua = { line: ['--'], str: [{ open: '[[', close: ']]', ml: 1 }, { open: '"', esc: 1 }, { open: "'", esc: 1 }], lit: words('true false nil'),
    kw: words('and break do else elseif end for function goto if in local not or repeat return then until while') };
  L.php = Object.assign({}, L.c, { line: ['//', '#'], extra: [[/\$[A-Za-z_]\w*/, 'var']], start: [] });
  L.diff = { diff: 1 };
  const NAME = { python: 'Python', js: 'JavaScript', ts: 'TypeScript', json: 'JSON', go: 'Go', rust: 'Rust', c: 'C 系', sh: 'Shell', sql: 'SQL',
    css: 'CSS', html: 'HTML', yaml: 'YAML', toml: 'TOML', ini: 'INI', md: 'Markdown', docker: 'Dockerfile', make: 'Makefile', ruby: 'Ruby',
    lua: 'Lua', php: 'PHP', diff: 'Diff' };

  const EXT = { py: 'python', pyi: 'python', pyw: 'python', js: 'js', mjs: 'js', cjs: 'js', jsx: 'js', ts: 'ts', tsx: 'ts', mts: 'ts', cts: 'ts',
    json: 'json', jsonc: 'js', json5: 'js', webmanifest: 'json', ipynb: 'json', go: 'go', rs: 'rust',
    c: 'c', h: 'c', cc: 'c', cpp: 'c', cxx: 'c', hpp: 'c', hh: 'c', m: 'c', mm: 'c', java: 'c', kt: 'c', kts: 'c', scala: 'c', swift: 'c',
    cs: 'c', dart: 'c', groovy: 'c', gradle: 'c', proto: 'c', zig: 'c', sh: 'sh', bash: 'sh', zsh: 'sh', fish: 'sh', ksh: 'sh', env: 'sh',
    sql: 'sql', css: 'css', scss: 'css', less: 'css', html: 'html', htm: 'html', xml: 'html', svg: 'html', vue: 'html', xhtml: 'html', plist: 'html',
    yaml: 'yaml', yml: 'yaml', toml: 'toml', ini: 'ini', cfg: 'ini', conf: 'ini', properties: 'ini', md: 'md', markdown: 'md', mdx: 'md',
    rb: 'ruby', rake: 'ruby', gemspec: 'ruby', lua: 'lua', php: 'php', diff: 'diff', patch: 'diff', mk: 'make' };
  const NAMES = { dockerfile: 'docker', containerfile: 'docker', makefile: 'make', gnumakefile: 'make', gemfile: 'ruby', rakefile: 'ruby',
    '.bashrc': 'sh', '.zshrc': 'sh', '.profile': 'sh', '.bash_profile': 'sh', '.zprofile': 'sh', '.gitignore': 'sh', '.dockerignore': 'sh',
    '.editorconfig': 'ini', '.gitconfig': 'ini', pipfile: 'toml' };
  const ALIAS = { javascript: 'js', node: 'js', jsx: 'js', typescript: 'ts', tsx: 'ts', py: 'python', python3: 'python', py3: 'python',
    shell: 'sh', bash: 'sh', zsh: 'sh', console: 'sh', shellscript: 'sh', golang: 'go', rs: 'rust', 'c++': 'c', cpp: 'c', csharp: 'c',
    'c#': 'c', java: 'c', kotlin: 'c', swift: 'c', objc: 'c', yml: 'yaml', jsonc: 'js', dockerfile: 'docker', makefile: 'make',
    markdown: 'md', rb: 'ruby', htm: 'html', xml: 'html', svg: 'html', vue: 'html', patch: 'diff', postgres: 'sql', postgresql: 'sql',
    mysql: 'sql', sqlite: 'sql' };

  function detect(path, text) {
    const base = String(path || '').split('/').pop().toLowerCase();
    if (own(NAMES, base)) return NAMES[base];
    if (/^dockerfile\./.test(base) || /\.dockerfile$/.test(base)) return 'docker';
    if (/^\.env(\.|$)/.test(base)) return 'sh';
    const m = base.match(/\.([a-z0-9+]+)$/);
    if (m && own(EXT, m[1])) return EXT[m[1]];
    const first = String(text || '').slice(0, 200).split('\n')[0];
    if (/^#!.*\b(?:ba|z|k|fi)?sh\b/.test(first)) return 'sh';
    if (/^#!.*\bpython/.test(first)) return 'python';
    if (/^#!.*\bnode\b/.test(first)) return 'js';
    if (/^#!.*\bruby\b/.test(first)) return 'ruby';
    return '';
  }
  function lang(name) {
    const n = String(name || '').toLowerCase();
    if (L[n]) return n;
    if (own(ALIAS, n)) return ALIAS[n];
    return own(EXT, n) ? EXT[n] : '';
  }

  const ID = /[A-Za-z_][\w]*/y;
  const WORD = /[A-Za-z_][\w]*/y;
  const CHR = /'(?:\\.|[^'\\\n])'/y;
  const LITW = /[A-Za-z~][\w]*/y;
  const NUM = /(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?)[a-zA-Z]{0,4}/y;
  const prep = (spec) => {
    if (!spec._p) {
      spec._p = { start: (spec.start || []).map(([re, c]) => [sticky(re), c]), extra: (spec.extra || []).map(([re, c]) => [sticky(re), c]),
        deco: spec.deco ? sticky(spec.deco) : null, id: spec.id ? sticky(spec.id) : ID, word: spec.wordRe ? sticky(spec.wordRe) : WORD };
    }
    return spec._p;
  };
  const isWord = (ch) => !!ch && /[\w$]/.test(ch);

  // text -> [[class or '', text], ...] covering all of `text`
  function scan(t, spec) {
    const p = prep(spec);
    const out = [];
    let plain = '';
    const push = (cls, s) => { if (!s) return; if (plain) { out.push(['', plain]); plain = ''; } out.push([cls, s]); };
    const n = t.length;
    let i = 0; let bol = true;
    const at = (re) => { re.lastIndex = i; const m = re.exec(t); return m ? m[0] : null; };
    // JS: is a '/' here a regex literal (not a division)? look at what comes before it on the line
    const regexHere = () => {
      let j = i - 1;
      while (j >= 0 && (t[j] === ' ' || t[j] === '\t')) j--;
      if (j < 0 || t[j] === '\n') return true;
      if ('(,=:[!&|?{};+-*%<>~^'.includes(t[j])) return true;
      const w = /[A-Za-z_$][\w$]*$/.exec(t.slice(Math.max(0, j - 15), j + 1));
      return !!w && RE_KW.has(w[0]);
    };
    while (i < n) {
      const ch = t[i];
      if (ch === '\n') { plain += ch; i++; bol = true; continue; }
      if (bol) {
        bol = false;
        let hit = null;
        for (const [re, c] of p.start) { const m = at(re); if (m) { hit = [c, m]; break; } }
        if (hit) { push(hit[0], hit[1]); i += hit[1].length; continue; }
      }
      if (ch === ' ' || ch === '\t') { plain += ch; i++; continue; }
      // comments
      let done = false;
      for (const lc of spec.line || []) {
        const tok = typeof lc === 'string' ? lc : lc.tok;
        if (!t.startsWith(tok, i)) continue;
        if (typeof lc !== 'string' && lc.word && (isWord(t[i - 1]) || t[i - 1] === '{')) continue;
        let e = t.indexOf('\n', i); if (e < 0) e = n;
        push('com', t.slice(i, e)); i = e; done = true; break;
      }
      if (done) continue;
      if (spec.block && t.startsWith(spec.block[0], i)) {
        let e = t.indexOf(spec.block[1], i + spec.block[0].length);
        e = e < 0 ? n : e + spec.block[1].length;
        push('com', t.slice(i, e)); i = e; continue;
      }
      if (spec.re && ch === '/' && regexHere()) {
        let j = i + 1; let cls = false;
        while (j < n && t[j] !== '\n') {
          if (t[j] === '\\') { j += 2; continue; }
          if (t[j] === '[') cls = true; else if (t[j] === ']') cls = false; else if (t[j] === '/' && !cls) break;
          j++;
        }
        if (j < n && t[j] === '/' && j > i + 1) {
          j++; while (j < n && /[a-z]/.test(t[j])) j++;
          push('str', t.slice(i, j)); i = j; continue;
        }
      }
      // strings (an unterminated one ends at the line end, or for multi-line ones at the end of the text)
      for (const s of spec.str || []) {
        if (!t.startsWith(s.open, i)) continue;
        const close = s.close || s.open;
        let j = i + s.open.length;
        while (j < n) {
          if (s.esc && t[j] === '\\') { j += 2; continue; }
          if (t[j] === '\n' && !s.ml) break;
          if (t.startsWith(close, j)) {
            if (s.dbl && t.startsWith(close + close, j)) { j += 2 * close.length; continue; }
            j += close.length; break;
          }
          j++;
        }
        if (j > n) j = n;
        let cls = 'str';
        if (s.key) { let k = j; while (t[k] === ' ' || t[k] === '\t') k++; if (t[k] === ':') cls = 'prop'; }
        push(cls, t.slice(i, j)); i = j; done = true; break;
      }
      if (done) continue;
      if (spec.chr && ch === "'") { const m = at(CHR); if (m) { push('str', m); i += m.length; continue; } }
      for (const [re, c] of p.extra) { const m = at(re); if (m) { push(c, m); i += m.length; done = true; break; } }
      if (done) continue;
      if (p.deco && ch === '@' && !isWord(t[i - 1])) { const m = at(p.deco); if (m) { push('fn', m); i += m.length; continue; } }
      if (!spec.nonum && ch >= '0' && ch <= '9' && !isWord(t[i - 1])) { const m = at(NUM); if (m) { push('num', m); i += m.length; continue; } }
      if (!spec.noId && /[A-Za-z_$]/.test(ch)) {
        const m = at(p.id);
        if (m) {
          const w = spec.ci ? m.toLowerCase() : m;
          let cls = '';
          if (spec.kw && spec.kw.has(w)) cls = 'kw';
          else if (spec.lit && spec.lit.has(w)) cls = 'lit';
          else if (spec.ty && spec.ty.has(w)) cls = 'ty';
          else if (!spec.nofn) {
            let k = i + m.length; while (t[k] === ' ') k++;
            if (t[k] === '(') cls = 'fn';
            else if (!spec.ci && /^[A-Z][a-z0-9]\w*$/.test(m)) cls = 'ty';
          }
          if (cls) push(cls, m); else plain += m;
          i += m.length; continue;
        }
      }
      if (spec.noId && /[A-Za-z_~]/.test(ch)) {
        // no identifiers (json, yaml, css, md...): a word is plain, or a literal (true / null ...) when it stands alone
        const m = spec.lit && !isWord(t[i - 1]) ? at(LITW) : at(p.word);
        if (m) {
          if (spec.lit && spec.lit.has(m) && !isWord(t[i + m.length])) push('lit', m); else plain += m;
          i += m.length; continue;
        }
      }
      plain += ch; i++;
    }
    if (plain) out.push(['', plain]);
    return out;
  }

  // HTML / XML: tags, attribute names, values, comments; text in between is plain
  function scanHtml(t) {
    const out = [];
    const re = /<!--[\s\S]*?(?:-->|$)|<\/?[A-Za-z][\w:.-]*|<!\w+|\/?>|[A-Za-z_:][\w:.-]*(?==)|"[^"]*"|'[^']*'/g;
    let last = 0; let inTag = false; let m;
    while ((m = re.exec(t))) {
      const s = m[0];
      if (!inTag && s[0] !== '<') continue;           // '>' or name= in text content is plain
      if (m.index > last) out.push(['', t.slice(last, m.index)]);
      if (s.startsWith('<!--')) out.push(['com', s]);
      else if (s[0] === '<') { out.push(['tag', s]); inTag = true; }
      else if (s === '>' || s === '/>') { out.push(['tag', s]); inTag = false; }
      else if (s[0] === '"' || s[0] === "'") out.push(['str', s]);
      else out.push(['attr', s]);
      last = m.index + s.length;
    }
    if (last < t.length) out.push(['', t.slice(last)]);
    return out;
  }

  // tokens -> HTML lines (a token spanning newlines is closed at the end of each line and reopened)
  function toLines(toks) {
    const lines = ['']; let cur = 0;
    for (const [cls, s] of toks) {
      s.split('\n').forEach((part, k) => {
        if (k) { lines.push(''); cur++; }
        if (part) lines[cur] += cls ? '<span class="hl-' + cls + '">' + esc(part) + '</span>' : esc(part);
      });
    }
    return lines;
  }
  function diffLines(t) {
    return t.split('\n').map((l) => {
      const c = /^\+(?!\+\+)/.test(l) ? 'add' : /^-(?!--)/.test(l) ? 'del' : /^@@/.test(l) ? 'hunk' : /^(?:diff |index |\+\+\+|---)/.test(l) ? 'com' : '';
      return c ? '<span class="hl-' + c + '">' + esc(l) + '</span>' : esc(l);
    });
  }

  const MAX = 400 * 1024;                     // longer text is shown plain (escaped)
  function lines(text, name) {
    const t = String(text == null ? '' : text);
    const n = lang(name);
    if (!n || t.length > MAX) return t.split('\n').map(esc);
    if (n === 'diff') return diffLines(t);
    return toLines(L[n].html ? scanHtml(t) : scan(t, L[n]));
  }
  function html(text, name) { return lines(text, name).join('\n'); }
  function label(name) { const n = lang(name); return n ? NAME[n] : ''; }

  const api = { lines, html, detect, lang, label, esc };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.HL = api;
})(typeof window !== 'undefined' ? window : this);
