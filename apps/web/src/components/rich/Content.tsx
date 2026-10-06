"use client";

// Информационные компоненты: info_card, image, link_list, sources.
import type { ImageComponent, InfoCard as InfoCardData, LinkList as LinkListData, Sources as SourcesData } from "@/contracts";
import { useI18n } from "@/lib/i18n";
import { Markdown } from "../chat/Markdown";

const EXTERNAL = { target: "_blank", rel: "noopener noreferrer" } as const;

export function InfoCard({ data }: { data: InfoCardData }) {
  return (
    <article aria-label={data.title} className="overflow-hidden rounded-chat border border-chat-border bg-chat-surface">
      {data.image_url && (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={data.image_url} alt="" loading="lazy" className="max-h-48 w-full object-cover" />
      )}
      <div className="flex flex-col gap-1 p-3">
        <h3 className="font-medium">
          {data.url ? (
            <a href={data.url} {...EXTERNAL} className="hover:underline">
              {data.title}
            </a>
          ) : (
            data.title
          )}
        </h3>
        <Markdown>{data.body_markdown}</Markdown>
      </div>
    </article>
  );
}

export function ImageBlock({ data }: { data: ImageComponent }) {
  return (
    <figure className="overflow-hidden rounded-chat border border-chat-border bg-chat-surface">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={data.url} alt={data.alt} loading="lazy" className="max-h-80 w-full object-contain" />
      {data.caption && <figcaption className="px-3 py-2 text-sm text-chat-muted">{data.caption}</figcaption>}
    </figure>
  );
}

export function LinkList({ data }: { data: LinkListData }) {
  return (
    <ul className="flex flex-col divide-y divide-chat-border rounded-chat border border-chat-border bg-chat-surface">
      {data.items.map((item) => (
        <li key={item.url} className="px-3 py-2">
          <a href={item.url} {...EXTERNAL} className="font-medium text-chat-primary hover:underline">
            {item.title}
          </a>
          {item.description && <p className="text-sm text-chat-muted">{item.description}</p>}
        </li>
      ))}
    </ul>
  );
}

export function Sources({ data }: { data: SourcesData }) {
  const { t } = useI18n();
  return (
    <section aria-label={t.sources} className="text-sm">
      <h3 className="mb-1 text-chat-muted">{t.sources}</h3>
      <ol className="flex flex-col gap-1">
        {data.items.map((item, i) => (
          <li key={item.url} className="flex gap-2">
            <span className="text-chat-muted">{i + 1}.</span>
            <div className="min-w-0">
              <a href={item.url} {...EXTERNAL} className="text-chat-primary hover:underline">
                {item.title}
              </a>
              {item.snippet && <p className="line-clamp-2 text-chat-muted">{item.snippet}</p>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
