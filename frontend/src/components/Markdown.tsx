import DOMPurify from "dompurify";
import { marked } from "marked";
import { useMemo } from "preact/hooks";

marked.use({ breaks: true, gfm: true });

export function Markdown({ content }: { content: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(content, { async: false }) as string, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "iframe", "form", "input", "button"],
    FORBID_ATTR: ["style"],
  }), [content]);

  return <div class="markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}
