import ReactMarkdown from "react-markdown";

// Картинки из текста модели не выводим: изображения в чат приносят только компоненты
// инструментов (ADR-0004). Сырой HTML пропускается, URL фильтрует defaultUrlTransform.
export function Markdown({ children }: { children: string }) {
  return (
    <div className="chat-md">
      <ReactMarkdown
        skipHtml
        disallowedElements={["img"]}
        components={{
          a: ({ href, title, children }) => (
            <a href={href} title={title} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
