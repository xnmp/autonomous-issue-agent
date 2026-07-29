import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MarkdownBody({ body }: { body: string }) {
  return <Markdown remarkPlugins={[remarkGfm]}>{body}</Markdown>;
}
