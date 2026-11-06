import type { ReactNode } from "react";

/** XSS-safe subset of Markdown: every node is rendered as React text. */
function inline(text: string): ReactNode[] {
  return text
    .split(/(\*\*[^*]+\*\*)/g)
    .map((part, i) => (part.startsWith("**") && part.endsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : part));
}

export function Markdown({ text }: { text: string }) {
  const out: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) {
      out.push(
        <ul key={`l${out.length}`}>
          {list.map((item, i) => (
            <li key={i}>{inline(item)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (/^[-*] /.test(line)) {
      list.push(line.slice(2));
      continue;
    }
    flush();
    if (!line || /^\|?\s*-{3,}/.test(line)) continue;
    if (line.startsWith("|")) {
      out.push(<p key={out.length}>{line.split("|").slice(1, -1).map((c) => c.trim()).join(" · ")}</p>);
      continue;
    }
    const heading = /^(#{1,3}) (.*)$/.exec(line);
    if (heading) out.push(<h3 key={out.length}>{inline(heading[2] ?? "")}</h3>);
    else out.push(<p key={out.length}>{inline(line)}</p>);
  }
  flush();
  return <div>{out}</div>;
}
