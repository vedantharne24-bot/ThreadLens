import type { ReactNode } from "react";

const KEYWORDS = new Set(
  "auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while bool true false class namespace template typename using std nullptr new delete public private protected".split(" "),
);
const PTHREAD = /^(pthread_\w+|std::(mutex|thread|lock_guard|scoped_lock|unique_lock|condition_variable)|sc_\w+|tl_\w+)$/;

/** Tiny C/C++ highlighter (comments, strings, keywords, pthread calls). Output is React text nodes, so
 * untrusted program text is escaped by React. */
export function highlight(line: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\/\/.*$|\/\*.*?\*\/|\/\*.*$|^\s*\*.*$|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])'|#\s*\w+|\b[A-Za-z_][\w:]*\b|\b\d+\b)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(line))) {
    if (m.index > last) out.push(line.slice(last, m.index));
    const tok = m[0];
    let color: string | undefined;
    if (tok.startsWith("//") || tok.startsWith("/*") || /^\s*\*/.test(tok)) color = "var(--fg-3)";
    else if (tok.startsWith('"') || tok.startsWith("'")) color = "var(--ok)";
    else if (tok.startsWith("#")) color = "var(--violet)";
    else if (PTHREAD.test(tok)) color = "var(--cyan)";
    else if (KEYWORDS.has(tok)) color = "var(--violet)";
    else if (/^\d/.test(tok)) color = "var(--warn)";
    out.push(color ? <span key={k++} style={{ color }}>{tok}</span> : tok);
    last = m.index + tok.length;
  }
  if (last < line.length) out.push(line.slice(last));
  return out;
}

export type DiffLine = { kind: "same" | "add" | "del"; a?: number; b?: number; text: string };

/** Line diff via longest common subsequence (fine for scenario-sized files). */
export function diffLines(a: string[], b: string[]): DiffLine[] {
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", a: i + 1, b: j + 1, text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ kind: "del", a: i + 1, text: a[i++] });
    } else {
      out.push({ kind: "add", b: j + 1, text: b[j++] });
    }
  }
  while (i < n) out.push({ kind: "del", a: i + 1, text: a[i++] });
  while (j < m) out.push({ kind: "add", b: j + 1, text: b[j++] });
  return out;
}
