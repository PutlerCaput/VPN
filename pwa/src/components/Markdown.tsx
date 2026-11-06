import type { ReactNode } from "react";

/** Minimal, XSS-safe Markdown renderer: everything is rendered as React text. */
function inline(text: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : part,
  );
}

export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  let table: string[][] = [];

  const flush = () => {
    if (list.length) {
      blocks.push(
        <ul key={`ul${blocks.length}`}>
          {list.map((item, i) => (
            <li key={i}>{inline(item)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
    if (table.length) {
      const [head, ...rows] = table;
      blocks.push(
        <div className="table-wrap" key={`t${blocks.length}`}>
          <table>
            <thead>
              <tr>{head?.map((cell, i) => <th key={i}>{inline(cell)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c}>{inline(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      table = [];
    }
  };

  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    if (line.startsWith("|")) {
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      if (!cells.every((c) => /^-+$/.test(c))) table.push(cells);
      continue;
    }
    if (/^[-*] /.test(line)) {
      list.push(line.slice(2));
      continue;
    }
    flush();
    if (!line) continue;
    const heading = /^(#{1,3}) (.*)$/.exec(line);
    if (heading) {
      const level = heading[1]?.length ?? 1;
      const content = inline(heading[2] ?? "");
      blocks.push(level === 1 ? <h2 key={blocks.length}>{content}</h2> : level === 2 ? <h3 key={blocks.length}>{content}</h3> : <h4 key={blocks.length}>{content}</h4>);
    } else {
      blocks.push(<p key={blocks.length}>{inline(line)}</p>);
    }
  }
  flush();
  return <div className="markdown">{blocks}</div>;
}
