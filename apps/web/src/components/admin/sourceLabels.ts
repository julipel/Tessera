import type { SourceSummary, SourceSyncItem } from "@/contracts";

export const KIND_LABELS: Record<SourceSummary["kind"], string> = {
  website: "сайт",
  file: "файлы",
  table: "таблицы",
  http_api: "HTTP API",
  database: "база данных",
};

export const SYNC_LABELS: Record<SourceSyncItem["status"], string> = {
  pending: "в очереди",
  running: "идёт",
  succeeded: "готово",
  failed: "ошибка",
};

/** Синхронизация ещё не закончилась — страница перезапрашивает её статус. */
export function isActive(sync: SourceSyncItem | null): boolean {
  return sync?.status === "pending" || sync?.status === "running";
}

export function formatTime(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString("ru-RU") : "—";
}

export function syncColor(status: SourceSyncItem["status"]): string {
  if (status === "failed") return "text-chat-danger";
  if (status === "succeeded") return "text-chat-primary";
  return "text-chat-muted";
}
