import type { ComparisonTable as ComparisonTableData } from "@/contracts";

const EMPTY = "—";

export function ComparisonTable({ data }: { data: ComparisonTableData }) {
  return (
    <div className="overflow-x-auto rounded-chat border border-chat-border bg-chat-surface">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr>
            <td className="sticky left-0 bg-chat-surface" />
            {data.columns.map((c, i) => (
              <th key={i} scope="col" className="min-w-32 px-3 py-2 text-left font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row, r) => (
            <tr key={r} className="border-t border-chat-border">
              <th scope="row" className="sticky left-0 bg-chat-surface px-3 py-2 text-left font-normal text-chat-muted">
                {row.label}
              </th>
              {data.columns.map((_, i) => (
                <td key={i} className="px-3 py-2">
                  {row.values[i] || EMPTY}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
